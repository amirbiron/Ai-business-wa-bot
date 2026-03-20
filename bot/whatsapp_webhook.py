"""
WhatsApp Webhook — Flask Blueprint לקבלת הודעות מ-Meta Cloud API.

Meta שולח הודעות נכנסות ל-POST /webhook/whatsapp.
אימות Webhook נעשה דרך GET /webhook/whatsapp עם verify_token.
עיבוד ההודעות מתבצע ב-thread נפרד כדי להחזיר 200 מיד ל-Meta.
"""

import hashlib
import hmac
import logging
import threading

from flask import Blueprint, request, jsonify

from ai_chatbot.config import WHATSAPP_VERIFY_TOKEN, WHATSAPP_APP_SECRET

logger = logging.getLogger(__name__)

whatsapp_bp = Blueprint("whatsapp", __name__)


@whatsapp_bp.route("/webhook/whatsapp", methods=["GET"])
def verify_webhook():
    """Meta Webhook Verification — מחזיר את ה-challenge אם ה-token תואם."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and WHATSAPP_VERIFY_TOKEN and token == WHATSAPP_VERIFY_TOKEN:
        logger.info("WhatsApp webhook verified successfully")
        return challenge, 200

    logger.warning("WhatsApp webhook verification failed: mode=%s", mode)
    return "Forbidden", 403


@whatsapp_bp.route("/webhook/whatsapp", methods=["POST"])
def receive_message():
    """קבלת הודעות נכנסות מ-WhatsApp Cloud API.

    מחזיר 200 מיד ומעביר את העיבוד ל-thread ברקע.
    Meta דורש תשובה תוך ~20 שניות — בלי thread, ה-RAG+LLM pipeline
    יכול לחרוג מהזמן ולגרום ל-retry ועיבוד כפול.
    """
    # אימות חתימת HMAC מ-Meta (אם App Secret מוגדר)
    if WHATSAPP_APP_SECRET:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_signature(request.get_data(), signature):
            logger.warning("WhatsApp webhook signature verification failed")
            return "Forbidden", 403

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "no data"}), 400

    # עיבוד ב-thread נפרד — מחזירים 200 מיד ל-Meta כדי להימנע מ-retry
    # (ה-RAG+LLM pipeline יכול לקחת 5-15 שניות, וה-timeout של Meta הוא ~20 שניות)
    thread = threading.Thread(
        target=_process_webhook_safe,
        args=(data,),
        daemon=True,
    )
    thread.start()

    return jsonify({"status": "ok"}), 200


def _verify_signature(payload: bytes, signature_header: str) -> bool:
    """אימות חתימת HMAC-SHA256 מ-Meta על ה-payload.

    Meta שולח את החתימה ב-header X-Hub-Signature-256 בפורמט sha256=<hex>.
    """
    if not signature_header.startswith("sha256="):
        return False
    expected_sig = signature_header[7:]  # הסרת prefix "sha256="
    computed = hmac.new(
        WHATSAPP_APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, expected_sig)


def _process_webhook_safe(data: dict) -> None:
    """עטיפת _process_webhook_payload עם try/except — לשימוש ב-thread."""
    try:
        _process_webhook_payload(data)
    except Exception as e:
        logger.error("WhatsApp webhook processing error: %s", e, exc_info=True)


def _process_webhook_payload(data: dict) -> None:
    """פירוק payload של Meta ושליחה ל-handler המתאים."""
    # ייבוא מאוחר — מונע circular imports
    from ai_chatbot.bot.whatsapp_handlers import (
        handle_whatsapp_message,
        handle_whatsapp_button_reply,
        handle_whatsapp_list_reply,
    )

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            # סטטוסי הודעות (delivered, read) — מתעלמים
            if "statuses" in value:
                continue

            messages = value.get("messages", [])
            contacts = value.get("contacts", [])

            for msg in messages:
                phone = msg.get("from", "")
                msg_id = msg.get("id", "")
                msg_type = msg.get("type", "")

                # שם הלקוח מ-contacts (אם זמין)
                display_name = ""
                if contacts:
                    profile = contacts[0].get("profile", {})
                    display_name = profile.get("name", "")

                if msg_type == "text":
                    text = msg.get("text", {}).get("body", "")
                    if text:
                        handle_whatsapp_message(
                            phone=phone,
                            text=text,
                            message_id=msg_id,
                            display_name=display_name,
                        )

                elif msg_type == "interactive":
                    interactive = msg.get("interactive", {})
                    interactive_type = interactive.get("type", "")

                    if interactive_type == "button_reply":
                        reply = interactive.get("button_reply", {})
                        handle_whatsapp_button_reply(
                            phone=phone,
                            button_id=reply.get("id", ""),
                            button_title=reply.get("title", ""),
                            display_name=display_name,
                        )
                    elif interactive_type == "list_reply":
                        reply = interactive.get("list_reply", {})
                        handle_whatsapp_list_reply(
                            phone=phone,
                            list_id=reply.get("id", ""),
                            list_title=reply.get("title", ""),
                            display_name=display_name,
                        )

                else:
                    # סוגי הודעות לא נתמכים (תמונות, קול, וידאו וכו')
                    logger.info(
                        "Unsupported WhatsApp message type '%s' from %s",
                        msg_type, phone,
                    )
                    from ai_chatbot.bot.whatsapp_api import send_text_message
                    send_text_message(
                        phone,
                        "מצטערים, כרגע אנחנו תומכים רק בהודעות טקסט. "
                        "אנא כתבו את השאלה שלכם בטקסט. 📝"
                    )
