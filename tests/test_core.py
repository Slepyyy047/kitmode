from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from kitmode.core import DomainError, Service
from kitmode.db import DB
from kitmode.reminders import COPY, ReminderEngine
from kitmode.schedules import in_quiet, personal_day, scheduled, wall_instant
from kitmode.transport import FakeTransport, TransportError
from kitmode.__main__ import scratch


class CoreFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 5, 9, tzinfo=timezone.utc)
        self.db = DB(":memory:")
        self.s = Service(self.db, lambda: self.now)
        self.s.settings(1, tz="UTC", quiet_start="23:00", quiet_end="08:00")

    def tearDown(self) -> None:
        self.db.close()

    def habit(self, **spec):
        return self.s.create(1, {"title": "Read", **spec})


class CoreTests(CoreFixture):
    def test_all_schedules_and_anchor(self):
        d = date(2026, 10, 5)
        base = {"start": d.isoformat(), "end": "2026-10-20"}
        self.assertTrue(scheduled(base | {"schedule": {"type": "daily"}}, d))
        self.assertFalse(scheduled(base | {"schedule": {"type": "weekdays", "days": [1]}}, d))
        self.assertTrue(scheduled(base | {"schedule": {"type": "weekly", "n": 3}}, d))
        interval = base | {"schedule": {"type": "interval", "n": 3}}
        self.assertTrue(scheduled(interval, d + timedelta(days=3)))
        self.assertFalse(scheduled(interval, d + timedelta(days=2)))
        self.assertFalse(scheduled(interval, d - timedelta(days=3)))
        self.assertFalse(scheduled(interval, d + timedelta(days=18)))

    def test_personal_day_and_manual_timezone(self):
        instant = datetime(2026, 10, 6, 1, tzinfo=timezone.utc)
        self.assertEqual(personal_day(instant, "Europe/Kyiv", "05:00"), date(2026, 10, 5))
        self.assertEqual(personal_day(instant, "America/New_York", "04:00"), date(2026, 10, 5))
        with self.assertRaises(DomainError):
            self.s.settings(1, tz="Guess/Language")
        self.assertIsNone(self.s.user(99)["tz"])

    def test_dst_gap_fold_quiet_midnight(self):
        gap = wall_instant(date(2026, 3, 8), "02:30", "America/New_York")
        self.assertEqual(gap, datetime(2026, 3, 8, 7, tzinfo=timezone.utc))
        fold = wall_instant(date(2026, 11, 1), "01:30", "America/New_York")
        self.assertEqual(fold, datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
        self.assertTrue(in_quiet(datetime(2026, 1, 1, 0, 30).time(), "23:00", "08:00"))
        self.assertFalse(in_quiet(datetime(2026, 1, 1, 12).time(), "23:00", "08:00"))
        self.assertEqual(wall_instant(date(2026, 10, 5), "01:00", "UTC", "04:00").date(), date(2026, 10, 6))

    def test_idempotent_partial_quantity_undo_rewards(self):
        hid = self.habit(kind="quantity", target=10, minimum=2)
        first = self.s.record(1, hid, "a", amount=2)
        self.assertEqual(first["status"], "minimum")
        self.assertEqual(self.s.record(1, hid, "a", amount=2)["value"], 2)
        self.s.record(1, hid, "b", amount=8)
        self.assertEqual(self.s.stats(1)["full"], 1)
        with self.assertRaises(DomainError):
            self.s.undo(1, "a")
        self.s.undo(1, "b")
        self.assertEqual(self.s._record(hid, self.s.day(1))["value"], 2)
        self.s.undo(1, "a")
        self.assertIsNone(self.s._record(hid, self.s.day(1)))
        self.s.record(1, hid, "c", amount=10)
        self.assertEqual(self.s.stats(1)["xp"], 10)

    def test_limits_explicit_zero_not_absence_and_bulk(self):
        binary = self.habit()
        limit = self.habit(title="Limit", kind="limit", target=2, minimum=3)
        self.s.bulk(1, "bulk")
        self.assertEqual(self.s._record(binary, self.s.day(1))["status"], "full")
        self.assertIsNone(self.s._record(limit, self.s.day(1)))
        with self.assertRaises(DomainError):
            self.s.record(1, limit, "bad")
        self.assertEqual(self.s.record(1, limit, "zero", amount=0)["status"], "full")
        self.assertEqual(self.s.record(1, limit, "max", amount=3)["status"], "minimum")
        self.assertEqual(self.s.record(1, limit, "over", amount=4)["status"], "fail")

    def test_version_snapshot_future_edit(self):
        hid = self.habit(kind="quantity", target=10, minimum=2)
        self.s.record(1, hid, "old", amount=3)
        self.s.edit(1, hid, {"target": 2, "minimum": None})
        self.assertEqual(self.s.habit(1, hid)["spec"]["target"], 10)
        with self.assertRaises(DomainError):
            self.s.edit(1, hid, {"target": 2}, self.s.day(1))
        self.now += timedelta(days=1)
        self.assertEqual(self.s.habit(1, hid)["spec"]["target"], 2)
        self.s.record(1, hid, "new", amount=2)
        old = self.s._record(hid, "2026-10-05")
        self.assertEqual(json.loads(old["snapshot"])["target"], 10)
        self.s.record(1, hid, "old-correction", amount=1, day="2026-10-05")
        self.assertEqual(self.s._record(hid, "2026-10-05")["status"], "minimum")

    def test_weekly_quota_no_invented_daily_misses(self):
        hid = self.habit(schedule={"type": "weekly", "n": 2})
        self.s.record(1, hid, "mon")
        self.assertEqual(self.s.stats(1)["opportunities"], 0)
        self.now += timedelta(days=1)
        self.s.record(1, hid, "tue")
        stats = self.s.stats(1)
        self.assertEqual((stats["completed"], stats["opportunities"], stats["missing"]), (1, 1, 0))
        self.assertEqual(stats["xp"], 10)
        self.assertEqual(self.s.today(1), [])
        self.now += timedelta(days=6)
        self.assertEqual(self.s.stats(1)["missing"], 0)

    def test_rest_minimum_pause_and_missing_streak(self):
        hid = self.habit(kind="quantity", target=10, minimum=2, schedule={"type": "weekdays", "days": [0, 2, 4]})
        self.s.record(1, hid, "mon", amount=2)
        self.now += timedelta(days=2)
        self.s.record(1, hid, "wed", amount=10)
        self.assertEqual(self.s.stats(1)["current_streak"], 2)
        self.s.pause(1, hid, "2026-10-07", "2026-10-09")
        self.now += timedelta(days=5)
        self.s.record(1, hid, "next", amount=10)
        self.assertEqual(self.s.stats(1)["current_streak"], 3)  # completed Wednesday retained
        self.now += timedelta(days=5)
        self.assertEqual(self.s.stats(1)["current_streak"], 0)
        with self.assertRaises(DomainError):
            self.s.pause(1, hid, "2026-10-05", None)

    def test_expired_ownership_and_seven_day_buttons(self):
        hid = self.habit()
        token = self.s.button(1, {"action": "record"}, ttl=1)
        with self.assertRaises(DomainError):
            self.s.consume_button(2, token)
        self.now += timedelta(seconds=2)
        with self.assertRaises(DomainError):
            self.s.consume_button(1, token)
        self.now += timedelta(days=7)
        with self.assertRaises(DomainError):
            self.s.record(1, hid, "old", day="2026-10-05")
        with self.assertRaises(DomainError):
            self.s.record(2, hid, "foreign")

    def test_future_days_do_not_distort_and_disabled_rewards(self):
        hid = self.habit()
        self.s.settings(1, gamification=False)
        self.s.record(1, hid, "x")
        stats = self.s.stats(1, "2026-10-05", "2027-10-05")
        self.assertEqual(stats["rate"], 1)
        self.assertEqual(stats["opportunities"], 1)
        self.assertEqual(stats["xp"], 0)

    def test_verification_separate_and_sensitive_media_rejected(self):
        hid = self.habit(verify="friend")
        self.s.record(1, hid, "done")
        self.assertEqual(self.s.stats(1)["completed"], 1)
        self.assertEqual(self.s._record(hid, self.s.day(1))["verification"], "pending")
        with self.assertRaises(DomainError):
            self.s.record(1, hid, "fake", verification="approved")
        with self.assertRaises(DomainError):
            self.habit(sensitive=True, verify="photo")

    def test_independence_schedule_aware(self):
        hid = self.habit(schedule={"type": "interval", "n": 2})
        for i in range(8):
            self.s.record(1, hid, str(i))
            if i < 7:
                self.now += timedelta(days=2)
        self.assertIn(hid, self.s.stats(1)["independence"])

    def test_nested_transaction_rolls_back_and_unique_rewards(self):
        with self.assertRaises(DomainError), self.db.transaction():
            self.habit()
            raise DomainError()
        self.assertEqual(self.s.habits(1), [])

    def test_binary_minimum_must_be_predeclared(self):
        hid = self.habit(minimum=0.5, minimum_description="Read one paragraph")
        self.s.record(1, hid, "min", status="minimum")
        self.assertEqual(self.s.stats(1)["minimum"], 1)
        with self.assertRaises(DomainError):
            self.s.record(1, self.habit(), "bad-min", status="minimum")

    def test_absolute_correction_and_proof_do_not_add_progress(self):
        hid = self.habit(kind="quantity", target=10, minimum=2, verify="text")
        self.s.record(1, hid, "done", amount=10)
        self.s.evidence(1, hid, "text", note="Read ten pages")
        self.assertEqual(self.s._record(hid, self.s.day(1))["value"], 10)
        self.assertEqual(self.s._record(hid, self.s.day(1))["verification"], "submitted")
        self.s.correct_record(1, hid, "correct", amount=3)
        self.s.correct_record(1, hid, "correct", amount=3)
        self.assertEqual(self.s._record(hid, self.s.day(1))["value"], 3)
        self.s.undo(1, "correct")
        self.assertEqual(self.s._record(hid, self.s.day(1))["value"], 10)
        self.assertEqual(self.s.stats(1)["xp"], 10)


class ReminderTests(CoreFixture):
    def test_reminder_restart_and_cancel(self):
        with scratch() as folder:
            path = Path(folder) / "bot.sqlite3"
            db = DB(path)
            s = Service(db, lambda: self.now)
            s.settings(1, tz="UTC")
            hid = s.create(1, {"title": "Read", "reminders": ["09:00"], "repeat": 15})
            transport = FakeTransport()
            self.assertEqual(ReminderEngine(s, transport).tick(), 1)
            db.close()
            db = DB(path)
            s = Service(db, lambda: self.now)
            self.assertEqual(ReminderEngine(s, transport).tick(), 0)
            s.record(1, hid, "done")
            self.now += timedelta(minutes=15)
            self.assertEqual(ReminderEngine(s, transport).tick(), 0)
            db.close()

    def test_downtime_coalesces_sensitive_names(self):
        self.habit(reminders=["06:00", "08:00", "08:30", "09:00"])
        self.habit(title="SECRET", sensitive=True, kind="limit", target=0, reminders=["09:00"])
        transport = FakeTransport()
        engine = ReminderEngine(self.s, transport)
        self.assertEqual(engine.tick(), 1)
        self.assertNotIn("SECRET", transport.events[-1]["text"])
        self.assertEqual(engine.tick(), 0)
        self.assertEqual(len(transport.events), 1)

    def test_timezone_edit_does_not_resend(self):
        self.habit(reminders=["09:00"])
        transport = FakeTransport()
        engine = ReminderEngine(self.s, transport)
        engine.tick()
        self.s.settings(1, tz="Europe/London")
        self.assertEqual(engine.tick(), 0)

    def test_quiet_and_unconfirmed_timezone(self):
        self.s.settings(1, tz=None)
        self.habit(reminders=["09:00"])
        transport = FakeTransport()
        self.assertEqual(ReminderEngine(self.s, transport).tick(), 0)
        self.s.settings(1, tz="UTC", quiet_start="08:00", quiet_end="10:00")
        engine = ReminderEngine(self.s, transport)
        self.assertEqual(engine.tick(), 0)
        self.now += timedelta(hours=1)
        self.assertEqual(engine.tick(), 1)

    def test_forbidden_disables_and_temporary_retries(self):
        self.habit(reminders=["09:00"])

        class Failing(FakeTransport):
            def send(self, *args, **kwargs):
                raise TransportError(403)

        self.assertEqual(ReminderEngine(self.s, Failing()).tick(), 0)
        self.assertFalse(self.s.user(1)["reminders"])

    def test_localization_keys_and_sleep_summary_preliminary(self):
        self.assertEqual(set(COPY["uk"]), set(COPY["en"]))
        self.assertEqual(set(COPY["uk"]), set(COPY["ru"]))
        self.habit()
        self.now = self.now.replace(hour=22, minute=30)
        transport = FakeTransport()
        ReminderEngine(self.s, transport).tick()
        self.assertIn("Попередній", transport.events[-1]["text"])
        self.assertEqual(self.s.stats(1)["fail"], 0)


if __name__ == "__main__":
    unittest.main()
