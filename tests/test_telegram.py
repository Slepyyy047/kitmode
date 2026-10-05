from __future__ import annotations

import unittest
from datetime import datetime, timezone

from kitmode.core import Service
from kitmode.db import DB
from kitmode.i18n import LANGUAGES, STRINGS, tr
from kitmode.telegram import Bot
from kitmode.transport import FakeTransport


class TelegramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = DB(":memory:")
        self.service = Service(self.db, lambda: datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
        self.transport = FakeTransport()
        self.bot = Bot(self.service, self.transport, admin_ids=(99,))

    def tearDown(self) -> None:
        self.db.close()

    @staticmethod
    def message(uid: int, update_id: int, text: str, chat_type: str = "private") -> dict:
        return {
            "update_id": update_id,
            "message": {
                "from": {"id": uid, "first_name": "Alex"},
                "chat": {"id": uid, "type": chat_type},
                "text": text,
            },
        }

    def callback(self, uid: int, update_id: int, data: str) -> dict:
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"cb-{update_id}",
                "from": {"id": uid},
                "message": {"chat": {"id": uid, "type": "private"}},
                "data": data,
            },
        }

    def test_localization_has_identical_complete_key_sets(self) -> None:
        self.assertEqual(set(LANGUAGES), set(STRINGS))
        self.assertEqual(set(STRINGS["uk"]), set(STRINGS["ru"]))
        self.assertEqual(set(STRINGS["uk"]), set(STRINGS["en"]))
        for language in LANGUAGES:
            for key, value in STRINGS[language].items():
                self.assertTrue(value.strip(), (language, key))
                self.assertEqual(value, tr(language, key))

    def test_start_persists_resumable_onboarding_and_private_chat_only(self) -> None:
        self.bot.handle(self.message(7, 1, "/start", "group"))
        self.assertIn("приватному", self.transport.events[-1]["text"])
        self.bot.handle(self.message(7, 2, "/start"))
        self.assertEqual(self.service.session(7)["flow"], "onboard")
        self.assertEqual(self.service.session(7)["step"], "language")
        self.assertTrue(self.transport.events[-1]["keyboard"])
        self.bot.handle(self.message(7, 3, ""))
        self.assertEqual(self.service.session(7)["step"], "language")

    def test_callback_data_is_opaque_short_and_update_is_idempotent(self) -> None:
        self.service.user(8)
        self.service.settings(8, lang="en", tz="UTC", onboarded=True)
        hid = self.service.create(8, {"title": "Walk", "kind": "binary", "start": "2026-10-05"})
        self.bot.handle(self.message(8, 10, "/today"))
        # Ask the bot to render the habit detail; all emitted callback data must
        # stay within Telegram's 64-byte limit and reveal no domain payload.
        self.bot.handle(self.message(8, 11, "/habits"))
        for row in self.transport.events[-1]["keyboard"]:
            for button in row:
                callback_data = button["callback_data"]
                self.assertLessEqual(len(callback_data.encode()), 64)
                self.assertNotIn(hid, callback_data)
        # Directly mint the same callback shape the UI uses, then deliver the
        # same Telegram update twice. The completion and reward stay singular.
        token = self.service.button(8, {"action": "record", "hid": hid, "status": "done", "once": True})
        update = self.callback(8, 12, "b:" + token)
        self.bot.handle(update)
        count = self.service.db.one("SELECT count(*) n FROM records WHERE habit_id=?", (hid,))["n"]
        self.bot.handle(update)
        self.assertEqual(self.service.db.one("SELECT count(*) n FROM records WHERE habit_id=?", (hid,))["n"], count)
        self.assertEqual(self.service.db.one("SELECT count(*) n FROM updates WHERE id=12")["n"], 1)
        self.assertEqual(self.service.stats(8, start="2026-10-05", end="2026-10-05")["full"], 1)

    def test_callback_token_cannot_be_used_by_another_user(self) -> None:
        self.service.user(1)
        self.service.user(2)
        self.service.settings(2, lang="en")
        token = self.service.button(1, {"action": "today", "once": False})
        self.bot.handle(self.callback(2, 1, "b:" + token))
        self.assertEqual(self.transport.events[-1]["text"], self.bot._text(2, "error_stale"))
        self.assertEqual(self.service.db.one("SELECT used FROM buttons WHERE token=?", (token,))["used"], 0)


if __name__ == "__main__":
    unittest.main()
