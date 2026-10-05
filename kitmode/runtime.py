"""Long polling never owns SQLite or blocks the reminder clock."""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from queue import Empty, Full, Queue
from typing import Any

from .transport import TransportError


def run_loop(
    service: Any,
    bot: Any,
    engine: Any,
    transport: Any,
    offset: int,
    stop: threading.Event | None = None,
    *,
    interval: float = 0.5,
) -> None:
    stop = stop if stop is not None else threading.Event()
    batches: Queue[list[dict[str, Any]] | TransportError] = Queue(maxsize=1)
    acknowledgements: Queue[int] = Queue(maxsize=1)

    def publish(item: list[dict[str, Any]] | TransportError) -> None:
        while not stop.is_set():
            try:
                batches.put(item, timeout=interval)
                return
            except Full:
                continue

    def poll() -> None:
        committed = offset
        failures = 0
        while not stop.is_set():
            try:
                batch = transport.updates(committed)
                failures = 0
                if not batch:
                    continue
                publish(batch)
                # Telegram must never acknowledge an update before its DB commit.
                while not stop.is_set():
                    try:
                        committed = acknowledgements.get(timeout=interval)
                        break
                    except Empty:
                        continue
            except TransportError as exc:
                publish(exc)
                if exc.code in (401, 409):
                    return
                failures += 1
                stop.wait(max(exc.retry_after, min(32, 2 ** min(failures, 5))))

    def report(exc: TransportError) -> None:
        service.extras.record_error("telegram")
        if exc.code in (401, 409):
            raise RuntimeError(
                "Telegram authentication/polling conflict: check token, webhook and other bot processes"
            ) from None
        print(f"Telegram temporarily unavailable (code {exc.code}); retrying.", file=sys.stderr)
        service.db.execute(
            "INSERT INTO system VALUES('transport_errors','1') ON CONFLICT(key) "
            "DO UPDATE SET value=cast(cast(value AS INTEGER)+1 AS TEXT)"
        )

    reader = threading.Thread(target=poll, name="kitmode-poll", daemon=True)
    reader.start()
    print(f"Reminder heartbeat: {interval:g}s; polling reader ready.")
    pending: deque[dict[str, Any]] = deque()
    retry_at = next_tick = next_plan = next_cleanup = 0.0
    failures = 0
    try:
        while not stop.is_set():
            now = time.monotonic()
            if not pending:
                try:
                    item = batches.get(timeout=max(0, min(interval, next_tick - now)))
                except Empty:
                    item = []
                if isinstance(item, TransportError):
                    report(item)
                else:
                    pending.extend(item)
            if pending and time.monotonic() >= retry_at:
                try:
                    update = pending[0]
                    bot.handle(update)
                    offset = int(update["update_id"]) + 1
                    service.db.execute(
                        "INSERT INTO system VALUES('poll_offset',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(offset),),
                    )
                    pending.popleft()
                    if not pending:
                        acknowledgements.put(offset)
                    failures = 0
                    next_plan = next_tick = 0.0
                except TransportError as exc:
                    report(exc)
                    failures += 1
                    retry_at = time.monotonic() + max(exc.retry_after, min(32, 2 ** min(failures, 5)))
            now = time.monotonic()
            if now >= next_tick:
                engine.tick(plan=now >= next_plan, max_batches=1, cleanup=now >= next_cleanup)
                if now >= next_plan:
                    next_plan = now + 1
                if now >= next_cleanup:
                    next_cleanup = now + 3600
                next_tick = time.monotonic() + interval
            if pending:
                # Fairness: one update and at most one reminder chat per iteration.
                stop.wait(min(interval, max(0, retry_at - time.monotonic())))
    finally:
        stop.set()
        reader.join(timeout=interval)
