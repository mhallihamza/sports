"""RAG retrieval from Astra DB vector store.

Embeds the user's question with Mistral embeddings and performs a vector
similarity search against the Astra DB collection configured in settings.
Returns text chunks that are injected into the FAQ prompt as extra context.

If any required setting is missing or a call fails, the module degrades
gracefully (empty results) so the FAQ node can fall back to SQL policies.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import requests

from app.config import settings

logger = logging.getLogger(__name__)

_EMBED_URL = "https://api.mistral.ai/v1/embeddings"


def rag_enabled() -> bool:
    """True when Astra DB + Mistral credentials are all configured."""
    return bool(
        settings.ASTRA_DB_API_ENDPOINT
        and settings.ASTRA_DB_TOKEN
        and settings.ASTRA_DB_COLLECTION
        and settings.MISTRAL_API_KEY
    )


def _embed(text: str) -> list[float] | None:
    """Embed a single text using the Mistral embeddings API."""
    if not settings.MISTRAL_API_KEY:
        return None
    try:
        resp = requests.post(
            _EMBED_URL,
            headers={
                "Authorization": f"Bearer {settings.MISTRAL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.MISTRAL_EMBEDDING_MODEL,
                "input": text,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"][0]["embedding"]
    except Exception as exc:
        logger.warning("Mistral embedding failed: %s", exc)
        return None


def _collection():
    """Get the Astra DB collection, or None if it cannot be initialised."""
    try:
        from astrapy import DataAPIClient

        client = DataAPIClient(token=settings.ASTRA_DB_TOKEN)
        database = client.get_database(settings.ASTRA_DB_API_ENDPOINT)
        return database.get_collection(settings.ASTRA_DB_COLLECTION)
    except Exception as exc:
        logger.warning("Astra DB collection init failed: %s", exc)
        return None


def json_safe_str(obj: Any) -> str:
    """Best-effort stringification of a document when no text field exists."""
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


def search_faq(query: str, k: int = 5) -> list[dict[str, Any]]:
    """Vector-search Astra DB for FAQ chunks relevant to ``query``.

    Returns a list of dicts: ``{"text": str, "source": str, "score": float}``.
    Returns an empty list when RAG is not configured or the search fails.
    """
    if not rag_enabled():
        return []

    collection = _collection()
    if collection is None:
        return []

    embedding = _embed(query)
    if embedding is None:
        return []

    try:
        cursor = collection.find(
            {},
            sort={"$vector": embedding},
            limit=k,
            include_similarity=True,
        )
        results: list[dict[str, Any]] = []
        for doc in cursor:
            text = (
                doc.get("text")
                or doc.get("content")
                or doc.get("body")
                or json_safe_str(doc)
            )
            metadata = doc.get("metadata") or {}
            results.append(
                {
                    "text": str(text).strip(),
                    "source": str(metadata.get("source", "Astra DB")),
                    "score": float(doc.get("$similarity", 0.0)),
                }
            )
        return results
    except Exception as exc:
        logger.warning("Astra DB vector search failed: %s", exc)
        return []

