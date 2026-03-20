"""
טסטים ל-WhatsApp handlers — לוגיקת טיפול בהודעות.

מוקים: DB, LLM, intent, rate_limiter, live_chat, WhatsApp API.
ייבוא המודול בתוך כל טסט — כמו test_handlers.py — כי יש תלות ב-numpy/openai.
"""

from unittest.mock import patch, MagicMock
import pytest


class TestHtmlToWhatsapp:
    """בדיקות המרת HTML ל-WhatsApp markdown."""

    def test_bold_conversion(self):
        from bot.whatsapp_handlers import html_to_whatsapp
        assert html_to_whatsapp("<b>מודגש</b>") == "*מודגש*"

    def test_italic_conversion(self):
        from bot.whatsapp_handlers import html_to_whatsapp
        assert html_to_whatsapp("<i>נטוי</i>") == "_נטוי_"

    def test_underline_removed(self):
        from bot.whatsapp_handlers import html_to_whatsapp
        assert html_to_whatsapp("<u>קו תחתון</u>") == "קו תחתון"

    def test_mixed_tags(self):
        from bot.whatsapp_handlers import html_to_whatsapp
        result = html_to_whatsapp("<b>כותרת</b> - <i>הערה</i>")
        assert result == "*כותרת* - _הערה_"

    def test_unknown_tags_removed(self):
        from bot.whatsapp_handlers import html_to_whatsapp
        assert html_to_whatsapp("<div>טקסט</div>") == "טקסט"

    def test_plain_text_unchanged(self):
        from bot.whatsapp_handlers import html_to_whatsapp
        assert html_to_whatsapp("שלום עולם") == "שלום עולם"

    def test_nested_tags(self):
        from bot.whatsapp_handlers import html_to_whatsapp
        result = html_to_whatsapp("<b>מודגש <i>ונטוי</i></b>")
        assert result == "*מודגש _ונטוי_*"


class TestWaUserId:
    """בדיקות מזהה משתמש וואטסאפ."""

    def test_wa_user_id(self):
        from bot.whatsapp_handlers import _wa_user_id
        assert _wa_user_id("972501234567") == "wa_972501234567"

    def test_is_wa_user(self):
        from bot.whatsapp_handlers import _is_wa_user
        assert _is_wa_user("wa_972501234567") is True
        assert _is_wa_user("123456") is False

    def test_phone_from_user_id(self):
        from bot.whatsapp_handlers import _phone_from_user_id
        assert _phone_from_user_id("wa_972501234567") == "972501234567"


class TestHandleWhatsappMessage:
    """בדיקות handler ראשי."""

    def test_greeting_intent(self):
        import bot.whatsapp_handlers as wh
        from intent import Intent

        with (
            patch.object(wh, "mark_as_read"),
            patch.object(wh, "send_text_message"),
            patch.object(wh, "send_buttons_message") as mock_send_btn,
            patch.object(wh, "db") as mock_db,
            patch.object(wh, "LiveChatService") as mock_lcs,
            patch.object(wh, "detect_intent") as mock_detect,
            patch.object(wh, "get_direct_response") as mock_direct,
            patch.object(wh, "check_rate_limit", return_value=None),
            patch.object(wh, "record_message"),
        ):
            mock_lcs.is_active.return_value = False
            mock_detect.return_value = Intent.GREETING
            mock_direct.return_value = "שלום! ברוכים הבאים"
            mock_db.get_wa_booking_state.return_value = None

            wh.handle_whatsapp_message("972501234567", "שלום", "wamid.1", "Test")

            mock_detect.assert_called_once_with("שלום")
            mock_send_btn.assert_called_once()

    def test_rate_limited(self):
        import bot.whatsapp_handlers as wh

        with (
            patch.object(wh, "mark_as_read"),
            patch.object(wh, "send_text_message") as mock_send_txt,
            patch.object(wh, "send_buttons_message"),
            patch.object(wh, "db") as mock_db,
            patch.object(wh, "LiveChatService") as mock_lcs,
            patch.object(wh, "check_rate_limit") as mock_rate,
            patch.object(wh, "record_message") as mock_record,
        ):
            mock_lcs.is_active.return_value = False
            mock_rate.return_value = "הגעתם למגבלת ההודעות."
            mock_db.get_wa_booking_state.return_value = None

            wh.handle_whatsapp_message("972501234567", "שלום", "wamid.2", "Test")

            mock_send_txt.assert_called_once()
            assert "הגעתם למגבלת" in mock_send_txt.call_args[0][1]
            mock_record.assert_not_called()

    def test_live_chat_active(self):
        """כש-live chat פעיל — שומרים את ההודעה ויוצאים ללא תשובה."""
        import bot.whatsapp_handlers as wh

        with (
            patch.object(wh, "mark_as_read"),
            patch.object(wh, "send_text_message") as mock_send_txt,
            patch.object(wh, "send_buttons_message") as mock_send_btn,
            patch.object(wh, "db") as mock_db,
            patch.object(wh, "LiveChatService") as mock_lcs,
        ):
            mock_lcs.is_active.return_value = True

            wh.handle_whatsapp_message("972501234567", "שלום", "wamid.3", "Test")

            mock_db.save_message.assert_called_once()
            mock_db.touch_live_chat.assert_called_once()
            mock_send_txt.assert_not_called()
            mock_send_btn.assert_not_called()


class TestHandleButtonReply:
    """בדיקות לחיצת כפתור."""

    def test_price_list_button(self):
        import bot.whatsapp_handlers as wh

        with (
            patch.object(wh, "send_text_message"),
            patch.object(wh, "send_buttons_message"),
            patch.object(wh, "db"),
            patch.object(wh, "LiveChatService") as mock_lcs,
            patch.object(wh, "_handle_rag_query_wa") as mock_rag,
            patch.object(wh, "check_rate_limit", return_value=None),
            patch.object(wh, "record_message"),
        ):
            mock_lcs.is_active.return_value = False

            wh.handle_whatsapp_button_reply("972501234567", "btn_price_list", "📋 מחירון", "Test")

            mock_rag.assert_called_once()

    def test_agent_button(self):
        import bot.whatsapp_handlers as wh

        with (
            patch.object(wh, "send_text_message"),
            patch.object(wh, "send_buttons_message"),
            patch.object(wh, "db"),
            patch.object(wh, "LiveChatService") as mock_lcs,
            patch.object(wh, "_request_agent") as mock_agent,
            patch.object(wh, "VacationService") as mock_vacation,
            patch.object(wh, "check_rate_limit", return_value=None),
            patch.object(wh, "record_message"),
        ):
            mock_lcs.is_active.return_value = False
            mock_vacation.is_active.return_value = False

            wh.handle_whatsapp_button_reply("972501234567", "btn_agent", "👤 נציג", "Test")

            mock_agent.assert_called_once()

    def test_booking_confirm_yes_routes_to_handler(self):
        """כפתור booking_confirm_yes מגיע ל-_handle_booking_button ולא נופל ל-else."""
        import bot.whatsapp_handlers as wh

        with (
            patch.object(wh, "send_text_message"),
            patch.object(wh, "send_buttons_message"),
            patch.object(wh, "db"),
            patch.object(wh, "LiveChatService") as mock_lcs,
            patch.object(wh, "_handle_booking_button") as mock_booking_btn,
            patch.object(wh, "check_rate_limit", return_value=None),
            patch.object(wh, "record_message"),
        ):
            mock_lcs.is_active.return_value = False

            wh.handle_whatsapp_button_reply("972501234567", "booking_confirm_yes", "✅ אישור", "Test")

            mock_booking_btn.assert_called_once()

    def test_booking_confirm_no_routes_to_handler(self):
        """כפתור booking_confirm_no מגיע ל-_handle_booking_button ולא נופל ל-else."""
        import bot.whatsapp_handlers as wh

        with (
            patch.object(wh, "send_text_message"),
            patch.object(wh, "send_buttons_message"),
            patch.object(wh, "db"),
            patch.object(wh, "LiveChatService") as mock_lcs,
            patch.object(wh, "_handle_booking_button") as mock_booking_btn,
            patch.object(wh, "check_rate_limit", return_value=None),
            patch.object(wh, "record_message"),
        ):
            mock_lcs.is_active.return_value = False

            wh.handle_whatsapp_button_reply("972501234567", "booking_confirm_no", "❌ ביטול", "Test")

            mock_booking_btn.assert_called_once()
