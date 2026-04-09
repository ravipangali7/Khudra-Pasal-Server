"""Unit tests for Aakash SMS client (mocked HTTP)."""

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from core.services.aakash_sms import AakashSmsError, send_sms


class AakashSmsTests(TestCase):
    @override_settings(
        AAKASHSMS_AUTH_TOKEN="test-token",
        AAKASHSMS_API_URL="https://sms.example.test/v3/send",
    )
    @patch("core.services.aakash_sms.requests.post")
    def test_send_posts_form_fields_and_succeeds(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(
            ok=True,
            status_code=200,
            json=lambda: {
                "error": False,
                "message": "1 messages has been queued for delivery.",
                "data": {
                    "valid": [
                        {
                            "id": 1,
                            "mobile": "9779811111111",
                            "text": "hi",
                            "credit": 1,
                            "network": "ncell",
                            "status": "queued",
                        }
                    ],
                    "invalid": [],
                },
            },
        )

        send_sms(to="9811111111", text="Hello")

        mock_post.assert_called_once()
        _args, kwargs = mock_post.call_args
        self.assertEqual(_args[0], "https://sms.example.test/v3/send")
        self.assertEqual(
            kwargs["data"],
            {
                "auth_token": "test-token",
                "to": "9811111111",
                "text": "Hello",
            },
        )
        self.assertIn("timeout", kwargs)

    @override_settings(AAKASHSMS_AUTH_TOKEN="test-token")
    @patch("core.services.aakash_sms.requests.post")
    def test_send_raises_on_provider_error_flag(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(
            ok=True,
            status_code=200,
            json=lambda: {
                "error": True,
                "message": "Not enough balance.",
                "data": [],
            },
        )

        with self.assertRaises(AakashSmsError) as ctx:
            send_sms(to="9811111111", text="Hello")
        self.assertIn("balance", str(ctx.exception).lower())

    @override_settings(AAKASHSMS_AUTH_TOKEN="test-token")
    @patch("core.services.aakash_sms.requests.post")
    def test_send_raises_on_http_error(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(
            ok=False,
            status_code=500,
            json=lambda: {"error": True, "message": "Server error", "data": []},
        )

        with self.assertRaises(AakashSmsError):
            send_sms(to="9811111111", text="Hello")

    @override_settings(AAKASHSMS_AUTH_TOKEN="test-token")
    @patch("core.services.aakash_sms.requests.post")
    def test_send_raises_when_only_invalid_recipients(self, mock_post: MagicMock) -> None:
        mock_post.return_value = MagicMock(
            ok=True,
            status_code=200,
            json=lambda: {
                "error": False,
                "message": "queued",
                "data": {
                    "valid": [],
                    "invalid": [
                        {
                            "mobile": "988585584",
                            "text": "x",
                            "credit": 0,
                            "network": "N/A",
                            "status": "aborted",
                        }
                    ],
                },
            },
        )

        with self.assertRaises(AakashSmsError):
            send_sms(to="988585584", text="x")

    def test_send_raises_without_token(self) -> None:
        with override_settings(AAKASHSMS_AUTH_TOKEN=""):
            with self.assertRaises(AakashSmsError):
                send_sms(to="9811111111", text="Hello")
