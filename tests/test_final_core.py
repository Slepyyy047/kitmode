import unittest
import json
from datetime import datetime, timedelta, timezone

from kitmode.core import DomainError, Service
from kitmode.db import DB
from kitmode.telegram import Bot
from kitmode.transport import FakeTransport, TransportError


class FinalCoreTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.now = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        self.s = Service(self.db, lambda: self.now)
        self.s.settings(1, tz="UTC")

    def tearDown(self):
        self.db.close()

    def test_undo_after_note_and_evidence_restores_previous_report(self):
        hid = self.s.create(1, {"title": "Read", "kind": "quantity", "unit": "pages", "target": 10, "verify": "text"})
        self.s.record(1, hid, "first", amount=2)
        self.s.record(1, hid, "second", amount=8)
        self.s.note(1, hid, "My private note")
        self.s.evidence(1, hid, "text", note="Finished chapter")
        with self.assertRaises(DomainError):
            self.s.undo(1, "first")
        self.s.undo(1, "second")
        rec = self.s.history(1, hid)[-1]["record"]
        self.assertEqual((rec["value"], rec["status"], rec["verification"]), (2, "progress", "pending"))
        self.s.undo(1, "first")
        self.assertIsNone(self.s.history(1, hid)[-1]["record"])

    def test_note_owner_and_window_are_enforced(self):
        hid = self.s.create(1, {"title": "Walk"})
        self.s.record(1, hid, "done")
        with self.assertRaises(DomainError):
            self.s.note(2, hid, "intrusion")
        self.now += timedelta(days=7)
        with self.assertRaises(DomainError):
            self.s.note(1, hid, "late", day="2026-10-05")

    def test_friend_cannot_approve_a_changed_record(self):
        self.s.settings(2, tz="UTC")
        token = self.s.extras.invite(1)
        self.s.extras.accept_invite(2, token)
        self.s.extras.consent_verifier(2, 1)
        hid = self.s.create(1, {"title": "Walk", "verify": "friend"})
        self.s.record(1, hid, "first")
        request = self.s.extras.request_verification(1, hid, self.s.day(1), 2)
        self.s.correct_record(1, hid, "new_report", status="done")
        with self.assertRaises(DomainError):
            self.s.extras.decide_verification(2, request, True)
        self.assertEqual(self.s.history(1, hid)[-1]["record"]["verification"], "pending")

    def test_quantity_units_follow_historical_versions(self):
        hid = self.s.create(1, {"title": "Reading", "kind": "quantity", "unit": "pages", "target": 5})
        self.s.record(1, hid, "pages", amount=5)
        self.s.edit(1, hid, {"unit": "minutes", "target": 10})
        self.now += timedelta(days=1)
        self.s.record(1, hid, "minutes", amount=10)
        stats = self.s.stats(1, start="2026-10-05", end="2026-10-06")
        self.assertEqual(stats["quantity_by_unit"], {"pages": 5, "minutes": 10})

    def test_transient_reply_failure_retries_the_update_without_double_progress(self):
        class FailingTransport(FakeTransport):
            fails = True

            def send(self, chat_id, text, keyboard=None):
                if self.fails:
                    raise TransportError(503)
                return super().send(chat_id, text, keyboard)

        transport = FailingTransport()
        bot = Bot(self.s, transport)
        hid = self.s.create(1, {"title": "Read", "kind": "quantity", "target": 10})
        bot._begin(1, "amount", "amount", {"hid": hid})
        update = {"update_id": 123, "message": {"from": {"id": 1}, "chat": {"id": 1, "type": "private"}, "text": "2"}}
        with self.assertRaises(TransportError):
            bot.handle(update)
        self.assertIsNone(self.s.history(1, hid)[-1]["record"])
        self.assertIsNone(self.db.one("SELECT id FROM updates WHERE id=123"))
        transport.fails = False
        bot.handle(update)
        bot.handle(update)
        self.assertEqual(self.s.history(1, hid)[-1]["record"]["value"], 2)

    def test_forward_extra_schema_is_rejected(self):
        self.db.execute("UPDATE extras_schema SET version=999")
        with self.assertRaises(RuntimeError):
            Service(self.db)
        self.assertEqual(self.db.one("SELECT version FROM extras_schema")["version"], 999)

    def test_replacing_quantity_with_zero_and_overflow_do_not_fake_success(self):
        hid = self.s.create(1, {"title": "Measure", "kind": "quantity", "target": 10})
        self.s.record(1, hid, "one", amount=5)
        self.s.correct_record(1, hid, "zero", amount=0)
        self.assertEqual(self.s.history(1, hid)[-1]["record"]["status"], "progress")
        self.s.undo(1, "zero")
        self.assertEqual(self.s.history(1, hid)[-1]["record"]["value"], 5)
        self.s.record(1, hid, "large", amount=1e308)
        with self.assertRaises(DomainError):
            self.s.record(1, hid, "overflow", amount=1e308)
        self.assertEqual(self.s.history(1, hid)[-1]["record"]["value"], 1e308)

    def test_onboarding_preserves_all_values_and_old_delete_confirmation_expires(self):
        transport = FakeTransport()
        bot = Bot(self.s, transport)
        update_id = 200

        def message(text):
            nonlocal update_id
            update_id += 1
            bot.handle(
                {
                    "update_id": update_id,
                    "message": {
                        "from": {"id": 1, "first_name": "Alex"},
                        "chat": {"id": 1, "type": "private"},
                        "text": text,
                    },
                }
            )

        def tap(action, value=None):
            nonlocal update_id
            for row in transport.events[-1]["keyboard"]:
                for button in row:
                    data = json.loads(
                        self.db.one("SELECT payload FROM buttons WHERE token=?", (button["callback_data"][2:],))[
                            "payload"
                        ]
                    )
                    if data["action"] == action and (value is None or data.get("value") == value):
                        update_id += 1
                        bot.handle(
                            {
                                "update_id": update_id,
                                "callback_query": {
                                    "id": str(update_id),
                                    "from": {"id": 1},
                                    "message": {"chat": {"id": 1, "type": "private"}, "message_id": 1},
                                    "data": button["callback_data"],
                                },
                            }
                        )
                        return button["callback_data"]
            self.fail("No matching current button")

        message("/start")
        tap("flow_pick", "en")
        tap("use_telegram_name")
        tap("choice", "Europe/Kyiv")
        tap("choice", "22:00")
        tap("flow_pick", "strict")
        user = self.s.user(1)
        self.assertEqual(
            (user["name"], user["lang"], user["tz"], user["sleep"], user["boundary"], user["tone"]),
            ("Alex", "en", "Europe/Kyiv", "22:00", "04:00", "strict"),
        )
        self.assertTrue(user["onboarded"])
        message("/delete")
        old = next(
            button["callback_data"]
            for row in transport.events[-1]["keyboard"]
            for button in row
            if json.loads(
                self.db.one("SELECT payload FROM buttons WHERE token=?", (button["callback_data"][2:],))["payload"]
            )["action"]
            == "delete_confirm"
        )
        message("/today")
        update_id += 1
        bot.handle(
            {
                "update_id": update_id,
                "callback_query": {
                    "id": str(update_id),
                    "from": {"id": 1},
                    "message": {"chat": {"id": 1, "type": "private"}},
                    "data": old,
                },
            }
        )
        self.assertIsNotNone(self.db.one("SELECT id FROM users WHERE id=1"))
        message("/delete")
        tap("delete_confirm")
        self.assertIsNone(self.db.one("SELECT id FROM users WHERE id=1"))
        self.assertIn("deleted", transport.events[-1]["text"].lower())


if __name__ == "__main__":
    unittest.main()
