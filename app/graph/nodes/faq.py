"""FAQ node — BookingFAQAgent.

Read-only answers about venues, courts, sports, prices, policies.
Context comes from three sources, merged before the LLM:

1. **Astra DB vector store (RAG)** — semantic search over your uploaded
   documents (PDFs, policies, etc.) via ``app.rag.retriever.search_faq``.
2. **PostgreSQL policies** — ``public.get_booking_policies()``.
3. **PostgreSQL venue catalog** — ``public.list_venues()``.

Finally applies a pure "answer gate" so we never invent facts (mirrors the
LangFlow FAQAnswerGate component). Output contract:
{status, language, facts:[{topic,content}]}.
"""
from __future__ import annotations

import json
from typing import Any

from app.db import queries
from app.graph.nodes.common import detect_language, extract_json, invoke_json
from app.graph.state import BookingState
from app.rag.retriever import rag_enabled, search_faq

VALID_LANGUAGES = {"fr", "ar", "en"}

_SYSTEM = """You are BookingFAQAgent for a WhatsApp sports-court booking assistant.
Answer ONLY from the provided context (RAG documents + official policies + venue catalog).
If the context does not contain the answer, set status to "no_information" and give no facts.
Output STRICT JSON:
{
  "status": "answered|no_information",
  "language": "fr|ar|en",
  "facts": [{"topic": "short label", "content": "one factual sentence"}]
}
Never invent prices, hours, or rules that are not in the context. Output ONLY the JSON."""


def build_faq_contract(
    answer: dict[str, Any] | None, language: str | None = None
) -> dict[str, Any]:
    """
    Pure gate that normalises an LLM answer into the response contract.

    Exposed separately (and unit-testable) so behaviour is guaranteed regardless of
    what the model returns.
    """
    fallback = {"status": "no_information", "language": language or "fr", "facts": []}
    if not isinstance(answer, dict):
        return fallback
    if str(answer.get("status", "")).strip().lower() != "answered":
        return fallback

    facts = []
    raw = answer.get("facts")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                content = str(item.get("content", "")).strip()
                topic = str(item.get("topic", "")).strip()
            else:
                content, topic = str(item).strip(), ""
            if content:
                facts.append({"topic": topic, "content": content})
    if not facts:
        return fallback

    lang = str(answer.get("language", "")).strip().lower()
    if lang not in VALID_LANGUAGES:
        lang = language or "fr"
    return {"status": "answered", "language": lang, "facts": facts}


def faq_node(state: BookingState) -> dict:
    message = state.get("customer_message", "") or ""
    language = state.get("language") or detect_language(message)

    # --- 1. RAG: Astra DB vector search over your documents ---
    rag_chunks: list[str] = []
    if rag_enabled():
        try:
            hits = search_faq(message, k=5)
            for h in hits:
                rag_chunks.append(f"[RAG:{h.get('source','Astra DB')}] {h.get('text','')}")
        except Exception:
            pass  # degrade gracefully; SQL context still available below

    # --- 2. SQL: policies + venues (existing behavior) ---
    keyword = _keyword(message)
    try:
        policies = queries.get_booking_policies(keyword) or queries.get_booking_policies(None)
        venues = queries.list_venues(None)
    except Exception as exc:
        return {
            "faq_result": {
                "status": "no_information",
                "language": language,
                "facts": [],
                "error": str(exc),
            }
        }

    # --- 3. Merge all context for the LLM ---
    context = {
        "rag_documents": rag_chunks,
        "policies": policies,
        "venues": venues,
    }
    context_str = json.dumps(context, ensure_ascii=False)
    user = f"Language: {language}\nQuestion: {message}\n\nContext:\n{context_str}"
    answer = invoke_json(_SYSTEM, user, temperature=0.2)

    contract = build_faq_contract(answer if answer else extract_json(json.dumps({})), language)
    return {"faq_result": contract}


def _keyword(message: str) -> str | None:
    """A crude keyword for policy lookup — first word that maps to a policy theme."""
    themes = ("prix", "tarif", "payment", "paiement", "rembours", "annulation",
              "règle", "regle", "politique", "hour", "heure", "ouvert")
    lowered = (message or "").lower()
    for t in themes:
        if t in lowered:
            return t
    return None
