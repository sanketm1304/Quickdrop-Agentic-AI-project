from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.config import Settings
from app.policy import money


class ProviderUnavailable(Exception):
    pass


class ProviderProtocolError(Exception):
    pass


@dataclass(frozen=True)
class ProviderPayment:
    payment_id: str
    idempotency_key: str
    rider_id: str
    dispute_reference: str
    amount: Decimal
    status: str


class MockPaySwiftClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.mockpayswift_url,
                headers={"Authorization": f"Bearer {self.settings.mockpayswift_api_key}"},
                timeout=httpx.Timeout(5.0),
                transport=self.transport,
            ) as client:
                response = await client.request(method, path, **kwargs)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderUnavailable(str(exc)) from exc
        if response.status_code == 404:
            raise KeyError(path)
        if response.status_code >= 500:
            raise ProviderUnavailable(f"provider HTTP {response.status_code}")
        if response.status_code >= 400:
            raise ProviderProtocolError(f"provider HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderProtocolError("provider returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ProviderProtocolError("provider returned a non-object")
        return data

    @staticmethod
    def _payment(data: dict) -> ProviderPayment:
        try:
            payment = ProviderPayment(
                payment_id=data["payment_id"],
                idempotency_key=data["idempotency_key"],
                rider_id=data["rider_id"],
                dispute_reference=data["dispute_reference"],
                amount=money(data["amount"]),
                status=data["status"],
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise ProviderProtocolError("provider payment shape invalid") from exc
        if payment.status not in {
            "CREATED",
            "PENDING",
            "CONFIRMED",
            "FAILED",
            "REVERSED",
            "UNKNOWN",
        }:
            raise ProviderProtocolError("provider payment status invalid")
        if not all((payment.payment_id, payment.idempotency_key, payment.rider_id)):
            raise ProviderProtocolError("provider payment identity invalid")
        return payment

    async def by_key(self, key: str) -> ProviderPayment | None:
        try:
            data = await self._request("GET", f"/v1/payments/by-idempotency-key/{key}")
        except KeyError:
            return None
        return self._payment(data)

    async def submit(
        self,
        *,
        key: str,
        rider_id: str,
        dispute_reference: str,
        amount: Decimal,
        source_ref: str | None = None,
        earning_period: str | None = None,
    ) -> ProviderPayment:
        body = {
            "idempotency_key": key,
            "rider_id": rider_id,
            "dispute_reference": dispute_reference,
            "amount": str(money(amount)),
            "currency": "INR",
        }
        if source_ref:
            body["source_ref"] = source_ref
        if earning_period:
            body["earning_period"] = earning_period
        data = await self._request("POST", "/v1/payments", json=body)
        payment = self._payment(data)
        if (
            payment.idempotency_key != key
            or payment.rider_id != rider_id
            or payment.dispute_reference != dispute_reference
            or payment.amount != money(amount)
        ):
            raise ProviderProtocolError("provider echoed mismatched payment")
        return payment

    async def rider_transactions(self, rider_id: str, limit: int = 50) -> list[ProviderPayment]:
        data = await self._request(
            "GET",
            f"/v1/riders/{rider_id}/transactions",
            params={"limit": min(limit, 100), "offset": 0},
        )
        items = data.get("items")
        if not isinstance(items, list):
            raise ProviderProtocolError("provider transaction list invalid")
        payments = [self._payment(item) for item in items]
        if any(payment.rider_id != rider_id for payment in payments):
            raise ProviderProtocolError("provider returned another rider's transaction")
        return payments
