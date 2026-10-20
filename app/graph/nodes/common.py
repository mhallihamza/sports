"""Shared helpers for nodes: strict JSON parsing, language detection, prompts."""
from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.llm import get_llm, llm_available

# Language keywords → BCP-47-ish code used for reply templating.
_LANG_HINTS = {
    "fr": ("fr", "français", "s'il vous plaît", "merci", "bonjour", "je veux", "rendez-vous"),
    "ar": ("ar", "العربية", "اريد", "أريد", "مرحبا", "شكرا", "من فضلك"),
    "en": ("en", "please", "thanks", "hello", "i want", "book", "booking"),
}


def detect_language(text: str) -> str:
    """Best-effort language detection: 'fr' | 'ar' | 'en'. Defaults to 'fr'."""
    lowered = (text or "").lower()
    scores = {"fr": 0, "ar": 0, "en": 0}
    for lang, hints in _LANG_HINTS.items():
        for h in hints:
            if h in lowered:
                scores[lang] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "fr"


def extract_json(text: str) -> dict[str, Any] | None:
    """Parse the first JSON object found in an LLM response, tolerating code fences."""
    if not text:
        return None
    cleaned = text.strip()
    # Strip ```json ... ``` fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    # First balanced-brace attempt.
    brace = re.search(r"\{.*\}", cleaned, re.DOTALL)
    candidate = brace.group(0) if brace else cleaned
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # Fallback: repair trailing commas conservatively.
    try:
        repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
        obj = json.loads(repaired)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def invoke_json(system: str, user: str, temperature: float = 0.1) -> dict[str, Any] | None:
    """Call the LLM and parse a JSON object out of the reply. None on any failure."""
    if not llm_available():
        return None
    llm = get_llm(temperature)
    try:
        resp = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        return extract_json(getattr(resp, "content", str(resp)))
    except Exception:
        return None
