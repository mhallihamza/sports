"""Planner / router node — classifies intent and detects language.

Mirrors the LangFlow 'BookingPlannerAgent' + 'SmartRouter'. Uses a strict-JSON LLM
call, with a deterministic keyword fallback so routing still works without an LLM key.
"""
from __future__ import annotations

import json

from app.db import queries
from app.graph.nodes.common import detect_language, invoke_json
from app.graph.state import BookingState

VALID_INTENTS = {
    "book",
    "choose_alternative",
    "confirm_booking",
    "decline_booking",
    "cancel_booking",
    "modify_booking",
    "question",
    "unclear",
}

_SYSTEM = """You are BookingPlannerAgent for a WhatsApp sports-court booking assistant.
Read the customer message (and any prior conversation context) and output STRICT JSON:
{
  "intent": "book|choose_alternative|confirm_booking|decline_booking|cancel_booking|modify_booking|question|unclear",
  "steps": ["short", "ordered", "plan", "steps"],
  "language": "fr|ar|en"
}
Rules:
- "book": wants to reserve a court (may be missing details).
- "question": asks about venues, prices, hours, policies, availability info (read-only).
- "confirm_booking"/"cancel_booking"/"decline_booking": act on an existing hold/booking.
- "choose_alternative": picking among offered slots.
- "modify_booking": change an existing booking.
- "unclear": cannot determine what they want.
Output ONLY the JSON object, no prose."""


def _fallback_intent(message: str) -> tuple[str, list[str]]:
    """Keyword routing so the graph still works when no LLM key is configured."""
    m = (message or "").lower()
    if any(w in m for w in ("annul", "cancel")):
        return "cancel_booking", ["cancel the referenced booking"]
    if any(w in m for w in ("confirme", "confirm", "oui", "yes", "d'accord")):
        return "confirm_booking", ["confirm the pending hold"]
    if any(w in m for w in ("autre", "alternative", "autre créneau", "noir", "other")):
        return "choose_alternative", ["offer alternative slots"]
    if any(w in m for w in ("modifi", "change", "déplacer", "deplacer")):
        return "modify_booking", ["modify the existing booking"]
    if any(w in m for w in ("?", "combien", "prix", "tarif", "heure", "ouvert", "où", "ou se",
                             "quel", "c'est", "regle", "règle", "politique", "rembours")):
        return "question", ["answer the informational question"]
    if any(w in m for w in ("réserver", "reserver", "book", "reserve", "veux", "veut",
                             "créneau", "creneau", "terrain", "padel", "tennis", "football")):
        return "book", ["collect missing booking details", "check availability", "hold the slot"]
    return "unclear", ["ask for clarification"]


def planner_node(state: BookingState) -> dict:
    message = state.get("customer_message", "") or ""
    phone = state.get("sender_phone", "") or ""
    conversation = state.get("conversation") or {}

    # Load persisted multi-turn context (pending intent + fields already collected).
    pending_intent = None
    collected: dict = {}
    if phone:
        try:
            cs = queries.get_conversation_state(phone) or {}
            pending_intent = cs.get("pending_intent")
            collected = cs.get("collected_fields") or {}
        except Exception:
            pass

    context_blob = ""
    if collected:
        context_blob += f"\nAlready collected: {json.dumps(collected, ensure_ascii=False)}"
    if conversation:
        context_blob += f"\nConversation record: {json.dumps(conversation, default=str, ensure_ascii=False)}"

    plan = invoke_json(_SYSTEM, f"Customer message:\n{message}{context_blob}")
    intent = (plan or {}).get("intent")
    if intent not in VALID_INTENTS:
        intent, steps = _fallback_intent(message)
        language = detect_language(message)
    else:
        steps = (plan or {}).get("steps") or []
        language = (plan or {}).get("language") or detect_language(message)
        if language not in ("fr", "ar", "en"):
            language = detect_language(message)

    # A terse reply ("casablanca", "19h", "le premier") mid-booking is the answer to
    # our last question, not a new request — continue the pending booking intent.
    if pending_intent in ("book", "modify_booking") and intent in ("unclear", "book"):
        if _looks_like_field_answer(message):
            intent = pending_intent

    return {"intent": intent, "steps": steps, "language": language}


def _looks_like_field_answer(message: str) -> bool:
    """
    True when a message is a short, fragmentary reply (a city, time, sport, or slot
    choice) rather than a new instruction. Keeps a multi-turn booking going.
    """
    m = (message or "").strip()
    if not m or len(m) > 40:
        return False
    # Any explicit action word means this is a new instruction, not a field answer.
    if any(w in m.lower() for w in (
        "annul", "cancel", "confirme", "confirm", "modifi", "change",
        "réserve", "reserve", "book", "veux", "veut", "prix", "tarif",
    )):
        return False
    return True
