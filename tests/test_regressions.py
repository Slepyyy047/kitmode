from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kitmode.core import DomainError, Service
from kitmode.db import DB
from kitmode.reminders import ReminderEngine
from kitmode.transport import FakeTransport, TransportError
from kitmode.__main__ import scratch


class ClockBoundaryRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
        self.db = DB(":memory:")
        self.service = Service(self.db, lambda: self.now)
        self.service.settings(1, tz="America/New_York", boundary="00:00", quiet_start="12:00", quiet_end="13:00")

    def tearDown(self) -> None:
        self.db.close()

    def test_repeated_fall_back_wall_time_sends_once(self) -> None:
        self.service.create(1, {"title": "Read", "reminders": ["01:30"]})
        transport = FakeTransport()
        engine = ReminderEngine(self.service, transport)

        self.assertEqual(engine.tick(), 1)
        self.now += timedelta(hours=1)  # second 01:30 during the fold
        self.assertEqual(engine.tick(), 0)
        self.assertEqual(len(transport.events), 1)

    def test_spring_forward_missing_wall_time_sends_once(self) -> None:
        self.now = datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc)
        self.service.settings(1, tz="America/New_York", boundary="00:00", quiet_start="12:00", quiet_end="13:00")
        self.service.create(1, {"title": "Read", "reminders": ["02:30"]})
        transport = FakeTransport()
        engine = ReminderEngine(self.service, transport)

        self.assertEqual(engine.tick(), 1)
        self.now += timedelta(minutes=1)
        self.assertEqual(engine.tick(), 0)
        self.assertEqual(len(transport.events), 1)

    def test_snooze_during_quiet_hours_is_rejected_without_job(self) -> None:
        self.now = datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc)  # 00:30 local, quiet
        self.service.settings(1, tz="America/New_York", quiet_start="22:00", quiet_end="07:00")
        hid = self.service.create(1, {"title": "Read", "reminders": ["01:30"]})
        with self.assertRaises(DomainError):
            self.service.snooze(1, hid, 15)
        self.assertEqual(self.db.one("SELECT count(*) n FROM jobs WHERE id LIKE 'snooze:%'")["n"], 0)

    def test_weekly_quota_rolls_over_at_monday_in_users_timezone(self) -> None:
        self.now = datetime(2026, 10, 11, 22, 30, tzinfo=timezone.utc)  # Monday 01:30 in Kyiv
        self.service.settings(1, tz="Europe/Kyiv", boundary="04:00", quiet_start="23:00", quiet_end="08:00")
        hid = self.service.create(1, {"title": "Walk", "schedule": {"type": "weekly", "n": 1}})
        self.assertEqual(self.service.day(1), "2026-10-11")  # boundary keeps Sunday personal day
        self.service.record(1, hid, "sun")
        self.now = datetime(2026, 10, 12, 2, 0, tzinfo=timezone.utc)  # 05:00 local; new personal day
        self.assertEqual(self.service.day(1), "2026-10-12")
        self.assertEqual(len(self.service.today(1)), 1)


class ReminderRetryRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 10, 5, 9, tzinfo=timezone.utc)
        self.db = DB(":memory:")
        self.service = Service(self.db, lambda: self.now)
        self.service.settings(1, tz="UTC", quiet_start="23:00", quiet_end="08:00")
        self.hid = self.service.create(1, {"title": "Read", "reminders": ["09:00"]})

    def tearDown(self) -> None:
        self.db.close()

    def test_429_honors_retry_after_and_503_uses_backoff(self) -> None:
        class Failing(FakeTransport):
            def __init__(self, code, retry_after=0):
                super().__init__()
                self.error = TransportError(code, retry_after)

            def send(self, *args, **kwargs):
                raise self.error

        engine = ReminderEngine(self.service, Failing(429, 120))
        self.assertEqual(engine.tick(), 0)
        job = self.db.one("SELECT attempts,state,due FROM jobs WHERE id LIKE 'remind:%'")
        self.assertEqual((job["attempts"], job["state"], job["due"]), (1, "pending", "2026-10-05T09:02:00+00:00"))

        self.now += timedelta(seconds=120)
        engine.transport = Failing(503)
        self.assertEqual(engine.tick(), 0)
        job = self.db.one("SELECT attempts,state,due FROM jobs WHERE id LIKE 'remind:%'")
        self.assertEqual((job["attempts"], job["state"], job["due"]), (2, "pending", "2026-10-05T09:03:00+00:00"))

    def test_private_habit_is_hidden_from_reminder_and_summary(self) -> None:
        self.service.create(1, {"title": "SECRET", "sensitive": True, "reminders": ["09:00"]})
        transport = FakeTransport()
        ReminderEngine(self.service, transport).tick()
        text = transport.events[0]["text"]
        self.assertNotIn("SECRET", text)
        self.assertIn("Приватна звичка", text)

    def test_archived_habit_does_not_accrue_future_misses(self) -> None:
        self.service.archive(1, self.hid)
        self.now += timedelta(days=1)
        stats = self.service.stats(1, "2026-10-06", "2026-10-06", self.hid)
        self.assertEqual(stats["opportunities"], 0)
        self.assertEqual(stats["missing"], 0)

    def test_active_timer_survives_database_restart(self) -> None:
        with scratch() as folder:
            path = Path(folder) / "timer.sqlite3"
            db = DB(path)
            service = Service(db, lambda: self.now)
            hid = service.create(1, {"title": "Read"})
            service.extras.start_timer(1, hid)
            db.close()

            self.now += timedelta(minutes=7, seconds=12)
            db = DB(path)
            service = Service(db, lambda: self.now)
            try:
                self.assertEqual(service.extras.finish_timer(1, hid), 432)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
