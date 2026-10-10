"""LLM provider switch + a tiny client the agent uses.

Why a wrapper instead of calling LangChain directly from the nodes?
- The provider (Gemini / Claude / OpenAI) is chosen by LLM_PROVIDER in .env,
  and the agent code never needs to know which one it's talking to.
- Resilience lives in ONE place:
    retry    - the same model is retried with exponential backoff (429 / 5xx)
    fallback - if it still fails (e.g. "503 model overloaded"), a second model is tried
- Tests swap in a FakeLLM with the same two methods - no API calls, no cost.
"""

from typing import Any, Protocol, TypeVar

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel

from app.config import Settings

log = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)

# Used when LLM_MODEL / LLM_FALLBACK_MODEL are empty. Gemini 2.5 models are closed
# to new keys, so these are current models verified working on the free tier.
DEFAULT_MODELS = {
    "gemini": "gemini-3.8-flash",
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-5-mini",
}
DEFAULT_FALLBACK_MODELS: dict[str, str | None] = {
    "gemini": "gemini-3.5-flash",
    "anthropic": "claude-haiku-5-5",
    "openai": None,
}


class LLMClient(Protocol):
    """The only two things the agent ever asks an LLM to do."""

    model_name: str

    async def structured(self, schema: type[T], messages: list[BaseMessage]) -> T:
        """Return an instance of `schema` (validated by Pydantic)."""
        ...

    async def text(self, messages: list[BaseMessage]) -> str:
        """Return plain text."""
        ...


def build_chat_model(settings: Settings, model: str) -> BaseChatModel:
    """Create the LangChain chat model for the configured provider.
    Imports are inside each branch so only the chosen SDK is loaded.
    SDK-level retries are 0 because LangChainLLM retries (avoids retrying twice)."""
    provider = settings.llm_provider
    common: dict[str, Any] = {
        "temperature": settings.llm_temperature,
        "timeout": settings.llm_timeout_seconds,
        "max_retries": 0,
    }
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        if not settings.google_api_key:
            raise RuntimeError("LLM_PROVIDER=gemini but GOOGLE_API_KEY is empty")
        return ChatGoogleGenerativeAI(model=model, google_api_key=settings.google_api_key, **common)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        if not settings.anthropic_api_key:
            raise RuntimeError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty")
        return ChatAnthropic(model_name=model, api_key=settings.anthropic_api_key, **common)
    if provider == "openai":
        from langchain_openai import ChatOpenAI

        if not settings.openai_api_key:
            raise RuntimeError("LLM_PROVIDER=openai but OPENAI_API_KEY is empty")
        return ChatOpenAI(model=model, api_key=settings.openai_api_key, **common)
    raise ValueError(f"Unknown LLM_PROVIDER: {provider}")


class LangChainLLM:
    """LLMClient backed by real LangChain chat models (primary + optional fallback)."""

    def __init__(
        self,
        primary: BaseChatModel,
        model_name: str,
        fallback: BaseChatModel | None = None,
        max_attempts: int = 3,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._max_attempts = max_attempts
        self.model_name = model_name

    def _resilient(self, make: Any) -> Runnable:
        """make(model) -> runnable. Wraps it as: retry(primary) -> else retry(fallback)."""
        retry = {"stop_after_attempt": self._max_attempts, "wait_exponential_jitter": True}
        runnable = make(self._primary).with_retry(**retry)
        if self._fallback is not None:
            runnable = runnable.with_fallbacks([make(self._fallback).with_retry(**retry)])
        return runnable

    async def structured(self, schema: type[T], messages: list[BaseMessage]) -> T:
        # with_structured_output sends the Pydantic JSON schema to the model and
        # parses + validates the reply into `schema` (raises if it doesn't fit).
        result = await self._resilient(lambda m: m.with_structured_output(schema)).ainvoke(messages)
        if not isinstance(result, schema):  # some providers return a dict
            result = schema.model_validate(result)
        return result

    async def text(self, messages: list[BaseMessage]) -> str:
        reply = await self._resilient(lambda m: m).ainvoke(messages)
        return reply.text if isinstance(reply.text, str) else str(reply.content)


def build_llm(settings: Settings) -> LangChainLLM:
    provider = settings.llm_provider
    model_name = settings.llm_model or DEFAULT_MODELS[provider]
    fallback_name = settings.llm_fallback_model or DEFAULT_FALLBACK_MODELS[provider]
    if fallback_name == model_name:
        fallback_name = None
    log.info("llm_configured", provider=provider, model=model_name, fallback=fallback_name)
    return LangChainLLM(
        build_chat_model(settings, model_name),
        model_name,
        fallback=build_chat_model(settings, fallback_name) if fallback_name else None,
        max_attempts=settings.llm_max_retries,
    )
