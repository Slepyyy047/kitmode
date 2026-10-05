"""Durable outbox; one logical job survives DST, restart and timezone edits."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .core import Service, dump
from .schedules import in_quiet, personal_day, wall_instant
from .transport import TransportError

COPY = {
    "uk": {
        "reminder": "Час для твого плану 🐾",
        "private": "Приватна звичка",
        "done": "✅ Відмітити",
        "snooze": "⏰ +15 хв / інше",
        "skip": "↪️ Пропустити",
        "today": "📋 План на сьогодні",
        "summary": "🌙 Попередній підсумок: {done}/{total} завершено. Ще є час для маленького кроку.",
        "inactive": "Давно не бачилися. Продовжити план, змінити ціль чи призупинити нагадування? Обери у налаштуваннях — я зачекаю.",
        "calm": "Один посильний крок достатній.",
        "friendly": "Кіт тримає блокнот, а рішення — твоє.",
        "strict": "Звірся зі своїм планом. За потреби обери заздалегідь установлений мінімум.",
    },
    "ru": {
        "reminder": "Время для твоего плана 🐾",
        "private": "Личная привычка",
        "done": "✅ Отметить",
        "snooze": "⏰ +15 мин / другое",
        "skip": "↪️ Пропустить",
        "today": "📋 План на сегодня",
        "summary": "🌙 Предварительный итог: {done}/{total} завершено. Ещё есть время для маленького шага.",
        "inactive": "Давно не виделись. Продолжить план, изменить цель или приостановить напоминания? Выбери в настройках — я подожду.",
        "calm": "Одного посильного шага достаточно.",
        "friendly": "Кот держит блокнот, а решение — твоё.",
        "strict": "Сверься со своим планом. При необходимости выбери заранее установленный минимум.",
    },
    "en": {
        "reminder": "Time for your plan 🐾",
        "private": "Private habit",
        "done": "✅ Record",
        "snooze": "⏰ +15 min / other",
        "skip": "↪️ Skip",
        "today": "📋 Today’s plan",
        "summary": "🌙 Preliminary summary: {done}/{total} completed. There’s still time for a small step.",
        "inactive": "It's been a while. Continue, adjust your plan or pause reminders? Choose in settings — I'll wait.",
        "calm": "One manageable step is enough.",
        "friendly": "The cat has the notebook; you make the decision.",
        "strict": "Check your own plan. Use your previously agreed minimum when needed.",
    },
}


class ReminderEngine:
    MAX_AGE = timedelta(minutes=90)

    def __init__(self, service: Service, transport: Any) -> None:
        self.service, self.db, self.transport = service, service.db, transport

    def _job(self, uid: int, key: str, due: Any, day: str, kind: str, payload: dict[str, Any]) -> None:
        user = self.service.user(uid)
        local = due.astimezone(ZoneInfo(user["tz"]))
        if in_quiet(local.time(), user["quiet_start"], user["quiet_end"]):
            quiet_end = wall_instant(local.date(), user["quiet_end"], user["tz"])
            if quiet_end <= due:
                quiet_end = wall_instant(local.date() + timedelta(days=1), user["quiet_end"], user["tz"])
            due = quiet_end
        # Do not push a reminder past its personal-day boundary.
        valid_day = personal_day(due, user["tz"], user["boundary"]).isoformat() == day
        self.db.execute(
            "INSERT INTO jobs(id,user_id,due,day,kind,payload,state) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET due=excluded.due,state=excluded.state WHERE jobs.state='cancelled' AND jobs.attempts=0",
            (key, uid, due.isoformat(), day, kind, dump(payload), "pending" if valid_day else "expired"),
        )

    def plan(self) -> None:
        now = self.service.now()
        for row in self.db.all("SELECT id,data FROM users"):
            uid, user = row["id"], json.loads(row["data"])
            blocked = self.db.one("SELECT 1 FROM blocked_users WHERE owner=?", (uid,))
            if not user["tz"] or not user["reminders"] or user.get("blocked") or blocked:
                self.db.execute("UPDATE jobs SET state='cancelled' WHERE user_id=? AND state='pending'", (uid,))
                continue
            day = self.service.day(uid)
            self.db.execute(
                "UPDATE jobs SET state='expired' WHERE user_id=? AND day<>? AND state='pending'", (uid, day)
            )
            for habit in self.service.today(uid):
                hid, spec, record = habit["id"], habit["spec"], habit["record"]
                if record and record["status"] in ("full", "minimum", "skip", "fail"):
                    continue
                for hhmm in spec["reminders"]:
                    due = wall_instant(date.fromisoformat(day), hhmm, user["tz"], user["boundary"])
                    self._job(uid, f"remind:{hid}:{day}:{hhmm}", due, day, "reminder", {"hid": hid})
            sleep = wall_instant(date.fromisoformat(day), user["sleep"], user["tz"], user["boundary"])
            self._job(uid, f"summary:{uid}:{day}", sleep - timedelta(minutes=30), day, "summary", {})
            last_seen = self.service.now().fromisoformat(user["last_seen"])
            if now - last_seen >= timedelta(days=3) and not user["inactivity_sent"]:
                self._job(uid, f"inactive:{uid}:{user['last_seen']}", now, day, "inactive", {})

    def _buttons(self, uid: int, jobs: list[dict[str, Any]], text: dict[str, str]) -> list[list[dict[str, str]]]:
        rows = []
        for job in jobs[:8]:
            payload = json.loads(job["payload"])
            if "hid" not in payload:
                continue
            rows.append(
                [
                    {
                        "text": text[label],
                        "callback_data": "b:"
                        + self.service.button(uid, {"action": action, "hid": payload["hid"], "day": job["day"]}),
                    }
                    for label, action in (
                        ("done", "reminder_done"),
                        ("snooze", "reminder_snooze"),
                        ("skip", "reminder_skip"),
                    )
                ]
            )
        rows.append(
            [
                {
                    "text": text["today"],
                    "callback_data": "b:" + self.service.button(uid, {"action": "today", "once": False}),
                }
            ]
        )
        return rows

    def tick(self, *, plan: bool = True, max_batches: int = 20, cleanup: bool = True) -> int:
        if plan:
            self.plan()
        now = self.service.now()
        jobs = self.db.all(
            "SELECT * FROM jobs WHERE state='pending' AND due<=? ORDER BY due LIMIT 100", (now.isoformat(),)
        )
        batches: dict[int, list[dict[str, Any]]] = {}
        # Collapse a downtime backlog to the newest job per habit, then one chat message.
        latest: dict[tuple[int, str], dict[str, Any]] = {}
        for row in jobs:
            job = dict(row)
            uid = job["user_id"]
            user = self.service.user(uid)
            local = now.astimezone(ZoneInfo(user["tz"] or "UTC"))
            if in_quiet(local.time(), user["quiet_start"], user["quiet_end"]):
                continue
            if now - now.fromisoformat(job["due"]) > self.MAX_AGE:
                self.db.execute("UPDATE jobs SET state='expired' WHERE id=?", (job["id"],))
                continue
            payload = json.loads(job["payload"])
            if "hid" in payload:
                habit = self.service.habit(uid, payload["hid"])
                record = self.service._record(habit["id"], job["day"])
                if (
                    habit["archived"]
                    or self.service._paused(habit["id"], job["day"])
                    or (record and record["status"] in ("full", "minimum", "skip", "fail"))
                ):
                    self.db.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job["id"],))
                    continue
            key = (uid, payload.get("hid", job["kind"]))
            if key in latest:
                self.db.execute("UPDATE jobs SET state='expired' WHERE id=?", (latest[key]["id"],))
            latest[key] = job
        for job in latest.values():
            batches.setdefault(job["user_id"], []).append(job)
        sent = 0
        for uid, batch in list(batches.items())[:max_batches]:
            user = self.service.user(uid)
            copy = COPY[user["lang"]]
            lines = [copy["reminder"], copy[user["tone"]]]
            for job in batch:
                payload = json.loads(job["payload"])
                if job["kind"] == "reminder":
                    habit = self.service.habit(uid, payload["hid"])
                    lines.append("• " + (copy["private"] if habit["sensitive"] else habit["title"]))
                elif job["kind"] == "summary":
                    # Sensitive records do not enter even a notification summary's denominator.
                    all_habits = [h for h in self.service.habits(uid) if not h["sensitive"]]
                    done = total = 0
                    for habit in all_habits:
                        stats = self.service.stats(uid, job["day"], job["day"], habit["id"])
                        done += stats["completed"]
                        total += len(stats["calendar"])
                    lines.append(copy["summary"].format(done=done, total=total))
                else:
                    lines.append(copy["inactive"])
            try:
                self.transport.send(uid, "\n".join(lines), self._buttons(uid, batch, copy))
            except TransportError as exc:
                self.service.extras.record_error("reminder")
                if exc.code == 403:
                    self.service.settings(uid, reminders=False)
                for job in batch:
                    attempts = job["attempts"] + 1
                    state = "pending" if exc.code in (429, 500, 502, 503, 504) and attempts <= 5 else "failed"
                    delay = min(3600, max(exc.retry_after, 15 * 2 ** min(attempts, 6)))
                    self.db.execute(
                        "UPDATE jobs SET attempts=?,state=?,due=? WHERE id=?",
                        (attempts, state, (now + timedelta(seconds=delay)).isoformat(), job["id"]),
                    )
                continue
            sent += 1
            with self.db.transaction():
                for job in batch:
                    self.db.execute("UPDATE jobs SET state='sent' WHERE id=?", (job["id"],))
                    if job["kind"] == "inactive":
                        self.service.settings(uid, inactivity_sent=True)
                    elif job["kind"] == "reminder":
                        payload = json.loads(job["payload"])
                        spec = self.service.habit(uid, payload["hid"])["spec"]
                        # One opt-in repeat, not an unbounded chain.
                        if spec["repeat"] and not spec["independent"] and not job["id"].startswith("repeat:"):
                            self._job(
                                uid,
                                "repeat:" + job["id"],
                                now + timedelta(minutes=spec["repeat"]),
                                job["day"],
                                "reminder",
                                payload,
                            )
        # Expired UI tokens are operational metadata; remove only after expiry.
        if cleanup:
            self.db.execute("DELETE FROM buttons WHERE expires<?", ((now - timedelta(days=7)).isoformat(),))
        return sent
