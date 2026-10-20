"""Transaction node — BookingTransactionAgent.

Executes the state-changing action (hold / confirm / cancel) through idempotent
Postgres functions, so retries and duplicate webhooks never double-charge or double-book.
"""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.db import queries
from app.graph.state import BookingState
from app.timeutils import add_minutes, combine_local


def _idempotency_key(state: BookingState, action: str) -> str:
    """
    Stable key for an action. Derived from immutable inputs so a retried run of the
    same workflow event maps to the same key and the DB dedupes it.
    """
    if action == "hold":
        slot = (state.get("availability_result") or {}).get("selected_slot") or {}
        basis = f"{state.get('request_id')}|hold|{slot.get('court_id')}|{slot.get('starts_at')}"
    else:
        # confirm/cancel act on the live booking the customer already has.
        live = queries.get_live_booking_by_phone(state.get("sender_phone", "")) or {}
        basis = f"{state.get('request_id')}|{action}|{live.get('idempotency_key')}"
    return basis


def transaction_node(state: BookingState) -> dict:
    intent = state.get("intent", "")
    phone = state.get("sender_phone", "")

    try:
        if intent in ("book", "modify_booking"):
            result = _do_hold(state, phone)
        elif intent == "confirm_booking":
            result = _do_confirm(state, phone)
        elif intent in ("cancel_booking", "decline_booking"):
            result = _do_cancel(state, phone)
        elif intent == "choose_alternative":
            result = _do_hold(state, phone)
        else:
            result = {"status": "noop", "error": f"unhandled intent {intent}"}
    except Exception as exc:
        result = {"status": "error", "error": str(exc)}

    out: dict[str, Any] = {"transaction_result": result}
    if result.get("booking_id"):
        out["booking_id"] = result["booking_id"]
    if result.get("booking_code"):
        out["booking_code"] = result["booking_code"]
    if result.get("idempotency_key"):
        out["idempotency_key"] = result["idempotency_key"]
    return out


def _do_hold(state: BookingState, phone: str) -> dict[str, Any]:
    slot = (state.get("availability_result") or {}).get("selected_slot")
    if not slot:
        return {"status": "no_slot", "error": "No slot selected to hold"}

    court_id = slot.get("court_id")
    starts = combine_local(*_split_dt(slot.get("starts_at")))
    ends = combine_local(*_split_dt(slot.get("ends_at")))
    if not starts or not ends:
        # Derive end from duration if not provided.
        fields = state.get("extracted_fields") or {}
        if starts:
            ends = add_minutes(starts, fields.get("duration_min") or 60)
    if not starts or not ends:
        return {"status": "invalid", "error": "Could not resolve slot start/end times"}

    key = _idempotency_key(state, "hold")
    result = queries.hold_booking(
        court_id=court_id,
        phone=phone,
        starts_at=starts,
        ends_at=ends,
        idempotency_key=key,
        hold_minutes=settings.HOLD_MINUTES,
    ) or {}
    result["idempotency_key"] = key
    return result


def _do_confirm(state: BookingState, phone: str) -> dict[str, Any]:
    live = queries.get_live_booking_by_phone(phone)
    if not live:
        return {"status": "not_found", "error": "No active booking to confirm"}
    result = queries.confirm_booking(live["idempotency_key"], phone) or {}
    result["idempotency_key"] = live["idempotency_key"]
    return result


def _do_cancel(state: BookingState, phone: str) -> dict[str, Any]:
    live = queries.get_live_booking_by_phone(phone)
    if not live:
        return {"status": "not_found", "error": "No active booking to cancel"}
    result = queries.cancel_booking(live["idempotency_key"], phone) or {}
    result["idempotency_key"] = live["idempotency_key"]
    return result


def _split_dt(value: str | None) -> tuple[str | None, str | None]:
    """'YYYY-MM-DD HH:MM' -> ('YYYY-MM-DD', 'HH:MM')."""
    if not value or " " not in value:
        return (None, None)
    d, t = value.split(" ", 1)
    return (d.strip(), t.strip())
