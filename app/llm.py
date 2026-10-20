"""LLM factory. One Mistral model powers every agent (matches the LangFlow wiring)."""
from __future__ import annotations

from functools import lru_cache

from langchain_core.language_models.chat_models import BaseChatModel

from app.config import settings


@lru_cache(maxsize=4)
def get_llm(temperature: float = 0.1) -> BaseChatModel:
    """
    Return a chat model. Uses Mistral when a key is present; otherwise falls back to
    an Ollama/local model only if configured. Callers must handle a missing key by
    checking `llm_available()`.
    """
    from langchain_mistralai.chat_models import ChatMistralAI

    return ChatMistralAI(
        model=settings.MISTRAL_MODEL,
        api_key=settings.MISTRAL_API_KEY or None,
        temperature=temperature,
        timeout=60,
        max_retries=2,
    )


def llm_available() -> bool:
    return bool(settings.MISTRAL_API_KEY)
