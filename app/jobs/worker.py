"""Background worker: delivers the outbox and retries due workflow events.

Runs as an asyncio task started by the FastAPI app on startup. Each tick it:
  1. claims + delivers pending outbound_message_jobs (with provider retries)
  2. re-runs any workflow_events whose retry backoff has elapsed
  3. expires stale booking holds so courts free up
"""
from __future__ import annotations

import asyncio
import logging

from app.config import settings
from app.db.connection import run_query
from app.jobs import event_store, outbox, runner

logger = logging.getLogger(__name__)


def _deliver_outbox_once(limit: int = 20) -> int:
    jobs = outbox.claim_pending(limit)
    sent = 0
    for job in jobs:
        if runner.deliver_outbound_job(job):
            sent += 1
    return sent


def _retry_events_once(limit: int = 10) -> int:
    retried = 0
    for ev in event_store.due_retries(limit):
        payload = ev["payload"]
        if not payload.get("message_id"):
            continue
        logger.info("Retrying workflow event %s (attempt %s)", ev["event_key"], ev["attempt_count"])
        runner.run_workflow(
            event_key=ev["event_key"],
            message_id=payload.get("message_id", ""),
            sender_phone=payload.get("sender_phone", ""),
            customer_message=payload.get("customer_message", ""),
            input_type=payload.get("input_type", "text"),
            extraction_status=payload.get("extraction_status", "ok"),
            is_retry=True,
        )
        retried += 1
    return retried


def _expire_holds_once() -> int:
    try:
        return run_query("SELECT public.expire_holds()", fetch="scalar") or 0
    except Exception:
        logger.exception("expire_holds failed")
        return 0


async def worker_loop(stop_event: asyncio.Event) -> None:
    """Run forever until `stop_event` is set. Started by the app lifespan."""
    logger.info("Workflow worker started (poll=%ss)", settings.WORKER_POLL_SECONDS)
    hold_tick = 0
    while not stop_event.is_set():
        try:
            _deliver_outbox_once()
            _retry_events_once()
            # Expire holds roughly once a minute without a separate timer.
            hold_tick += 1
            if hold_tick >= max(60 // max(settings.WORKER_POLL_SECONDS, 1), 1):
                _expire_holds_once()
                hold_tick = 0
        except Exception:
            logger.exception("Worker tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.WORKER_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("Workflow worker stopped")
