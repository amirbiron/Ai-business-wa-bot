"""
WhatsApp Handlers — לוגיקת טיפול בהודעות וואטסאפ.

שכבה דקה שמתרגמת בין WhatsApp payloads ← → הלוגיקה העסקית הקיימת
(RAG, intent detection, booking, live chat).

מזהה משתמשים: wa_{phone_number} — נפרד ממזהי טלגרם.
"""

import json
import logging
import re

from ai_chatbot import database as db
from ai_chatbot.llm import generate_answer, strip_source_citation, maybe_summarize
from ai_chatbot.intent import Intent, detect_intent, get_direct_response
from ai_chatbot.business_hours import is_currently_open, get_weekly_schedule_text
from ai_chatbot.config import (
    BUSINESS_NAME,
    BUSINESS_PHONE,
    BUSINESS_ADDRESS,
    FALLBACK_RESPONSE,
    CONTEXT_WINDOW_SIZE,
    FOLLOW_UP_ENABLED,
    TELEGRAM_OWNER_CHAT_ID,
    WHATSAPP_OWNER_PHONE,
)
from ai_chatbot.rate_limiter import check_rate_limit, record_message
from ai_chatbot.live_chat_service import LiveChatService
from ai_chatbot.vacation_service import VacationService
from ai_chatbot.bot.whatsapp_api import (
    send_text_message,
    send_buttons_message,
    send_list_message,
    mark_as_read,
)

logger = logging.getLogger(__name__)

# ── קידומת מזהה משתמש וואטסאפ ─────────────────────────────────────────────
_WA_PREFIX = "wa_"

# ── מצבי booking flow ─────────────────────────────────────────────────────
_BOOKING_STATES = {
    "service": "waiting_service",
    "date": "waiting_date",
    "time": "waiting_time",
    "confirm": "waiting_confirm",
}

# ── כפתורים ─────────────────────────────────────────────────────────────────
# מוגבלים ל-3 כפתורי quick reply + רשימה לשאר האפשרויות
_MAIN_BUTTONS = [
    {"id": "btn_price_list", "title": "📋 מחירון"},
    {"id": "btn_booking", "title": "📅 בקשת תור"},
    {"id": "btn_agent", "title": "👤 נציג"},
]

_MAIN_LIST_SECTIONS = [
    {
        "title": "אפשרויות נוספות",
        "rows": [
            {"id": "list_location", "title": "📍 מיקום", "description": "כתובת ומפה של העסק"},
            {"id": "list_hours", "title": "🕐 שעות פתיחה", "description": "מתי אנחנו פתוחים"},
        ],
    }
]


def _wa_user_id(phone: str) -> str:
    """המרת מספר טלפון למזהה משתמש פנימי."""
    return f"{_WA_PREFIX}{phone}"


def _is_wa_user(user_id: str) -> bool:
    """בדיקה אם מזהה משתמש הוא של וואטסאפ."""
    return user_id.startswith(_WA_PREFIX)


def _phone_from_user_id(user_id: str) -> str:
    """חילוץ מספר טלפון ממזהה משתמש וואטסאפ."""
    return user_id[len(_WA_PREFIX):]


def html_to_whatsapp(text: str) -> str:
    """המרת HTML tags (שנוצר ע"י LLM לטלגרם) ל-WhatsApp markdown.

    - <b>bold</b> → *bold*
    - <i>italic</i> → _italic_
    - <u>underline</u> → מסירים (אין תמיכה בוואטסאפ)
    - תגים אחרים — מסירים
    """
    # המרת תגים נתמכים
    text = re.sub(r"<b>(.*?)</b>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>", r"_\1_", text, flags=re.DOTALL)
    # הסרת תגים לא נתמכים
    text = re.sub(r"</?[^>]+>", "", text)
    return text


def _send_main_menu(phone: str, text: str) -> None:
    """שליחת הודעה עם תפריט ראשי (כפתורים)."""
    send_buttons_message(phone, text, _MAIN_BUTTONS)


def _send_with_menu(phone: str, text: str) -> None:
    """שליחת טקסט + תפריט ראשי כהודעה נפרדת."""
    send_text_message(phone, text)
    send_buttons_message(phone, "איך עוד אפשר לעזור? 👇", _MAIN_BUTTONS)


def _notify_owner_wa(text: str) -> None:
    """שליחת התראה לבעל העסק — וואטסאפ (אם מוגדר) + טלגרם (אם מוגדר)."""
    # התראה בוואטסאפ לבעל העסק
    if WHATSAPP_OWNER_PHONE:
        send_text_message(WHATSAPP_OWNER_PHONE, text)

    # התראה בטלגרם לבעל העסק (משתמש בפונקציה הקיימת)
    if TELEGRAM_OWNER_CHAT_ID:
        from ai_chatbot.live_chat_service import send_telegram_message
        send_telegram_message(TELEGRAM_OWNER_CHAT_ID, text)


# ── Booking State Machine ──────────────────────────────────────────────────

def _get_booking_state(user_id: str) -> dict | None:
    """קריאת מצב booking נוכחי מה-DB."""
    return db.get_wa_booking_state(user_id)


def _set_booking_state(user_id: str, state: str, data: dict) -> None:
    """עדכון מצב booking ב-DB."""
    db.set_wa_booking_state(user_id, state, json.dumps(data, ensure_ascii=False))


def _clear_booking_state(user_id: str) -> None:
    """מחיקת מצב booking."""
    db.clear_wa_booking_state(user_id)


# ── Handler ראשי ────────────────────────────────────────────────────────────

def handle_whatsapp_message(
    phone: str,
    text: str,
    message_id: str,
    display_name: str = "",
) -> None:
    """טיפול בהודעת טקסט נכנסת מוואטסאפ — הפונקציה הראשית."""
    user_id = _wa_user_id(phone)
    if not display_name:
        display_name = phone

    # סימון הודעה כנקראה
    mark_as_read(message_id)

    # רישום המשתמש כמנוי שידורים
    db.ensure_user_subscribed(user_id)

    # בדיקת live chat — אם פעיל, שומרים את ההודעה ויוצאים
    if LiveChatService.is_active(user_id):
        db.save_message(user_id, display_name, "user", text)
        db.touch_live_chat(user_id)
        return

    # בדיקת rate limit
    limit_msg = check_rate_limit(user_id)
    if limit_msg is not None:
        send_text_message(phone, html_to_whatsapp(limit_msg))
        return

    # רישום הודעה ב-rate limiter
    record_message(user_id)

    # בדיקת booking flow פעיל
    booking = _get_booking_state(user_id)
    if booking:
        _handle_booking_step(phone, user_id, display_name, text, booking)
        return

    # זיהוי Intent
    intent = detect_intent(text)

    # Greeting / Farewell
    if intent in (Intent.GREETING, Intent.FAREWELL):
        db.save_message(user_id, display_name, "user", text)
        response = get_direct_response(intent)
        db.save_message(user_id, display_name, "assistant", response)
        _send_main_menu(phone, response)
        return

    # שעות פתיחה
    if intent == Intent.BUSINESS_HOURS:
        db.save_message(user_id, display_name, "user", text)
        status = is_currently_open()
        schedule = get_weekly_schedule_text()
        response = f"{status['message']}\n\n{schedule}"
        db.save_message(user_id, display_name, "assistant", response)
        _send_with_menu(phone, response)
        return

    # בקשת תור
    if intent == Intent.APPOINTMENT_BOOKING:
        db.save_message(user_id, display_name, "user", text)
        if VacationService.is_active():
            response = VacationService.get_booking_message()
            db.save_message(user_id, display_name, "assistant", response)
            _send_with_menu(phone, response)
            return
        _start_booking(phone, user_id, display_name)
        return

    # ביטול תור
    if intent == Intent.APPOINTMENT_CANCEL:
        db.save_message(user_id, display_name, "user", text)
        confirm_buttons = [
            {"id": "cancel_appt_yes", "title": "כן, לבטל"},
            {"id": "cancel_appt_no", "title": "לא, טעות"},
        ]
        confirm_text = "האם אתם בטוחים שתרצו לבטל את התור?"
        db.save_message(user_id, display_name, "assistant", confirm_text)
        send_buttons_message(phone, confirm_text, confirm_buttons)
        return

    # בקשת נציג
    if intent == Intent.HUMAN_AGENT:
        db.save_message(user_id, display_name, "user", text)
        if VacationService.is_active():
            response = VacationService.get_agent_message()
            db.save_message(user_id, display_name, "assistant", response)
            _send_with_menu(phone, response)
            return
        _request_agent(phone, user_id, display_name, text)
        return

    # תלונה
    if intent == Intent.COMPLAINT:
        db.save_message(user_id, display_name, "user", text)
        response = (
            "אנחנו מצטערים לשמוע שהחוויה לא הייתה טובה. 😔\n"
            "נשמח לטפל בפנייתכם באופן אישי.\n\n"
            "לחצו על *👤 נציג* למטה כדי שנציג אנושי יחזור אליכם."
        )
        db.save_message(user_id, display_name, "assistant", response)
        _send_main_menu(phone, response)
        return

    # ── RAG pipeline — GENERAL, PRICING, LOCATION ────────────────────────
    query = text
    if intent == Intent.PRICING:
        query = "מחירון: " + text
    elif intent == Intent.LOCATION:
        query = "מיקום כתובת הגעה: " + text

    handoff_reason = f"הלקוח שאל (וואטסאפ): {text}"
    _handle_rag_query_wa(phone, user_id, display_name, text, query, handoff_reason)


def handle_whatsapp_button_reply(
    phone: str,
    button_id: str,
    button_title: str,
    display_name: str = "",
) -> None:
    """טיפול בלחיצת כפתור quick reply בוואטסאפ."""
    user_id = _wa_user_id(phone)
    if not display_name:
        display_name = phone

    # live chat guard
    if LiveChatService.is_active(user_id):
        db.save_message(user_id, display_name, "user", button_title)
        db.touch_live_chat(user_id)
        return

    # rate limit
    limit_msg = check_rate_limit(user_id)
    if limit_msg is not None:
        send_text_message(phone, html_to_whatsapp(limit_msg))
        return
    record_message(user_id)

    if button_id == "btn_price_list":
        db.save_message(user_id, display_name, "user", "📋 מחירון")
        _handle_rag_query_wa(
            phone, user_id, display_name,
            user_message="📋 מחירון",
            query="הצג לי את המחירון המלא עם כל השירותים והמחירים",
            handoff_reason="הלקוח ביקש מחירון (וואטסאפ).",
        )

    elif button_id == "btn_booking":
        db.save_message(user_id, display_name, "user", "📅 בקשת תור")
        if VacationService.is_active():
            response = VacationService.get_booking_message()
            db.save_message(user_id, display_name, "assistant", response)
            _send_with_menu(phone, response)
            return
        _start_booking(phone, user_id, display_name)

    elif button_id == "btn_agent":
        db.save_message(user_id, display_name, "user", "👤 שיחה עם נציג")
        if VacationService.is_active():
            response = VacationService.get_agent_message()
            db.save_message(user_id, display_name, "assistant", response)
            _send_with_menu(phone, response)
            return
        _request_agent(phone, user_id, display_name, "הלקוח מבקש לדבר עם נציג אנושי.")

    elif button_id == "cancel_appt_yes":
        _handle_cancel_confirm(phone, user_id, display_name, confirmed=True)

    elif button_id == "cancel_appt_no":
        _handle_cancel_confirm(phone, user_id, display_name, confirmed=False)

    elif button_id.startswith("booking_service_") or button_id.startswith("booking_confirm_"):
        # כפתורי booking — בחירת שירות או אישור/ביטול
        _handle_booking_button(phone, user_id, display_name, button_id, button_title)

    else:
        logger.warning("Unknown WhatsApp button_id: %s", button_id)


def handle_whatsapp_list_reply(
    phone: str,
    list_id: str,
    list_title: str,
    display_name: str = "",
) -> None:
    """טיפול בבחירה מרשימה בוואטסאפ."""
    user_id = _wa_user_id(phone)
    if not display_name:
        display_name = phone

    # live chat guard
    if LiveChatService.is_active(user_id):
        db.save_message(user_id, display_name, "user", list_title)
        db.touch_live_chat(user_id)
        return

    # rate limit
    limit_msg = check_rate_limit(user_id)
    if limit_msg is not None:
        send_text_message(phone, html_to_whatsapp(limit_msg))
        return
    record_message(user_id)

    if list_id == "list_location":
        db.save_message(user_id, display_name, "user", "📍 מיקום")
        _handle_rag_query_wa(
            phone, user_id, display_name,
            user_message="📍 מיקום",
            query="מה הכתובת והמיקום של העסק? איך מגיעים?",
            handoff_reason="הלקוח ביקש מיקום (וואטסאפ).",
        )

    elif list_id == "list_hours":
        db.save_message(user_id, display_name, "user", "🕐 שעות פתיחה")
        status = is_currently_open()
        schedule = get_weekly_schedule_text()
        response = f"{status['message']}\n\n{schedule}"
        db.save_message(user_id, display_name, "assistant", response)
        _send_with_menu(phone, response)

    else:
        logger.warning("Unknown WhatsApp list_id: %s", list_id)


# ── RAG Pipeline (וואטסאפ) ─────────────────────────────────────────────────

def _handle_rag_query_wa(
    phone: str,
    user_id: str,
    display_name: str,
    user_message: str,
    query: str,
    handoff_reason: str,
) -> None:
    """הרצת צינור RAG + LLM ושליחת התוצאה דרך וואטסאפ."""
    history = db.get_conversation_history(user_id, limit=CONTEXT_WINDOW_SIZE)
    db.save_message(user_id, display_name, "user", user_message)

    result = generate_answer(
        user_query=query,
        conversation_history=history,
        user_id=user_id,
        username=display_name,
    )

    stripped = strip_source_citation(result["answer"])
    if _should_handoff_to_human(stripped):
        _request_agent(phone, user_id, display_name, handoff_reason)
        return

    db.save_message(user_id, display_name, "assistant", result["answer"], ", ".join(result["sources"]))
    wa_text = html_to_whatsapp(stripped)
    _send_with_menu(phone, wa_text)

    # שאלות המשך — כפתורים (מקסימום 3)
    follow_up_qs = result.get("follow_up_questions", [])
    if FOLLOW_UP_ENABLED and follow_up_qs:
        # בוואטסאפ אין bot_data — שולחים את השאלות כטקסט ישיר
        fu_text = "💡 *אולי תרצו גם לשאול:*\n"
        for i, q in enumerate(follow_up_qs[:3], 1):
            fu_text += f"{i}. {q}\n"
        send_text_message(phone, fu_text)

    # סיכום שיחה ברקע
    try:
        maybe_summarize(user_id)
    except Exception as e:
        logger.error("WhatsApp summarization failed for user %s: %s", user_id, e)


def _should_handoff_to_human(text: str) -> bool:
    """זיהוי תשובות LLM שמעידות על חוסר מידע ודורשות העברה לנציג."""
    if not text:
        return False
    t = text.strip()
    if t == FALLBACK_RESPONSE.strip():
        return True
    if "תנו לי להעביר" in t and "נציג אנושי" in t:
        return True
    return False


# ── Booking Flow ────────────────────────────────────────────────────────────

def _start_booking(phone: str, user_id: str, display_name: str) -> None:
    """התחלת תהליך קביעת תור בוואטסאפ."""
    # שליפת שירותים דרך RAG
    result = generate_answer("אילו שירותים אתם מציעים? פרטו בקצרה.")
    stripped = strip_source_citation(result["answer"])

    if _should_handoff_to_human(stripped):
        _request_agent(phone, user_id, display_name, "הלקוח ביקש לקבוע תור — אין מידע על שירותים.")
        return

    wa_text = html_to_whatsapp(stripped)
    text = f"📅 *בקשת תור*\n\n{wa_text}\n\nאנא כתבו את *השירות* שתרצו להזמין (או כתבו 'ביטול' לחזרה):"
    send_text_message(phone, text)

    _set_booking_state(user_id, _BOOKING_STATES["service"], {})
    db.save_message(user_id, display_name, "assistant", "[התחלת תהליך קביעת תור]")


def _handle_booking_step(
    phone: str, user_id: str, display_name: str, text: str, booking: dict
) -> None:
    """טיפול בשלב נוכחי ב-booking flow."""
    state = booking["state"]
    data = json.loads(booking.get("data_json") or "{}")

    # ביטול בכל שלב
    if text.strip() in ("ביטול", "cancel", "/cancel"):
        _clear_booking_state(user_id)
        _send_main_menu(phone, "תהליך בקשת התור בוטל. איך עוד אפשר לעזור לכם?")
        return

    if state == _BOOKING_STATES["service"]:
        data["service"] = text
        _set_booking_state(user_id, _BOOKING_STATES["date"], data)
        send_text_message(
            phone,
            "📆 מעולה! באיזה *תאריך* תעדיפו?\n"
            "(לדוגמה, 'יום שני', '15 במרץ', 'מחר')\n\n"
            "כתבו 'ביטול' לחזרה."
        )

    elif state == _BOOKING_STATES["date"]:
        data["date"] = text
        _set_booking_state(user_id, _BOOKING_STATES["time"], data)
        send_text_message(
            phone,
            "🕐 איזו *שעה* מתאימה לכם?\n"
            "(לדוגמה, '10:00', 'אחר הצהריים', '14:00')\n\n"
            "כתבו 'ביטול' לחזרה."
        )

    elif state == _BOOKING_STATES["time"]:
        data["time"] = text
        _set_booking_state(user_id, _BOOKING_STATES["confirm"], data)

        confirm_text = (
            f"📋 *סיכום בקשת התור:*\n\n"
            f"• שירות: {data['service']}\n"
            f"• תאריך: {data['date']}\n"
            f"• שעה: {data['time']}\n\n"
            f"אנא אשרו:"
        )
        confirm_buttons = [
            {"id": "booking_confirm_yes", "title": "✅ אישור"},
            {"id": "booking_confirm_no", "title": "❌ ביטול"},
        ]
        send_buttons_message(phone, confirm_text, confirm_buttons)

    elif state == _BOOKING_STATES["confirm"]:
        # תשובת טקסט לאישור (כפתורים מטופלים ב-handle_whatsapp_button_reply)
        answer = text.strip().lower()
        if answer in ("כן", "yes", "y", "אישור", "confirm"):
            _complete_booking(phone, user_id, display_name, data)
        else:
            _clear_booking_state(user_id)
            _send_main_menu(phone, "❌ בקשת התור בוטלה. אין בעיה!\nאתם מוזמנים לבקש תור חדש בכל עת.")


def _handle_booking_button(
    phone: str, user_id: str, display_name: str, button_id: str, button_title: str
) -> None:
    """טיפול בלחיצת כפתור בתהליך booking."""
    if button_id == "booking_confirm_yes":
        booking = _get_booking_state(user_id)
        if booking:
            data = json.loads(booking.get("data_json") or "{}")
            _complete_booking(phone, user_id, display_name, data)
        else:
            _send_main_menu(phone, "תהליך בקשת התור לא נמצא. אנא התחילו מחדש.")
    elif button_id == "booking_confirm_no":
        _clear_booking_state(user_id)
        _send_main_menu(phone, "❌ בקשת התור בוטלה. אתם מוזמנים לבקש תור חדש בכל עת.")


def _complete_booking(
    phone: str, user_id: str, display_name: str, data: dict
) -> None:
    """השלמת תהליך booking — שמירה ב-DB והתראה לבעל העסק."""
    service = data.get("service", "")
    date = data.get("date", "")
    preferred_time = data.get("time", "")

    appt_id = db.create_appointment(
        user_id=user_id,
        username=display_name,
        service=service,
        preferred_date=date,
        preferred_time=preferred_time,
        telegram_username="",
        platform="whatsapp",
    )

    notification = (
        f"📅 בקשת תור חדשה לאישור #{appt_id}\n\n"
        f"לקוח: {display_name}\n"
        f"ערוץ: וואטסאפ ({phone})\n"
        f"שירות: {service}\n"
        f"תאריך: {date}\n"
        f"שעה: {preferred_time}\n"
    )
    _notify_owner_wa(notification)

    db.save_message(
        user_id, display_name, "assistant",
        f"בקשת תור: {service} בתאריך {date} בשעה {preferred_time}",
    )

    response = (
        f"📋 בקשת התור התקבלה!\n\n"
        f"• שירות: {service}\n"
        f"• תאריך: {date}\n"
        f"• שעה: {preferred_time}\n\n"
        f"העברנו את הפרטים לבית העסק. "
        f"ניצור איתכם קשר בהקדם לאישור סופי של השעה."
    )
    _send_main_menu(phone, response)
    _clear_booking_state(user_id)


# ── Agent Request ──────────────────────────────────────────────────────────

def _request_agent(
    phone: str, user_id: str, display_name: str, message: str
) -> None:
    """יצירת בקשת נציג ושליחת התראה לבעל העסק."""
    request_id = db.create_agent_request(
        user_id,
        display_name,
        message=message,
        telegram_username="",
        platform="whatsapp",
    )

    notification = (
        f"🔔 בקשת נציג #{request_id}\n\n"
        f"לקוח: {display_name}\n"
        f"ערוץ: וואטסאפ ({phone})\n"
        f"זמן: עכשיו\n\n"
        f"{message}"
    )
    _notify_owner_wa(notification)

    response = (
        "👤 הודעתי לצוות שלנו שאתם מעוניינים לדבר עם מישהו.\n\n"
        "נציג אנושי יחזור אליכם בקרוב. "
        "בינתיים, אתם מוזמנים לשאול אותי כל שאלה נוספת!"
    )
    db.save_message(user_id, display_name, "assistant", response)
    _send_main_menu(phone, response)


# ── Cancellation Confirmation ──────────────────────────────────────────────

def _handle_cancel_confirm(
    phone: str, user_id: str, display_name: str, confirmed: bool
) -> None:
    """טיפול באישור/דחיית ביטול תור."""
    if confirmed:
        request_id = db.create_agent_request(
            user_id,
            display_name,
            message="הלקוח אישר ביטול תור.",
            telegram_username="",
            platform="whatsapp",
        )
        notification = (
            f"🔔 בקשת ביטול תור #{request_id}\n\n"
            f"לקוח: {display_name}\n"
            f"ערוץ: וואטסאפ ({phone})\n"
        )
        _notify_owner_wa(notification)

        response = (
            "קיבלתי את בקשתכם לביטול התור. ✅\n\n"
            "העברתי את הבקשה לצוות שלנו — נציג יחזור אליכם בקרוב לאשר את הביטול."
        )
    else:
        response = "בסדר גמור, התור נשאר! 👍\nאיך עוד אפשר לעזור?"

    db.save_message(user_id, display_name, "assistant", response)
    _send_main_menu(phone, response)
