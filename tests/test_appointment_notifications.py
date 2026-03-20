"""טסטים ל-appointment_notifications — הודעות ביטול/אישור תור."""

from unittest.mock import patch

from appointment_notifications import (
    _build_cancelled_message,
    _build_confirmed_message,
    notify_appointment_status,
)


class TestBuildCancelledMessage:
    """הודעת ביטול תור — מותאמת לפלטפורמה."""

    def test_telegram_user_gets_slash_book(self):
        """משתמש טלגרם מקבל הוראה עם /book."""
        msg = _build_cancelled_message("תספורת", "2026-03-21", "10:00")
        assert "/book" in msg
        assert "*תור*" not in msg

    def test_whatsapp_user_gets_text_instruction(self):
        """משתמש וואטסאפ מקבל הוראה טקסטואלית (לא /book)."""
        msg = _build_cancelled_message(
            "תספורת", "2026-03-21", "10:00", is_whatsapp=True,
        )
        assert "/book" not in msg
        assert "תור" in msg

    def test_owner_message_included(self):
        """הודעה אישית מבעל העסק מופיעה."""
        msg = _build_cancelled_message(
            "תספורת", "2026-03-21", "10:00", owner_message="סליחה על הביטול",
        )
        assert "סליחה על הביטול" in msg


class TestBuildConfirmedMessage:
    """הודעת אישור — לא תלויה בפלטפורמה (אין פקודות ספציפיות)."""

    def test_accepts_is_whatsapp_kwarg(self):
        """_build_confirmed_message מקבל is_whatsapp בלי שגיאה (kwargs)."""
        msg = _build_confirmed_message(
            "תספורת", "2026-03-21", "10:00", is_whatsapp=True,
        )
        assert "אושר" in msg


class TestNotifyAppointmentStatus:
    """notify_appointment_status — ניתוב לפי פלטפורמה."""

    @patch("appointment_notifications.send_message_to_user", return_value=True)
    def test_telegram_user_gets_book_command(self, mock_send):
        """משתמש טלגרם מקבל /book בהודעת ביטול."""
        appt = {
            "id": 1,
            "user_id": "123456",
            "status": "cancelled",
            "service": "תספורת",
            "preferred_date": "2026-03-21",
            "preferred_time": "10:00",
        }
        notify_appointment_status(appt)
        text = mock_send.call_args[0][1]
        assert "/book" in text

    @patch("appointment_notifications.send_message_to_user", return_value=True)
    def test_whatsapp_user_no_book_command(self, mock_send):
        """משתמש וואטסאפ לא מקבל /book — מקבל הוראה טקסטואלית."""
        appt = {
            "id": 2,
            "user_id": "wa_972501234567",
            "status": "cancelled",
            "service": "תספורת",
            "preferred_date": "2026-03-21",
            "preferred_time": "10:00",
        }
        notify_appointment_status(appt)
        text = mock_send.call_args[0][1]
        assert "/book" not in text
        assert "תור" in text
