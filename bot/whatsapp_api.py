"""
WhatsApp Cloud API Client — שליחת הודעות דרך Meta Graph API.

משתמש ב-WhatsApp Business Cloud API (v21.0) לשליחת:
- הודעות טקסט
- הודעות עם כפתורים (מקסימום 3)
- הודעות רשימה (מקסימום 10 פריטים)
- הודעות מיקום
- סימון הודעה כנקראה
"""

import logging
from typing import Optional

import requests as http_requests

from ai_chatbot.config import WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN

logger = logging.getLogger(__name__)

_API_VERSION = "v21.0"
_BASE_URL = f"https://graph.facebook.com/{_API_VERSION}"


def _get_messages_url() -> str:
    return f"{_BASE_URL}/{WHATSAPP_PHONE_NUMBER_ID}/messages"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def _send_request(payload: dict) -> bool:
    """שליחת בקשה ל-WhatsApp API עם טיפול בשגיאות."""
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        logger.error("WhatsApp API credentials not configured")
        return False
    try:
        resp = http_requests.post(
            _get_messages_url(),
            headers=_headers(),
            json=payload,
            timeout=15,
        )
        if not resp.ok:
            logger.error(
                "WhatsApp API error %d: %s", resp.status_code, resp.text
            )
            return False
        return True
    except Exception as e:
        logger.error("WhatsApp API request failed: %s", e)
        return False


def send_text_message(to: str, text: str) -> bool:
    """שליחת הודעת טקסט פשוטה."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    return _send_request(payload)


def send_buttons_message(
    to: str, body: str, buttons: list[dict[str, str]]
) -> bool:
    """שליחת הודעה עם כפתורי quick reply (מקסימום 3).

    buttons: [{"id": "btn_1", "title": "טקסט הכפתור"}, ...]
    """
    if len(buttons) > 3:
        logger.warning("WhatsApp supports max 3 buttons, truncating %d → 3", len(buttons))
        buttons = buttons[:3]

    wa_buttons = [
        {"type": "reply", "reply": {"id": btn["id"], "title": btn["title"][:20]}}
        for btn in buttons
    ]

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": wa_buttons},
        },
    }
    return _send_request(payload)


def send_list_message(
    to: str,
    body: str,
    button_text: str,
    sections: list[dict],
) -> bool:
    """שליחת הודעת רשימה (מקסימום 10 פריטים, 1 סקשן).

    sections: [{"title": "כותרת", "rows": [{"id": "row_1", "title": "...", "description": "..."}]}]
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_text[:20],
                "sections": sections,
            },
        },
    }
    return _send_request(payload)


def send_location_message(
    to: str,
    latitude: float,
    longitude: float,
    name: str = "",
    address: str = "",
) -> bool:
    """שליחת הודעת מיקום."""
    location = {"latitude": latitude, "longitude": longitude}
    if name:
        location["name"] = name
    if address:
        location["address"] = address

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "location",
        "location": location,
    }
    return _send_request(payload)


def send_template_message(
    to: str,
    template_name: str,
    language_code: str = "he",
    components: Optional[list[dict]] = None,
) -> bool:
    """שליחת הודעת תבנית (template) — נדרש להודעות יזומות (broadcast).

    תבניות חייבות להיות מאושרות מראש ע"י Meta.
    """
    template = {
        "name": template_name,
        "language": {"code": language_code},
    }
    if components:
        template["components"] = components

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": template,
    }
    return _send_request(payload)


def mark_as_read(message_id: str) -> bool:
    """סימון הודעה כנקראה (V סימון כחול)."""
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    return _send_request(payload)
