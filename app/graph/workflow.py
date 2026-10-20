"""The booking workflow graph.

Mirrors the LangFlow 'Booking - Main workflow' topology:

    intake ─▶ planner ─┬─▶ extraction ─▶ availability ─▶ transaction ─▶ respond
                       ├─▶ availability ─▶ transaction ─▶ respond
                       ├─▶ transaction ─▶ respond
                       └─▶ faq ─▶ respond

Routing between the branches is decided by the classified intent. Every run ends at
`respond`, which composes the outbound reply.
"""
from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.graph.nodes import (
    availability_node,
    extraction_node,
    faq_node,
    intake_node,
    planner_node,
    respond_node,
    transaction_node,
)
from app.graph.state import BookingState


def _route_after_intake(state: BookingState) -> str:
    return state.get("route", "reply_directly")


def _route_after_planner(state: BookingState) -> str:
    intent = state.get("intent", "unclear")
    if intent in ("book", "modify_booking"):
        return "extraction"
    if intent == "choose_alternative":
        return "availability"
    if intent in ("confirm_booking", "decline_booking", "cancel_booking"):
        return "transaction"
    if intent == "question":
        return "faq"
    return "respond"  # unclear / anything else


def _route_after_extraction(state: BookingState) -> str:
    # Missing critical fields -> skip straight to respond (which asks for them).
    if state.get("missing_fields"):
        return "respond"
    return "availability"


def _route_after_availability(state: BookingState) -> str:
    avail = state.get("availability_result") or {}
    intent = state.get("intent", "")
    # If we were only choosing a slot, availability resolves the choice then holds it.
    if intent == "choose_alternative":
        return "transaction"
    if avail.get("status") == "available":
        return "transaction"
    # No slots / need more info -> respond directly.
    return "respond"


_INTAKE_MAP = {
    "process_message": "planner",
    "duplicate_message": END,
    "reply_directly": "respond",
}


def build_booking_graph():
    workflow = StateGraph(BookingState)

    workflow.add_node("intake", intake_node)
    workflow.add_node("planner", planner_node)
    workflow.add_node("extraction", extraction_node)
    workflow.add_node("availability", availability_node)
    workflow.add_node("transaction", transaction_node)
    workflow.add_node("faq", faq_node)
    workflow.add_node("respond", respond_node)

    workflow.set_entry_point("intake")

    workflow.add_conditional_edges("intake", _route_after_intake, _INTAKE_MAP)

    workflow.add_conditional_edges(
        "planner",
        _route_after_planner,
        {
            "extraction": "extraction",
            "availability": "availability",
            "transaction": "transaction",
            "faq": "faq",
            "respond": "respond",
        },
    )

    workflow.add_conditional_edges(
        "extraction",
        _route_after_extraction,
        {"availability": "availability", "respond": "respond"},
    )

    workflow.add_conditional_edges(
        "availability",
        _route_after_availability,
        {"transaction": "transaction", "respond": "respond"},
    )

    workflow.add_edge("transaction", "respond")
    workflow.add_edge("faq", "respond")
    workflow.add_edge("respond", END)

    return workflow.compile()


app_graph = build_booking_graph()
