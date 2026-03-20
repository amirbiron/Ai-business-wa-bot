"""
טסטים ל-WhatsApp webhook endpoint — אימות ופירוק payload.
"""

import hashlib
import hmac
import json
from unittest.mock import patch, MagicMock, ANY
import pytest

# ייבוא המודול ברמת הקובץ — מאפשר ל-patch למצוא אותו
import bot.whatsapp_webhook as ww


@pytest.fixture(autouse=True)
def _mock_wa_config(monkeypatch):
    """הגדרת credentials מדומים."""
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "fake-wa-token")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "test-verify-token")


@pytest.fixture
def app():
    """יצירת Flask app עם WhatsApp webhook blueprint."""
    from flask import Flask
    app = Flask(__name__)
    app.config["TESTING"] = True

    with patch.object(ww, "WHATSAPP_VERIFY_TOKEN", "test-verify-token"):
        app.register_blueprint(ww.whatsapp_bp)

    return app


@pytest.fixture
def client(app):
    return app.test_client()


class TestWebhookVerification:
    """בדיקות אימות Meta webhook (GET)."""

    def test_verify_success(self, client):
        with patch.object(ww, "WHATSAPP_VERIFY_TOKEN", "test-verify-token"):
            resp = client.get(
                "/webhook/whatsapp",
                query_string={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "test-verify-token",
                    "hub.challenge": "CHALLENGE_STRING",
                },
            )
            assert resp.status_code == 200
            assert resp.data.decode() == "CHALLENGE_STRING"

    def test_verify_wrong_token(self, client):
        with patch.object(ww, "WHATSAPP_VERIFY_TOKEN", "test-verify-token"):
            resp = client.get(
                "/webhook/whatsapp",
                query_string={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "wrong-token",
                    "hub.challenge": "CHALLENGE_STRING",
                },
            )
            assert resp.status_code == 403

    def test_verify_missing_params(self, client):
        resp = client.get("/webhook/whatsapp")
        assert resp.status_code == 403

    def test_verify_empty_token_rejected(self, client):
        """אם WHATSAPP_VERIFY_TOKEN ריק — לא מאשרים webhook גם עם token ריק."""
        with patch.object(ww, "WHATSAPP_VERIFY_TOKEN", ""):
            resp = client.get(
                "/webhook/whatsapp",
                query_string={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "",
                    "hub.challenge": "HACK",
                },
            )
            assert resp.status_code == 403


class TestWebhookReceive:
    """בדיקות קבלת הודעות (POST)."""

    def test_empty_body_returns_400(self, client):
        resp = client.post("/webhook/whatsapp")
        assert resp.status_code == 400

    def test_text_message_dispatched(self, client):
        """POST מחזיר 200 ומעביר עיבוד ל-thread ברקע."""
        with patch.object(ww, "_process_webhook_safe") as mock_safe, \
             patch.object(ww.threading, "Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            payload = _build_text_payload("972501234567", "שלום")
            resp = client.post(
                "/webhook/whatsapp",
                data=json.dumps(payload),
                content_type="application/json",
            )
            assert resp.status_code == 200
            mock_thread.assert_called_once()
            mock_thread.return_value.start.assert_called_once()

    def test_always_returns_200(self, client):
        """Meta דורש 200 — תמיד, כי העיבוד קורה ב-thread נפרד."""
        with patch.object(ww.threading, "Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            payload = _build_text_payload("972501234567", "שלום")
            resp = client.post(
                "/webhook/whatsapp",
                data=json.dumps(payload),
                content_type="application/json",
            )
            assert resp.status_code == 200

    def test_process_text_message(self):
        """בדיקת פירוק payload עם הודעת טקסט."""
        # ייבוא המודול כדי שנוכל לעשות patch.object
        import bot.whatsapp_handlers as wh
        mock_msg = MagicMock()
        with patch.object(wh, "handle_whatsapp_message", mock_msg):
            payload = _build_text_payload("972501234567", "מה שעות הפתיחה?")
            ww._process_webhook_payload(payload)
            mock_msg.assert_called_once_with(
                phone="972501234567",
                text="מה שעות הפתיחה?",
                message_id="wamid.test123",
                display_name="Test User",
            )

    def test_process_webhook_safe_catches_errors(self):
        """_process_webhook_safe לוכדת שגיאות ורושמת ללוג."""
        with patch.object(ww, "_process_webhook_payload", side_effect=RuntimeError("boom")):
            # לא צריך לזרוק — השגיאה נתפסת ונרשמת ללוג
            ww._process_webhook_safe({"entry": []})

    def test_process_status_ignored(self):
        """סטטוסי הודעות (delivered/read) מדולגים."""
        import bot.whatsapp_handlers as wh
        mock_msg = MagicMock()
        with patch.object(wh, "handle_whatsapp_message", mock_msg):
            payload = {
                "entry": [{
                    "changes": [{
                        "value": {
                            "statuses": [{"id": "wamid.xxx", "status": "delivered"}]
                        }
                    }]
                }]
            }
            ww._process_webhook_payload(payload)
            mock_msg.assert_not_called()


class TestSignatureVerification:
    """בדיקות אימות חתימת HMAC מ-Meta."""

    def test_valid_signature_passes(self, client):
        """כש-WHATSAPP_APP_SECRET מוגדר וחתימה תואמת — מעבד את ההודעה."""
        secret = "test-app-secret"
        payload = json.dumps(_build_text_payload("972501234567", "שלום")).encode()
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        with patch.object(ww, "WHATSAPP_APP_SECRET", secret), \
             patch.object(ww.threading, "Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            resp = client.post(
                "/webhook/whatsapp",
                data=payload,
                content_type="application/json",
                headers={"X-Hub-Signature-256": sig},
            )
            assert resp.status_code == 200
            mock_thread.return_value.start.assert_called_once()

    def test_invalid_signature_rejected(self, client):
        """חתימה שגויה — מחזיר 403."""
        secret = "test-app-secret"
        payload = json.dumps(_build_text_payload("972501234567", "שלום")).encode()

        with patch.object(ww, "WHATSAPP_APP_SECRET", secret):
            resp = client.post(
                "/webhook/whatsapp",
                data=payload,
                content_type="application/json",
                headers={"X-Hub-Signature-256": "sha256=bad_signature"},
            )
            assert resp.status_code == 403

    def test_missing_signature_rejected(self, client):
        """ללא header חתימה כש-secret מוגדר — מחזיר 403."""
        with patch.object(ww, "WHATSAPP_APP_SECRET", "test-app-secret"):
            payload = json.dumps(_build_text_payload("972501234567", "שלום")).encode()
            resp = client.post(
                "/webhook/whatsapp",
                data=payload,
                content_type="application/json",
            )
            assert resp.status_code == 403

    def test_no_secret_skips_verification(self, client):
        """כש-WHATSAPP_APP_SECRET ריק — לא בודק חתימה (תאימות לאחור)."""
        with patch.object(ww, "WHATSAPP_APP_SECRET", ""), \
             patch.object(ww.threading, "Thread") as mock_thread:
            mock_thread.return_value = MagicMock()
            payload = json.dumps(_build_text_payload("972501234567", "שלום")).encode()
            resp = client.post(
                "/webhook/whatsapp",
                data=payload,
                content_type="application/json",
            )
            assert resp.status_code == 200


# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_text_payload(phone: str, text: str) -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "contacts": [{"profile": {"name": "Test User"}}],
                    "messages": [{
                        "from": phone,
                        "id": "wamid.test123",
                        "type": "text",
                        "text": {"body": text},
                    }],
                }
            }]
        }]
    }


def _build_button_payload(phone: str, button_id: str, title: str) -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {
                    "contacts": [{"profile": {"name": "Test User"}}],
                    "messages": [{
                        "from": phone,
                        "id": "wamid.test456",
                        "type": "interactive",
                        "interactive": {
                            "type": "button_reply",
                            "button_reply": {"id": button_id, "title": title},
                        },
                    }],
                }
            }]
        }]
    }
