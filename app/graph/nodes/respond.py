"""Response node — composes the final WhatsApp reply from whichever branch ran.

Pure text templating (no LLM) so replies are deterministic and testable. Supports
fr / ar / en. Also records the assistant reply into the conversation history.
"""
from __future__ import annotations

from app.config import settings
from app.db.connection import run_query
from app.graph.state import BookingState

_T = {
    "fr": {
        "held": ("Réservation provisoire {code} ⏳\n"
                 "{venue} — {court}\n{start} → {end}\n"
                 "Prix : {price} {currency}\n"
                 "Valable {mins} min. Répondez « confirmer » pour valider, « annuler » pour abandonner."),
        "confirmed": "✅ Réservation {code} confirmée. À bientôt !",
        "already_confirmed": "✅ Votre réservation {code} est déjà confirmée.",
        "cancelled": "❎ Réservation {code} annulée. À bientôt !",
        "already_cancelled": "❎ Votre réservation {code} était déjà annulée.",
        "conflict": "Désolé, ce créneau vient d'être réservé. Voici d'autres options :\n{offers}",
        "no_slots": "Aucun créneau disponible pour {sport} à {city} le {date}. Voulez-vous une autre date ?",
        "not_found": "Je ne trouve pas de réservation active à confirmer ou annuler.",
        "expired": "Ce créneau n'est plus valable (délai dépassé). Puis-je vous proposer d'autres disponibilités ?",
        "need_info": "Pour vous réserver un terrain, j'ai besoin de : {missing}. Pouvez-vous me préciser ?",
        "answered": "• {facts}",
        "no_information": "Je n'ai pas cette information. Voulez-vous réserver un terrain ou parler à un conseiller ?",
        "unclear": "Je n'ai pas bien compris. Souhaitez-vous réserver un terrain, vérifier les disponibilités ou poser une question ?",
        "error": "Désolé, une erreur est survenue. Veuillez réessayer dans un instant.",
    },
    "ar": {
        "held": ("حجز مؤقت {code} ⏳\n{venue} — {court}\n{start} → {end}\n"
                 "الثمن : {price} {currency}\n"
                 "صالح لمدة {mins} دقيقة. أرسل « تأكيد » للتأكيد أو « إلغاء » للإلغاء."),
        "confirmed": "✅ تم تأكيد الحجز {code}. إلى اللقاء!",
        "already_confirmed": "✅ حجزك {code} مؤكد بالفعل.",
        "cancelled": "❎ تم إلغاء الحجز {code}. إلى اللقاء!",
        "already_cancelled": "❎ حجزك {code} كان ملغى بالفعل.",
        "conflict": "عذراً، تم حجز هذا الموعد للتو. إليك خيارات أخرى:\n{offers}",
        "no_slots": "لا توجد أماكن متاحة لـ {sport} في {city} يوم {date}. هل تريد تاريخاً آخر؟",
        "not_found": "لا أجد حجزاً نشطاً للتأكيد أو الإلغاء.",
        "expired": "انتهت صلاحية هذا الموعد. هل أقترح عليك أماكن أخرى؟",
        "need_info": "للحجز أحتاج إلى : {missing}. هل يمكنك التحديد؟",
        "answered": "• {facts}",
        "no_information": "لا أملك هذه المعلومة. هل تريد حجز ملعب أو التحدث مع مستشار؟",
        "unclear": "لم أفهم جيداً. هل تريد حجز ملعب، التحقق من التوافر، أو طرح سؤال؟",
        "error": "عذراً، حدث خطأ. يرجى المحاولة مرة أخرى.",
    },
    "en": {
        "held": ("Provisional booking {code} ⏳\n{venue} — {court}\n{start} → {end}\n"
                 "Price: {price} {currency}\n"
                 "Valid for {mins} min. Reply « confirm » to lock it in, « cancel » to release."),
        "confirmed": "✅ Booking {code} confirmed. See you soon!",
        "already_confirmed": "✅ Your booking {code} is already confirmed.",
        "cancelled": "❎ Booking {code} cancelled. See you soon!",
        "already_cancelled": "❎ Your booking {code} was already cancelled.",
        "conflict": "Sorry, that slot was just taken. Here are other options:\n{offers}",
        "no_slots": "No availability for {sport} in {city} on {date}. Want another date?",
        "not_found": "I couldn't find an active booking to confirm or cancel.",
        "expired": "That slot is no longer held (time expired). Shall I show other availability?",
        "need_info": "To book a court I need: {missing}. Could you provide them?",
        "answered": "• {facts}",
        "no_information": "I don't have that information. Want to book a court or talk to an agent?",
        "unclear": "I didn't quite catch that. Book a court, check availability, or ask a question?",
        "error": "Sorry, something went wrong. Please try again shortly.",
    },
}


def _fmt(lang: str, key: str, **kwargs) -> str:
    table = _T.get(lang, _T["fr"])
    template = table.get(key) or table["error"]
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError):
        return template


def _missing_labels(missing: list[str], lang: str) -> str:
    labels = {
        "sport": {"fr": "le sport", "ar": "الرياضة", "en": "the sport"},
        "city": {"fr": "la ville", "ar": "المدينة", "en": "the city"},
        "date": {"fr": "la date", "ar": "التاريخ", "en": "the date"},
        "start_time": {"fr": "l'heure de début", "ar": "وقت البدء", "en": "the start time"},
    }
    return ", ".join(labels.get(m, {}).get(lang, m) for m in missing)


def respond_node(state: BookingState) -> dict:
    lang = state.get("language") or "fr"

    # Intake already set an error/direct reply.
    if state.get("route") == "reply_directly":
        return {
            "reply_text": state.get("reply_text"),
            "final_status": state.get("final_status", "completed"),
        }

    intent = state.get("intent", "")

    if intent in ("book", "modify_booking", "choose_alternative"):
        reply = _booking_reply(state, lang)
    elif intent == "confirm_booking":
        tr = state.get("transaction_result") or {}
        status = tr.get("status")
        if status in ("confirmed", "already_confirmed"):
            reply = _fmt(lang, status, code=tr.get("booking_code") or state.get("booking_code", ""))
        elif status == "expired":
            reply = _fmt(lang, "expired")
        elif status == "not_found":
            reply = _fmt(lang, "not_found")
        else:
            reply = _fmt(lang, "error")
    elif intent in ("cancel_booking", "decline_booking"):
        tr = state.get("transaction_result") or {}
        status = tr.get("status")
        if status in ("cancelled", "already_cancelled"):
            reply = _fmt(lang, status, code=tr.get("booking_code") or state.get("booking_code", ""))
        elif status == "not_found":
            reply = _fmt(lang, "not_found")
        else:
            reply = _fmt(lang, "error")
    elif intent == "question":
        faq = state.get("faq_result") or {}
        if faq.get("status") == "answered":
            facts = "\n".join(f["content"] for f in faq.get("facts", []))
            reply = _fmt(lang, "answered", facts=facts)
        else:
            reply = _fmt(lang, "no_information")
    else:  # unclear / unknown
        reply = _fmt(lang, "unclear")

    _record_assistant_message(state, reply)
    return {"reply_text": reply, "final_status": _final_status(state)}


def _booking_reply(state: BookingState, lang: str) -> str:
    fields = state.get("extracted_fields") or {}
    missing = state.get("missing_fields") or []
    if missing:
        return _fmt(lang, "need_info", missing=_missing_labels(missing, lang))

    tr = state.get("transaction_result") or {}
    status = tr.get("status")
    avail = state.get("availability_result") or {}

    if status in ("held", "idempotent"):
        slot = avail.get("selected_slot") or {}
        return _fmt(
            lang, "held",
            code=tr.get("booking_code", ""),
            venue=slot.get("venue_name", fields.get("venue") or ""),
            court=slot.get("court_name", ""),
            start=slot.get("starts_at", ""),
            end=slot.get("ends_at", ""),
            price=tr.get("price_total", slot.get("price_per_hour", "")),
            currency=tr.get("currency", settings.DEFAULT_CURRENCY),
            mins=settings.HOLD_MINUTES,
        )
    if status == "conflict":
        return _fmt(lang, "conflict", offers=avail.get("offers_text", ""))
    if status == "no_slot" or avail.get("status") == "none":
        return _fmt(lang, "no_slots",
                    sport=fields.get("sport", "?"), city=fields.get("city", "?"),
                    date=fields.get("date", "?"))
    if status in ("invalid", "error", "not_found"):
        return _fmt(lang, "error")
    if avail.get("status") == "available" and not tr:
        return avail.get("offers_text") or _fmt(
            lang, "no_slots",
            sport=fields.get("sport", "?"), city=fields.get("city", "?"),
            date=fields.get("date", "?"))
    return _fmt(lang, "error")


def _final_status(state: BookingState) -> str:
    tr = state.get("transaction_result") or {}
    status = tr.get("status")
    if status in ("held", "idempotent"):
        return "held"
    if status in ("confirmed", "already_confirmed"):
        return "confirmed"
    if status == "cancelled" or status == "already_cancelled":
        return "cancelled"
    if status == "expired":
        return "completed"
    if state.get("missing_fields"):
        return "needs_clarification"
    return "completed"


def _record_assistant_message(state: BookingState, reply: str) -> None:
    """Best-effort: store the assistant reply against the open conversation."""
    conversation_id = state.get("conversation_id")
    if not conversation_id:
        return
    try:
        run_query(
            """
            INSERT INTO public.messages (conversation_id, booking_request_id, sender_type, content, metadata)
            VALUES (:cid, :brid, 'response_agent', :content, :meta)
            """,
            {
                "cid": conversation_id,
                "brid": state.get("request_id"),
                "content": reply,
                "meta": '{"input_source": "BookingResponseAgent"}',
            },
            fetch="none",
        )
    except Exception:
        pass
