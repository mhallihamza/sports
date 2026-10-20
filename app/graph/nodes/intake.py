"""Intake node — registers the message, detects duplicates, loads conversation.

Mirrors the LangFlow 'BookingIntakeAgent': calls public.register_incoming_message()
which is idempotent on (provider, provider_message_id) and opens/loads a conversation.
"""
from __future__ import annotations

from app.db import queries
from app.graph.state import BookingState


def intake_node(state: BookingState) -> dict:
    message_id = state.get("message_id")
    sender_phone = state.get("sender_phone")
    customer_message = state.get("customer_message", "") or ""

    if state.get("extraction_status") == "ignored":
        return {"route": "duplicate_message", "reason": "status_event"}

    if not message_id or not sender_phone:
        return {
            "route": "reply_directly",
            "reply_text": "Désolé, votre message n'a pas pu être traité.",
            "reason": "missing_ids",
            "final_status": "failed",
        }

    is_retry = bool(state.get("is_retry"))

    try:
        db_res = queries.register_incoming_message(message_id, sender_phone, customer_message)
    except Exception as exc:  # DB down, etc.
        return {
            "route": "reply_directly",
            "reply_text": "Désolé, une erreur technique est survenue. Veuillez réessayer.",
            "reason": f"intake_error:{exc}",
            "final_status": "failed",
        }

    # Duplicate handling: a genuine redelivery short-circuits, but a workflow retry
    # (same message being re-run after a failure) must continue past intake.
    if not db_res or db_res.get("duplicate"):
        if is_retry:
            existing_id = state.get("existing_request_id") or queries.get_request_by_message_id(message_id)
            return {
                "route": "process_message",
                "request_id": existing_id,
                "conversation_id": queries.get_open_conversation_id(sender_phone),
                "reason": "retry_continue",
            }
        return {"route": "duplicate_message", "reason": "already_processed"}

    conversation = db_res.get("conversation") or {}
    return {
        "route": "process_message",
        "request_id": db_res.get("request_id"),
        "now_local": db_res.get("now_local"),
        "weekday": db_res.get("weekday"),
        "timezone": db_res.get("timezone"),
        "conversation": conversation,
        "conversation_id": conversation.get("id"),
    }
