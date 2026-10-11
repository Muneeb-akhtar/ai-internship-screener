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
    # env_ignore_empty: "LLM_MODEL=" (empty) means "use the default", not "".
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

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

    # ---- LLM -----------------------------------------------------------------
    llm_provider: Literal["gemini", "anthropic", "openai"] = "gemini"
    llm_model: str | None = None  # None -> provider default (see app/llm.py)
    llm_fallback_model: str | None = None  # tried if the main model keeps failing (e.g. overloaded)
    llm_temperature: float = 0.0  # 0 = as consistent as possible (same rubric for everyone)
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 3
    google_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    # ---- Interview scheduling (HR's calendar) ----------------------------------
    hr_timezone: str = "Asia/Karachi"
    work_day_start_hour: int = 10  # first slot starts at 10:00
    work_day_end_hour: int = 17  # last slot must END by 17:00
    interview_slot_minutes: int = 30
    scheduling_lookahead_working_days: int = 5
    scheduling_min_lead_hours: int = 2  # don't book a slot starting in the next 2 hours

    # ---- Google Calendar (Phase 4) ---------------------------------------------------
    google_calendar_id: str = "primary"  # "primary" = the main calendar of the authorised account
    google_token_file: str = "/run/secrets/google-token.json"  # from scripts/google_auth.py
    calendar_invite_candidate: bool = True  # add the candidate as attendee (Google emails them)

    # ---- Voice interview (Vapi) -------------------------------------------------
    vapi_public_key: str | None = None  # PUBLIC key: safe in the browser (restrict it in Vapi)
    vapi_assistant_id: str | None = None
    # PRIVATE key: server-side only, used to fetch call recordings for HR (app/recordings.py)
    vapi_private_key: SecretStr | None = None
    interview_link_ttl_hours: int = 72

    # ---- Voice provider -------------------------------------------------------------
    # "vapi": candidate opens a link and talks in the browser.
    # "elevenlabs": the AI interviewer calls the candidate on WhatsApp (after they allow it).
    voice_provider: Literal["vapi", "elevenlabs"] = "vapi"
    elevenlabs_api_key: SecretStr | None = None
    elevenlabs_agent_id: str | None = None
    elevenlabs_webhook_secret: SecretStr | None = None  # verifies ElevenLabs-Signature (post-call)
    meta_wa_phone_number_id: str | None = None  # the WhatsApp number ElevenLabs calls from
    wa_call_permission_template: str = "interview_call_permission"
    wa_template_lang: str = "en"

    # ---- WhatsApp (Meta Cloud API) webhook + n8n (Phase 5) ----------------------------
    meta_app_secret: SecretStr | None = None  # verifies X-Hub-Signature-256 on incoming webhooks
    meta_webhook_verify_token: SecretStr | None = None  # Meta's GET verification handshake
    hr_whatsapp_number: str | None = None  # only this number can APPROVE/REJECT
    n8n_internal_url: str = "http://n8n:5678"  # n8n inside the Docker network

    # Facts the voice assistant may share with candidates (sent as Vapi variableValues)
    company_name: str = "Our Company"
    internship_duration: str = "three months"
    internship_schedule: str = "full-time, Monday to Friday"
    internship_start: str = "next month"
    internship_faq: str = (
        "Each intern gets a mentor. The internship is hybrid with two office days a week. "
        "There is a monthly stipend. HR shares exact details with shortlisted candidates."
    )

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
