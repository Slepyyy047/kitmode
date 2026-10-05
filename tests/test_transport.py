from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from kitmode.transport import TelegramAPI, TransportError


class TelegramTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = TelegramAPI("123456:secret-token")

    def test_http_error_preserves_retry_after_without_exposing_token(self) -> None:
        body = json.dumps({"parameters": {"retry_after": 37}}).encode()
        error = urllib.error.HTTPError(
            "https://api.telegram.org/bot123456:secret-token/sendMessage", 429, "limited", {}, io.BytesIO(body)
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(TransportError) as caught:
                self.api._request("sendMessage", {"chat_id": 1})
        self.assertEqual((caught.exception.code, caught.exception.retry_after), (429, 37))
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertNotIn("secret-token", repr(caught.exception.__cause__))

    def test_url_error_becomes_redacted_service_unavailable(self) -> None:
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("private network detail")):
            with self.assertRaises(TransportError) as caught:
                self.api._request("sendMessage", {"chat_id": 1})
        self.assertEqual(caught.exception.code, 503)
        self.assertNotIn("private network detail", str(caught.exception))

    def test_deleted_message_edit_falls_back_to_new_message(self) -> None:
        with (
            patch.object(self.api, "_throttle"),
            patch.object(self.api, "_request", side_effect=[TransportError(400), {"message_id": 42}]) as request,
        ):
            result = self.api.edit(8, 10, "Updated", [[{"text": "Open", "callback_data": "opaque"}]])
        self.assertEqual(result, {"message_id": 42})
        self.assertEqual([call.args[0] for call in request.call_args_list], ["editMessageText", "sendMessage"])
        self.assertEqual(request.call_args_list[1].args[1]["text"], "Updated")

    def test_non_400_edit_failure_is_not_hidden_by_fallback(self) -> None:
        with (
            patch.object(self.api, "_throttle"),
            patch.object(self.api, "_request", side_effect=TransportError(403)) as request,
        ):
            with self.assertRaises(TransportError) as caught:
                self.api.edit(8, 10, "Updated")
        self.assertEqual(caught.exception.code, 403)
        request.assert_called_once()

    def test_unchanged_edit_does_not_send_a_duplicate_message(self):
        with patch.object(self.api, "_throttle"), patch.object(
            self.api, "_request", side_effect=TransportError(400, unchanged=True)
        ) as request:
            self.assertEqual(self.api.edit(8, 10, "Same"), {"message_id": 10})
        request.assert_called_once()

    def test_photo_navigation_edits_caption_without_failed_text_edit(self):
        with patch.object(self.api, "_throttle"), patch.object(self.api, "_request", return_value={}) as request:
            self.api.edit(8, 10, "Next", caption=True)
        self.assertEqual(request.call_args.args, ("editMessageCaption", {
            "chat_id": 8, "message_id": 10, "caption": "Next", "reply_markup": {"inline_keyboard": []}
        }))

    def test_long_caption_uses_text_message_without_truncating_content(self):
        with patch.object(self.api, "_throttle"), patch.object(self.api, "_request", return_value={}) as request:
            self.api.edit(8, 10, "A" * 1500, caption=True)
        self.assertEqual(request.call_args.args[0], "sendMessage")
        self.assertEqual(len(request.call_args.args[1]["text"]), 1500)

    def test_cached_photo_reuses_telegram_file_instead_of_uploading(self):
        with patch.object(self.api, "_throttle"), patch.object(self.api, "_request", return_value={}) as request:
            self.api.photo(8, "cached-file-id", "Welcome", [[{"text": "Next", "callback_data": "opaque"}]])
        self.assertEqual(len(request.call_args.args), 2)
        self.assertEqual(request.call_args.args[1]["photo"], "cached-file-id")
        self.assertTrue(json.loads(request.call_args.args[1]["reply_markup"])["inline_keyboard"])

    def test_optional_callback_ack_failure_does_not_discard_action(self):
        for code in (400, 429, 503):
            with patch.object(self.api, "_request", side_effect=TransportError(code)):
                self.api.answer("expired-or-transient")


if __name__ == "__main__":
    unittest.main()
