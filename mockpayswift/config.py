"""Configuration for the standalone simulated provider."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSettings:
    database_path: str = "./data/mockpayswift.sqlite3"
    api_key: str = ""
    test_mode: bool = False
    test_token: str = ""

    @classmethod
    def from_env(cls) -> ProviderSettings:
        raw_path = os.getenv("MOCKPAYSWIFT_DATABASE_PATH") or os.getenv(
            "MOCKPAYSWIFT_DB_PATH", "./data/mockpayswift.sqlite3"
        )
        if raw_path.startswith("sqlite:///"):
            raw_path = raw_path.removeprefix("sqlite:///")
        return cls(
            database_path=raw_path,
            api_key=os.getenv("MOCKPAYSWIFT_API_KEY", ""),
            test_mode=os.getenv("MOCKPAYSWIFT_TEST_MODE", "").lower() in {"1", "true", "yes", "on"},
            test_token=os.getenv("MOCKPAYSWIFT_TEST_TOKEN", ""),
        )
