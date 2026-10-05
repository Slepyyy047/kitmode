from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from kitmode.runtime import run_loop
from kitmode.core import Service
from kitmode.db import DB
from kitmode.reminders import ReminderEngine
from kitmode.transport import FakeTransport, TransportError


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.release = threading.Event()
        self.service = Mock()
        self.bot = Mock()
        self.engine = Mock()
        self.transport = Mock()

    def tearDown(self):
        self.stop.set()
        self.release.set()

    def test_blocked_long_poll_does_not_delay_reminders(self):
        self.transport.updates.side_effect = lambda offset: self.release.wait(2) and [] or []
        ticks = []

        def tick(**kwargs):
            ticks.append(time.monotonic())
            if len(ticks) == 3:
                self.stop.set()

        self.engine.tick.side_effect = tick
        started = time.monotonic()
        try:
            run_loop(self.service, self.bot, self.engine, self.transport, 7, self.stop, interval=0.02)
        finally:
            self.release.set()
        self.assertEqual(len(ticks), 3)
        self.assertLess(ticks[-1] - started, 0.5)
        self.service.db.execute.assert_not_called()

    def test_offset_is_not_acknowledged_until_update_succeeds(self):
        offsets = []
        update = {"update_id": 8}

        def poll(offset):
            offsets.append(offset)
            if len(offsets) == 1:
                return [update]
            self.stop.set()
            return []

        self.transport.updates.side_effect = poll
        self.bot.handle.side_effect = [TransportError(503), None]
        run_loop(self.service, self.bot, self.engine, self.transport, 7, self.stop, interval=0.01)
        self.assertEqual(offsets, [7, 9])
        self.assertEqual(self.bot.handle.call_count, 2)
        self.assertGreater(self.engine.tick.call_count, 2)
        self.assertEqual(self.service.db.execute.call_count, 2)  # error metric + committed offset

    def test_poll_authentication_error_is_fatal_and_redacted(self):
        self.transport.updates.side_effect = TransportError(401)
        with self.assertRaisesRegex(RuntimeError, "authentication/polling conflict"):
            run_loop(self.service, self.bot, self.engine, self.transport, 0, self.stop, interval=0.01)

    def test_poll_network_backoff_does_not_pause_scheduler(self):
        self.transport.updates.side_effect = TransportError(503)
        ticks = []

        def tick(**kwargs):
            ticks.append(time.monotonic())
            if len(ticks) >= 4:
                self.stop.set()

        self.engine.tick.side_effect = tick
        run_loop(self.service, self.bot, self.engine, self.transport, 0, self.stop, interval=0.01)
        self.assertEqual(len(ticks), 4)
        self.assertLess(ticks[-1] - ticks[0], 0.5)

    def test_real_reminder_deadline_during_idle_poll(self):
        db = DB(":memory:")
        service = Service(db)
        service.user(1)
        service.settings(1, tz="UTC", boundary="00:00", quiet_start="00:00", quiet_end="00:00")
        hid = service.create(1, {"title": "Timing check", "reminders": []})
        transport = FakeTransport()
        sent_at = []
        original_send = transport.send

        def send(*args):
            result = original_send(*args)
            if "Timing check" in args[1]:
                sent_at.append(datetime.now(timezone.utc))
                self.stop.set()
            return result

        poll = Mock()
        poll.updates.side_effect = lambda offset: self.release.wait(2) and [] or []
        transport.send = send
        engine = ReminderEngine(service, transport)
        deadline = service.now() + timedelta(seconds=0.2)
        engine._job(1, "timing-check", deadline, service.day(1), "reminder", {"hid": hid})
        # Fail boundedly if a regression prevents dispatch; no user messages or network.
        watchdog = threading.Timer(2, self.stop.set)
        watchdog.start()
        try:
            run_loop(service, self.bot, engine, poll, 0, self.stop)
            self.assertEqual(len(sent_at), 1)
            lateness = (sent_at[0] - deadline).total_seconds()
            self.assertGreaterEqual(lateness, 0)
            self.assertLess(lateness, 0.75)
            self.assertEqual(db.one("SELECT state FROM jobs WHERE id='timing-check'")[0], "sent")
        finally:
            self.release.set()
            watchdog.cancel()
            db.close()


if __name__ == "__main__":
    unittest.main()
