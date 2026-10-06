import runpy
import sys
import unittest
from unittest.mock import Mock, patch

import requests

import telegram_notifier as telegram


class TelegramNotifierTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.post.return_value = Mock(
            status_code=200,
            json=Mock(
                return_value={
                    "ok": True,
                    "result": {"message_id": 42},
                }
            ),
        )

    def send(self, text="Example message", **overrides):
        # Synthetic destination; every send uses the mock HTTP client.
        kwargs = {"bot_token": "123:synthetic_test_token", "chat_id": "-100123"}
        kwargs.update(overrides)
        return telegram.send_telegram_message(text, session=self.client, **kwargs)

    def test_confirms_delivery_and_makes_exactly_one_verified_request(self):
        self.assertTrue(self.send().delivered)
        self.client.post.assert_called_once_with(
            "https://api.telegram.org/bot123:synthetic_test_token/sendMessage",
            json={"chat_id": "-100123", "text": "Example message"},
            timeout=(10, 20),
            allow_redirects=False,
            verify=True,
        )

    def test_missing_configuration_and_invalid_message_do_not_send(self):
        for kwargs in (
            {"bot_token": ""},
            {"bot_token": "invalid/token"},
            {"chat_id": ""},
            {"chat_id": None},
            {"chat_id": True},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(
                    self.send(**kwargs).status, telegram.DeliveryStatus.PERMANENT_FAILURE
                )
        for text in ("", "x" * 4097, None, "\ud800"):
            with self.subTest(length=len(text) if isinstance(text, str) else None):
                self.assertEqual(self.send(text).status, telegram.DeliveryStatus.PERMANENT_FAILURE)
        self.client.post.assert_not_called()

    def test_text_limit_and_unicode_are_handled_without_silent_truncation(self):
        self.assertTrue(self.send("x" * 4096).delivered)
        self.assertEqual(self.client.post.call_args.kwargs["json"]["text"], "x" * 4096)
        self.assertTrue(self.send("Visa — Montréal").delivered)
        self.assertFalse(self.send("\U0001f600" * 4096).delivered)

    def test_invalid_timeouts_never_send(self):
        for timeout in (0, -1, None, True, float("inf"), float("nan"), (), (1,), (1, 0), (1, 2, 3)):
            with self.subTest(timeout=timeout):
                self.assertEqual(
                    self.send(timeout=timeout).status, telegram.DeliveryStatus.PERMANENT_FAILURE
                )
        self.client.post.assert_not_called()

    def test_numeric_chat_and_explicit_timeout(self):
        self.assertTrue(self.send(chat_id=123, timeout=(2, 4)).delivered)
        self.assertEqual(self.client.post.call_args.kwargs["json"]["chat_id"], "123")
        self.assertEqual(self.client.post.call_args.kwargs["timeout"], (2, 4))

    def test_rate_limit_returns_retry_delay_without_retrying(self):
        self.client.post.return_value.status_code = 429
        self.client.post.return_value.json.return_value = {
            "ok": False,
            "error_code": 429,
            "parameters": {"retry_after": 45},
        }
        result = self.send()
        self.assertEqual(result.status, telegram.DeliveryStatus.RETRYABLE_FAILURE)
        self.assertEqual(result.retry_after, 45)
        self.client.post.assert_called_once()

    def test_invalid_retry_metadata_is_ignored(self):
        for parameters in (
            None,
            [],
            {"retry_after": True},
            {"retry_after": -1},
            {"retry_after": "5"},
        ):
            self.client.post.return_value.status_code = 429
            self.client.post.return_value.json.return_value = {"parameters": parameters}
            self.assertIsNone(self.send().retry_after)

    def test_server_errors_are_retryable_and_rejections_are_permanent(self):
        for status in (500, 502, 503):
            self.client.post.return_value.status_code = status
            self.assertEqual(self.send().status, telegram.DeliveryStatus.RETRYABLE_FAILURE)
        for status in (301, 400, 401, 403, 404):
            self.client.post.return_value.status_code = status
            self.assertEqual(self.send().status, telegram.DeliveryStatus.PERMANENT_FAILURE)

    def test_telegram_error_can_arrive_with_http_200(self):
        for code, expected in (
            (400, telegram.DeliveryStatus.PERMANENT_FAILURE),
            (429, telegram.DeliveryStatus.RETRYABLE_FAILURE),
            (500, telegram.DeliveryStatus.RETRYABLE_FAILURE),
        ):
            self.client.post.return_value.json.return_value = {"ok": False, "error_code": code}
            self.assertEqual(self.send().status, expected)

    def test_malformed_or_incomplete_success_is_uncertain(self):
        for payload in (
            None,
            [],
            {},
            {"ok": True},
            {"ok": True, "result": {}},
            {"ok": True, "result": {"message_id": True}},
        ):
            self.client.post.return_value.json.return_value = payload
            self.assertEqual(self.send().status, telegram.DeliveryStatus.UNCERTAIN)
        self.client.post.return_value.json.side_effect = ValueError("not JSON")
        self.assertEqual(self.send().status, telegram.DeliveryStatus.UNCERTAIN)

    def test_network_outcomes_and_exception_secrets_are_not_exposed(self):
        for exception, expected in (
            (requests.exceptions.ConnectTimeout, telegram.DeliveryStatus.RETRYABLE_FAILURE),
            (requests.exceptions.ReadTimeout, telegram.DeliveryStatus.UNCERTAIN),
            (requests.exceptions.ConnectionError, telegram.DeliveryStatus.UNCERTAIN),
            (requests.exceptions.SSLError, telegram.DeliveryStatus.PERMANENT_FAILURE),
        ):
            self.client.post.side_effect = exception(
                "https://api.telegram.org/bot123:synthetic_test_token/sendMessage"
            )
            result = self.send()
            self.assertEqual(result.status, expected)
            self.assertNotIn("synthetic_test_token", repr(result))
            self.assertNotIn("api.telegram.org", result.reason)

    def test_server_descriptions_cannot_leak_tokens(self):
        self.client.post.return_value.json.return_value = {
            "ok": False,
            "description": "secret 123:synthetic_test_token",
        }
        self.assertNotIn("synthetic_test_token", repr(self.send()))

    def test_import_is_independent_of_visa_settings_and_network(self):
        with patch.dict(
            sys.modules, {"settings": None, "reschedule": None, "payment_tracker": None}
        ):
            with patch("requests.post") as post:
                namespace = runpy.run_path(telegram.__file__)
                self.assertIn("send_telegram_message", namespace)
                post.assert_not_called()

    def test_two_callers_do_not_share_delivery_policy_or_state(self):
        self.assertTrue(self.send("Payment tracker message").delivered)
        self.assertTrue(self.send("Future paid-flow message").delivered)
        self.assertEqual(self.client.post.call_count, 2)
        self.assertFalse(hasattr(telegram, "TEST_MODE"))


if __name__ == "__main__":
    unittest.main()
