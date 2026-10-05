"""Habit rules, immutable historical versions, idempotency and real statistics."""

from __future__ import annotations

import json
import math
import secrets
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import DB
from .schedules import dates, in_quiet, monday, personal_day, scheduled


class DomainError(ValueError):
    def __init__(self, key: str = "error_input") -> None:
        super().__init__(key)
        self.key = key


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def number(value: Any, positive: bool = False) -> float:
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise DomainError() from None
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise DomainError()
    return result


def hhmm(value: Any) -> str:
    try:
        parsed = time.fromisoformat(str(value))
    except ValueError:
        raise DomainError() from None
    if parsed.tzinfo or parsed.second or parsed.microsecond or len(str(value)) != 5:
        raise DomainError()
    return parsed.strftime("%H:%M")


class Service:
    def __init__(self, db: DB, clock: Callable[[], datetime] | None = None) -> None:
        self.db = db
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        from .extras import Extras

        self.extras = Extras(self)

    def now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("Clock must return an aware UTC instant")
        return now.astimezone(timezone.utc)

    def user(self, uid: int, name: str = "") -> dict[str, Any]:
        row = self.db.one("SELECT data FROM users WHERE id=?", (uid,))
        if row:
            return json.loads(row["data"])
        data = dict(
            lang="uk",
            name=name[:80],
            tz=None,
            sleep="23:00",
            boundary="04:00",
            quiet_start="23:00",
            quiet_end="08:00",
            tone="friendly",
            gamification=True,
            reminders=True,
            ai_consent=False,
            ai_sensitive=False,
            last_seen=self.now().isoformat(),
            inactivity_sent=False,
            blocked=False,
        )
        self.db.execute("INSERT INTO users VALUES(?,?,?)", (uid, dump(data), self.now().isoformat()))
        return data

    def settings(self, uid: int, **changes: Any) -> dict[str, Any]:
        with self.db.transaction():
            data = self.user(uid)
            allowed = set(data) | {"onboarded", "alternative", "support_contact"}
            if set(changes) - allowed:
                raise DomainError()
            for key, value in changes.items():
                if key in ("sleep", "boundary", "quiet_start", "quiet_end"):
                    changes[key] = hhmm(value)
                elif key == "tz" and value:
                    try:
                        ZoneInfo(value)
                    except (ValueError, TypeError, ZoneInfoNotFoundError):
                        raise DomainError("error_timezone") from None
                elif key == "lang" and value not in ("uk", "ru", "en"):
                    raise DomainError()
                elif key == "tone" and value not in ("calm", "friendly", "strict"):
                    raise DomainError()
                elif (
                    key
                    in (
                        "gamification",
                        "reminders",
                        "ai_consent",
                        "ai_sensitive",
                        "blocked",
                        "inactivity_sent",
                        "onboarded",
                    )
                    and type(value) is not bool
                ):
                    raise DomainError()
                elif key in ("name", "alternative", "support_contact"):
                    if not isinstance(value, str) or len(value) > 200:
                        raise DomainError()
            if changes.get("ai_sensitive") and not changes.get("ai_consent", data.get("ai_consent")):
                raise DomainError()
            if changes.get("ai_consent") is False:
                changes["ai_sensitive"] = False
            data.update(changes)
            self.db.execute(
                "UPDATE users SET data=?,updated_at=? WHERE id=?", (dump(data), self.now().isoformat(), uid)
            )
            if set(changes) & {"tz", "sleep", "boundary", "quiet_start", "quiet_end", "reminders"}:
                # Cancel, don't delete: unique notification identities survive settings changes.
                self.db.execute("UPDATE jobs SET state='cancelled' WHERE user_id=? AND state='pending'", (uid,))
            return data

    def day(self, uid: int) -> str:
        user = self.user(uid)
        return personal_day(self.now(), user["tz"], user["boundary"]).isoformat()

    def _validate(self, spec: dict[str, Any]) -> dict[str, Any]:
        default: dict[str, Any] = dict(
            kind="binary",
            target=1,
            unit="",
            minimum=None,
            minimum_description="",
            description="",
            category="",
            icon="🐾",
            routine="",
            sensitive=False,
            private_title="Private habit",
            verify="none",
            reminders=[],
            repeat=0,
            independent=False,
            end=None,
            schedule={"type": "daily"},
        )
        result = default | spec
        for field, length in (
            ("title", 100),
            ("description", 500),
            ("minimum_description", 500),
            ("category", 80),
            ("unit", 40),
            ("icon", 12),
            ("routine", 80),
            ("private_title", 100),
        ):
            if not isinstance(result.get(field), str) or len(result[field]) > length:
                raise DomainError()
        if not result["title"].strip():
            raise DomainError()
        if result["kind"] not in ("binary", "quantity", "limit") or result["verify"] not in (
            "none",
            "text",
            "photo",
            "timer",
            "friend",
        ):
            raise DomainError()
        for field in ("sensitive", "independent"):
            if type(result[field]) is not bool:
                raise DomainError()
        if result["sensitive"] and result["verify"] in ("photo", "friend"):
            raise DomainError("error_sensitive")
        result["target"] = 1.0 if result["kind"] == "binary" else number(result["target"], result["kind"] == "quantity")
        if result["minimum"] is not None:
            result["minimum"] = number(result["minimum"], result["kind"] != "limit")
            if (
                (result["kind"] == "binary" and (not result["minimum_description"].strip() or result["minimum"] >= 1))
                or (result["kind"] == "quantity" and result["minimum"] > result["target"])
                or (result["kind"] == "limit" and result["minimum"] < result["target"])
            ):
                raise DomainError()
        try:
            start = date.fromisoformat(result["start"])
            end = date.fromisoformat(result["end"]) if result["end"] else None
            if end and end < start:
                raise ValueError()
            rule = result["schedule"]
            if not isinstance(rule, dict) or rule["type"] not in ("daily", "weekdays", "weekly", "interval"):
                raise ValueError()
            if rule["type"] == "weekdays" and (
                not rule.get("days") or any(type(d) is not int or not 0 <= d <= 6 for d in rule["days"])
            ):
                raise ValueError()
            if rule["type"] in ("weekly", "interval") and (
                type(rule.get("n")) is not int or not 1 <= rule["n"] <= (7 if rule["type"] == "weekly" else 365)
            ):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise DomainError() from None
        if not isinstance(result["reminders"], list) or len(result["reminders"]) > 24:
            raise DomainError()
        result["reminders"] = sorted(set(hhmm(value) for value in result["reminders"]))
        if type(result["repeat"]) is not int or result["repeat"] not in (0, 15, 30, 60, 120):
            raise DomainError()
        return result

    def create(self, uid: int, spec: dict[str, Any]) -> str:
        self.user(uid)
        spec = self._validate(dict(start=self.day(uid)) | spec)
        hid = secrets.token_hex(6)
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO habits VALUES(?,?,?,?,0,?)",
                (hid, uid, spec["title"], int(spec["sensitive"]), self.now().isoformat()),
            )
            self.db.execute("INSERT INTO versions VALUES(?,?,?)", (hid, spec["start"], dump(spec)))
        return hid

    def habit(self, uid: int, hid: str, day: str | None = None) -> dict[str, Any]:
        row = self.db.one("SELECT * FROM habits WHERE id=? AND user_id=?", (hid, uid))
        if not row:
            raise DomainError("error_access")
        version = self.db.one(
            "SELECT spec FROM versions WHERE habit_id=? AND effective<=? ORDER BY effective DESC LIMIT 1",
            (hid, day or self.day(uid)),
        )
        if not version:
            version = self.db.one("SELECT spec FROM versions WHERE habit_id=? ORDER BY effective LIMIT 1", (hid,))
        assert version
        spec = json.loads(version["spec"])
        return dict(row) | {"spec": spec, "title": spec["title"], "sensitive": bool(spec["sensitive"])}

    def habits(self, uid: int, include_archived: bool = False) -> list[dict[str, Any]]:
        sql = (
            "SELECT id FROM habits WHERE user_id=?"
            + ("" if include_archived else " AND archived=0")
            + " ORDER BY created_at,id"
        )
        return [self.habit(uid, row["id"]) for row in self.db.all(sql, (uid,))]

    def preview_edit(self, uid: int, hid: str, changes: dict[str, Any], effective: str | None = None) -> dict[str, Any]:
        current = self.habit(uid, hid)
        today = date.fromisoformat(self.day(uid))
        tomorrow = today + timedelta(days=1)
        if current["spec"]["schedule"]["type"] == "weekly" or changes.get("schedule", {}).get("type") == "weekly":
            tomorrow = monday(today) + timedelta(days=7)
        try:
            when = date.fromisoformat(effective) if effective else tomorrow
        except (ValueError, TypeError):
            raise DomainError() from None
        if when < tomorrow:
            raise DomainError("error_future")
        last = self.db.one(
            "SELECT spec FROM versions WHERE habit_id=? AND effective<=? ORDER BY effective DESC LIMIT 1",
            (hid, when.isoformat()),
        )
        assert last
        spec = self._validate(json.loads(last["spec"]) | changes)
        return {"spec": spec, "effective": when.isoformat()}

    def edit(self, uid: int, hid: str, changes: dict[str, Any], effective: str | None = None) -> dict[str, Any]:
        preview = self.preview_edit(uid, hid, changes, effective)
        spec, when = preview["spec"], date.fromisoformat(preview["effective"])
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO versions VALUES(?,?,?) ON CONFLICT(habit_id,effective) DO UPDATE SET spec=excluded.spec",
                (hid, when.isoformat(), dump(spec)),
            )
            self.db.execute(
                "UPDATE jobs SET state='cancelled' WHERE user_id=? AND day>=? AND state='pending' AND json_extract(payload,'$.hid')=?",
                (uid, when.isoformat(), hid),
            )
        return self.habit(uid, hid, when.isoformat()) | {"effective": when.isoformat()}

    def pause(self, uid: int, hid: str, start: str, end: str | None) -> None:
        self.habit(uid, hid)
        try:
            if date.fromisoformat(start) < date.fromisoformat(self.day(uid)) or (
                end and date.fromisoformat(end) < date.fromisoformat(start)
            ):
                raise ValueError()
        except (TypeError, ValueError):
            raise DomainError("error_future") from None
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO pauses VALUES(?,?,?) ON CONFLICT(habit_id,start) DO UPDATE SET end=excluded.end",
                (hid, start, end),
            )
            self.db.execute(
                "UPDATE jobs SET state='cancelled' WHERE user_id=? AND day>=? AND (? IS NULL OR day<=?) AND state='pending' AND json_extract(payload,'$.hid')=?",
                (uid, start, end, end, hid),
            )

    def resume(self, uid: int, hid: str) -> None:
        self.habit(uid, hid)
        today = date.fromisoformat(self.day(uid))
        with self.db.transaction():
            self.db.execute(
                "UPDATE pauses SET end=? WHERE habit_id=? AND start<? AND (end IS NULL OR end>=?)",
                ((today - timedelta(days=1)).isoformat(), hid, today.isoformat(), today.isoformat()),
            )
            self.db.execute("DELETE FROM pauses WHERE habit_id=? AND start>=?", (hid, today.isoformat()))

    def archive(self, uid: int, hid: str) -> None:
        self.habit(uid, hid)
        with self.db.transaction():
            self.db.execute("UPDATE habits SET archived=1 WHERE id=?", (hid,))
            self.db.execute("INSERT OR IGNORE INTO archives VALUES(?,?)", (hid, self.day(uid)))
            self.db.execute(
                "UPDATE jobs SET state='cancelled' WHERE user_id=? AND state='pending' AND json_extract(payload,'$.hid')=?",
                (uid, hid),
            )

    def _paused(self, hid: str, day: str) -> bool:
        return bool(
            self.db.one(
                "SELECT 1 FROM pauses WHERE habit_id=? AND start<=? AND (end IS NULL OR end>=?)", (hid, day, day)
            )
        )

    def _record(self, hid: str, day: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM records WHERE habit_id=? AND day=?", (hid, day))
        return dict(row) if row else None

    def _weekly_done(self, hid: str, day: str) -> int:
        start = monday(date.fromisoformat(day)).isoformat()
        end = (monday(date.fromisoformat(day)) + timedelta(days=6)).isoformat()
        row = self.db.one(
            "SELECT count(*) AS n FROM records WHERE habit_id=? AND day BETWEEN ? AND ? AND status IN ('full','minimum')",
            (hid, start, end),
        )
        return int(row["n"]) if row else 0

    def today(self, uid: int) -> list[dict[str, Any]]:
        day = self.day(uid)
        result = []
        for habit in self.habits(uid):
            spec = habit["spec"]
            if not scheduled(spec, date.fromisoformat(day)) or self._paused(habit["id"], day):
                continue
            if spec["schedule"]["type"] == "weekly" and self._weekly_done(habit["id"], day) >= spec["schedule"]["n"]:
                continue
            result.append(habit | {"record": self._record(habit["id"], day)})
        return result

    def record(
        self,
        uid: int,
        hid: str,
        action_key: str,
        status: str = "done",
        amount: Any = None,
        note: str = "",
        day: str | None = None,
        verification: str | None = None,
        evidence: Any = None,
    ) -> dict[str, Any]:
        day = day or self.day(uid)
        try:
            offset = (date.fromisoformat(self.day(uid)) - date.fromisoformat(day)).days
        except (ValueError, TypeError):
            raise DomainError() from None
        if not 0 <= offset <= 6:
            raise DomainError("error_old")
        if not isinstance(note, str) or len(note) > 1000 or not action_key or len(action_key) > 200:
            raise DomainError()
        with self.db.transaction():
            self.habit(uid, hid, day)
            existing_action = self.db.one(
                "SELECT after,habit_id,day FROM actions WHERE user_id=? AND key=?", (uid, action_key)
            )
            if existing_action:
                if existing_action["habit_id"] != hid or existing_action["day"] != day:
                    raise DomainError("error_stale")
                return json.loads(existing_action["after"])
            before = self._record(hid, day)
            spec = json.loads(before["snapshot"]) if before else self.habit(uid, hid, day)["spec"]
            archive = self.db.one("SELECT day FROM archives WHERE habit_id=?", (hid,))
            if (
                not scheduled(spec, date.fromisoformat(day))
                or (not before and self._paused(hid, day))
                or (not before and archive and day >= archive["day"])
            ):
                raise DomainError("error_stale")
            if status not in ("done", "minimum", "fail", "skip", "none"):
                raise DomainError()
            value = before["value"] if before else 0.0
            if status == "minimum":
                if spec["minimum"] is None or spec["kind"] == "limit":
                    raise DomainError()
                value = spec["minimum"] if spec["kind"] == "limit" else max(value, spec["minimum"])
                computed = "full" if spec["kind"] != "limit" and value >= spec["target"] else "minimum"
            elif status == "done":
                if spec["kind"] == "binary":
                    value, computed = 1.0, "full"
                elif spec["kind"] == "quantity":
                    value = number(value + number(amount, positive=True))
                    computed = (
                        "full"
                        if value >= spec["target"]
                        else ("minimum" if spec["minimum"] is not None and value >= spec["minimum"] else "progress")
                    )
                else:
                    if amount is None:
                        raise DomainError()
                    value = number(amount)
                    computed = (
                        "full"
                        if value <= spec["target"]
                        else ("minimum" if spec["minimum"] is not None and value <= spec["minimum"] else "fail")
                    )
            else:
                computed = status
                if status == "none":
                    value = 0.0
            method = spec["verify"]
            verify = verification or ("none" if method == "none" else "pending")
            if verify not in ("none", "pending", "submitted", "approved", "rejected"):
                raise DomainError()
            # Friend approvals are exclusively written by the consent-checked Extras API.
            if method == "friend" and verify in ("approved", "rejected"):
                raise DomainError("error_access")
            if spec["sensitive"] and (method == "photo" or (isinstance(evidence, dict) and "file_id" in evidence)):
                raise DomainError("error_sensitive")
            if verify == "submitted" and (
                (method == "text" and not note.strip())
                or (method == "photo" and not evidence)
                or method in ("none", "friend")
            ):
                raise DomainError()
            result = dict(
                habit_id=hid,
                day=day,
                status=computed,
                value=value,
                note=note,
                verification=verify,
                evidence=dump(evidence or {}),
                snapshot=dump(spec),
                updated_at=self.now().isoformat(),
            )
            self.db.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(habit_id,day) DO UPDATE SET status=excluded.status,value=excluded.value,note=excluded.note,verification=excluded.verification,evidence=excluded.evidence,snapshot=excluded.snapshot,updated_at=excluded.updated_at",
                tuple(result.values()),
            )
            self.db.execute(
                "INSERT INTO actions VALUES(?,?,?,?,?,?,?,?)",
                (uid, action_key, "record", hid, day, dump(before), dump(result), self.now().isoformat()),
            )
            if computed in ("full", "minimum"):
                self.db.execute(
                    "UPDATE jobs SET state='cancelled' WHERE user_id=? AND day=? AND state='pending' AND json_extract(payload,'$.hid')=?",
                    (uid, day, hid),
                )
                opportunity = day
                eligible = True
                if spec["schedule"]["type"] == "weekly":
                    opportunity = "week:" + monday(date.fromisoformat(day)).isoformat()
                    eligible = self._weekly_done(hid, day) >= spec["schedule"]["n"]
                if eligible and self.user(uid)["gamification"]:
                    self.db.execute("INSERT OR IGNORE INTO rewards VALUES(?,?,?,?)", (hid, opportunity, uid, 10))
            return result

    def correct_record(
        self,
        uid: int,
        hid: str,
        action_key: str,
        status: str = "done",
        amount: Any = None,
        note: str = "",
        day: str | None = None,
    ) -> dict[str, Any]:
        """Replace an absolute historical total; the ordinary record API adds increments."""
        day = day or self.day(uid)
        with self.db.transaction():
            self.habit(uid, hid, day)
            action = self.db.one("SELECT habit_id,day,after FROM actions WHERE user_id=? AND key=?", (uid, action_key))
            if action:
                if action["habit_id"] != hid or action["day"] != day:
                    raise DomainError("error_stale")
                return json.loads(action["after"])
            before = self._record(hid, day)
            spec = self.habit(uid, hid, day)["spec"]
            zero_quantity = status == "done" and spec["kind"] == "quantity" and number(amount) == 0
            if before and status == "done" and spec["kind"] == "quantity":
                self.db.execute("UPDATE records SET value=0 WHERE habit_id=? AND day=?", (hid, day))
            result = self.record(uid, hid, action_key, "none" if zero_quantity else status, amount, note, day)
            if zero_quantity or (status in ("fail", "skip") and amount is not None and spec["kind"] != "binary"):
                result["value"] = number(amount)
                if zero_quantity:
                    result["status"] = "progress"
                self.db.execute(
                    "UPDATE records SET value=?,status=? WHERE habit_id=? AND day=?",
                    (result["value"], result["status"], hid, day),
                )
                self.db.execute("UPDATE actions SET after=? WHERE user_id=? AND key=?", (dump(result), uid, action_key))
            self.db.execute("UPDATE actions SET before=? WHERE user_id=? AND key=?", (dump(before), uid, action_key))
            return result

    def evidence(
        self,
        uid: int,
        hid: str,
        method: str,
        note: str = "",
        file_id: str | None = None,
        day: str | None = None,
        seconds: int | None = None,
    ) -> dict[str, Any]:
        """Attach a factual verification method without changing self-report or XP."""
        day = day or self.day(uid)
        with self.db.transaction():
            habit = self.habit(uid, hid, day)
            record = self._record(hid, day)
            offset = (date.fromisoformat(self.day(uid)) - date.fromisoformat(day)).days
            if not record or not 0 <= offset <= 6 or record["status"] not in ("full", "minimum", "progress"):
                raise DomainError("error_stale")
            spec = json.loads(record["snapshot"])
            if method not in ("text", "photo", "timer") or method != spec["verify"]:
                raise DomainError()
            if (spec["sensitive"] or habit["sensitive"]) and method == "photo":
                raise DomainError("error_sensitive")
            data: dict[str, Any] = {"method": method}
            if method == "text":
                if not isinstance(note, str) or not note.strip() or len(note) > 1000:
                    raise DomainError()
            elif method == "photo":
                if not file_id or len(file_id) > 300:
                    raise DomainError()
                data["file_id"] = file_id
            else:
                if type(seconds) is not int or seconds < 0:
                    raise DomainError()
                data["seconds"] = seconds
            self.db.execute(
                "UPDATE records SET verification='submitted',evidence=?,note=?,updated_at=? WHERE habit_id=? AND day=?",
                (dump(data), note or record["note"], self.now().isoformat(), hid, day),
            )
            updated = self._record(hid, day)
            assert updated
            return updated

    def undo(self, uid: int, action_key: str) -> None:
        with self.db.transaction():
            action = self.db.one("SELECT * FROM actions WHERE user_id=? AND key=?", (uid, action_key))
            if not action or action["kind"] != "record":
                raise DomainError("error_stale")
            self.habit(uid, action["habit_id"])
            if (date.fromisoformat(self.day(uid)) - date.fromisoformat(action["day"])).days > 6:
                raise DomainError("error_old")
            latest = self.db.one(
                "SELECT key FROM actions WHERE user_id=? AND habit_id=? AND day=? AND kind='record' ORDER BY rowid DESC LIMIT 1",
                (uid, action["habit_id"], action["day"]),
            )
            current = self._record(action["habit_id"], action["day"])
            after = json.loads(action["after"])
            fields = ("habit_id", "day", "status", "value", "snapshot")
            if not latest or latest["key"] != action_key or not current or any(current[k] != after[k] for k in fields):
                raise DomainError("error_stale")
            before = json.loads(action["before"])
            self.db.execute("DELETE FROM records WHERE habit_id=? AND day=?", (action["habit_id"], action["day"]))
            if before:
                self.db.execute(
                    "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)",
                    tuple(
                        before[k]
                        for k in (
                            "habit_id",
                            "day",
                            "status",
                            "value",
                            "note",
                            "verification",
                            "evidence",
                            "snapshot",
                            "updated_at",
                        )
                    ),
                )
            self.db.execute("UPDATE actions SET kind='undone' WHERE user_id=? AND key=?", (uid, action_key))
            # Earned rewards are retained, and the UNIQUE key prevents earning again.

    def note(self, uid: int, hid: str, note: str, day: str | None = None) -> None:
        day = day or self.day(uid)
        with self.db.transaction():
            self.habit(uid, hid, day)
            if not isinstance(note, str) or len(note) > 2000:
                raise DomainError()
            offset = (date.fromisoformat(self.day(uid)) - date.fromisoformat(day)).days
            if not 0 <= offset <= 6 or not self._record(hid, day):
                raise DomainError("error_old")
            self.db.execute(
                "UPDATE records SET note=?,updated_at=? WHERE habit_id=? AND day=?",
                (note, self.now().isoformat(), hid, day),
            )

    def bulk(self, uid: int, action_key: str) -> list[dict[str, Any]]:
        result = []
        with self.db.transaction():
            for habit in self.today(uid):
                spec, rec = habit["spec"], habit["record"]
                if (
                    spec["kind"] == "limit"
                    or spec["verify"] != "none"
                    or (rec and rec["status"] in ("full", "minimum"))
                ):
                    continue
                needed = spec["target"] - (rec["value"] if rec else 0)
                result.append(self.record(uid, habit["id"], action_key + ":" + habit["id"], amount=needed))
        return result

    def history(self, uid: int, hid: str, days: int = 7) -> list[dict[str, Any]]:
        self.habit(uid, hid)
        end = date.fromisoformat(self.day(uid))
        return [
            dict(
                day=d.isoformat(),
                record=self._record(hid, d.isoformat()),
                scheduled=scheduled(self.habit(uid, hid, d.isoformat())["spec"], d),
                paused=self._paused(hid, d.isoformat()),
            )
            for d in dates(end - timedelta(days=min(7, max(1, days)) - 1), end)
        ]

    def _opportunities(self, uid: int, habit: dict[str, Any], start: date, end: date) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        weekly: dict[str, list[tuple[date, dict[str, Any], dict[str, Any] | None]]] = {}
        today = date.fromisoformat(self.day(uid))
        archive = self.db.one("SELECT day FROM archives WHERE habit_id=?", (habit["id"],))
        for d in dates(start, min(end, today)):
            day = d.isoformat()
            record = self._record(habit["id"], day)
            spec = json.loads(record["snapshot"]) if record else self.habit(uid, habit["id"], day)["spec"]
            if not scheduled(spec, d) or (not record and self._paused(habit["id"], day)):
                continue
            if not record and archive and day >= archive["day"]:
                continue
            if spec["schedule"]["type"] == "weekly":
                weekly.setdefault(monday(d).isoformat(), []).append((d, spec, record))
            else:
                status = record["status"] if record else "missing"
                result.append(
                    dict(
                        day=day,
                        status=status,
                        value=record["value"] if record else 0,
                        closed=d < today or status in ("full", "minimum", "fail", "skip"),
                    )
                )
        for week, entries in weekly.items():
            # A window touching a week includes its full eligible week for quota assessment.
            weekstart = date.fromisoformat(week)
            spec = self.habit(uid, habit["id"], entries[0][0].isoformat())["spec"]
            eligible_days = [
                d
                for d in dates(weekstart, weekstart + timedelta(days=6))
                if scheduled(self.habit(uid, habit["id"], d.isoformat())["spec"], d)
                and (not self._paused(habit["id"], d.isoformat()) or self._record(habit["id"], d.isoformat()))
                and (not archive or d.isoformat() < archive["day"] or self._record(habit["id"], d.isoformat()))
            ]
            target = min(spec["schedule"]["n"], len(eligible_days))
            if not target:
                continue
            records = [self._record(habit["id"], d.isoformat()) for d in eligible_days if d <= today]
            full = sum(1 for r in records if r and r["status"] == "full")
            minimum = sum(1 for r in records if r and r["status"] == "minimum")
            closed = weekstart + timedelta(days=6) < today or full + minimum >= target
            status = "full" if full >= target else ("minimum" if full + minimum >= target else "missing")
            result.append(
                dict(day=week, status=status, value=sum(r["value"] for r in records if r), closed=closed, weekly=True)
            )
        return sorted(result, key=lambda value: value["day"])

    def stats(
        self, uid: int, start: str | None = None, end: str | None = None, hid: str | None = None
    ) -> dict[str, Any]:
        today = date.fromisoformat(self.day(uid))
        try:
            last = min(date.fromisoformat(end), today) if end else today
            first = date.fromisoformat(start) if start else last - timedelta(days=29)
        except (ValueError, TypeError):
            raise DomainError() from None
        if first > last or (last - first).days > 3660:
            raise DomainError()
        habits = [self.habit(uid, hid)] if hid else self.habits(uid, True)
        counts: dict[str, Any] = dict(
            completed=0,
            full=0,
            minimum=0,
            fail=0,
            skip=0,
            missing=0,
            opportunities=0,
            quantity=0.0,
            quantity_by_unit={},
            calendar=[],
        )
        previous_completed = previous_total = 0
        current_streak = best_streak = 0
        independence = []
        for habit in habits:
            items = self._opportunities(uid, habit, first, last)
            for item in items:
                counts["calendar"].append(
                    item | {"habit_id": habit["id"], "title": habit["title"], "sensitive": habit["sensitive"]}
                )
                counts["quantity"] += item["value"]
                if not item["closed"]:
                    continue
                counts["opportunities"] += 1
                state = item["status"]
                if state in ("full", "minimum"):
                    counts[state] += 1
                    counts["completed"] += 1
                elif state in ("fail", "skip"):
                    counts[state] += 1
                else:
                    counts["missing"] += 1
            for record in self.db.all(
                "SELECT value,snapshot FROM records WHERE habit_id=? AND day BETWEEN ? AND ?",
                (habit["id"], first.isoformat(), last.isoformat()),
            ):
                snapshot = json.loads(record["snapshot"])
                if snapshot["kind"] != "binary":
                    unit = snapshot["unit"]
                    counts["quantity_by_unit"][unit] = counts["quantity_by_unit"].get(unit, 0) + record["value"]
            span = (last - first).days + 1
            prior = self._opportunities(uid, habit, first - timedelta(days=span), first - timedelta(days=1))
            previous_total += sum(int(i["closed"]) for i in prior)
            previous_completed += sum(int(i["closed"] and i["status"] in ("full", "minimum")) for i in prior)
            first_version = self.db.one("SELECT min(effective) AS d FROM versions WHERE habit_id=?", (habit["id"],))
            assert first_version
            inception = date.fromisoformat(first_version["d"])
            all_items = self._opportunities(uid, habit, inception, today)
            run = best = 0
            stable_run = 0
            weekly_rule = habit["spec"]["schedule"]["type"] == "weekly"
            for item in all_items:
                if not item["closed"]:
                    continue
                run = run + 1 if item["status"] in ("full", "minimum") else 0
                stable_run = (
                    stable_run + 1
                    if item["status"] in ("full", "minimum") and bool(item.get("weekly")) == weekly_rule
                    else 0
                )
                best = max(best, run)
            current_streak = max(current_streak, run)
            best_streak = max(best_streak, best)
            required = 4 if habit["spec"]["schedule"]["type"] == "weekly" else 8
            if stable_run >= required and not habit["spec"]["independent"] and not habit["archived"]:
                independence.append(habit["id"])
        xp_row = self.db.one("SELECT coalesce(sum(xp),0) AS xp,count(*) AS n FROM rewards WHERE user_id=?", (uid,))
        assert xp_row
        xp = int(xp_row["xp"])
        return counts | dict(
            start=first.isoformat(),
            end=last.isoformat(),
            rate=counts["completed"] / counts["opportunities"] if counts["opportunities"] else 0.0,
            previous_rate=previous_completed / previous_total if previous_total else 0.0,
            current_streak=current_streak,
            best_streak=best_streak,
            xp=xp,
            level=1 + xp // 100,
            badges=[
                name
                for threshold, name in ((1, "first"), (8, "steady"), (30, "consistent"))
                if xp_row["n"] >= threshold
            ],
            independence=independence,
        )

    def session(self, uid: int, data: dict[str, Any] | None = None) -> dict[str, Any]:
        if data is None:
            row = self.db.one("SELECT data FROM sessions WHERE user_id=?", (uid,))
            return json.loads(row["data"]) if row else {}
        self.db.execute(
            "INSERT INTO sessions VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data", (uid, dump(data))
        )
        return data

    def snooze(self, uid: int, hid: str, minutes: int = 15, day: str | None = None) -> None:
        habit = self.habit(uid, hid)
        if minutes not in (15, 30, 60) or (day and day != self.day(uid)):
            raise DomainError("error_stale")
        if not self.user(uid)["tz"]:
            raise DomainError("error_timezone")
        due = self.now() + timedelta(minutes=minutes)
        if in_quiet(
            due.astimezone(ZoneInfo(self.user(uid)["tz"])).time(),
            self.user(uid)["quiet_start"],
            self.user(uid)["quiet_end"],
        ):
            raise DomainError("error_limit")
        if personal_day(due, self.user(uid)["tz"], self.user(uid)["boundary"]).isoformat() != self.day(uid):
            raise DomainError("error_old")
        job_id = "snooze:" + hid + ":" + self.day(uid)
        self.db.execute(
            "INSERT INTO jobs(id,user_id,due,day,kind,payload) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET due=excluded.due,state='pending'",
            (job_id, uid, due.isoformat(), self.day(uid), "reminder", dump({"hid": habit["id"]})),
        )

    def button(self, uid: int, payload: dict[str, Any], ttl: int = 86400) -> str:
        token = secrets.token_urlsafe(12)
        self.db.execute(
            "INSERT INTO buttons VALUES(?,?,?,?,0)",
            (token, uid, dump(payload), (self.now() + timedelta(seconds=ttl)).isoformat()),
        )
        return token

    def consume_button(self, uid: int, token: str) -> dict[str, Any]:
        with self.db.transaction():
            row = self.db.one("SELECT * FROM buttons WHERE token=? AND user_id=?", (token, uid))
            if not row or row["used"] or row["expires"] < self.now().isoformat():
                raise DomainError("error_stale")
            data: dict[str, Any] = json.loads(row["payload"])
            if data.get("once", True):
                self.db.execute("UPDATE buttons SET used=1 WHERE token=?", (token,))
            return data
