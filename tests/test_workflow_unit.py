"""Unit tests for the pure (no DB / no LLM) workflow logic.

Run with:  pytest -q
"""
from __future__ import annotations

from app.graph.nodes.availability import _format_offers
from app.graph.nodes.common import detect_language, extract_json
from app.graph.nodes.faq import build_faq_contract
from app.graph.nodes.planner import VALID_INTENTS, _fallback_intent
from app.graph.nodes.respond import _fmt, _missing_labels, respond_node
from app.timeutils import add_minutes, combine_local


# --- common -----------------------------------------------------------------

def test_detect_language_fr():
    assert detect_language("Bonjour, je veux réserver un terrain") == "fr"


def test_detect_language_en():
    assert detect_language("Hello, I want to book a court please") == "en"


def test_detect_language_ar():
    assert detect_language("مرحبا اريد حجز ملعب") == "ar"


def test_detect_language_defaults_to_fr():
    assert detect_language("") == "fr"


def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced_and_trailing_comma():
    assert extract_json('```json\n{"a": 1, "b": [1,2],}\n```') == {"a": 1, "b": [1, 2]}


def test_extract_json_garbage_returns_none():
    assert extract_json("no json here") is None


# --- planner fallback -------------------------------------------------------

def test_fallback_intent_book():
    intent, steps = _fallback_intent("je veux réserver un padel demain")
    assert intent == "book"
    assert steps


def test_fallback_intent_cancel():
    assert _fallback_intent("annule ma réservation")[0] == "cancel_booking"


def test_fallback_intent_question():
    assert _fallback_intent("quel est le prix ?")[0] == "question"


def test_fallback_intent_unclear():
    assert _fallback_intent("zxcvbn")[0] == "unclear"


def test_fallback_intents_are_valid():
    for msg in ("réserver", "annuler", "confirmer", "prix ?", "autre"):
        assert _fallback_intent(msg)[0] in VALID_INTENTS


# --- faq gate ---------------------------------------------------------------

def test_faq_gate_releases_answered():
    out = build_faq_contract(
        {"status": "answered", "language": "fr", "facts": [{"topic": "p", "content": "30 MAD"}]}
    )
    assert out["status"] == "answered"
    assert out["facts"] == [{"topic": "p", "content": "30 MAD"}]


def test_faq_gate_blocks_no_information():
    out = build_faq_contract({"status": "no_information", "facts": [{"content": "x"}]})
    assert out["status"] == "no_information"
    assert out["facts"] == []


def test_faq_gate_blocks_empty_facts():
    out = build_faq_contract({"status": "answered", "facts": []})
    assert out["status"] == "no_information"


def test_faq_gate_normalises_language():
    out = build_faq_contract(
        {"status": "answered", "language": "xx", "facts": [{"content": "y"}]}, language="en"
    )
    assert out["language"] == "en"


def test_faq_gate_handles_none():
    assert build_faq_contract(None)["status"] == "no_information"


# --- timeutils --------------------------------------------------------------

def test_combine_local():
    dt = combine_local("2026-10-21", "18:00")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.hour == 18 and dt.day == 21


def test_combine_local_invalid():
    assert combine_local(None, "18:00") is None
    assert combine_local("2026-10-21", "not-a-time") is None


def test_add_minutes():
    dt = combine_local("2026-10-21", "18:00")
    assert add_minutes(dt, 90).hour == 19
    assert add_minutes(dt, 30).minute == 30


# --- respond / formatting ---------------------------------------------------

def test_format_offers_numbered():
    text = _format_offers([
        {"venue_name": "A", "court_name": "1", "city": "Casa",
         "starts_at": "18:00", "ends_at": "19:00", "price_per_hour": 30, "currency": "MAD"},
    ])
    assert text.startswith("1.")
    assert "A" in text and "MAD" in text


def test_missing_labels_multilingual():
    assert "sport" in _missing_labels(["sport"], "en")
    assert _missing_labels(["sport"], "fr") == "le sport"


def test_respond_directly_passthrough():
    state = {"route": "reply_directly", "reply_text": "oops", "final_status": "failed"}
    out = respond_node(state)
    assert out["reply_text"] == "oops"
    assert out["final_status"] == "failed"


def test_respond_unclear_asks():
    out = respond_node({"language": "fr", "intent": "unclear"})
    assert "réserver" in out["reply_text"].lower() or "disponibilit" in out["reply_text"].lower()


def test_respond_need_info_lists_missing():
    state = {"language": "en", "intent": "book", "missing_fields": ["sport", "city"]}
    out = respond_node(state)
    assert "the sport" in out["reply_text"] and "the city" in out["reply_text"]


def test_respond_held_includes_code_and_price():
    state = {
        "language": "en",
        "intent": "book",
        "missing_fields": [],
        "extracted_fields": {},
        "availability_result": {"selected_slot": {
            "venue_name": "Club", "court_name": "P1",
            "starts_at": "2026-10-21 18:00", "ends_at": "2026-10-21 19:00",
        }},
        "transaction_result": {
            "status": "held", "booking_code": "ABCD1234",
            "price_total": 30, "currency": "MAD",
        },
    }
    out = respond_node(state)
    assert "ABCD1234" in out["reply_text"]
    assert "30" in out["reply_text"]
    assert out["final_status"] == "held"


def test_respond_confirmed():
    state = {
        "language": "en",
        "intent": "confirm_booking",
        "transaction_result": {"status": "confirmed", "booking_code": "XYZ"},
    }
    out = respond_node(state)
    assert "XYZ" in out["reply_text"]
    assert out["final_status"] == "confirmed"


def test_respond_faq_answered():
    state = {
        "language": "en",
        "intent": "question",
        "faq_result": {"status": "answered", "facts": [{"content": "Open 8h-23h"}]},
    }
    out = respond_node(state)
    assert "8h-23h" in out["reply_text"]


def test_respond_faq_no_information():
    state = {"language": "en", "intent": "question", "faq_result": {"status": "no_information"}}
    out = respond_node(state)
    assert "don't have" in out["reply_text"]


def test_fmt_tolerates_missing_keys():
    # Missing kwargs must not raise; template returned as-is.
    assert isinstance(_fmt("en", "held"), str)