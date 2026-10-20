"""Availability node — BookingAvailabilityAgent.

Read-only: calls public.check_availability() and formats the offered slots. If the
customer is choosing among previously-offered options, it resolves the chosen slot.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from app.config import settings
from app.db import queries
from app.db.connection import run_query
from app.graph.state import BookingState


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def _best_match(value: str, candidates: list[str]) -> str | None:
    """Pick the closest candidate to `value` (case/diacritic/typo tolerant)."""
    if not value:
        return None
    nv = _norm(value)
    best, best_score = None, 0.0
    for cand in candidates:
        nc = _norm(cand)
        if not nc:
            continue
        score = SequenceMatcher(None, nv, nc).ratio()
        # Substring containment is a strong signal ("padel" in "terrain de padel").
        if nv and (nv in nc or nc in nv):
            score = max(score, 0.85)
        if score > best_score:
            best, best_score = cand, score
    return best if best_score >= 0.75 else None


def _resolve_ref(value: str | None, fetch_all) -> str | None:
    """
    Normalise a free-text sport/city/venue to a canonical name from the catalog.

    `fetch_all` returns the candidate names for that dimension. Returns the original
    value when nothing matches closely enough, so we never lose the user's intent.
    """
    if not value:
        return value
    try:
        candidates = fetch_all() or []
    except Exception:
        return value
    if not candidates:
        return value
    return _best_match(value, candidates) or value


def _format_offers(slots: list[dict[str, Any]]) -> str:
    lines = []
    for i, s in enumerate(slots, start=1):
        lines.append(
            f"{i}. {s.get('venue_name')} — {s.get('court_name')} ({s.get('city')})\n"
            f"   {s.get('starts_at')} → {s.get('ends_at')} | "
            f"{s.get('price_per_hour')} {s.get('currency')}"
        )
    return "\n".join(lines)


def availability_node(state: BookingState) -> dict:
    fields = state.get("extracted_fields") or {}
    intent = state.get("intent", "")

    # Choosing among alternatives: resolve the picked index (1-based from the user).
    if intent == "choose_alternative":
        slots = (state.get("availability_result") or {}).get("slots") or []
        chosen = _resolve_choice(state, slots)
        if chosen is not None:
            return {
                "selected_index": chosen,
                "availability_result": {
                    **(state.get("availability_result") or {}),
                    "selected_slot": slots[chosen],
                },
            }

    sport = fields.get("sport")
    if not sport:
        return {"availability_result": {"status": "need_info", "slots": []}}

    # Normalise free-text sport/city/venue against the catalog so typos and
    # phrases ("terrain de padel couvert", "casabla,ca") still match.
    sport = _resolve_ref(sport, _catalog_sports)
    city = _resolve_ref(fields.get("city"), _catalog_cities)
    venue = _resolve_ref(fields.get("venue"), _catalog_venues)

    try:
        slots = queries.check_availability(
            sport=sport,
            city=city,
            venue=venue,
            date_str=fields.get("date"),
            duration_min=fields.get("duration_min") or 60,
            limit=settings.SLOTS_PER_PAGE,
            prefer_start=fields.get("start_time"),
        )
    except Exception as exc:
        return {
            "availability_result": {"status": "error", "slots": [], "error": str(exc)}
        }

    status = "available" if slots else "none"
    result: dict[str, Any] = {
        "status": status,
        "slots": slots,
        "offers_text": _format_offers(slots) if slots else "",
    }
    # For a fresh booking the first available slot is the default choice, so the
    # downstream transaction node has something concrete to hold.
    if slots:
        result["selected_slot"] = slots[0]
    return {"availability_result": result}


def _resolve_choice(state: BookingState, slots: list[dict[str, Any]]) -> int | None:
    """Map a user-supplied index (or a matching slot) to a 0-based position."""
    explicit = state.get("selected_index")
    if explicit is not None and 0 <= explicit < len(slots):
        return explicit
    # Fall back to first slot when the user just says "the first / yes".
    return 0 if slots else None


# --- catalog lookups used for free-text normalisation ------------------------

def _catalog_sports() -> list[str]:
    rows = run_query("SELECT name FROM public.sports WHERE active", fetch="all")
    return [r[0] for r in rows]


def _catalog_cities() -> list[str]:
    rows = run_query("SELECT DISTINCT city FROM public.venues WHERE active", fetch="all")
    return [r[0] for r in rows]


def _catalog_venues() -> list[str]:
    rows = run_query("SELECT name FROM public.venues WHERE active", fetch="all")
    return [r[0] for r in rows]
