#!/usr/bin/env python
"""Manual chat console — talk to the booking workflow like a chat window.

Runs the LangGraph workflow in-process (no WhatsApp, no web server needed).
Each message you type gets an auto-generated message_id and is sent through the
full pipeline (intake → planner → … → respond). The reply is printed right here.

Usage:
    python chat.py

Then type a message and press Enter. Type quit / exit / q (or Ctrl+C) to leave.
"""
from __future__ import annotations

import time
import uuid

from app.jobs.runner import run_workflow

# --- Who we are chatting as (edit these if you like) ------------------------
SENDER_PHONE = "0659865507"
SENDER_NAME = "HAMZA MHALLI"
# ----------------------------------------------------------------------------


def new_message_id() -> str:
    """Auto-generated, unique message id (dedupe key for the workflow)."""
    return f"cli-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def print_reply(reply: str) -> None:
    print("\nagent >")
    for line in (reply or "(no reply)").splitlines():
        print(f"  {line}")
    print()


def main() -> None:
    print("=" * 60)
    print(f"  Booking chat — {SENDER_NAME} ({SENDER_PHONE})")
    print("  Type a message and press Enter.")
    print("  Commands: quit / exit / q  (or Ctrl+C)")
    print("=" * 60)
    print("  Try: je veux réserver un padel à Casablanca demain à 18h\n")

    while True:
        try:
            message = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye 👋")
            break

        if not message:
            continue
        if message.lower() in {"quit", "exit", "q"}:
            print("bye 👋")
            break

        message_id = new_message_id()
        result = run_workflow(
            event_key=message_id,
            message_id=message_id,
            sender_phone=SENDER_PHONE,
            customer_message=message,
            input_type="text",
            extraction_status="ok",
        )

        if not result.get("ok"):
            print_reply(f"⚠️ error: {result.get('error', 'unknown')}")
            continue

        state = result.get("state") or {}
        print_reply(state.get("reply_text") or "(no reply)")

        # Small debug line so you can see how it was routed.
        intent = state.get("intent", "?")
        status = state.get("final_status", "?")
        print(f"  [intent={intent}  status={status}  msg_id={message_id}]\n")


if __name__ == "__main__":
    main()
