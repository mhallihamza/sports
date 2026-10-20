"""run_workflow — the single entry point that executes one booking request.

Used by the webhook (live path) and by the retry worker (re-run path). It wraps the
LangGraph run with the durable job lifecycle:

    start_event -> mark_processing -> app_graph.invoke -> enqueue outbound -> mark_completed
                                                    \\-> record_failure (retry / DLQ)

Because every effectful step is idempotent (register/hold/confirm/cancel dedupe on
keys, and the outbox is keyed by event_key), re-running a failed event is safe.
"""
from __future__ import annotations

import logging
from typing import Any

from app.channels.whatsapp import WhatsAppSendError, send
from app.graph.workflow import app_graph
from app.jobs import event_store, outbox

logger = logging.getLogger(__name__)


def run_workflow(
    event_key: str,
    message_id: str,
    sender_phone: str,
    customer_message: str,
    input_type: str = "text",
    extraction_status: str = "ok",
    is_retry: bool = False,
) -> dict[str, Any]:
    """Execute the booking graph for one inbound message, with job bookkeeping."""
    event_store.start_event(
        event_key,
        payload={
            "message_id": message_id,
            "sender_phone": sender_phone,
            "customer_message": customer_message,
            "input_type": input_type,
            "extraction_status": extraction_status,
        },
    )
    event_store.mark_processing(event_key, "intake")

    initial_state = {
        "message_id": message_id,
        "sender_phone": sender_phone,
        "customer_message": customer_message,
        "input_type": input_type,
        "extraction_status": extraction_status,
        "is_retry": is_retry,
    }

    try:
        final_state = app_graph.invoke(initial_state) or {}
    except Exception as exc:
        logger.exception("Graph run failed for %s", event_key)
        event_store.record_failure(event_key, "graph_error", str(exc))
        return {"ok": False, "error": str(exc)}

    # Duplicate messages end early with no reply — mark completed, nothing to send.
    if final_state.get("route") == "duplicate_message":
        event_store.mark_completed(event_key)
        return {"ok": True, "duplicate": True, "state": final_state}

    booking_id = final_state.get("booking_id")
    reply = final_state.get("reply_text")

    # Link the booking_requests row to this event so retries can resume from it.
    if final_state.get("request_id"):
        event_store.link_booking_request(event_key, final_state["request_id"])

    # Durably enqueue the outbound reply (outbox pattern).
    if reply:
        try:
            outbox.enqueue_message(
                event_key=event_key,
                recipient_ref=sender_phone,
                message_body=reply,
                conversation_id=final_state.get("conversation_id"),
            )
        except Exception as exc:
            logger.exception("Outbox enqueue failed for %s", event_key)
            event_store.record_failure(event_key, "outbox_error", str(exc))
            return {"ok": False, "error": str(exc)}

    event_store.mark_completed(event_key, booking_id=booking_id)
    return {"ok": True, "state": final_state, "reply": reply}


def deliver_outbound_job(job: dict[str, Any]) -> bool:
    """Attempt to deliver one claimed outbox job. Returns True on success."""
    try:
        provider_id = send(job["recipient_ref"], job["message_body"])
    except WhatsAppSendError as exc:
        outbox.mark_failure(job["id"], job["attempt_count"], exc.code, str(exc))
        logger.warning("Outbound job %s failed (%s): %s", job["id"], exc.code, exc)
        return False
    except Exception as exc:  # unexpected
        outbox.mark_failure(job["id"], job["attempt_count"], "unexpected", str(exc))
        logger.exception("Outbound job %s failed unexpectedly", job["id"])
        return False

    outbox.mark_sent(job["id"], provider_id)
    return True
