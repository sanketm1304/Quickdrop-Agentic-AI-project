from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./quickdrop-dev.sqlite3"
    session_secret: str = ""
    webhook_token: str = ""
    mockpayswift_api_key: str = ""
    mockpayswift_test_token: str = ""
    mockpayswift_url: str = "http://localhost:8001"
    quickdrop_test_mode: bool = False
    demo_rider_password: str = ""
    demo_employee_password: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    agent_mode: str = Field(default="offline", pattern="^(offline|gemini)$")
    secure_cookies: bool = False

    @model_validator(mode="after")
    def validate_secrets(self) -> "Settings":
        if not self.session_secret or len(self.session_secret) < 32:
            raise ValueError("SESSION_SECRET must contain at least 32 characters")
        if not self.webhook_token or not self.mockpayswift_api_key:
            raise ValueError("WEBHOOK_TOKEN and MOCKPAYSWIFT_API_KEY are required")
        if self.agent_mode == "gemini" and not self.gemini_api_key:
            raise ValueError("GEMINI_API_KEY is required for AGENT_MODE=gemini")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
