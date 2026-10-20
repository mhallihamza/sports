"""WhatsApp webhook endpoint.

Handles both Meta's verification handshake (GET) and inbound messages (POST).
Supports the official Meta Cloud API payload shape as well as the simplified
{"message_id","sender_phone","customer_message"} body used for local testing.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.config import settings
from app.jobs.runner import run_workflow

logger = logging.getLogger(__name__)
router = APIRouter()


class IncomingMessage(BaseModel):
    message_id: str
    sender_phone: str
    customer_message: str
    input_type: str = "text"
    extraction_status: str = "ok"


@router.get("/whatsapp")
async def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    """Meta's webhook verification handshake."""
    if hub_mode == "subscribe" and hub_verify_token == settings.WHATSAPP_VERIFY_TOKEN:
        return int(hub_challenge or 0)
    raise HTTPException(status_code=403, detail="Verification token mismatch")


def _extract_from_meta(payload: dict[str, Any]) -> list[IncomingMessage]:
    """Flatten a Meta Cloud webhook payload into normalized incoming messages."""
    messages: list[IncomingMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            contact_phone = None
            contacts = value.get("contacts") or []
            if contacts:
                contact_phone = contacts[0].get("wa_id") or contacts[0].get("phone_number", {}).get("phone_number")
            for msg in value.get("messages", []):
                if msg.get("type") != "text":
                    # Non-text (image, status, etc.) — mark ignored so intake skips it.
                    messages.append(IncomingMessage(
                        message_id=msg.get("id", ""),
                        sender_phone=contact_phone or msg.get("from", ""),
                        customer_message="",
                        extraction_status="ignored",
                    ))
                    continue
                messages.append(IncomingMessage(
                    message_id=msg.get("id", ""),
                    sender_phone=contact_phone or msg.get("from", ""),
                    customer_message=msg.get("text", {}).get("body", ""),
                ))
    return messages


@router.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Meta Cloud payload vs simple test payload.
    if "entry" in body:
        incoming = _extract_from_meta(body)
        if not incoming:
            return {"status": "ignored", "reason": "no_messages"}
    else:
        try:
            incoming = [IncomingMessage(**body)]
        except Exception:
            raise HTTPException(status_code=422, detail="Missing required message fields")

    results = []
    for msg in incoming:
        # Offload the blocking graph/DB work so we don't stall the event loop.
        result = await _run_in_thread(
            run_workflow,
            event_key=msg.message_id,
            message_id=msg.message_id,
            sender_phone=msg.sender_phone,
            customer_message=msg.customer_message,
            input_type=msg.input_type,
            extraction_status=msg.extraction_status,
        )
        results.append({"message_id": msg.message_id, "ok": result.get("ok", False)})

    return {"status": "processed", "results": results}


async def _run_in_thread(fn, **kwargs):
    import anyio.to_thread

    return await anyio.to_thread.run_sync(lambda: fn(**kwargs))
