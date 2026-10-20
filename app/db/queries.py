"""Typed helpers over the Postgres functions and workflow tables.

Every function here is a thin, side-effect-explicit wrapper. Nodes call these
instead of writing SQL inline, so the LangGraph layer stays readable and the DB
contract lives in one place.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.db.connection import call_function, run_query
from app.timeutils import iso


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------

def register_incoming_message(message_id: str, phone: str, content: str) -> dict[str, Any]:
    """Insert the incoming message + open/load a conversation. Idempotent on message_id."""
    return call_function(
        "register_incoming_message",
        {"p_message_id": message_id, "p_sender_phone": phone, "p_customer_message": content},
    )


def get_request_by_message_id(message_id: str) -> str | None:
    """The booking_requests.id for an already-registered provider message (used on retries)."""
    return run_query(
        """
        SELECT id FROM public.booking_requests
        WHERE provider = 'whatsapp' AND provider_message_id = :mid
        LIMIT 1
        """,
        {"mid": message_id},
        fetch="scalar",
    )


# ---------------------------------------------------------------------------
# Conversation state (multi-turn field collection)
# ---------------------------------------------------------------------------

def get_conversation_state(phone: str) -> dict[str, Any]:
    return call_function("get_conversation_state", {"p_sender_phone": phone}) or {}


def upsert_conversation_state(
    phone: str,
    pending_intent: str | None,
    collected_fields: dict[str, Any] | None = None,
    last_question: str | None = None,
    ttl_minutes: int = 30,
) -> None:
    _call_void(
        "upsert_conversation_state",
        {
            "p_sender_phone": phone,
            "p_pending_intent": pending_intent,
            "p_collected_fields": json.dumps(collected_fields or {}),
            "p_last_question": last_question,
            "p_ttl_minutes": ttl_minutes,
        },
    )


def clear_conversation_state(phone: str) -> None:
    _call_void("clear_conversation_state", {"p_sender_phone": phone})


def get_open_conversation_id(phone: str) -> str | None:
    """The sender's most recent open conversation id, if any (used on retries)."""
    return run_query(
        """
        SELECT id FROM public.conversations
        WHERE sender_phone = :phone AND status = 'open'
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        {"phone": phone},
        fetch="scalar",
    )


# ---------------------------------------------------------------------------
# Availability (read-only)
# ---------------------------------------------------------------------------

def check_availability(
    sport: str,
    city: str | None = None,
    venue: str | None = None,
    date_str: str | None = None,
    duration_min: int = 60,
    limit: int = 20,
    prefer_start: str | None = None,
) -> list[dict[str, Any]]:
    result = call_function(
        "check_availability",
        {
            "p_sport": sport,
            "p_city": city,
            "p_venue": venue,
            "p_date": date_str,
            "p_duration_min": duration_min,
            "p_limit": limit,
            "p_prefer_start": prefer_start,
        },
    )
    return result or []


# ---------------------------------------------------------------------------
# Transactions (idempotent, safe under concurrency)
# ---------------------------------------------------------------------------

def hold_booking(
    court_id: str,
    phone: str,
    starts_at: datetime,
    ends_at: datetime,
    idempotency_key: str,
    hold_minutes: int = 10,
) -> dict[str, Any]:
    return call_function(
        "hold_booking",
        {
            "p_court_id": court_id,
            "p_sender_phone": phone,
            "p_starts_at": iso(starts_at),
            "p_ends_at": iso(ends_at),
            "p_idempotency_key": idempotency_key,
            "p_hold_minutes": hold_minutes,
        },
    )


def confirm_booking(idempotency_key: str, phone: str) -> dict[str, Any]:
    return call_function(
        "confirm_booking", {"p_idempotency_key": idempotency_key, "p_sender_phone": phone}
    )


def cancel_booking(idempotency_key: str, phone: str) -> dict[str, Any]:
    return call_function(
        "cancel_booking", {"p_idempotency_key": idempotency_key, "p_sender_phone": phone}
    )


def expire_holds() -> int:
    return run_query("SELECT public.expire_holds()", fetch="scalar") or 0


# ---------------------------------------------------------------------------
# Read helpers for the transaction agent
# ---------------------------------------------------------------------------

def get_live_booking_by_phone(phone: str) -> dict[str, Any] | None:
    """Most recent held/confirmed booking for a sender — used to confirm/cancel."""
    row = run_query(
        """
        SELECT id, booking_code, idempotency_key, status,
               starts_at, ends_at, price_total, currency, hold_expires_at
        FROM public.bookings
        WHERE sender_phone = :phone
          AND status IN ('held', 'confirmed')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        {"phone": phone},
        fetch="one",
    )
    return _row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# Catalog / FAQ
# ---------------------------------------------------------------------------

def list_venues(city: str | None = None) -> list[dict[str, Any]]:
    return call_function("list_venues", {"p_city": city}) or []


def get_booking_policies(keyword: str | None = None) -> list[dict[str, Any]]:
    return call_function("get_booking_policies", {"p_keyword": keyword}) or []


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _call_void(fn_name: str, params: dict[str, Any]) -> None:
    placeholders = ", ".join(f":{k}" for k in params)
    run_query(f"SELECT public.{fn_name}({placeholders})", params, fetch="scalar")


def _row_to_dict(row) -> dict[str, Any]:
    keys = row._mapping.keys()
    return {k: (iso(v) if isinstance(v, datetime) else v) for k, v in zip(keys, row)}
