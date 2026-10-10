"""Typed application settings, loaded from environment variables.

pydantic-settings reads each field from an env var with the same name
(case-insensitive), validates the type, and fails at startup if a required
value is missing. That beats discovering a missing secret mid-request.
"""

from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Locally (outside Docker) values can also come from a .env file next to
    # where you run the app. Inside Docker, Compose passes real env vars.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"

    # Shared secret that n8n sends in the X-API-Key header
    screener_api_key: SecretStr = Field(min_length=16)

    # Database (the "screener" DB created by postgres/init)
    db_host: str = "localhost"
    db_port: int = 5432
    screener_db_name: str = "screener"
    screener_db_user: str = "screener"
    screener_db_password: SecretStr

    @property
    def database_url(self) -> str:
        """libpq connection string used by psycopg (and the LangGraph checkpointer)."""
        # quote() escapes special characters (@, :, /) that would break the URL
        password = quote(self.screener_db_password.get_secret_value(), safe="")
        return (
            f"postgresql://{quote(self.screener_db_user, safe='')}:{password}"
            f"@{self.db_host}:{self.db_port}/{self.screener_db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    """Build Settings once and reuse it (also easy to override in tests)."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
