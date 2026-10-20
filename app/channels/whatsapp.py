"""WhatsApp Cloud API sender.

`send` raises `WhatsAppSendError` on any provider failure so the outbox worker can
requeue the job. On success it returns the provider message id. When no token is
configured it runs in mock mode (logs and returns a fake id) so local dev works.
"""
from __future__ import annotations

import uuid

import requests

from app.config import settings


class WhatsAppSendError(RuntimeError):
    """Raised when the WhatsApp provider rejects or fails a send."""

    def __init__(self, message: str, code: str = "provider_error"):
        super().__init__(message)
        self.code = code


def send(phone_number: str, text: str) -> str:
    """Send a text message. Returns the provider message id. Raises WhatsAppSendError."""
    if not settings.WHATSAPP_TOKEN or not settings.WHATSAPP_PHONE_NUMBER_ID:
        print(f"[Mock WhatsApp] To: {phone_number} | {text}")
        return f"mock-{uuid.uuid4().hex[:12]}"

    url = f"https://graph.facebook.com/v17.0/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {settings.WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone_number,
        "type": "text",
        "text": {"body": text},
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise WhatsAppSendError(f"Network error: {exc}", code="network_error") from exc

    if response.status_code >= 400:
        raise WhatsAppSendError(
            f"HTTP {response.status_code}: {response.text[:500]}",
            code=f"http_{response.status_code}",
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise WhatsAppSendError("Invalid JSON from provider", code="bad_response") from exc

    if data.get("error"):
        err = data["error"]
        raise WhatsAppSendError(
            f"Provider error: {err.get('message', '')}",
            code=str(err.get("code", "provider_error")),
        )

    message_id = (data.get("messages") or [{}])[0].get("id")
    return message_id or ""


# Backwards-compatible alias used by older call sites.
def send_whatsapp_message(phone_number: str, text: str):
    return {"status": "sent", "provider_message_id": send(phone_number, text)}
