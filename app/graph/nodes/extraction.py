"""Extraction node — BookingExtractionAgent.

Turns free text into structured booking fields, merging any fields already collected
in prior turns (conversation_state). Determines which required fields are still missing.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.db import queries
from app.graph.nodes.common import invoke_json
from app.graph.state import BookingState
from app.timeutils import local_date_today

# Fields we ultimately need to place a booking, and the LLM-facing descriptions.
REQUIRED_FIELDS = {
    "sport": "sport name, e.g. padel / tennis / football",
    "city": "city, e.g. Casablanca",
    "date": "date in YYYY-MM-DD",
    "start_time": "start time in HH:MM (24h)",
    "duration_min": "duration in minutes as an integer",
}

_SYSTEM = """You are BookingExtractionAgent for a WhatsApp sports-court booking assistant.
Extract booking fields from the customer message into STRICT JSON:
{
  "sport": "string|null",
  "city": "string|null",
  "venue": "string|null",
  "date": "YYYY-MM-DD|null",
  "start_time": "HH:MM|null",
  "duration_min": integer|null,
  "participants": integer|null,
  "booking_reference": "string|null"
}
Only include what is clearly stated. Use null for anything absent or ambiguous.
Output ONLY the JSON object, no prose."""


def _coerce_fields(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("sport", "city", "venue", "date", "start_time", "booking_reference"):
        val = raw.get(key)
        if isinstance(val, str):
            val = val.strip()
        out[key] = val or None
    for key in ("duration_min", "participants"):
        val = raw.get(key)
        try:
            out[key] = int(val) if val is not None else None
        except (TypeError, ValueError):
            out[key] = None
    if out.get("duration_min") is not None and out["duration_min"] <= 0:
        out["duration_min"] = None
    return out


def _missing(fields: dict[str, Any]) -> list[str]:
    """Required for a NEW booking hold (venue/booking_reference are optional)."""
    missing = []
    for key in ("sport", "city", "date", "start_time"):
        if not fields.get(key):
            missing.append(key)
    if not fields.get("duration_min"):
        # Sensible default; not treated as blocking.
        fields["duration_min"] = 60
    return missing


# Relative-date keywords -> offset in days from today (checked case-insensitively).
# English/French/Arabic common phrasings; first match in the message wins.
_RELATIVE_DATES: list[tuple[tuple[str, ...], int]] = [
    (("aujourd'hui", "aujourdhui", "today", "اليوم"), 0),
    (("demain", "tomorrow", "غدا", "غداً"), 1),
    (("après-demain", "apres-demain", "after tomorrow"), 2),
]

_WEEKDAYS: dict[str, int] = {
    # French
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6,
    # English
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
}


def resolve_relative_date(message: str, today: date | None = None) -> str | None:
    """
    Turn a relative date phrase in the message into 'YYYY-MM-DD'.

    Returns None when the message contains no recognised relative date, so the
    caller can keep whatever the LLM extracted (or leave it missing).
    """
    lowered = (message or "").lower()
    base = today or local_date_today()

    # Specific day names first ("prochain/vendredi next Friday" treated as the
    # next occurrence of that weekday) — but only when no explicit offset word.
    for word, dow in _WEEKDAYS.items():
        if word in lowered:
            delta = (dow - base.weekday()) % 7
            if delta == 0:
                delta = 7  # "vendredi" said on a Friday means next week
            return (base + timedelta(days=delta)).isoformat()

    for keywords, offset in _RELATIVE_DATES:
        if any(k in lowered for k in keywords):
            return (base + timedelta(days=offset)).isoformat()

    return None


def extraction_node(state: BookingState) -> dict:
    message = state.get("customer_message", "") or ""
    phone = state.get("sender_phone", "") or ""

    # Prior-turn fields: start from anything already in this state, then layer in
    # the persisted conversation_state (this is what makes multi-turn work).
    prior = dict(state.get("collected_fields") or {})
    pending_intent = None
    if phone:
        try:
            cs = queries.get_conversation_state(phone) or {}
            prior = {**(cs.get("collected_fields") or {}), **prior}
            pending_intent = cs.get("pending_intent")
        except Exception:
            pass  # memory is a nicety; never block extraction on it

    extracted = invoke_json(_SYSTEM, message)
    new_fields = _coerce_fields(extracted or {})

    # Merge: new non-null values win, otherwise keep prior.
    merged = {**prior, **{k: v for k, v in new_fields.items() if v is not None}}

    # Resolve relative dates ("demain", "vendredi") the LLM couldn't pin down.
    if not merged.get("date"):
        resolved = resolve_relative_date(message)
        if resolved:
            merged["date"] = resolved

    intent = state.get("intent", "")
    missing: list[str] = []
    if intent in ("book", "modify_booking"):
        missing = _missing(merged)

        # Persist the partial collection so the next turn can continue it.
        if phone:
            try:
                queries.upsert_conversation_state(
                    phone,
                    pending_intent=intent,
                    collected_fields=merged,
                    last_question=_question_for(missing, state.get("language") or "fr"),
                )
            except Exception:
                pass
    elif pending_intent and intent in ("confirm_booking", "cancel_booking", "decline_booking"):
        # Acting on a booking ends the collection phase.
        _clear_state(phone)

    return {
        "extracted_fields": merged,
        "collected_fields": merged,
        "missing_fields": missing,
    }


def _clear_state(phone: str) -> None:
    if not phone:
        return
    try:
        queries.clear_conversation_state(phone)
    except Exception:
        pass


_LABELS = {
    "fr": {"sport": "le sport", "city": "la ville", "date": "la date", "start_time": "l'heure de début"},
    "en": {"sport": "the sport", "city": "the city", "date": "the date", "start_time": "the start time"},
    "ar": {"sport": "الرياضة", "city": "المدينة", "date": "التاريخ", "start_time": "وقت البدء"},
}


def _question_for(missing: list[str], lang: str) -> str | None:
    labels = _LABELS.get(lang, _LABELS["fr"])
    if not missing:
        return None
    joined = ", ".join(labels.get(m, m) for m in missing)
    return {
        "fr": f"J'ai besoin de : {joined}.",
        "en": f"I still need: {joined}.",
        "ar": f"أحتاج إلى: {joined}.",
    }.get(lang, f"I still need: {joined}.")

    return {
        "extracted_fields": merged,
        "collected_fields": merged,
        "missing_fields": missing,
    }
