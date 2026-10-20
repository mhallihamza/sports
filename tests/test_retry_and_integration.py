"""Tests for the retry/job machinery that don't need a live database.

Covers:
  - exponential backoff schedule in event_store
  - intake node: genuine duplicates short-circuit, but retries continue past intake
  - the full graph routes a 'book' request through extraction -> availability ->
    transaction -> respond, using a stubbed DB layer (no network / no DB).
"""
from __future__ import annotations

import pytest

from app.graph.nodes import intake as intake_mod
from app.graph.workflow import app_graph
from app.jobs import event_store


# --- backoff -----------------------------------------------------------------

def test_backoff_is_exponential_and_capped():
    assert event_store._backoff(0) >= 1
    assert event_store._backoff(1) == 5      # base 5s
    assert event_store._backoff(2) == 10
    assert event_store._backoff(3) == 20
    assert event_store._backoff(50) == 3600  # capped at 1 hour


# --- intake dedup vs retry --------------------------------------------------

class _StubQueries:
    """Minimal stand-in for app.db.queries used by intake_node."""
    duplicate = True

    @staticmethod
    def register_incoming_message(message_id, phone, content):
        return {"duplicate": _StubQueries.duplicate, "request_id": None}

    @staticmethod
    def get_request_by_message_id(message_id):
        return "existing-req-id"

    @staticmethod
    def get_open_conversation_id(phone):
        return "conv-123"


@pytest.fixture()
def stub_queries(monkeypatch):
    monkeypatch.setattr(intake_mod, "queries", _StubQueries)
    return _StubQueries


def test_intake_duplicate_short_circuits(stub_queries):
    stub_queries.duplicate = True
    out = intake_mod.intake_node({
        "message_id": "m1", "sender_phone": "+1", "customer_message": "hi",
    })
    assert out["route"] == "duplicate_message"


def test_intake_retry_continues_past_duplicate(stub_queries):
    stub_queries.duplicate = True
    out = intake_mod.intake_node({
        "message_id": "m1", "sender_phone": "+1", "customer_message": "hi",
        "is_retry": True,
    })
    assert out["route"] == "process_message"
    assert out["request_id"] == "existing-req-id"
    assert out["conversation_id"] == "conv-123"


def test_intake_missing_ids_fails():
    out = intake_mod.intake_node({"message_id": "", "sender_phone": "", "customer_message": "x"})
    assert out["route"] == "reply_directly"
    assert out["final_status"] == "failed"


# --- full-graph routing with stubbed DB --------------------------------------

def test_graph_routes_book_end_to_end(monkeypatch):
    """A complete 'book' request should reach respond with a held booking."""
    # Stub the DB-backed pieces so no live DB/LLM is required.
    monkeypatch.setattr(
        intake_mod, "queries",
        type("Q", (), {
            "register_incoming_message": staticmethod(
                lambda m, p, c: {"duplicate": False, "request_id": "req-1",
                                 "now_local": "2026-10-21 18:00", "weekday": "Wednesday",
                                 "timezone": "Africa/Casablanca",
                                 "conversation": {"id": "conv-1"}},
            ),
        }),
    )

    from app.graph.nodes import availability as avail_mod
    from app.graph.nodes import extraction as extract_mod
    from app.graph.nodes import transaction as txn_mod
    from app.graph.nodes import planner as planner_mod

    # Force deterministic intent + fields without an LLM.
    monkeypatch.setattr(
        planner_mod, "invoke_json",
        lambda *a, **k: {"intent": "book", "steps": [], "language": "en"},
    )
    monkeypatch.setattr(
        extract_mod, "invoke_json",
        lambda *a, **k: {"sport": "padel", "city": "Casablanca",
                         "date": "2026-10-21", "start_time": "18:00", "duration_min": 60},
    )
    monkeypatch.setattr(
        avail_mod, "queries",
        type("Q", (), {"check_availability": staticmethod(lambda **k: [{
            "court_id": "c1", "court_name": "P1", "venue_name": "Club",
            "city": "Casablanca", "starts_at": "2026-10-21 18:00",
            "ends_at": "2026-10-21 19:00", "price_per_hour": 30, "currency": "MAD",
        }])}),
    )
    monkeypatch.setattr(
        txn_mod, "queries",
        type("Q", (), {"hold_booking": staticmethod(lambda **k: {
            "status": "held", "booking_code": "ABCD1234", "booking_id": "b1",
            "price_total": 30, "currency": "MAD",
        })}),
    )
    # respond_node records the assistant reply via run_query; stub it out.
    from app.graph.nodes import respond as respond_mod
    monkeypatch.setattr(respond_mod, "run_query", lambda *a, **k: None)

    final = app_graph.invoke({
        "message_id": "m1", "sender_phone": "+1",
        "customer_message": "I want to book a padel court in Casablanca on 2026-10-21 at 18:00",
        "input_type": "text", "extraction_status": "ok",
    })

    assert final["intent"] == "book"
    assert final["booking_code"] == "ABCD1234"
    assert final["final_status"] == "held"
    assert "ABCD1234" in final["reply_text"]
