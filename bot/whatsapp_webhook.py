"""
WhatsApp Webhook — Flask Blueprint לקבלת הודעות מ-Meta Cloud API.

Meta שולח הודעות נכנסות ל-POST /webhook/whatsapp.
אימות Webhook נעשה דרך GET /webhook/whatsapp עם verify_token.
"""

import logging

from flask import Blueprint, request, jsonify

from ai_chatbot.config import WHATSAPP_VERIFY_TOKEN

logger = logging.getLogger(__name__)

whatsapp_bp = Blueprint("whatsapp", __name__)


@whatsapp_bp.route("/webhook/whatsapp", methods=["GET"])
def verify_webhook():
    """Meta Webhook Verification — מחזיר את ה-challenge אם ה-token תואם."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        logger.info("WhatsApp webhook verified successfully")
        return challenge, 200

    logger.warning("WhatsApp webhook verification failed: mode=%s", mode)
    return "Forbidden", 403


@whatsapp_bp.route("/webhook/whatsapp", methods=["POST"])
def receive_message():
    """קבלת הודעות נכנסות מ-WhatsApp Cloud API.

    מחזיר 200 מיד (Meta דורש תשובה תוך 20 שניות) ומעבד ברקע.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "no data"}), 400

    try:
        _process_webhook_payload(data)
    except Exception as e:
        # לוגים בלבד — תמיד מחזירים 200 ל-Meta כדי לא לגרום ל-retry storm
        logger.error("WhatsApp webhook processing error: %s", e, exc_info=True)

    return jsonify({"status": "ok"}), 200


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
