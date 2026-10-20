# WhatsApp Booking Agent (LangGraph)

A WhatsApp sports-court booking assistant built with **LangGraph** + **FastAPI** +
**PostgreSQL**. It replaces the earlier LangFlow flows with a single, durable,
event-driven workflow and the job machinery defined by your database schema
(`workflow_events`, `outbound_message_jobs`, `workflow_dlq`).

## Architecture

```
WhatsApp webhook (POST /api/whatsapp)
        │
        ▼
   run_workflow  ──▶ workflow_events (received → processing → completed / retry / DLQ)
        │
        ▼
   LangGraph graph
   intake ─▶ planner ─┬─▶ extraction ─▶ availability ─▶ transaction ─▶ respond
                      ├─▶ availability ─▶ transaction ─▶ respond
                      ├─▶ transaction ─▶ respond
                      └─▶ faq ─▶ respond
        │
        ▼
   outbound_message_jobs (transactional outbox) ──▶ WhatsApp Cloud API
        ▲
        └── background worker: delivers outbox, retries failed events, expires stale holds
```

### Mapping to the original LangFlow flows

| LangFlow flow                     | LangGraph node(s)                                   |
|-----------------------------------|-----------------------------------------------------|
| Booking - Main workflow (router)  | `planner` (+ conditional edges in `workflow.py`)     |
| BookingIntakeAgent                | `intake` → `register_incoming_message()`             |
| BookingExtractionAgent            | `extraction`                                         |
| BookingAvailabilityAgent          | `availability` → `check_availability()`              |
| BookingTransactionAgent           | `transaction` → `hold/confirm/cancel_booking()`      |
| BookingFAQAgent + FAQAnswerGate   | `faq` (+ pure `build_faq_contract` gate)             |
| Response agent                    | `respond` (fr / ar / en templating)                  |
| Webhook / Chat output             | `outbound_message_jobs` outbox + worker              |

### The "workflow jobs" machinery

- **`workflow_events`** (`app/jobs/event_store.py`) — one durable row per message.
  `received → processing → completed`, or `→ retry_pending → dlq_pending` with
  exponential backoff and `attempt_count` / `max_attempts`.
- **`outbound_message_jobs`** (`app/jobs/outbox.py`) — transactional outbox. The
  workflow never sends inline; it enqueues, and a worker delivers with retries
  (`FOR UPDATE SKIP LOCKED` claiming).
- **`workflow_dlq`** — written automatically when a message exhausts its attempts.
- **worker** (`app/jobs/worker.py`) — asyncio task started on app boot: delivers
  the outbox, re-runs due retries, and expires stale booking holds.

Everything is **idempotent**: `register_incoming_message` dedupes on
`provider_message_id`; `hold/confirm/cancel_booking` dedupe on `idempotency_key`;
the outbox is keyed by `event_key`. Re-running a failed message is always safe.

## Quick start

### 1. Manual chat test (no WhatsApp, no server) — recommended first step

```powershell
py chat.py
```

Opens an interactive chat console that runs the full LangGraph workflow in-process:

- **Sender:** HAMZA MHALLI (`0659865507`) — edit `SENDER_PHONE` / `SENDER_NAME` at the top of `chat.py` to change.
- **Message ID:** auto-generated per message (`cli-<timestamp>-<random>`).
- **Message:** typed by hand in the terminal.

```
you > je veux réserver un padel à Casablanca demain à 18h

agent >
  Réservation provisoire CB01D73A ⏳
  Padel Arena Casa — Padel 1
  2026-10-21 08:00 → 2026-10-21 09:00
  Prix : 220.0 MAD
  Valable 10 min. Répondez « confirmer » pour valider, « annuler » pour abandonner.

  [intent=book  status=held  msg_id=cli-...]
```

Then type `confirmer` to confirm the hold, `annuler` to cancel, or ask a question
(`quel sont vos prix ?`). Type `quit` / `exit` / `q` (or `Ctrl+C`) to leave.

Relative dates are supported: `demain`, `aujourd'hui`, `vendredi`, `tomorrow`, etc.

### 2. Full app (web API + background worker)

```powershell
py -m uvicorn app.main:app --reload
```

- Webhook: `POST /api/whatsapp` (Meta verification handshake on `GET /api/whatsapp`)
- Health: `GET /health`
- The worker (outbox delivery, retries, hold expiry) starts automatically with the app.

### 3. Run the tests

```powershell
py -m pytest tests/ -q
```

56 tests cover the pure logic (routing, extraction coercion, relative dates, FAQ
gate, response templating, backoff) and an end-to-end graph run with a stubbed DB
layer — no database or API keys required.

## Setup (from scratch)

1. **Install deps** (a full `pip freeze` is in `requirements.txt`):
   ```powershell
   py -m pip install -r requirements.txt
   ```
2. **Configure** `.env` (already present in this workspace):
   ```powershell
   copy .env.example .env    # then fill in DATABASE_URL, MISTRAL_API_KEY, WHATSAPP_*
   ```
   Notable: `MISTRAL_MODEL` defaults to `codestral-latest` — the model allowed by
   your subscription tier (`mistral-large-latest` returns 403 `tier_not_allowed`).

   > **Security:** the previous `.env.example` contained real credentials that were
   > pasted into a LangFlow export. Rotate your Supabase password, WhatsApp token,
   > Mistral key, and Astra token before deploying.
3. **Apply the database functions** (idempotent): run `000_base_schema.sql` then
   `001_booking_functions.sql` against your Supabase project (SQL editor or `psql`),
   or use a script that executes them with a raw cursor (no bind params, so
   `%ROWTYPE` in plpgsql bodies survives). Both migrations are **already applied**
   to the current database.
4. **Run** step 1 or 2 above.

## Configuration reference

See `.env.example`. Notable knobs: `HOLD_MINUTES` (booking hold TTL),
`SLOTS_PER_PAGE`, `MAX_ATTEMPTS`, `RETRY_BASE_SECONDS`, `WORKER_POLL_SECONDS`,
`OUTBOX_MAX_ATTEMPTS`, and the working-hour / timezone defaults.

## Notes & limitations

- **LLM**: a single Mistral model (`codestral-latest` by default) powers
  router/extraction/FAQ. Your subscription tier blocks `mistral-large-latest`
  (403 `tier_not_allowed`) and rate-limits `mistral-small-latest`/`open-mixtral-*`
  (429), so codestral is the reliable choice. Without any API key, the router falls
  back to deterministic keyword intent detection and the graph still runs (FAQ
  returns `no_information`), so local dev stays functional.
- **Relative dates**: `demain`, `aujourd'hui`, weekday names, `tomorrow`, etc. are
  resolved to real dates in `extraction.resolve_relative_date`.
- **FAQ knowledge base**: answers come from `get_booking_policies()` +
  `list_venues()`. The original LangFlow flow also used an Astra DB vector store for
  RAG; that path is optional and not wired in by default.
- **Concurrency**: booking overlaps are guarded by a Postgres exclusion constraint
  (`bookings_no_overlap`, needs `btree_gist`) plus an advisory lock per court.
- **`chat.py` vs the server**: the chat console runs the workflow in-process and
  prints the reply directly. The outbox/worker path (real WhatsApp delivery) is
  exercised when you run the full app with `py -m uvicorn app.main:app --reload`.
