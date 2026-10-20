"""Routing + helper unit tests (no DB / no LLM)."""
from __future__ import annotations

from app.graph.nodes.availability import _best_match, _norm
from app.graph.nodes.extraction import _coerce_fields, _missing, resolve_relative_date
from app.graph.nodes.transaction import _idempotency_key, _split_dt
from app.graph.workflow import (
    _route_after_availability,
    _route_after_extraction,
    _route_after_intake,
    _route_after_planner,
)


# --- fuzzy catalog matching -------------------------------------------------

def test_best_match_city_typo():
    cities = ["Casablanca", "Rabat", "Tanger", "Marrakech"]
    assert _best_match("casabla,ca", cities) == "Casablanca"


def test_best_match_sport_phrase():
    sports = ["Football", "Futsal", "Tennis", "Padel", "Basketball", "Badminton"]
    assert _best_match("terrain de padel couvert", sports) == "Padel"


def test_best_match_no_match_returns_none():
    assert _best_match("zzzzzz", ["Casablanca", "Rabat"]) is None


def test_norm_strips_non_alpha():
    assert _norm("Casabla,ca") == "casablaca"


# --- relative date resolution ----------------------------------------------

def test_relative_date_demain():
    from datetime import date
    assert resolve_relative_date("padel demain à 18h", today=date(2026, 10, 21)) == "2026-10-22"


def test_relative_date_today():
    from datetime import date
    assert resolve_relative_date("aujourd'hui", today=date(2026, 10, 21)) == "2026-10-21"


def test_relative_date_weekday():
    from datetime import date
    # 2026-10-21 is a Wednesday; "vendredi" (Fri=4) -> 2026-10-23
    assert resolve_relative_date("vendredi prochain", today=date(2026, 10, 21)) == "2026-10-23"


def test_relative_date_none_when_absent():
    assert resolve_relative_date("un terrain à Casablanca") is None


# --- planner routing --------------------------------------------------------

def test_route_planner_book_goes_to_extraction():
    assert _route_after_planner({"intent": "book"}) == "extraction"
    assert _route_after_planner({"intent": "modify_booking"}) == "extraction"


def test_route_planner_question_goes_to_faq():
    assert _route_after_planner({"intent": "question"}) == "faq"


def test_route_planner_confirm_cancel_goes_to_transaction():
    for intent in ("confirm_booking", "cancel_booking", "decline_booking"):
        assert _route_after_planner({"intent": intent}) == "transaction"


def test_route_planner_choose_alternative_goes_to_availability():
    assert _route_after_planner({"intent": "choose_alternative"}) == "availability"


def test_route_planner_unclear_goes_to_respond():
    assert _route_after_planner({"intent": "unclear"}) == "respond"


# --- intake routing ---------------------------------------------------------

def test_route_intake_pass_through():
    # The router returns the raw route; the graph's _INTAKE_MAP maps it to a node.
    assert _route_after_intake({"route": "process_message"}) == "process_message"
    assert _route_after_intake({"route": "reply_directly"}) == "reply_directly"
    assert _route_after_intake({"route": "duplicate_message"}) == "duplicate_message"
    assert _route_after_intake({}) == "reply_directly"


def test_intake_map_targets_valid_nodes():
    from app.graph.workflow import _INTAKE_MAP
    assert _INTAKE_MAP["reply_directly"] == "respond"
    assert _INTAKE_MAP["process_message"] == "planner"


# --- extraction routing -----------------------------------------------------

def test_route_extraction_missing_fields_skips_to_respond():
    assert _route_after_extraction({"missing_fields": ["sport"]}) == "respond"


def test_route_extraction_complete_goes_to_availability():
    assert _route_after_extraction({"missing_fields": []}) == "availability"


# --- availability routing ---------------------------------------------------

def test_route_availability_available_to_transaction():
    assert _route_after_availability({"availability_result": {"status": "available"}}) == "transaction"


def test_route_availability_none_to_respond():
    assert _route_after_availability({"availability_result": {"status": "none"}}) == "respond"


def test_route_availability_choose_alternative_to_transaction():
    assert _route_after_availability(
        {"intent": "choose_alternative", "availability_result": {"status": "none"}}
    ) == "transaction"


# --- extraction helpers -----------------------------------------------------

def test_coerce_fields_types():
    out = _coerce_fields({"sport": " Padel ", "duration_min": "90", "participants": "4", "date": None})
    assert out["sport"] == "Padel"
    assert out["duration_min"] == 90
    assert out["participants"] == 4
    assert out["date"] is None


def test_coerce_fields_bad_numbers():
    out = _coerce_fields({"duration_min": "abc"})
    assert out["duration_min"] is None


def test_missing_reports_required():
    fields = {"sport": "padel"}
    missing = _missing(fields)
    assert "city" in missing and "date" in missing and "start_time" in missing
    assert fields["duration_min"] == 60  # defaulted


# --- transaction helpers ----------------------------------------------------

def test_split_dt():
    assert _split_dt("2026-10-21 18:00") == ("2026-10-21", "18:00")
    assert _split_dt(None) == (None, None)
    assert _split_dt("no-space") == (None, None)


def test_idempotency_key_hold_is_stable():
    state = {
        "request_id": "req-1",
        "availability_result": {"selected_slot": {"court_id": "c1", "starts_at": "2026-10-21 18:00"}},
    }
    a = _idempotency_key(state, "hold")
    b = _idempotency_key(state, "hold")
    assert a == b
    assert "c1" in a and "req-1" in a
