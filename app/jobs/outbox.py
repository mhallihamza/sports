"""outbound_message_jobs — transactional outbox for WhatsApp replies.

The workflow never sends WhatsApp messages inline. It inserts a row here (the
"outbox"), and a worker delivers it with retries. This decouples processing
durability from provider availability and guarantees at-least-once delivery.
"""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.db.connection import run_query


def enqueue_message(
    event_key: str,
    recipient_ref: str,
    message_body: str,
    conversation_id: str | None = None,
    channel: str = "whatsapp",
) -> None:
    run_query(
        """
        INSERT INTO public.outbound_message_jobs (event_key, conversation_id, channel, recipient_ref, message_body, status)
        VALUES (:key, :cid, :channel, :recipient, :body, 'pending')
        """,
        {
            "key": event_key,
            "cid": conversation_id,
            "channel": channel,
            "recipient": recipient_ref,
            "body": message_body,
        },
        fetch="none",
    )


def claim_pending(limit: int = 10) -> list[dict[str, Any]]:
    """Atomically claim due outbound jobs (pending / retry_pending). Returns claimed rows."""
    rows = run_query(
        """
        WITH due AS (
            SELECT id FROM public.outbound_message_jobs
            WHERE status IN ('pending', 'retry_pending')
            ORDER BY id
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
        )
        UPDATE public.outbound_message_jobs j
        SET status = 'sending', attempt_count = j.attempt_count + 1, updated_at = now()
        FROM due
        WHERE j.id = due.id
        RETURNING j.id, j.event_key, j.conversation_id, j.channel, j.recipient_ref,
                  j.message_body, j.attempt_count
        """,
        {"limit": limit},
        fetch="all",
    )
    return [
        {
            "id": r.id,
            "event_key": r.event_key,
            "conversation_id": r.conversation_id,
            "channel": r.channel,
            "recipient_ref": r.recipient_ref,
            "message_body": r.message_body,
            "attempt_count": r.attempt_count,
        }
        for r in rows
    ]


def mark_sent(job_id: int, provider_message_id: str | None = None) -> None:
    run_query(
        """
        UPDATE public.outbound_message_jobs
        SET status = 'sent', sent_at = now(), updated_at = now(),
            provider_message_id = :pmid, last_error_code = NULL, last_error_message = NULL
        WHERE id = :id
        """,
        {"id": job_id, "pmid": provider_message_id},
        fetch="none",
    )


def mark_failure(job_id: int, attempt_count: int, error_code: str, error_message: str) -> str:
    """Requeue with backoff, or fail permanently once OUTBOX_MAX_ATTEMPTS is reached."""
    exhausted = attempt_count >= settings.OUTBOX_MAX_ATTEMPTS
    status = "failed" if exhausted else "retry_pending"
    run_query(
        """
        UPDATE public.outbound_message_jobs
        SET status = :status, updated_at = now(),
            last_error_code = :code, last_error_message = :msg
        WHERE id = :id
        """,
        {
            "id": job_id,
            "status": status,
            "code": error_code,
            "msg": (error_message or "")[:2000],
        },
        fetch="none",
    )
    return status
