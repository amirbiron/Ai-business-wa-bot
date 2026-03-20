"""
טסטים ל-WhatsApp Cloud API client — שליחת הודעות.
"""

from unittest.mock import patch, MagicMock
import pytest


@pytest.fixture(autouse=True)
def _patch_wa_credentials():
    """Patch credentials ישירות ברמת המודול — env vars מגיעים מאוחר מדי."""
    with patch("bot.whatsapp_api.WHATSAPP_ACCESS_TOKEN", "fake-wa-token"), \
         patch("bot.whatsapp_api.WHATSAPP_PHONE_NUMBER_ID", "123456"):
        yield


class TestSendTextMessage:
    """בדיקות שליחת הודעת טקסט."""

    @patch("bot.whatsapp_api.http_requests.post")
    def test_send_text_success(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        from bot.whatsapp_api import send_text_message

        result = send_text_message("972501234567", "שלום!")
        assert result is True
        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["to"] == "972501234567"
        assert payload["type"] == "text"
        assert payload["text"]["body"] == "שלום!"

    @patch("bot.whatsapp_api.http_requests.post")
    def test_send_text_api_error(self, mock_post):
        mock_post.return_value = MagicMock(ok=False, status_code=401, text="Unauthorized")
        from bot.whatsapp_api import send_text_message

        result = send_text_message("972501234567", "שלום!")
        assert result is False

    @patch("bot.whatsapp_api.http_requests.post")
    def test_send_text_network_error(self, mock_post):
        mock_post.side_effect = ConnectionError("timeout")
        from bot.whatsapp_api import send_text_message

        result = send_text_message("972501234567", "שלום!")
        assert result is False


class TestSendButtonsMessage:
    """בדיקות שליחת הודעה עם כפתורים."""

    @patch("bot.whatsapp_api.http_requests.post")
    def test_send_buttons_max_3(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        from bot.whatsapp_api import send_buttons_message

        buttons = [
            {"id": "b1", "title": "כפתור 1"},
            {"id": "b2", "title": "כפתור 2"},
            {"id": "b3", "title": "כפתור 3"},
            {"id": "b4", "title": "כפתור 4"},  # צריך להיחתך
        ]
        result = send_buttons_message("972501234567", "בחר:", buttons)
        assert result is True

        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        wa_buttons = payload["interactive"]["action"]["buttons"]
        # מקסימום 3 כפתורים
        assert len(wa_buttons) == 3

    @patch("bot.whatsapp_api.http_requests.post")
    def test_send_buttons_title_truncated(self, mock_post):
        """כותרת כפתור מוגבלת ל-20 תווים."""
        mock_post.return_value = MagicMock(ok=True)
        from bot.whatsapp_api import send_buttons_message

        buttons = [{"id": "b1", "title": "כפתור עם כותרת ארוכה מאוד מאוד"}]
        send_buttons_message("972501234567", "בחר:", buttons)

        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        title = payload["interactive"]["action"]["buttons"][0]["reply"]["title"]
        assert len(title) <= 20


class TestMarkAsRead:
    """בדיקות סימון הודעה כנקראה."""

    @patch("bot.whatsapp_api.http_requests.post")
    def test_mark_as_read(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        from bot.whatsapp_api import mark_as_read

        result = mark_as_read("wamid.12345")
        assert result is True
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["status"] == "read"
        assert payload["message_id"] == "wamid.12345"


class TestMissingCredentials:
    """בדיקות עם credentials חסרים."""

    def test_send_without_token(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "")
        # צריך לרענן את המודול כדי שהמשתנה ייקרא מחדש
        with patch("bot.whatsapp_api.WHATSAPP_ACCESS_TOKEN", ""):
            from bot.whatsapp_api import send_text_message
            result = send_text_message("972501234567", "test")
            assert result is False
