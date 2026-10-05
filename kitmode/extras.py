from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import secrets
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class AIConfig:
    enabled: bool = False
    key: str = ""
    model: str = ""
    daily_limit: int = 3
    timeout: float = 10


class Extras:
    """Privacy-aware persistence and optional integrations for KitMode."""

    MAX_IMPORT_BYTES = 1_000_000
    # Per-file safety bound for import parsing; the product has no habit-count cap.
    MAX_HABITS = 500
    MAX_RECORDS = 100_000
    INVITE_TTL = timedelta(days=7)
    AI_MAX_INPUT = 4_000
    AI_MAX_OUTPUT = 1_000

    def __init__(self, service: Any):
        self.service = service
        self.db = service.db
        if self.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='extras_schema'"):
            version = self.db.one("SELECT max(version) version FROM extras_schema")
            if version and version["version"] > 2:
                raise RuntimeError("Extra data schema is newer than this application")
        self.db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS extras_schema(version INTEGER NOT NULL);
        INSERT INTO extras_schema(version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM extras_schema);
        CREATE TABLE IF NOT EXISTS friends(
          id INTEGER PRIMARY KEY AUTOINCREMENT, a INTEGER NOT NULL, b INTEGER NOT NULL,
          created_at TEXT NOT NULL, UNIQUE(a,b), CHECK(a < b));
        CREATE TABLE IF NOT EXISTS invites(
          token_hash TEXT PRIMARY KEY, owner INTEGER NOT NULL, expires TEXT NOT NULL,
          used_by INTEGER, used_at TEXT);
        CREATE TABLE IF NOT EXISTS visibility(
          owner INTEGER NOT NULL, friend INTEGER NOT NULL, habit_id TEXT NOT NULL,
          visible INTEGER NOT NULL, PRIMARY KEY(owner,friend,habit_id));
        CREATE TABLE IF NOT EXISTS verifier_consent(
          owner INTEGER NOT NULL, friend INTEGER NOT NULL, enabled INTEGER NOT NULL,
          updated_at TEXT NOT NULL, PRIMARY KEY(owner,friend));
        CREATE TABLE IF NOT EXISTS verification_requests(
          id TEXT PRIMARY KEY, owner INTEGER NOT NULL, friend INTEGER NOT NULL,
          habit_id TEXT NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL,
          created_at TEXT NOT NULL, decided_at TEXT,
          UNIQUE(owner,habit_id,day));
        CREATE TABLE IF NOT EXISTS timers(
          owner INTEGER NOT NULL, habit_id TEXT NOT NULL, started_at TEXT NOT NULL,
          PRIMARY KEY(owner,habit_id));
        CREATE TABLE IF NOT EXISTS urges(
          id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL, habit_id TEXT NOT NULL,
          happened_at TEXT NOT NULL, intensity INTEGER NOT NULL, trigger_text TEXT NOT NULL,
          episode INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS feedback(
          id TEXT PRIMARY KEY, owner INTEGER NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open');
        CREATE TABLE IF NOT EXISTS blocked_users(owner INTEGER PRIMARY KEY, by_admin INTEGER NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS imports(owner INTEGER NOT NULL, digest TEXT NOT NULL, imported_at TEXT NOT NULL,
          PRIMARY KEY(owner,digest));
        CREATE TABLE IF NOT EXISTS challenges(
          id TEXT PRIMARY KEY, owner INTEGER NOT NULL, title TEXT NOT NULL, target INTEGER NOT NULL,
          start TEXT NOT NULL, end TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_usage(owner INTEGER NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL,
          PRIMARY KEY(owner,day));
        CREATE TABLE IF NOT EXISTS error_events(id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        if "signature" not in {row["name"] for row in self.db.all("PRAGMA table_info(verification_requests)")}:
            self.db.execute("ALTER TABLE verification_requests ADD COLUMN signature TEXT")
        self.db.execute("UPDATE extras_schema SET version=2")

    def _err(self, key: str) -> Exception:
        try:
            from .core import DomainError

            return DomainError(key)
        except (ImportError, AttributeError):
            return ValueError(key)

    def _now(self) -> datetime:
        value = self.service.now()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _iso(self) -> str:
        return self._now().isoformat()

    def _habit(self, uid: int, hid: str) -> dict[str, Any]:
        return self.service.habit(uid, hid)

    def _friend(self, uid: int, fid: int) -> bool:
        return (
            self.db.one(
                "SELECT 1 FROM friends WHERE (a=? AND b=?) OR (a=? AND b=?)",
                (min(uid, fid), max(uid, fid), min(uid, fid), max(uid, fid)),
            )
            is not None
        )

    def invite(self, uid: int) -> str:
        self.service.user(uid)
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        expires = (self._now() + self.INVITE_TTL).isoformat()
        self.db.execute("INSERT INTO invites(token_hash,owner,expires) VALUES(?,?,?)", (digest, uid, expires))
        return token

    def accept_invite(self, uid: int, token: str) -> int:
        if not isinstance(token, str) or len(token) > 200:
            raise self._err("error_access")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db.transaction():
            row = self.db.one("SELECT owner,expires,used_by FROM invites WHERE token_hash=?", (digest,))
            if row is None or row["used_by"] is not None or datetime.fromisoformat(row["expires"]) <= self._now():
                raise self._err("error_old")
            owner = int(row["owner"])
            if owner == uid:
                raise self._err("error_access")
            self.service.user(uid)
            a, b = sorted((uid, owner))
            self.db.execute("INSERT OR IGNORE INTO friends(a,b,created_at) VALUES(?,?,?)", (a, b, self._iso()))
            self.db.execute(
                "UPDATE invites SET used_by=?,used_at=? WHERE token_hash=? AND used_by IS NULL",
                (uid, self._iso(), digest),
            )
            return owner

    def friends(self, uid: int) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT CASE WHEN a=? THEN b ELSE a END AS id FROM friends WHERE a=? OR b=? ORDER BY id", (uid, uid, uid)
        )
        out = []
        for row in rows:
            friend = int(row["id"])
            user = self.service.user(friend)
            out.append(
                {
                    "id": friend,
                    "name": user.get("name", ""),
                    "verifier_consent": bool(
                        (
                            self.db.one(
                                "SELECT enabled FROM verifier_consent WHERE owner=? AND friend=?", (uid, friend)
                            )
                            or {"enabled": 0}
                        )["enabled"]
                    ),
                }
            )
        return out

    def revoke(self, uid: int, friend_id: int) -> None:
        if not self._friend(uid, friend_id):
            raise self._err("error_access")
        a, b = sorted((uid, friend_id))
        with self.db.transaction():
            self.db.execute("DELETE FROM friends WHERE a=? AND b=?", (a, b))
            self.db.execute(
                "DELETE FROM visibility WHERE (owner=? AND friend=?) OR (owner=? AND friend=?)",
                (uid, friend_id, friend_id, uid),
            )
            self.db.execute(
                "DELETE FROM verifier_consent WHERE (owner=? AND friend=?) OR (owner=? AND friend=?)",
                (uid, friend_id, friend_id, uid),
            )
            self.db.execute(
                "DELETE FROM verification_requests WHERE (owner=? AND friend=?) OR (owner=? AND friend=?)",
                (uid, friend_id, friend_id, uid),
            )

    def visibility(self, uid: int, hid: str, friend_id: int, visible: bool) -> None:
        habit = self._habit(uid, hid)
        if not self._friend(uid, friend_id):
            raise self._err("error_access")
        if habit.get("sensitive"):
            raise self._err("error_sensitive")
        self.db.execute(
            "INSERT INTO visibility(owner,friend,habit_id,visible) VALUES(?,?,?,?) ON CONFLICT(owner,friend,habit_id) DO UPDATE SET visible=excluded.visible",
            (uid, friend_id, hid, int(bool(visible))),
        )

    def shared(self, viewer: int, owner: int) -> list[dict[str, Any]]:
        if not self._friend(viewer, owner):
            raise self._err("error_access")
        rows = self.db.all("SELECT habit_id FROM visibility WHERE owner=? AND friend=? AND visible=1", (owner, viewer))
        result = []
        for row in rows:
            try:
                h = self._habit(owner, row["habit_id"])
            except Exception:
                continue
            if h.get("sensitive"):
                continue
            rec = self.db.one(
                "SELECT day,status,value FROM records WHERE habit_id=? ORDER BY day DESC LIMIT 1", (h["id"],)
            )
            result.append({"id": h["id"], "title": h["title"], "latest": dict(rec) if rec else None})
        return result

    def consent_verifier(self, uid: int, friend_id: int, enabled: bool = True) -> None:
        if not self._friend(uid, friend_id):
            raise self._err("error_access")
        # The caller volunteers to verify the friend's records; an owner cannot
        # volunteer another person on their behalf.
        self.db.execute(
            "INSERT INTO verifier_consent(owner,friend,enabled,updated_at) VALUES(?,?,?,?) ON CONFLICT(owner,friend) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at",
            (friend_id, uid, int(enabled), self._iso()),
        )

    def request_verification(self, uid: int, hid: str, day: str, friend_id: int) -> str:
        h = self._habit(uid, hid)
        if h.get("sensitive"):
            raise self._err("error_sensitive")
        if not self._friend(uid, friend_id):
            raise self._err("error_access")
        consent = self.db.one("SELECT enabled FROM verifier_consent WHERE owner=? AND friend=?", (uid, friend_id))
        if not consent or not consent["enabled"]:
            raise self._err("error_access")
        r = self.db.one("SELECT status FROM records WHERE habit_id=? AND day=?", (hid, day))
        if r is None or r["status"] not in ("full", "minimum"):
            raise self._err("error_input")
        rid = secrets.token_urlsafe(18)
        try:
            self.db.execute(
                "INSERT INTO verification_requests(id,owner,friend,habit_id,day,status,created_at) VALUES(?,?,?,?,?,'pending',?)",
                (rid, uid, friend_id, hid, day, self._iso()),
            )
        except sqlite3.IntegrityError:
            prior = self.db.one(
                "SELECT id,friend,status FROM verification_requests WHERE owner=? AND habit_id=? AND day=?",
                (uid, hid, day),
            )
            if prior["friend"] != friend_id or prior["status"] != "pending":
                raise self._err("error_stale")
            return prior["id"]
        self.db.execute("UPDATE records SET verification='pending' WHERE habit_id=? AND day=?", (hid, day))
        self.db.execute(
            "UPDATE verification_requests SET signature=? WHERE id=?", (self._verification_signature(hid, day), rid)
        )
        return rid

    def _verification_signature(self, hid: str, day: str) -> str:
        record = self.db.one("SELECT status,value,snapshot FROM records WHERE habit_id=? AND day=?", (hid, day))
        action = self.db.one(
            "SELECT rowid FROM actions WHERE habit_id=? AND day=? ORDER BY rowid DESC LIMIT 1", (hid, day)
        )
        return json.dumps([dict(record) if record else None, action["rowid"] if action else None], sort_keys=True)

    def decide_verification(self, friend_id: int, rid: str, approve: bool) -> None:
        with self.db.transaction():
            row = self.db.one("SELECT * FROM verification_requests WHERE id=?", (rid,))
            if row is None or row["friend"] != friend_id or row["status"] != "pending":
                raise self._err("error_access")
            consent = self.db.one(
                "SELECT enabled FROM verifier_consent WHERE owner=? AND friend=?", (row["owner"], friend_id)
            )
            habit = self._habit(row["owner"], row["habit_id"])
            record = self.db.one("SELECT status FROM records WHERE habit_id=? AND day=?", (row["habit_id"], row["day"]))
            if (
                not self._friend(friend_id, row["owner"])
                or not consent
                or not consent["enabled"]
                or habit["sensitive"]
                or not record
                or record["status"] not in ("full", "minimum")
            ):
                raise self._err("error_access")
            if not row["signature"] or row["signature"] != self._verification_signature(row["habit_id"], row["day"]):
                raise self._err("error_stale")
            status = "approved" if approve else "rejected"
            self.db.execute(
                "UPDATE verification_requests SET status=?,decided_at=? WHERE id=?", (status, self._iso(), rid)
            )
            self.db.execute(
                "UPDATE records SET verification=? WHERE habit_id=? AND day=?", (status, row["habit_id"], row["day"])
            )

    def start_timer(self, uid: int, hid: str) -> dict[str, str]:
        self._habit(uid, hid)
        started = self._iso()
        self.db.execute(
            "INSERT INTO timers(owner,habit_id,started_at) VALUES(?,?,?) ON CONFLICT(owner,habit_id) DO NOTHING",
            (uid, hid, started),
        )
        row = self.db.one("SELECT started_at FROM timers WHERE owner=? AND habit_id=?", (uid, hid))
        return {"started_at": row["started_at"], "note": "Час не є доказом виконання."}

    def finish_timer(self, uid: int, hid: str) -> int:
        self._habit(uid, hid)
        row = self.db.one("SELECT started_at FROM timers WHERE owner=? AND habit_id=?", (uid, hid))
        if not row:
            raise self._err("error_input")
        started = datetime.fromisoformat(row["started_at"])
        seconds = max(0, int((self._now() - started).total_seconds()))
        self.db.execute("DELETE FROM timers WHERE owner=? AND habit_id=?", (uid, hid))
        return seconds

    def urge(self, uid: int, hid: str, intensity: int, trigger: str = "", episode: bool = False) -> None:
        h = self._habit(uid, hid)
        if not h.get("sensitive"):
            raise self._err("error_access")
        if not isinstance(intensity, int) or not 1 <= intensity <= 10 or len(trigger) > 500:
            raise self._err("error_input")
        self.db.execute(
            "INSERT INTO urges(owner,habit_id,happened_at,intensity,trigger_text,episode) VALUES(?,?,?,?,?,?)",
            (uid, hid, self._iso(), intensity, trigger, int(episode)),
        )

    def journal(self, uid: int, hid: str) -> list[dict[str, Any]]:
        h = self._habit(uid, hid)
        if not h.get("sensitive"):
            raise self._err("error_access")
        return [
            dict(x)
            for x in self.db.all(
                "SELECT happened_at,intensity,trigger_text,episode FROM urges WHERE owner=? AND habit_id=? ORDER BY id",
                (uid, hid),
            )
        ]

    def _export_payload(self, uid: int) -> dict[str, Any]:
        user = self.service.user(uid)
        habits = [dict(x) for x in self.db.all("SELECT * FROM habits WHERE user_id=? ORDER BY created_at,id", (uid,))]
        for habit in habits:
            habit["sensitive"] = bool(habit["sensitive"])
            habit["archived"] = bool(habit["archived"])
        ids = [x["id"] for x in habits]
        records: list[dict[str, Any]] = []
        versions: list[dict[str, Any]] = []
        for hid in ids:
            for record in self.db.all("SELECT * FROM records WHERE habit_id=? ORDER BY day", (hid,)):
                item = dict(record)
                # Telegram file IDs are remote access references; they are not portable export data.
                item["evidence"] = "{}"
                records.append(item)
            for version in self.db.all("SELECT * FROM versions WHERE habit_id=? ORDER BY effective", (hid,)):
                item = dict(version)
                item["spec"] = json.loads(item["spec"])
                versions.append(item)
        urges: list[dict[str, Any]] = []
        for hid in ids:
            urges.extend(
                dict(x)
                for x in self.db.all(
                    "SELECT habit_id,happened_at,intensity,trigger_text,episode FROM urges WHERE owner=? AND habit_id=? ORDER BY id",
                    (uid, hid),
                )
            )
        pauses: list[dict[str, Any]] = []
        for hid in ids:
            pauses.extend(
                dict(x)
                for x in self.db.all("SELECT habit_id,start,end FROM pauses WHERE habit_id=? ORDER BY start", (hid,))
            )
        archives: list[dict[str, Any]] = []
        for hid in ids:
            archives.extend(dict(x) for x in self.db.all("SELECT habit_id,day FROM archives WHERE habit_id=?", (hid,)))
        return {
            "format": "kitmode-export",
            "version": 1,
            "exported_at": self._iso(),
            "user": user,
            "habits": habits,
            "versions": versions,
            "records": records,
            "pauses": pauses,
            "urges": urges,
            "archives": archives,
        }

    def export_json(self, uid: int) -> str:
        return json.dumps(self._export_payload(uid), ensure_ascii=False, sort_keys=True, indent=2)

    def export_csv(self, uid: int) -> str:
        payload = self._export_payload(uid)
        output = io.StringIO(newline="")
        lang = self.service.user(uid).get("lang", "uk")
        translations = {
            "uk": {
                "habit_id": "id_звички",
                "title": "звичка",
                "sensitive": "чутлива",
                "day": "дата",
                "status": "статус",
                "value": "значення",
                "note": "нотатка",
                "verification": "перевірка",
                "private": "Приватна звичка",
            },
            "en": {
                "habit_id": "habit_id",
                "title": "habit",
                "sensitive": "sensitive",
                "day": "date",
                "status": "status",
                "value": "value",
                "note": "note",
                "verification": "verification",
                "private": "Private habit",
            },
            "ru": {
                "habit_id": "id_привычки",
                "title": "привычка",
                "sensitive": "чувствительная",
                "day": "дата",
                "status": "статус",
                "value": "значение",
                "note": "заметка",
                "verification": "проверка",
                "private": "Приватная привычка",
            },
        }
        strings = translations.get(lang, translations["uk"])
        source_fields = ["habit_id", "title", "sensitive", "day", "status", "value", "note", "verification"]
        fields = [strings[field] for field in source_fields]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()

        def safe_cell(value: Any) -> Any:
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                return "'" + value
            return value

        habits = {h["id"]: h for h in payload["habits"]}
        for rec in payload["records"]:
            h = habits[rec["habit_id"]]
            row = {
                "habit_id": h["id"],
                "title": strings["private"] if h["sensitive"] else h["title"],
                "sensitive": h["sensitive"],
                **{k: rec.get(k) for k in source_fields if k in rec},
            }
            writer.writerow({strings[key]: safe_cell(value) for key, value in row.items()})
        return output.getvalue()

    def _parse_import(self, uid: int, text: str) -> tuple[dict[str, Any], str]:
        if not isinstance(text, str):
            raise self._err("error_input")
        try:
            encoded_size = len(text.encode("utf-8"))
        except UnicodeError:
            raise self._err("error_input") from None
        if encoded_size > self.MAX_IMPORT_BYTES:
            raise self._err("error_limit")

        def reject_constant(value: str) -> None:
            raise ValueError(f"Invalid JSON constant: {value}")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON key")
                result[key] = value
            return result

        try:
            payload = json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_object)
        except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError):
            raise self._err("error_input")
        if not isinstance(payload, dict) or payload.get("format") != "kitmode-export" or payload.get("version") != 1:
            raise self._err("error_input")
        if set(payload) - {
            "format",
            "version",
            "exported_at",
            "user",
            "habits",
            "versions",
            "records",
            "pauses",
            "urges",
            "archives",
        }:
            raise self._err("error_input")
        habits = payload.get("habits")
        records = payload.get("records")
        versions = payload.get("versions", [])
        urges = payload.get("urges", [])
        pauses = payload.get("pauses", [])
        archives = payload.get("archives", [])
        if "user" in payload and not isinstance(payload["user"], dict):
            raise self._err("error_input")
        if (
            not isinstance(habits, list)
            or not isinstance(records, list)
            or not isinstance(versions, list)
            or not isinstance(urges, list)
            or not isinstance(pauses, list)
            or not isinstance(archives, list)
            or len(habits) > self.MAX_HABITS
            or len(records) > self.MAX_RECORDS
            or len(urges) > self.MAX_RECORDS
            or len(pauses) > self.MAX_RECORDS
            or len(archives) > self.MAX_HABITS
        ):
            raise self._err("error_limit")
        allowed_habit = {"id", "user_id", "title", "sensitive", "archived", "created_at"}
        source_ids: list[str] = []
        sensitive_by_id: dict[str, bool] = {}
        for h in habits:
            if (
                not isinstance(h, dict)
                or set(h) - allowed_habit
                or not all(k in h for k in ("id", "title", "sensitive", "archived"))
                or not isinstance(h["title"], str)
                or len(h["title"]) > 200
                or type(h["sensitive"]) is not bool
                or type(h["archived"]) not in (bool, int)
                or h["archived"] not in (0, 1, False, True)
            ):
                raise self._err("error_input")
            if not isinstance(h["id"], str) or not h["id"] or len(h["id"]) > 200 or h["id"] in source_ids:
                raise self._err("error_input")
            source_ids.append(h["id"])
            sensitive_by_id[h["id"]] = h["sensitive"]
        allowed_record = {
            "habit_id",
            "day",
            "status",
            "value",
            "note",
            "verification",
            "evidence",
            "snapshot",
            "updated_at",
        }
        record_keys: set[tuple[str, str]] = set()
        snapshot_specs: dict[tuple[str, str], dict[str, Any]] = {}
        for r in records:
            if (
                not isinstance(r, dict)
                or set(r) - allowed_record
                or not all(k in r for k in ("habit_id", "day", "status"))
                or not isinstance(r.get("note", ""), str)
                or len(r.get("note", "")) > 1000
            ):
                raise self._err("error_input")
            try:
                date.fromisoformat(r["day"])
            except (TypeError, ValueError):
                raise self._err("error_input")
            if r["habit_id"] not in source_ids or r["status"] not in (
                "full",
                "minimum",
                "progress",
                "fail",
                "skip",
                "none",
            ):
                raise self._err("error_input")
            if (r["habit_id"], r["day"]) in record_keys:
                raise self._err("error_input")
            record_keys.add((r["habit_id"], r["day"]))
            value = r.get("value", 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise self._err("error_input")
            try:
                snapshot = (
                    json.loads(r["snapshot"], parse_constant=reject_constant, object_pairs_hook=unique_object)
                    if isinstance(r.get("snapshot"), str)
                    else r.get("snapshot")
                )
                validated = self.service._validate(snapshot)
                if bool(validated["sensitive"]) != sensitive_by_id[r["habit_id"]]:
                    raise self._err("error_sensitive")
                snapshot_specs[(r["habit_id"], r["day"])] = validated
            except (ValueError, TypeError, json.JSONDecodeError):
                raise self._err("error_input")
        if any(
            not isinstance(v, dict)
            or set(v) - {"habit_id", "effective", "spec"}
            or not all(k in v for k in ("habit_id", "effective", "spec"))
            for v in versions
        ):
            raise self._err("error_input")
        seen_versions: set[tuple[str, str]] = set()
        version_specs: dict[str, list[tuple[date, dict[str, Any]]]] = {}
        for v in versions:
            if not isinstance(v["habit_id"], str) or v["habit_id"] not in source_ids:
                raise self._err("error_input")
            try:
                date.fromisoformat(v["effective"])
                spec = self.service._validate(v["spec"])
                if bool(spec["sensitive"]) != sensitive_by_id[v["habit_id"]]:
                    raise self._err("error_sensitive")
            except (ValueError, TypeError):
                raise self._err("error_input")
            version_key = (v["habit_id"], v["effective"])
            if version_key in seen_versions:
                raise self._err("error_input")
            seen_versions.add(version_key)
            version_specs.setdefault(v["habit_id"], []).append((date.fromisoformat(v["effective"]), spec))
        if any(not any(hid == version_hid for version_hid, _ in seen_versions) for hid in source_ids):
            raise self._err("error_input")
        for (hid, day), snapshot in snapshot_specs.items():
            versions_for_habit = version_specs[hid]
            effective_day = date.fromisoformat(day)
            prior = [item for item in versions_for_habit if item[0] <= effective_day]
            expected = (
                max(prior, key=lambda item: item[0])[1]
                if prior
                else min(versions_for_habit, key=lambda item: item[0])[1]
            )
            if self.service._validate(snapshot) != expected:
                raise self._err("error_input")
        for pause in pauses:
            if (
                not isinstance(pause, dict)
                or set(pause) - {"habit_id", "start", "end"}
                or not all(k in pause for k in ("habit_id", "start"))
            ):
                raise self._err("error_input")
            if not isinstance(pause["habit_id"], str) or pause["habit_id"] not in source_ids:
                raise self._err("error_input")
            try:
                date.fromisoformat(pause["start"])
                if pause.get("end"):
                    date.fromisoformat(pause["end"])
            except (ValueError, TypeError):
                raise self._err("error_input")
        archived_ids: set[str] = set()
        for archive in archives:
            if (
                not isinstance(archive, dict)
                or set(archive) - {"habit_id", "day"}
                or not isinstance(archive.get("habit_id"), str)
                or archive["habit_id"] not in source_ids
                or archive["habit_id"] in archived_ids
            ):
                raise self._err("error_input")
            try:
                date.fromisoformat(archive["day"])
            except (ValueError, TypeError):
                raise self._err("error_input")
            archived_ids.add(archive["habit_id"])
        for urge in urges:
            if (
                not isinstance(urge, dict)
                or set(urge) - {"habit_id", "happened_at", "intensity", "trigger_text", "episode"}
                or not all(k in urge for k in ("habit_id", "happened_at", "intensity"))
                or not isinstance(urge["intensity"], int)
                or not 1 <= urge["intensity"] <= 10
                or len(str(urge.get("trigger_text", ""))) > 500
            ):
                raise self._err("error_input")
            if not isinstance(urge["habit_id"], str) or urge["habit_id"] not in source_ids:
                raise self._err("error_input")
        # Identity digest is stable across preview and import. Root IDs are never trusted.
        identity_payload = {k: v for k, v in payload.items() if k != "exported_at"}
        digest = hashlib.sha256(
            json.dumps(identity_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        return payload, digest

    def preview_import(self, uid: int, text: str) -> dict[str, Any]:
        payload, digest = self._parse_import(uid, text)
        dup = self.db.one("SELECT 1 FROM imports WHERE owner=? AND digest=?", (uid, digest)) is not None
        return {
            "version": 1,
            "habits": len(payload["habits"]),
            "records": len(payload["records"]),
            "duplicate": dup,
            "can_import": not dup,
        }

    def import_data(self, uid: int, text: str) -> dict[str, int]:
        payload, digest = self._parse_import(uid, text)
        remap = {str(h["id"]): secrets.token_hex(12) for h in payload["habits"]}
        with self.db.transaction():
            if self.db.one("SELECT 1 FROM imports WHERE owner=? AND digest=?", (uid, digest)):
                return {"habits": 0, "records": 0, "duplicate": 1}
            self.service.user(uid)
            for h in payload["habits"]:
                self.db.execute(
                    "INSERT INTO habits(id,user_id,title,sensitive,archived,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        remap[str(h["id"])],
                        uid,
                        "Прихована звичка" if h["sensitive"] else h["title"],
                        int(bool(h["sensitive"])),
                        int(bool(h["archived"])),
                        h.get("created_at") or self._iso(),
                    ),
                )
            for v in payload.get("versions", []):
                if v["habit_id"] in remap:
                    self.db.execute(
                        "INSERT INTO versions(habit_id,effective,spec) VALUES(?,?,?)",
                        (remap[v["habit_id"]], v["effective"], json.dumps(v["spec"], ensure_ascii=False)),
                    )
            for pause in payload.get("pauses", []):
                if pause["habit_id"] in remap:
                    self.db.execute(
                        "INSERT INTO pauses(habit_id,start,end) VALUES(?,?,?)",
                        (remap[pause["habit_id"]], pause["start"], pause.get("end")),
                    )
            for archive in payload.get("archives", []):
                if archive["habit_id"] in remap:
                    self.db.execute(
                        "INSERT INTO archives(habit_id,day) VALUES(?,?)", (remap[archive["habit_id"]], archive["day"])
                    )
            for r in payload["records"]:
                if r["habit_id"] not in remap:
                    continue
                verification = "none" if json.loads(r["snapshot"]).get("verify", "none") == "none" else "pending"
                self.db.execute(
                    "INSERT OR IGNORE INTO records(habit_id,day,status,value,note,verification,evidence,snapshot,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        remap[r["habit_id"]],
                        r["day"],
                        r["status"],
                        r.get("value") or 0,
                        r.get("note", ""),
                        verification,
                        "{}",
                        r.get("snapshot") or "{}",
                        r.get("updated_at") or self._iso(),
                    ),
                )
            for urge in payload.get("urges", []):
                if urge["habit_id"] in remap and any(
                    bool(h["sensitive"]) for h in payload["habits"] if str(h["id"]) == urge["habit_id"]
                ):
                    self.db.execute(
                        "INSERT INTO urges(owner,habit_id,happened_at,intensity,trigger_text,episode) VALUES(?,?,?,?,?,?)",
                        (
                            uid,
                            remap[urge["habit_id"]],
                            urge["happened_at"],
                            urge["intensity"],
                            urge.get("trigger_text", ""),
                            int(bool(urge.get("episode", False))),
                        ),
                    )
            self.db.execute("INSERT INTO imports(owner,digest,imported_at) VALUES(?,?,?)", (uid, digest, self._iso()))
        return {"habits": len(remap), "records": len(payload["records"]), "duplicate": 0}

    def delete_user(self, uid: int) -> None:
        with self.db.transaction():
            ids = [r["id"] for r in self.db.all("SELECT id FROM habits WHERE user_id=?", (uid,))]
            for hid in ids:
                self.db.execute("DELETE FROM records WHERE habit_id=?", (hid,))
                self.db.execute("DELETE FROM versions WHERE habit_id=?", (hid,))
                self.db.execute("DELETE FROM rewards WHERE habit_id=?", (hid,))
                self.db.execute("DELETE FROM habits WHERE id=?", (hid,))
            for table in ("actions", "sessions", "buttons", "jobs"):
                self.db.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
            self.db.execute("DELETE FROM users WHERE id=?", (uid,))
            self.db.execute("DELETE FROM invites WHERE owner=? OR used_by=?", (uid, uid))
            self.db.execute("DELETE FROM friends WHERE a=? OR b=?", (uid, uid))
            for table in (
                "visibility",
                "verifier_consent",
                "verification_requests",
                "timers",
                "urges",
                "feedback",
                "imports",
                "challenges",
                "ai_usage",
            ):
                cols = {
                    "visibility": ("owner", "friend"),
                    "verifier_consent": ("owner", "friend"),
                    "verification_requests": ("owner", "friend"),
                    "timers": ("owner",),
                    "urges": ("owner",),
                    "feedback": ("owner",),
                    "imports": ("owner",),
                    "challenges": ("owner",),
                    "ai_usage": ("owner",),
                }[table]
                clause = " OR ".join(f"{c}=?" for c in cols)
                self.db.execute(f"DELETE FROM {table} WHERE {clause}", tuple(uid for _ in cols))
            self.db.execute("DELETE FROM blocked_users WHERE owner=? OR by_admin=?", (uid, uid))

    def feedback(self, uid: int, text: str) -> str:
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise self._err("error_input")
        ident = secrets.token_urlsafe(12)
        self.db.execute(
            "INSERT INTO feedback(id,owner,message,created_at) VALUES(?,?,?,?)", (ident, uid, text.strip(), self._iso())
        )
        return ident

    def admin(self, uid: int, admin_ids: Collection[int]) -> dict[str, Any]:
        if uid not in admin_ids:
            raise self._err("error_access")
        return {
            "users": int(self.db.one("SELECT count(*) n FROM users")["n"]),
            "habits": int(self.db.one("SELECT count(*) n FROM habits")["n"]),
            "open_feedback": int(self.db.one("SELECT count(*) n FROM feedback WHERE status='open'")["n"]),
            "blocked": int(self.db.one("SELECT count(*) n FROM blocked_users")["n"]),
        }

    def admin_feedback(self, uid: int, admin_ids: Collection[int], limit: int = 50) -> list[dict[str, Any]]:
        if uid not in admin_ids:
            raise self._err("error_access")
        limit = min(100, max(1, int(limit)))
        return [
            dict(row)
            for row in self.db.all(
                "SELECT id,owner,message,created_at,status FROM feedback ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        ]

    def resolve_feedback(
        self, uid: int, feedback_id: str, admin_ids: Collection[int], status: str = "resolved"
    ) -> None:
        if uid not in admin_ids:
            raise self._err("error_access")
        if status not in ("open", "resolved", "dismissed"):
            raise self._err("error_input")
        cursor = self.db.execute("UPDATE feedback SET status=? WHERE id=?", (status, feedback_id))
        if cursor.rowcount != 1:
            raise self._err("error_input")

    def record_error(self, category: str) -> None:
        """Store an allowlisted technical category only, never an exception or user text."""
        allowed = {"telegram", "database", "reminder", "ai", "handler", "other"}
        if category not in allowed:
            category = "other"
        self.db.execute("INSERT INTO error_events(category,created_at) VALUES(?,?)", (category, self._iso()))

    def system_status(self, uid: int, admin_ids: Collection[int]) -> dict[str, Any]:
        if uid not in admin_ids:
            raise self._err("error_access")
        counts = self.db.all(
            "SELECT category,count(*) AS count FROM error_events WHERE created_at>=? GROUP BY category ORDER BY category",
            ((self._now() - timedelta(days=1)).isoformat(),),
        )
        return {
            "database": "ok",
            "users": int(self.db.one("SELECT count(*) n FROM users")["n"]),
            "pending_jobs": int(self.db.one("SELECT count(*) n FROM jobs WHERE state='pending'")["n"]),
            "errors_24h": {row["category"]: int(row["count"]) for row in counts},
        }

    def block(self, admin: int, uid: int, admin_ids: Collection[int], blocked: bool = True) -> None:
        if admin not in admin_ids:
            raise self._err("error_access")
        with self.db.transaction():
            self.service.settings(uid, blocked=bool(blocked))
            if blocked:
                self.db.execute(
                    "INSERT INTO blocked_users(owner,by_admin,created_at) VALUES(?,?,?) ON CONFLICT(owner) DO UPDATE SET by_admin=excluded.by_admin,created_at=excluded.created_at",
                    (uid, admin, self._iso()),
                )
            else:
                self.db.execute("DELETE FROM blocked_users WHERE owner=?", (uid,))

    def challenge(self, uid: int, title: str, target: int, start: str, end: str) -> str:
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > 100
            or not isinstance(target, int)
            or not 1 <= target <= 365
        ):
            raise self._err("error_input")
        try:
            s = date.fromisoformat(start)
            e = date.fromisoformat(end)
        except (TypeError, ValueError):
            raise self._err("error_input")
        if e < s:
            raise self._err("error_input")
        ident = secrets.token_urlsafe(12)
        self.db.execute(
            "INSERT INTO challenges(id,owner,title,target,start,end,created_at) VALUES(?,?,?,?,?,?,?)",
            (ident, uid, title.strip(), target, start, end, self._iso()),
        )
        return ident

    def challenges(self, uid: int) -> list[dict[str, Any]]:
        rows = self.db.all("SELECT * FROM challenges WHERE owner=? ORDER BY created_at DESC", (uid,))
        out = []
        for c in rows:
            count = self.db.one(
                "SELECT count(*) n FROM records r JOIN habits h ON h.id=r.habit_id WHERE h.user_id=? AND r.status IN ('full','minimum') AND r.day BETWEEN ? AND ?",
                (uid, c["start"], c["end"]),
            )["n"]
            out.append(
                {
                    "id": c["id"],
                    "title": c["title"],
                    "target": c["target"],
                    "completed": int(count),
                    "done": int(count) >= c["target"],
                    "start": c["start"],
                    "end": c["end"],
                }
            )
        return out

    def share_card(self, uid: int) -> str:
        habits = self.service.habits(uid)
        eligible = [h for h in habits if not h.get("sensitive") and not h.get("archived")]
        translations = {
            "uk": ("KitMode • мій прогрес", "Відкритих звичок: {}", "{}: {} виконань"),
            "en": ("KitMode • my progress", "Open habits: {}", "{}: {} completions"),
            "ru": ("KitMode • мой прогресс", "Открытых привычек: {}", "{}: {} выполнений"),
        }
        title, summary, row = translations.get(self.service.user(uid).get("lang", "uk"), translations["uk"])
        lines = [title, summary.format(len(eligible))]
        for h in eligible[:10]:
            stats = self.service.stats(uid, hid=h["id"])
            lines.append("• " + row.format(h["title"], stats.get("completed", 0)))
        return "\n".join(lines)

    def _fallback(self, uid: int, prompt: str) -> str:
        from .i18n import tr

        user = self.service.user(uid)
        lang = user.get("lang", "uk")
        words = prompt.casefold()
        if any(word in words for word in ("прогрес", "progress", "статист", "series", "сері", "сери")):
            stats = self.service.stats(uid)
            return tr(
                lang,
                "local_progress",
                completed=stats["completed"],
                opportunities=stats["opportunities"],
                rate=round(stats["rate"] * 100, 1),
            )
        if any(word in words for word in ("план", "plan", "розклад", "распис")):
            return tr(lang, "local_plan")
        return tr(lang, "local_goal")

    def ai(self, uid: int, prompt: str, config: AIConfig, requester: int | None = None, sensitive: bool = False) -> str:
        prompt = prompt.strip() if isinstance(prompt, str) else ""
        if not prompt or len(prompt.encode("utf-8")) > self.AI_MAX_INPUT:
            return self._fallback(uid, prompt)
        user = self.service.user(uid)
        if not config.enabled or not config.key or not config.model or not user.get("ai_consent"):
            return self._fallback(uid, prompt)
        sensitive_terms: list[str] = []
        for habit in self.service.habits(uid, include_archived=True):
            if habit.get("sensitive"):
                sensitive_terms.extend((habit.get("title", ""), habit.get("spec", {}).get("description", "")))
        prompt_folded = prompt.casefold()
        contains_sensitive = any(
            len(term.strip()) >= 3 and term.casefold() in prompt_folded for term in sensitive_terms
        )
        if (sensitive or contains_sensitive) and not user.get("ai_sensitive"):
            return self._fallback(uid, prompt)
        if (requester is not None and requester != uid) or user.get("blocked"):
            return self._fallback(uid, prompt)
        if config.daily_limit <= 0:
            return self._fallback(uid, prompt)
        # Do not send sensitive topics or journal-like personal context to an external provider.
        day = self._now().date().isoformat()
        with self.db.transaction():
            row = self.db.one("SELECT count FROM ai_usage WHERE owner=? AND day=?", (uid, day))
            if row and row["count"] >= max(0, config.daily_limit):
                return self._fallback(uid, prompt)
            self.db.execute(
                "INSERT INTO ai_usage(owner,day,count) VALUES(?,?,1) ON CONFLICT(owner,day) DO UPDATE SET count=count+1",
                (uid, day),
            )
        body = json.dumps(
            {
                "model": config.model,
                "instructions": f"Respond in {user['lang']} with a {user['tone']} respectful tone. Give age-appropriate practical habit support. Do not diagnose, prescribe treatment, or recommend dangerous withdrawal steps. Never shame the user or claim to have changed account data.",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                "max_output_tokens": self.AI_MAX_OUTPUT,
                "store": False,
            },
            ensure_ascii=False,
        ).encode()
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=body,
            headers={"Authorization": f"Bearer {config.key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=min(max(float(config.timeout), 1), 20)) as response:
                result = json.loads(response.read(100_000))
            texts = [
                part.get("text", "")
                for item in result.get("output", [])
                for part in item.get("content", [])
                if part.get("type") == "output_text"
            ]
            text = "\n".join(texts).strip()
            return text[: self.AI_MAX_OUTPUT] if text else self._fallback(uid, prompt)
        except Exception:
            self.record_error("ai")
            return self._fallback(uid, prompt)
