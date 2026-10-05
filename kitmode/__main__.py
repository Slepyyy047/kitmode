"""python -m kitmode check|demo|run. No network in check or demo."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import secrets
import shutil
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from .core import Service
from .db import DB
from .extras import AIConfig
from .reminders import ReminderEngine
from .runtime import run_loop
from .telegram import Bot
from .transport import FakeTransport, TelegramAPI, TransportError


def load_env(path: str = ".env") -> None:
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key, value = key.strip(), value.strip()
        if key.replace("_", "").isalnum():
            os.environ.setdefault(key, value.strip("\"'"))


def config() -> AIConfig:
    allowed = (
        os.getenv("AI_ENABLED", "false").lower() == "true"
        and os.getenv("AI_SPENDING_APPROVED", "false").lower() == "true"
    )
    return AIConfig(
        enabled=allowed,
        key=os.getenv("OPENAI_API_KEY", ""),
        model=os.getenv("OPENAI_MODEL", ""),
        daily_limit=max(0, min(20, int(os.getenv("AI_DAILY_LIMIT", "3")))),
        timeout=10,
    )


@contextmanager
def scratch() -> Iterator[Path]:
    """Disposable synthetic files under our own working directory, never user data."""
    parent = (Path.cwd() / "data" / "scratch").resolve()
    folder = parent / secrets.token_hex(12)
    folder.mkdir(parents=True, exist_ok=False)
    try:
        yield folder
    finally:
        if folder.resolve().parent != parent or folder.is_symlink():
            raise RuntimeError("Scratch cleanup target changed")
        shutil.rmtree(folder)


@contextmanager
def single_process(db_path: str) -> Iterator[None]:
    """OS lock prevents duplicate schedulers; released by OS even after a crash."""
    lockpath = Path(db_path + ".lock")
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    with lockpath.open("a+b") as lock:
        lock.seek(0)
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl: Any = importlib.import_module("fcntl")
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("Another KitMode process is already using this database") from None
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                importlib.import_module("fcntl").flock(lock.fileno(), fcntl.LOCK_UN)


def check() -> None:
    ZoneInfo("Europe/Kyiv")
    with scratch() as folder:
        db = DB(Path(folder) / "check.sqlite3")
        Service(db)
        integrity = db.one("PRAGMA integrity_check")
        assert integrity and integrity[0] == "ok"
        db.close()
    print("Python, SQLite migrations and IANA timezone data: OK")
    print(
        "Telegram token configured: "
        + ("yes (not contacted)" if os.getenv("TELEGRAM_BOT_TOKEN") else "no (demo still works)")
    )
    print("AI network calls permitted: " + ("yes" if config().enabled else "no"))


def demo() -> dict[str, Any]:
    now = [datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)]
    with scratch() as folder:
        dbpath = Path(folder) / "demo.sqlite3"
        db = DB(dbpath)
        service = Service(db, clock=lambda: now[0])
        transport = FakeTransport()
        bot = Bot(service, transport)
        bot.handle(
            {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 101, "type": "private"},
                    "from": {"id": 101, "first_name": "Demo"},
                    "text": "/start",
                },
            }
        )
        service.settings(101, tz="UTC", sleep="23:00", quiet_start="23:00", quiet_end="08:00", onboarded=True)
        hid = service.create(
            101,
            {"title": "Read", "kind": "quantity", "target": 10, "minimum": 2, "unit": "pages", "reminders": ["09:00"]},
        )
        ReminderEngine(service, transport).tick()
        service.record(101, hid, "demo:done", amount=10)
        service.undo(101, "demo:done")
        service.record(101, hid, "demo:minimum", amount=2)
        before = service.stats(101)
        now[0] += timedelta(minutes=1)
        db.close()
        db = DB(dbpath)
        service = Service(db, clock=lambda: now[0])
        assert service.stats(101)["completed"] == before["completed"] == 1
        assert service.stats(101)["xp"] == before["xp"] == 10
        assert ReminderEngine(service, transport).tick() == 0
        summary = dict(
            mode="SIMULATED; no Telegram or AI request",
            habit="Read",
            minimum=before["minimum"],
            completed=before["completed"],
            xp=before["xp"],
            restart="persisted",
            undo="verified",
            reminders="one, cancelled after completion",
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        db.close()
        return summary


def run() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise ValueError("Set TELEGRAM_BOT_TOKEN locally in .env; run `python -m kitmode demo` without it")
    dbpath = os.getenv("KITMODE_DB", "data/kitmode.sqlite3")
    admins = tuple(int(value.strip()) for value in os.getenv("ADMIN_IDS", "").split(",") if value.strip())
    with single_process(dbpath):
        db = DB(dbpath)
        try:
            service = Service(db)
            transport = TelegramAPI(token)
            identity = transport.me()
            configured_bot = db.one("SELECT value FROM system WHERE key='bot_id'")
            if configured_bot and configured_bot["value"] != str(identity["id"]):
                raise RuntimeError("This database belongs to a different bot. Set a separate KITMODE_DB path.")
            db.execute("INSERT OR IGNORE INTO system VALUES('bot_id',?)", (str(identity["id"]),))
            bot = Bot(service, transport, admins, config())
            engine = ReminderEngine(service, transport)
            row = db.one("SELECT value FROM system WHERE key='poll_offset'")
            offset = int(row["value"]) if row else 0
            print("KitMode running. Private chats only. Press Ctrl+C to stop.")
            run_loop(service, bot, engine, transport, offset)
        finally:
            db.close()


def profile() -> None:
    from .branding import apply_profile

    avatar = Path(__file__).resolve().parent.parent / "assets" / "kitmode-avatar.jpg"
    if not avatar.is_file():
        raise ValueError("The generated avatar is missing from assets/kitmode-avatar.jpg")
    api = TelegramAPI(os.getenv("TELEGRAM_BOT_TOKEN", ""))
    identity = api.me()
    result = apply_profile(api, avatar)
    print(json.dumps({"bot": "@" + identity["username"], **result}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="KitMode Telegram habit coach")
    parser.add_argument("command", choices=("run", "demo", "check", "profile"), default="check", nargs="?")
    args = parser.parse_args()
    load_env()
    try:
        {"run": run, "demo": demo, "check": check, "profile": profile}[args.command]()
    except KeyboardInterrupt:
        print("KitMode stopped. Persistent data retained.")
    except (ValueError, RuntimeError, TransportError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
