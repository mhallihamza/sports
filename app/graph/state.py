"""Shared LangGraph state for the booking workflow.

Every node reads from and returns partial updates to this TypedDict. LangGraph
merges the partial dicts, so a node only needs to return the keys it changed.
"""
from __future__ import annotations

from typing import Any, Optional, TypedDict


class BookingState(TypedDict, total=False):
    # ---- Identity / intake -------------------------------------------------
    message_id: str
    sender_phone: str
    customer_message: str
    input_type: str
    extraction_status: str
    # Set by the retry worker so intake continues instead of deduping.
    is_retry: bool
    existing_request_id: Optional[str]

    # ---- Routing -----------------------------------------------------------
    route: str
    intent: str
    reason: str

    # ---- Intake results ----------------------------------------------------
    request_id: Optional[str]
    conversation_id: Optional[str]
    now_local: Optional[str]
    weekday: Optional[str]
    timezone: Optional[str]
    conversation: Optional[dict[str, Any]]

    # ---- Planner / router --------------------------------------------------
    steps: Optional[list[str]]
    language: Optional[str]

    # ---- Extraction --------------------------------------------------------
    extracted_fields: Optional[dict[str, Any]]
    collected_fields: Optional[dict[str, Any]]
    missing_fields: Optional[list[str]]

    # ---- Availability ------------------------------------------------------
    availability_result: Optional[dict[str, Any]]
    selected_index: Optional[int]

    # ---- Transaction -------------------------------------------------------
    transaction_result: Optional[dict[str, Any]]
    booking_id: Optional[str]
    booking_code: Optional[str]
    idempotency_key: Optional[str]

    # ---- FAQ ---------------------------------------------------------------
    faq_result: Optional[dict[str, Any]]

    # ---- Response ----------------------------------------------------------
    reply_text: Optional[str]
    final_status: str
