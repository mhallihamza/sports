"""workflow_events store — the durable job record behind every workflow run.

Statuses (mirrors the CHECK constraint):
    received -> processing -> completed
                             -> retry_pending -> (re-run) processing
                             -> failed
                             -> dlq_pending   (attempts exhausted -> workflow_dlq)

`event_key` is the primary key and is caller-supplied so the same logical message
maps to one row across retries (idempotency).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.db.connection import run_query

TERMINAL = {"completed", "failed"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _backoff(attempt: int) -> int:
    """Exponential backoff in seconds: base * 2^(attempt-1), capped at 1 hour."""
    if attempt <= 0:
        return settings.RETRY_BASE_SECONDS
    return min(settings.RETRY_BASE_SECONDS * (2 ** (attempt - 1)), 3600)


def start_event(event_key: str, payload: dict[str, Any] | None = None,
                booking_request_id: str | None = None) -> None:
    """Create (or reset-for-retry) the event row in 'received'."""
    run_query(
        """
        INSERT INTO public.workflow_events (event_key, status, booking_request_id, payload, attempt_count, max_attempts)
        VALUES (:key, 'received', :brid, CAST(:payload AS jsonb), 0, :max_attempts)
        ON CONFLICT (event_key) DO UPDATE
            SET status = CASE WHEN workflow_events.status IN ('completed')
                              THEN 'completed' ELSE 'received' END,
                booking_request_id = COALESCE(EXCLUDED.booking_request_id, workflow_events.booking_request_id),
                updated_at = now()
        """,
        {
            "key": event_key,
            "brid": booking_request_id,
            "payload": json.dumps(payload or {}),
            "max_attempts": settings.MAX_ATTEMPTS,
        },
        fetch="none",
    )


def mark_processing(event_key: str, stage: str) -> None:
    run_query(
        """
        UPDATE public.workflow_events
        SET status = 'processing', stage = :stage,
            attempt_count = attempt_count + 1, updated_at = now()
        WHERE event_key = :key AND status <> 'completed'
        """,
        {"key": event_key, "stage": stage},
        fetch="none",
    )


def link_booking_request(event_key: str, booking_request_id: str) -> None:
    """Attach the booking_requests row to the event (so retries can find it)."""
    run_query(
        """
        UPDATE public.workflow_events
        SET booking_request_id = COALESCE(:brid, booking_request_id), updated_at = now()
        WHERE event_key = :key
        """,
        {"key": event_key, "brid": booking_request_id},
        fetch="none",
    )


def mark_completed(event_key: str, booking_id: str | None = None) -> None:
    run_query(
        """
        UPDATE public.workflow_events
        SET status = 'completed', completed_at = now(), updated_at = now(),
            booking_id = COALESCE(:bid, booking_id), last_error_code = NULL, last_error_message = NULL
        WHERE event_key = :key
        """,
        {"key": event_key, "bid": booking_id},
        fetch="none",
    )


def record_failure(event_key: str, error_code: str, error_message: str) -> str:
    """
    Record a failure. Returns the resulting status:
      - 'retry_pending' when attempts remain (schedules next_attempt_at)
      - 'dlq_pending'   when attempts are exhausted (and writes workflow_dlq)
    """
    row = run_query(
        """
        UPDATE public.workflow_events
        SET status = CASE WHEN attempt_count >= max_attempts THEN 'dlq_pending' ELSE 'retry_pending' END,
            next_attempt_at = CASE WHEN attempt_count >= max_attempts THEN NULL
                                   ELSE now() + make_interval(secs => :secs) END,
            last_error_code = :code, last_error_message = :msg,
            updated_at = now()
        WHERE event_key = :key
        RETURNING status, attempt_count, max_attempts, payload, stage
        """,
        {
            "key": event_key,
            "code": error_code,
            "msg": (error_message or "")[:2000],
            "secs": _backoff(1),
        },
        fetch="one",
    )
    if row is None:
        return "failed"

    status, attempt, _max_attempts, payload, stage = row
    if status == "dlq_pending":
        _write_dlq(event_key, stage, payload, error_code, error_message, attempt)
    else:
        # Recompute backoff using the real attempt number.
        run_query(
            """
            UPDATE public.workflow_events
            SET next_attempt_at = now() + make_interval(secs => :secs)
            WHERE event_key = :key
            """,
            {"key": event_key, "secs": _backoff(attempt)},
            fetch="none",
        )
    return status


def _write_dlq(event_key: str, stage: str | None, payload: Any,
               error_code: str, error_message: str, attempts: int) -> None:
    try:
        payload_str = payload if isinstance(payload, str) else json.dumps(payload or {})
    except (TypeError, ValueError):
        payload_str = "{}"
    run_query(
        """
        INSERT INTO public.workflow_dlq (event_key, stage, payload, error_code, error_message, attempts, status)
        VALUES (:key, :stage, CAST(:payload AS jsonb), :code, :msg, :attempts, 'pending')
        ON CONFLICT DO NOTHING
        """,
        {
            "key": event_key,
            "stage": stage,
            "payload": payload_str,
            "code": error_code,
            "msg": (error_message or "")[:2000],
            "attempts": attempts,
        },
        fetch="none",
    )


def due_retries(limit: int = 10) -> list[dict[str, Any]]:
    """Events in 'retry_pending' whose next_attempt_at has arrived (claimable)."""
    rows = run_query(
        """
        SELECT event_key, booking_request_id, payload, attempt_count, stage
        FROM public.workflow_events
        WHERE status = 'retry_pending'
          AND next_attempt_at IS NOT NULL
          AND next_attempt_at <= now()
        ORDER BY next_attempt_at
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
        """,
        {"limit": limit},
        fetch="all",
    )
    out = []
    for r in rows:
        payload = r.payload
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        out.append({
            "event_key": r.event_key,
            "booking_request_id": r.booking_request_id,
            "payload": payload or {},
            "attempt_count": r.attempt_count,
            "stage": r.stage,
        })
    return out
