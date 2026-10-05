"""Private-chat Telegram interface for KitMode.

The bot keeps each in-progress flow in the service database. Callback data only
contains a short opaque token; ownership and expiry are checked by Service.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .choices import ChoiceUI
from .extras import AIConfig
from .i18n import LANGUAGES, tr
from .transport import TransportError


class Bot:
    PAGE_SIZE = 8
    SETTINGS = {
        "language": "lang",
        "name": "name",
        "timezone": "tz",
        "sleep": "sleep",
        "boundary": "boundary",
        "quiet": "quiet_start",
        "quiet_end": "quiet_end",
        "tone": "tone",
        "reminders": "reminders",
        "gamification": "gamification",
        "ai": "ai_consent",
        "ai_sensitive": "ai_sensitive",
        "alternative": "alternative",
    }

    def __init__(
        self, service: Any, transport: Any, admin_ids: tuple[int, ...] | list[int] = (), ai_config: Any = None
    ):
        self.service, self.transport = service, transport
        self.admin_ids, self.ai_config = set(map(int, admin_ids)), ai_config
        self._active_message: tuple[int, int] | None = None
        self._active_photo = False
        self._welcoming = False
        self._current_update_key = ""
        self.ui = ChoiceUI(self)
        # DB owns the shared updates table (id, created_at); this also keeps
        # Bot usable with small service doubles in isolated tests.
        self.service.db.conn.execute(
            "CREATE TABLE IF NOT EXISTS updates(id INTEGER PRIMARY KEY,created_at TEXT NOT NULL)"
        )
        # Old keyboards must not trigger actions after a conversation redesign.
        with self.service.db.transaction():
            version = self.service.db.one("SELECT value FROM system WHERE key='ui_version'")
            if not version or version["value"] != "2":
                self.service.db.conn.execute("UPDATE buttons SET used=1")
                self.service.db.conn.execute("INSERT OR REPLACE INTO system VALUES('ui_version','2')")

    def _text(self, uid: int, key: str, **values: Any) -> str:
        row = self.service.db.one("SELECT data FROM users WHERE id=?", (uid,))
        try:
            lang = json.loads(row["data"]).get("lang", "uk") if row else "uk"
        except (TypeError, ValueError, KeyError):
            lang = "uk"
        return tr(lang, key, **values)

    def _token(self, uid: int, action: str, **data: Any) -> str:
        if action in {"record", "record_amount", "note_existing"}:
            data.setdefault("day", self.service.day(uid))
        payload = {
            "action": action,
            **data,
            "once": action
            not in {
                "menu",
                "page",
                "detail",
                "habit_more",
                "settings_section",
                "progress",
                "settings",
                "today",
                "habits",
                "friends",
                "support",
                "more_menu",
                "help",
            },
        }
        if action in {
            "flow_pick",
            "flow_skip",
            "flow_back",
            "day_toggle",
            "flow_days_done",
            "use_telegram_name",
            "choice",
            "custom_input",
            "schedule_more",
            "advanced_menu",
            "advanced_field",
            "preview_return",
            "amount_all",
            "edit_schedule_type",
            "edit_day_toggle",
            "edit_days_done",
            "region_page",
            "create_save",
            "edit_confirm",
            "import_confirm",
            "challenge_confirm",
            "delete_confirm",
        }:
            session = self._session(uid)
            payload["_flow"] = session.get("id")
            payload["_step"] = session.get("step")
            payload["_revision"] = session.get("revision")
        token = self.service.button(uid, payload, ttl=86400)
        callback = "b:" + token
        if len(callback.encode("utf-8")) > 64:
            raise ValueError("Service.button token exceeds Telegram callback_data limit")
        return callback

    def _keyboard(self, uid: int, rows: list[list[tuple[str, str, dict[str, Any]]]]) -> list[list[dict[str, str]]]:
        return [
            [{"text": label, "callback_data": self._token(uid, action, **data)} for label, action, data in row]
            for row in rows
        ]

    def _send(self, uid: int, text: str, rows: list[list[tuple[str, str, dict[str, Any]]]] | None = None) -> None:
        keyboard = self._keyboard(uid, rows) if rows else None
        if self._welcoming:
            self._welcoming = False
            asset = Path(__file__).resolve().parent.parent / "assets" / "kitmode-avatar.jpg"
            cached = self.service.db.one("SELECT value FROM system WHERE key='welcome_photo'")
            if len(text) <= 1024 and (cached or asset.is_file()):
                try:
                    photo = self.transport.photo(uid, cached["value"] if cached else str(asset), text, keyboard)
                except TransportError as exc:
                    if exc.code != 400:
                        raise
                    self.service.db.execute("DELETE FROM system WHERE key='welcome_photo'")
                else:
                    photos = photo.get("photo") or []
                    if photos:
                        self.service.db.execute("INSERT OR REPLACE INTO system VALUES('welcome_photo',?)",
                                                (photos[-1]["file_id"],))
                    return
        if self._active_message and self._active_message[0] == uid:
            if self._active_photo:
                self.transport.edit(uid, self._active_message[1], text, keyboard, caption=True)
            else:
                self.transport.edit(uid, self._active_message[1], text, keyboard)
        else:
            self.transport.send(uid, text, keyboard)

    def _main_menu(self, uid: int) -> None:
        keys = ["menu_today", "habits_add_short", "menu_habits", "menu_progress", "menu_settings", "menu_more"]
        acts = ["today", "create_begin", "habits", "progress", "settings", "more_menu"]
        rows: list[list[tuple[str, str, dict[str, Any]]]] = [
            [(self._text(uid, keys[n]), acts[n], {}), (self._text(uid, keys[n + 1]), acts[n + 1], {})]
            for n in (0, 2, 4)
        ]
        if self._session(uid).get("flow"):
            rows.insert(0, [(self._text(uid, "resume_flow"), "resume_flow", {})])
        self._send(
            uid,
            self._text(uid, "main_prompt"),
            rows,
        )

    def _more_menu(self, uid: int) -> None:
        self._send(
            uid,
            self._text(uid, "more_prompt"),
            [
                [(self._text(uid, "menu_friends"), "friends", {})],
                [(self._text(uid, "menu_help"), "help", {})],
                [(self._text(uid, "bulk_done"), "bulk", {})],
                *self._home_rows(uid),
            ],
        )

    def _show_help(self, uid: int) -> None:
        self._send(
            uid,
            self._text(uid, "plain_help"),
            [
                [
                    (self._text(uid, "menu_today"), "today", {}),
                    (self._text(uid, "habits_add_short"), "create_begin", {}),
                ],
                *self._home_rows(uid),
            ],
        )

    def _session(self, uid: int, data: dict[str, Any] | None = None) -> dict[str, Any]:
        if data is not None:
            self.service.session(uid, data)
            return data
        return self.service.session(uid) or {}

    def _begin(self, uid: int, flow: str, step: str, values: dict[str, Any] | None = None) -> None:
        values = dict(values or {})
        if flow == "create":
            values["_quick"] = True
        self._session(
            uid, {"id": secrets.token_urlsafe(8), "flow": flow, "step": step, "values": values, "history": []}
        )

    def _nav(self, uid: int, back: bool | None = None) -> list[list[tuple[str, str, dict[str, Any]]]]:
        if back is None:
            session = self._session(uid)
            back = bool(session.get("history") or session.get("values", {}).get("_manual"))
        return (
            [[(self._text(uid, "back"), "flow_back", {}), (self._text(uid, "cancel"), "flow_cancel", {})]]
            if back
            else [[(self._text(uid, "cancel"), "flow_cancel", {})]]
        )

    def _flow_prompt(self, uid: int) -> None:
        rows: list[list[tuple[str, str, dict[str, Any]]]]
        s = self._session(uid)
        s["revision"] = secrets.token_urlsafe(8)
        self._session(uid, s)
        flow, step = s.get("flow"), s.get("step")
        v = s.get("values", {})
        if not isinstance(step, str):
            self._send(uid, self._text(uid, "error_stale"))
            return
        if flow == "create" and not v.get("_quick"):
            v["_quick"] = True
            if step in {
                "minimum",
                "start",
                "end",
                "sensitive",
                "verify",
                "description",
                "category",
                "icon",
                "routine",
                "repeat",
            }:
                s["step"] = "preview"
            self._session(uid, s)
        if self.ui.prompt(uid, s):
            return
        if flow == "onboard":
            if step == "language":
                rows = [[(self._text(uid, "language_" + lang), "flow_pick", {"value": lang}) for lang in LANGUAGES]]
                self._send(uid, self._text(uid, "onboard_lang"), rows + self._nav(uid))
            elif step == "tone":
                self._send(
                    uid,
                    self._text(uid, "onboard_tone"),
                    [
                        [
                            (self._text(uid, "tone_" + t), "flow_pick", {"value": t})
                            for t in ("calm", "friendly", "strict")
                        ]
                    ]
                    + self._nav(uid)
                    + [[(self._text(uid, "skip"), "flow_skip", {})]],
                )
            else:
                keys = {
                    "name": "onboard_name",
                    "timezone": "onboard_timezone",
                    "sleep": "onboard_sleep",
                    "boundary": "onboard_boundary",
                }
                rows = self._nav(uid) + [[(self._text(uid, "skip"), "flow_skip", {})]]
                if step == "name" and v.get("telegram_name"):
                    rows.insert(
                        0, [(self._text(uid, "use_telegram_name"), "use_telegram_name", {"name": v["telegram_name"]})]
                    )
                self._send(uid, self._text(uid, keys[step]), rows)
        elif flow == "create":
            prompts = {
                "title": "create_title",
                "target": "create_target",
                "custom_unit": "create_custom_unit",
                "days": "schedule_days",
                "n": "schedule_n",
                "minimum": "create_minimum",
                "start": "create_start",
                "end": "create_end",
                "reminder": "create_reminders",
                "description": "create_description",
                "category": "create_category",
                "icon": "create_icon",
                "routine": "create_routine",
                "repeat": "create_repeat",
            }
            if step == "days":
                selected = set(v.get("days", []))
                names = (
                    "weekday_mon",
                    "weekday_tue",
                    "weekday_wed",
                    "weekday_thu",
                    "weekday_fri",
                    "weekday_sat",
                    "weekday_sun",
                )
                rows = [
                    [(self._text(uid, names[i]) + (" ✓" if i in selected else ""), "day_toggle", {"day": i})]
                    for i in range(7)
                ]
                rows.append([(self._text(uid, "done"), "flow_days_done", {})])
                rows.extend(self._nav(uid))
                self._send(uid, self._text(uid, "schedule_days"), rows)
            elif step in prompts:
                prompt_key = (
                    "create_minimum_binary" if step == "minimum" and v.get("kind") == "binary" else prompts[step]
                )
                self._send(
                    uid,
                    self._text(uid, prompt_key),
                    self._nav(uid)
                    + (
                        [[(self._text(uid, "skip"), "flow_skip", {})]]
                        if step
                        in {
                            "minimum",
                            "start",
                            "end",
                            "reminder",
                            "description",
                            "category",
                            "icon",
                            "routine",
                            "repeat",
                        }
                        else []
                    ),
                )
            elif step == "kind":
                self._send(
                    uid,
                    self._text(uid, "create_kind"),
                    [
                        [
                            (self._text(uid, "kind_" + x), "flow_pick", {"value": x})
                            for x in ("binary", "quantity", "limit")
                        ]
                    ]
                    + self._nav(uid),
                )
            elif step == "unit":
                self._send(
                    uid,
                    self._text(uid, "create_unit"),
                    [
                        [
                            (self._text(uid, "unit_" + x), "flow_pick", {"value": x})
                            for x in ("minutes", "pages", "reps", "custom")
                        ]
                    ]
                    + self._nav(uid),
                )
            elif step == "schedule":
                self._send(
                    uid,
                    self._text(uid, "create_schedule"),
                    [
                        [
                            (self._text(uid, "schedule_" + x), "flow_pick", {"value": x})
                            for x in ("daily", "weekdays", "weekly", "interval")
                        ]
                    ]
                    + self._nav(uid),
                )
            elif step == "sensitive":
                self._send(
                    uid,
                    self._text(uid, "create_sensitive"),
                    [
                        [
                            (self._text(uid, "yes"), "flow_pick", {"value": True}),
                            (self._text(uid, "no"), "flow_pick", {"value": False}),
                        ]
                    ]
                    + self._nav(uid),
                )
            elif step == "verify":
                choices = ["none", "text", "timer"] + ([] if v.get("sensitive") else ["friend", "photo"])
                self._send(
                    uid,
                    self._text(uid, "create_verify"),
                    [[(self._text(uid, "verify_" + x), "flow_pick", {"value": x}) for x in choices]] + self._nav(uid),
                )
            elif step == "preview":
                spec = self._create_spec(uid, v)
                minimum = spec.get("minimum", "—")
                if spec.get("minimum_description"):
                    minimum = f"{minimum} · {spec['minimum_description']}"
                disp = {
                    "title": spec["title"],
                    "kind": self._text(uid, "kind_" + spec["kind"]),
                    "target": spec["target"],
                    "unit": self._unit_label(uid, spec["unit"]),
                    "schedule": self._schedule_label(uid, spec["schedule"]),
                    "minimum": minimum,
                    "start": spec["start"],
                    "end": spec.get("end") or "—",
                    "sensitive": self._text(uid, "yes" if spec["sensitive"] else "no"),
                    "verify": self._text(uid, "verify_" + spec["verify"]),
                    "reminders": ", ".join(spec["reminders"]) or "—",
                }
                preview_rows: list[list[tuple[str, str, dict[str, Any]]]] = [
                    [(self._text(uid, "create_confirm"), "create_save", {})]
                ]
                preview_rows.extend(self._nav(uid))
                extra = "\n" + self._text(uid, "field_repeat") + f": {spec['repeat']}"
                for field in ("description", "category", "icon", "routine"):
                    extra += "\n" + self._text(uid, "field_" + field) + ": " + (spec[field] or "—")
                self._send(uid, self._text(uid, "create_preview", **disp) + extra, preview_rows)
        elif flow == "setting":
            field = self._text(uid, "setting_" + v["field"])
            self._send(uid, self._text(uid, "settings_prompt", field=field), self._nav(uid))
        elif flow in {"amount", "correct_amount"}:
            habit = self.service.habit(uid, v["hid"], v.get("day"))
            key = (
                "amount_correct"
                if flow == "correct_amount"
                else ("amount_limit" if habit["spec"]["kind"] == "limit" else "amount_prompt")
            )
            self._send(uid, self._text(uid, key), self._nav(uid))
        elif flow in {"note", "record_note"}:
            self._send(
                uid, self._text(uid, "note_prompt"), self._nav(uid) + [[(self._text(uid, "skip"), "flow_skip", {})]]
            )
        elif flow == "evidence":
            key = "proof_text_prompt" if s.get("values", {}).get("method") == "text" else "proof_photo_prompt"
            self._send(uid, self._text(uid, key), self._nav(uid) + [[(self._text(uid, "skip"), "flow_skip", {})]])
        elif flow == "urge":
            if step == "intensity":
                self._send(
                    uid,
                    self._text(uid, "urge_intensity"),
                    [[(str(n), "flow_pick", {"value": n}) for n in range(1, 6)]] + self._nav(uid),
                )
            else:
                self._send(
                    uid,
                    self._text(uid, "urge_trigger"),
                    self._nav(uid) + [[(self._text(uid, "skip"), "flow_skip", {})]],
                )
        elif flow == "friend_token":
            self._send(uid, self._text(uid, "friend_token_prompt"), self._nav(uid))
        elif flow == "feedback":
            self._send(uid, self._text(uid, "feedback_prompt"), self._nav(uid))
        elif flow == "custom_period":
            self._send(uid, self._text(uid, "custom_start" if step == "start" else "custom_end"), self._nav(uid))
        elif flow == "challenge":
            if step == "confirm":
                self._send(
                    uid,
                    self._text(uid, "confirm_challenge", **v),
                    [[(self._text(uid, "confirm"), "challenge_confirm", {})], *self._nav(uid)],
                )
                return
            key = {
                "title": "challenge_title",
                "target": "challenge_target",
                "start": "challenge_start",
                "end": "challenge_end",
            }[step]
            self._send(uid, self._text(uid, key), self._nav(uid))
        elif flow == "edit":
            labels = {
                "title": "habit_title",
                "target": "create_target",
                "minimum": "habit_minimum",
                "reminders": "habit_reminders",
            }
            self._send(
                uid,
                self._text(
                    uid,
                    "settings_prompt",
                    field=self._text(uid, labels.get(s.get("values", {}).get("field"), "habit_edit")),
                ),
                self._nav(uid) + ([[(self._text(uid, "skip"), "flow_skip", {})]] if step == "value" else []),
            )
        elif flow == "ai":
            self._send(uid, self._text(uid, "ai_prompt"), self._nav(uid))
        elif flow == "admin_block":
            self._send(uid, self._text(uid, "admin_id_prompt"), self._nav(uid))
        elif flow == "urge_pause":
            self.service.session(uid, {})
            self._main_menu(uid)
        elif flow == "edit_confirm":
            self._edit_preview(uid, v["hid"], v["changes"])
        elif flow == "note_existing":
            self._send(uid, self._text(uid, "note_prompt"), self._nav(uid))
        elif flow == "import_confirm":
            preview = self.service.extras.preview_import(uid, v["payload"])
            self._send(
                uid,
                self._text(uid, "import_preview", habits=preview.get("habits", 0), records=preview.get("records", 0)),
                [[(self._text(uid, "confirm"), "import_confirm", {})], *self._nav(uid)],
            )
        elif flow == "settings":
            self._show_settings(uid)
        elif flow == "delete":
            self._send(
                uid,
                self._text(uid, "delete_confirm"),
                [[(self._text(uid, "confirm"), "delete_confirm", {})], *self._nav(uid)],
            )
        elif flow == "habits":
            self._show_habits(uid, int(v.get("page", 0)))
        elif flow == "today":
            self._show_today(uid)

    def _create_spec(self, uid: int, v: dict[str, Any]) -> dict[str, Any]:
        sched = v.get("schedule", "daily")
        schedule: dict[str, Any] = {"type": sched, "days": [], "n": 1}
        if sched == "weekdays":
            schedule["days"] = v.get("days", [0, 1, 2, 3, 4])
        if sched == "weekly":
            schedule["n"] = int(v.get("n", 3))
        if sched == "interval":
            schedule["n"] = int(v.get("n", 2))
        start = v.get("start") or self.service.day(uid)
        spec = {
            "title": v["title"],
            "kind": v.get("kind", "binary"),
            "target": float(v.get("target", 1)),
            "unit": v.get("unit", ""),
            "schedule": schedule,
            "start": start,
            "description": v.get("description", ""),
            "category": v.get("category", ""),
            "icon": v.get("icon") or "🐾",
            "routine": v.get("routine", ""),
            "minimum_description": v.get("minimum_description", ""),
            "sensitive": bool(v.get("sensitive", False)),
            "verify": v.get("verify", "none"),
            "reminders": v.get("reminders", []),
            "repeat": int(v.get("repeat", 0)),
            "independent": False,
            "private_title": v.get("private_title") or self._text(uid, "private_habit"),
        }
        if v.get("minimum") not in (None, ""):
            spec["minimum"] = float(v["minimum"])
        if v.get("end"):
            spec["end"] = v["end"]
        return spec

    def _unit_label(self, uid: int, unit: str) -> str:
        keys = {
            "minutes": "unit_minutes",
            "pages": "unit_pages",
            "reps": "unit_reps",
            "glasses": "unit_glasses",
            "km": "unit_km",
        }
        return self._text(uid, keys[unit]) if unit in keys else unit

    def _habit_label(self, uid: int, habit: dict[str, Any]) -> str:
        if habit.get("sensitive"):
            return "🔒 " + (habit.get("spec", {}).get("private_title") or self._text(uid, "private_habit"))
        return habit.get("title", "")

    def _schedule_label(self, uid: int, schedule: dict[str, Any]) -> str:
        kind = schedule.get("type", "daily")
        if kind == "weekly":
            return self._text(uid, "schedule_n_count", n=schedule.get("n", 1))
        if kind == "interval":
            return self._text(uid, "schedule_interval_count", n=schedule.get("n", 1))
        if kind == "weekdays":
            names = (
                "weekday_mon",
                "weekday_tue",
                "weekday_wed",
                "weekday_thu",
                "weekday_fri",
                "weekday_sat",
                "weekday_sun",
            )
            return ", ".join(
                self._text(uid, names[d]) for d in schedule.get("days", []) if isinstance(d, int) and 0 <= d < 7
            )
        return self._text(uid, "schedule_" + kind)

    def _show_today(self, uid: int, page: int = 0) -> None:
        items = self.service.today(uid)
        if not items:
            self._send(
                uid,
                self._text(uid, "today_empty"),
                [[(self._text(uid, "habits_add_short"), "create_begin", {})], *self._home_rows(uid)],
            )
            return
        rows = []
        lines = [self._text(uid, "today_title", day=self.service.day(uid))]
        page = max(0, min(page, (len(items) - 1) // self.PAGE_SIZE))
        for item in items[page * self.PAGE_SIZE : (page + 1) * self.PAGE_SIZE]:
            h = item
            rec = item.get("record")
            name = self._habit_label(uid, h)
            status = (rec or {}).get("status")
            lines.append(f"• {name}: {self._text(uid, 'status_' + (status or 'none'))}")
            hid = h["id"]
            kind = h.get("spec", {}).get("kind")
            row = [(name[:32], "detail", {"hid": hid})]
            if kind == "binary" and status not in {"full", "minimum"}:
                row.append(
                    (
                        self._text(uid, "mark_done"),
                        "record",
                        {"hid": hid, "status": "done", "day": self.service.day(uid)},
                    )
                )
            rows.append(row)
        rows += self._home_rows(uid)
        rows += self._pagination(uid, "today", page, len(items))
        self._send(uid, "\n".join(lines), rows)

    def _home_rows(self, uid: int) -> list[list[tuple[str, str, dict[str, Any]]]]:
        return [[(self._text(uid, "home"), "menu", {})]]

    def _result_rows(self, uid: int, hid: str, day: str | None = None) -> list[list[tuple[str, str, dict[str, Any]]]]:
        action = self.service.db.one(
            "SELECT key FROM actions WHERE user_id=? AND habit_id=? AND day=? AND kind='record' ORDER BY rowid DESC LIMIT 1",
            (uid, hid, day or self.service.day(uid)),
        )
        undo = [[(self._text(uid, "undo"), "undo", {"key": action["key"]})]] if action else []
        rows: list[list[tuple[str, str, dict[str, Any]]]] = [[(self._text(uid, "menu_today"), "today", {})]]
        return rows + undo + self._home_rows(uid)

    def _show_habits(self, uid: int, page: int = 0, archived: bool = False) -> None:
        habits = [h for h in self.service.habits(uid, include_archived=archived) if bool(h["archived"]) == archived]
        habits.sort(key=lambda h: (h["spec"]["routine"], h["id"]))
        page = max(0, min(page, max(0, (len(habits) - 1) // self.PAGE_SIZE)))
        start = page * self.PAGE_SIZE
        subset = habits[start : start + self.PAGE_SIZE]
        if not habits:
            rows: list[list[tuple[str, str, dict[str, Any]]]] = [
                [(self._text(uid, "habit_new"), "create_begin", {})],
                [(self._text(uid, "preset_packs"), "preset_menu", {})],
            ]
            rows.extend(self._home_rows(uid))
            rows.insert(1, [(self._text(uid, "archive_list"), "page", {"archived": True, "page": 0})])
            self._send(uid, self._text(uid, "habit_empty"), rows)
            return
        rows = []
        lines = [self._text(uid, "habit_list")]
        for h in subset:
            name = self._habit_label(uid, h)
            lines.append(f"{start + len(lines)}. {name}")
            routine = h["spec"]["routine"]
            category = h["spec"]["category"]
            if routine and not h.get("sensitive"):
                lines[-1] += " · " + routine
            if category and not h.get("sensitive"):
                lines[-1] += " · " + category
            rows.append([(name[:32], "detail", {"hid": h["id"]})])
        nav = []
        if page > 0:
            nav.append((self._text(uid, "habit_prev"), "page", {"page": page - 1, "archived": archived}))
        if start + self.PAGE_SIZE < len(habits):
            nav.append((self._text(uid, "habit_next"), "page", {"page": page + 1, "archived": archived}))
        if nav:
            rows.append(nav)
        rows += [
            [(self._text(uid, "habit_new"), "create_begin", {}), (self._text(uid, "preset_packs"), "preset_menu", {})],
            [(self._text(uid, "archive_list"), "page", {"page": 0, "archived": True})],
            *self._home_rows(uid),
        ]
        self._send(uid, "\n".join(lines), rows)

    def _pagination(
        self, uid: int, action: str, page: int, count: int, extra: dict[str, Any] | None = None
    ) -> list[list[tuple[str, str, dict[str, Any]]]]:
        row = []
        for next_page, label in ((page - 1, "habit_prev"), (page + 1, "habit_next")):
            if 0 <= next_page and next_page * self.PAGE_SIZE < count:
                row.append((self._text(uid, label), action, {**(extra or {}), "page": next_page}))
        return [row] if row else []

    def _finish_onboard(self, uid: int, values: dict[str, Any]) -> None:
        allowed = {k: v for k, v in values.items() if k in {"lang", "name", "tz", "sleep", "boundary", "tone"}}
        self.service.settings(uid, **allowed, onboarded=True)
        self.service.session(uid, {})
        self.ui.welcome(uid)

    def _edit_preview(self, uid: int, hid: str, changes: dict[str, Any]) -> None:
        result = self.service.preview_edit(uid, hid, changes)
        spec = result["spec"]
        self._begin(uid, "edit_confirm", "confirm", {"hid": hid, "changes": changes, "effective": result["effective"]})
        details = [
            spec["title"],
            f"{spec['target']} {self._unit_label(uid, spec['unit'])}",
            self._schedule_label(uid, spec["schedule"]),
        ]
        for key in changes:
            value = spec[key]
            if key == "schedule":
                continue
            if key == "verify":
                value = self._text(uid, "verify_" + value)
            if isinstance(value, list):
                value = ", ".join(map(str, value)) or "—"
            if value is None:
                value = "—"
            labels = {
                "title": "habit_title",
                "target": "create_target",
                "minimum": "habit_minimum",
                "reminders": "habit_reminders",
            }
            details.append(f"{self._text(uid, labels.get(key, 'field_' + key))}: {value}")
        text = self._text(uid, "edit_preview", effective=result["effective"], details="\n".join(details))
        self._send(uid, text, [[(self._text(uid, "confirm"), "edit_confirm", {})], *self._nav(uid)])

    def _journal(self, uid: int, hid: str, page: int = 0) -> None:
        entries = list(reversed(self.service.extras.journal(uid, hid)))
        lines = [self._text(uid, "journal")]
        for item in entries[page * self.PAGE_SIZE : (page + 1) * self.PAGE_SIZE]:
            lines.append(
                self._text(
                    uid,
                    "journal_line",
                    at=item["happened_at"],
                    intensity=item["intensity"],
                    episode=self._text(uid, "yes" if item["episode"] else "no"),
                    trigger=item["trigger_text"],
                )
            )
        self._send(
            uid,
            "\n".join(lines) if entries else self._text(uid, "journal_empty"),
            self._pagination(uid, "journal", page, len(entries), {"hid": hid}) + self._home_rows(uid),
        )

    def _show_settings(self, uid: int) -> None:
        self.ui.settings(uid)

    def _progress(
        self,
        uid: int,
        period: str = "week",
        start: str | None = None,
        end: str | None = None,
        page: int = 0,
        detailed: bool = False,
    ) -> None:
        today = date.fromisoformat(self.service.day(uid))
        if not start:
            if period == "week":
                start = (today - timedelta(days=today.weekday())).isoformat()
            elif period == "month":
                start = today.replace(day=1).isoformat()
            else:
                start = today.replace(month=1, day=1).isoformat()
        end = end or today.isoformat()
        stats = self.service.stats(uid, start=start, end=end)
        percent = round(float(stats.get("rate", 0)) * 100, 1)
        previous = round(float(stats.get("previous_rate", 0)) * 100, 1)
        out = (
            self._text(uid, "progress_title", period=self._text(uid, "period_" + period))
            + "\n\n"
            + self._text(
                uid,
                "progress_plain",
                opportunities=stats["opportunities"],
                completed=stats["completed"],
                rate=percent,
                full=stats["full"],
                minimum=stats["minimum"],
                fail=stats["fail"],
                skip=stats["skip"],
                missing=stats["missing"],
                current=stats["current_streak"],
                best=stats["best_streak"],
            )
        )
        if self.service.user(uid)["gamification"]:
            badges = ", ".join(self._text(uid, "badge_" + badge) for badge in stats["badges"]) or "—"
            out += "\n" + self._text(uid, "rewards_line", xp=stats["xp"], level=stats["level"], badges=badges)
        for unit, value in stats.get("quantity_by_unit", {}).items():
            out += "\n" + self._text(uid, "quantity_total", quantity=f"{value:g} {self._unit_label(uid, unit)}")
        out += "\n" + self._text(uid, "rate_comparison", rate=previous) + "\n\n" + self._text(uid, "calendar")
        days = stats.get("calendar", [])
        if not detailed:
            out = (
                self._text(uid, "progress_title", period=self._text(uid, "period_" + period))
                + "\n\n"
                + self._text(
                    uid,
                    "progress_brief",
                    completed=stats["completed"],
                    opportunities=stats["opportunities"],
                    rate=percent,
                    current=stats["current_streak"],
                    best=stats["best_streak"],
                )
            )
        if days and detailed:
            marks = {"full": "●", "minimum": "◐", "fail": "×", "skip": "–", "missing": "·", "progress": "◔"}
            page = max(0, min(page, (len(days) - 1) // self.PAGE_SIZE))
            out += "\n" + "\n".join(
                f"{x['day']} {self._text(uid, 'private_habit') if x['sensitive'] else x['title']}: {marks.get(x['status'], '·')}"
                for x in days[page * self.PAGE_SIZE : (page + 1) * self.PAGE_SIZE]
            )
        rows: list[list[tuple[str, str, dict[str, Any]]]] = [
            [
                (self._text(uid, "period_week"), "progress", {"period": "week"}),
                (self._text(uid, "period_month"), "progress", {"period": "month"}),
                (self._text(uid, "period_year"), "progress", {"period": "year"}),
            ],
            [(self._text(uid, "period_custom"), "custom_period", {})],
        ]
        for independent_hid in stats.get("independence", []):
            rows.append([(self._text(uid, "independence"), "independence", {"hid": independent_hid})])
        if not detailed:
            rows.append(
                [
                    (
                        self._text(uid, "show_details"),
                        "progress",
                        {"period": period, "start": start, "end": end, "detailed": True},
                    )
                ]
            )
        rows += [*self._home_rows(uid)]
        if detailed:
            rows += self._pagination(
                uid, "progress", page, len(days), {"period": period, "start": start, "end": end, "detailed": True}
            )
        self._send(uid, out, rows)

    def _friends(self, uid: int, page: int = 0) -> None:
        friends = self.service.extras.friends(uid)
        rows: list[list[tuple[str, str, dict[str, Any]]]] = [
            [(self._text(uid, "friend_invite"), "invite", {})],
            [(self._text(uid, "friend_accept"), "friend_accept", {})],
            [(self._text(uid, "friend_share"), "share_card", {})],
            [
                (self._text(uid, "challenge"), "challenges", {}),
                (self._text(uid, "challenge_create"), "challenge_create", {}),
            ],
        ]
        for f in friends[page * self.PAGE_SIZE : (page + 1) * self.PAGE_SIZE]:
            rows.append([(f.get("name") or str(f["id"]), "friend_detail", {"friend": f["id"]})])
        rows += self._pagination(uid, "friends", page, len(friends))
        rows += [*self._home_rows(uid)]
        self._send(uid, self._text(uid, "friends_title") if friends else self._text(uid, "friend_empty"), rows)

    def _share_card(self, uid: int) -> str:
        habits = [h for h in self.service.habits(uid) if not h.get("sensitive") and not h.get("archived")]
        lines = [self._text(uid, "card_title"), self._text(uid, "card_count", count=len(habits))]
        for h in habits[:10]:
            stats = self.service.stats(uid, hid=h["id"])
            lines.append(self._text(uid, "card_item", title=h["title"], count=stats.get("completed", 0)))
        return "\n".join(lines)

    def _preset_menu(self, uid: int) -> None:
        names = ("preset_read", "preset_walk", "preset_water")
        rows = [[(self._text(uid, name), "preset", {"name": name})] for name in names]
        rows.append([(self._text(uid, "back"), "habits", {})])
        self._send(uid, self._text(uid, "preset_intro"), rows)

    def _preset_preview(self, uid: int, name: str) -> None:
        choices = {
            "preset_read": dict(
                title=self._text(uid, "preset_read_title"),
                kind="quantity",
                target=5,
                unit="pages",
                schedule="daily",
                n=1,
                minimum=1,
            ),
            "preset_walk": dict(
                title=self._text(uid, "preset_walk_title"),
                kind="quantity",
                target=10,
                unit="minutes",
                schedule="weekly",
                n=3,
                minimum=2,
            ),
            "preset_water": dict(
                title=self._text(uid, "preset_water_title"),
                kind="quantity",
                target=1,
                unit="glasses",
                schedule="daily",
                n=1,
                minimum=None,
            ),
        }
        if name not in choices:
            raise ValueError("error_input")
        choice = choices[name]
        rule = {"type": choice["schedule"], "n": choice["n"]}
        values = {
            "title": choice["title"],
            "kind": choice["kind"],
            "target": choice["target"],
            "unit": choice["unit"],
            "schedule": choice["schedule"],
            "n": choice["n"],
            "minimum": choice["minimum"],
            "start": self.service.day(uid),
            "reminders": [],
            "verify": "none",
            "preview_schedule": rule,
        }
        self._begin(uid, "create", "preview", values)
        self._flow_prompt(uid)

    def _day_toggle(self, uid: int, day: int) -> None:
        if not 0 <= day <= 6:
            raise ValueError("error_input")
        session = self._session(uid)
        values = session.get("values", {})
        selected = set(values.get("days", []))
        if day in selected:
            selected.remove(day)
        else:
            selected.add(day)
        values["days"] = sorted(selected)
        session["values"] = values
        self._session(uid, session)
        self._flow_prompt(uid)

    def _callback_action(self, uid: int, p: dict[str, Any]) -> None:
        rows: list[list[tuple[str, str, dict[str, Any]]]]
        action = p.get("action")
        hid = str(p.get("hid", ""))
        if action in {
            "menu",
            "today",
            "habits",
            "progress",
            "friends",
            "settings",
            "support",
            "admin",
            "detail",
            "habit_more",
            "edit_begin",
            "edit_more",
            "settings_section",
            "preset_menu",
            "more_menu",
            "help",
        }:
            self.service.session(uid, {})
        if "_flow" in p:
            session = self._session(uid)
            if (
                p["_flow"] != session.get("id")
                or p["_step"] != session.get("step")
                or p.get("_revision") != session.get("revision")
            ):
                raise ValueError("error_stale")
        if self.ui.action(uid, p):
            return
        if action == "menu":
            self._main_menu(uid)
        elif action == "more_menu":
            self._more_menu(uid)
        elif action == "help":
            self._show_help(uid)
        elif action in {"today", "reminder_done"}:
            if action == "reminder_done":
                if p.get("day") != self.service.day(uid):
                    raise ValueError("error_stale")
                self._record_action(uid, hid, "done", p.get("day"))
            else:
                self._show_today(uid, int(p.get("page", 0)))
        elif action == "reminder_snooze":
            self._send(
                uid,
                self._text(uid, "snooze_prompt"),
                [
                    [
                        (
                            self._text(uid, "minute_choice", n=n),
                            "snooze",
                            {"hid": hid, "day": p.get("day"), "minutes": n},
                        )
                        for n in (15, 30, 60)
                    ]
                ],
            )
        elif action == "snooze":
            self.service.snooze(uid, hid, int(p.get("minutes", 15)), day=p.get("day"))
            self._send(uid, self._text(uid, "snoozed", minutes=p.get("minutes", 15)), self._home_rows(uid))
        elif action == "reminder_skip":
            if p.get("day") != self.service.day(uid):
                raise ValueError("error_stale")
            key = self._action_key(uid, hid, str(p["day"]), "skip")
            self.service.record(uid, hid, key, status="skip", day=p.get("day"))
            self._send(uid, self._text(uid, "marked"), self._home_rows(uid))
        elif action == "habits":
            self._show_habits(uid)
        elif action == "page":
            self._show_habits(uid, int(p.get("page", 0)), bool(p.get("archived")))
        elif action == "create_begin":
            self._begin(uid, "create", "title")
            self._flow_prompt(uid)
        elif action == "preset_menu":
            self._preset_menu(uid)
        elif action == "preset":
            self._preset_preview(uid, str(p.get("name", "")))
        elif action == "day_toggle":
            self._day_toggle(uid, int(p["day"]))
        elif action == "flow_days_done":
            if not self._session(uid).get("values", {}).get("days"):
                raise ValueError("error_input")
            state = self._session(uid)
            if state.get("values", {}).get("_quick"):
                self.ui.next_create(uid, state)
            else:
                self._advance(uid, "minimum")
        elif action == "use_telegram_name":
            name = str(p.get("name", ""))[:80]
            if name:
                self.service.settings(uid, name=name)
            session = self._session(uid)
            session.setdefault("values", {})["name"] = name
            self._session(uid, session)
            self._advance(uid, "timezone")
        elif action == "create_save":
            v = self._session(uid).get("values", {})
            self.service.create(uid, self._create_spec(uid, v))
            self.service.session(uid, {})
            self._send(uid, self._text(uid, "create_created"), self._home_rows(uid))
        elif action == "flow_cancel":
            self.service.session(uid, {})
            self._send(uid, self._text(uid, "cancelled"), self._home_rows(uid))
        elif action == "flow_back":
            self._back(uid)
        elif action == "flow_skip":
            self._skip(uid)
        elif action == "flow_pick":
            self._pick(uid, p.get("value"))
        elif action == "record":
            self._record_action(uid, hid, p.get("status", "done"), p.get("day"))
        elif action == "bulk":
            key = self._action_key(uid, "bulk", self.service.day(uid), "done")
            records = self.service.bulk(uid, key)
            rows = [
                [(self._text(uid, "undo") + f" {n + 1}", "undo", {"key": key + ":" + record["habit_id"]})]
                for n, record in enumerate(records)
            ]
            self._send(uid, self._text(uid, "marked"), rows + self._home_rows(uid))
        elif action == "record_amount":
            self._begin(uid, "amount", "amount", {"hid": hid, "day": p.get("day")})
            self._flow_prompt(uid)
        elif action == "record_note_begin":
            self._begin(
                uid, "record_note", "note", {"hid": hid, "status": p.get("status", "done"), "day": p.get("day")}
            )
            self._flow_prompt(uid)
        elif action == "correct_record":
            habit = self.service.habit(uid, hid, p.get("day"))
            status = p.get("status", "done")
            if habit["spec"].get("kind") == "binary":
                self.service.correct_record(
                    uid, hid, self._action_key(uid, hid, p["day"], "correct"), status=status, day=p["day"]
                )
                self._send(uid, self._text(uid, "marked"), self._home_rows(uid))
            else:
                self._begin(uid, "correct_amount", "amount", {"hid": hid, "day": p["day"], "status": status})
                self._flow_prompt(uid)
        elif action == "timer_start":
            started = self.service.extras.start_timer(uid, hid)
            self._send(
                uid,
                self._text(uid, "timer_note") + f"\n{started.get('started_at', '')}",
                [[(self._text(uid, "done"), "timer_finish", {"hid": hid})]],
            )
        elif action == "timer_finish":
            seconds = self.service.extras.finish_timer(uid, hid)
            habit = self.service.habit(uid, hid)
            if habit["spec"].get("kind") == "binary":
                self._record_action(uid, hid, "done", seconds=seconds)
            else:
                self._begin(uid, "amount", "amount", {"hid": hid, "timer_seconds": seconds})
                self._flow_prompt(uid)
        elif action == "evidence_begin":
            h = self.service.habit(uid, hid, p.get("day"))
            method = h["spec"].get("verify")
            if method not in {"text", "photo"}:
                raise ValueError("error_input")
            self._begin(uid, "evidence", "attach", {"hid": hid, "day": p.get("day"), "method": method})
            self._flow_prompt(uid)
        elif action == "friend_verify_list":
            friends = [f for f in self.service.extras.friends(uid) if f.get("verifier_consent")]
            current_record = self.service.db.one(
                "SELECT status FROM records WHERE habit_id=? AND day=?", (hid, p.get("day") or self.service.day(uid))
            )
            if not current_record or current_record["status"] not in {"full", "minimum"}:
                raise ValueError("error_input")
            rows = [
                [
                    (
                        f.get("name") or str(f["id"]),
                        "friend_verify_request",
                        {"hid": hid, "friend": f["id"], "day": p.get("day") or self.service.day(uid)},
                    )
                ]
                for f in friends
            ]
            self._send(
                uid,
                self._text(uid, "friend_verify_none") if not rows else self._text(uid, "friend_verify_choose"),
                rows,
            )
        elif action == "friend_verify_request":
            day = str(p.get("day") or self.service.day(uid))
            friend = int(p["friend"])
            prior = self.service.db.one(
                "SELECT id,friend,status FROM verification_requests WHERE owner=? AND habit_id=? AND day=?",
                (uid, hid, day),
            )
            if prior and (prior["friend"] != friend or prior["status"] != "pending"):
                raise ValueError("error_stale")
            rid = self.service.extras.request_verification(uid, hid, day, friend)
            if not prior:
                yes = self._token(friend, "verify_decision", rid=rid, approve=True)
                no = self._token(friend, "verify_decision", rid=rid, approve=False)
                keyboard = [
                    [
                        {"text": self._text(friend, "yes"), "callback_data": yes},
                        {"text": self._text(friend, "no"), "callback_data": no},
                    ]
                ]
                habit = self.service.habit(uid, hid, day)
                rec = self.service.db.one("SELECT value FROM records WHERE habit_id=? AND day=?", (hid, day))
                detail = f"{self.service.user(uid)['name']} · {habit['title']}\n{day}: {rec['value']} {self._unit_label(friend, habit['spec']['unit'])}"
                self.transport.send(friend, self._text(friend, "friend_verify_received") + "\n" + detail, keyboard)
            self._send(uid, self._text(uid, "friend_verify_sent"), self._home_rows(uid))
        elif action == "verify_decision":
            self.service.extras.decide_verification(uid, str(p["rid"]), bool(p["approve"]))
            self._send(uid, self._text(uid, "friend_verify_decided"), self._home_rows(uid))
        elif action == "undo":
            undo_key: str | None = p.get("key") or self._session(uid).get("last_action")
            if not undo_key:
                raise ValueError("error_input")
            self.service.undo(uid, undo_key)
            self._send(uid, self._text(uid, "undone"), self._home_rows(uid))
        elif action == "history":
            hist = self.service.history(uid, hid, days=7)
            lines = [self._text(uid, "history_title")]
            for row in hist:
                lines.append(
                    f"{row.get('day')}: {self._text(uid, 'status_' + ('paused' if row['paused'] and not row['record'] else (row.get('record') or {}).get('status', 'none')))}"
                )
            self._send(
                uid,
                "\n".join(lines),
                [[(self._text(uid, "edit_record"), "history_edit", {"hid": hid})], *self._home_rows(uid)],
            )
        elif action == "history_edit":
            hist = self.service.history(uid, hid, days=7)
            rows = []
            for row in hist:
                if row.get("scheduled"):
                    rows.append([(row.get("day", "?"), "history_day", {"hid": hid, "day": row["day"]})])
            self._send(uid, self._text(uid, "history_title"), rows + [*self._home_rows(uid)])
        elif action == "history_day":
            record_rows: list[list[tuple[str, str, dict[str, Any]]]] = [
                [
                    (self._text(uid, "mark_done"), "correct_record", {"hid": hid, "day": p["day"], "status": "done"}),
                    (self._text(uid, "mark_fail"), "correct_record", {"hid": hid, "day": p["day"], "status": "fail"}),
                    (self._text(uid, "mark_skip"), "correct_record", {"hid": hid, "day": p["day"], "status": "skip"}),
                ]
            ]
            record_rows.extend(self._home_rows(uid))
            self._send(uid, p["day"], record_rows)
        elif action == "progress":
            self._progress(
                uid,
                p.get("period", "week"),
                p.get("start"),
                p.get("end"),
                int(p.get("page", 0)),
                bool(p.get("detailed")),
            )
        elif action == "custom_period":
            self._begin(uid, "custom_period", "start", {})
            self._flow_prompt(uid)
        elif action in {"support", "pause_choose", "pause_start", "pause_finish"}:
            self.service.session(uid, {})
            self._main_menu(uid)
        elif action == "urge_begin":
            self._begin(uid, "urge", "intensity", {"hid": hid, "episode": bool(p.get("episode"))})
            self._flow_prompt(uid)
        elif action == "journal":
            self._journal(uid, hid, int(p.get("page", 0)))
        elif action == "ai_choose":
            self._send(
                uid,
                self._text(uid, "ai_help"),
                [
                    [(self._text(uid, "ai_public"), "ai_begin", {"sensitive": False})],
                    [(self._text(uid, "ai_private"), "ai_begin", {"sensitive": True})],
                    *self._home_rows(uid),
                ],
            )
        elif action == "ai_begin":
            self._begin(uid, "ai", "prompt", {"sensitive": bool(p.get("sensitive"))})
            self._flow_prompt(uid)
        elif action == "edit_confirm":
            s = self._session(uid)
            v = s.get("values", {})
            if s.get("flow") != "edit_confirm":
                raise ValueError("error_stale")
            self.service.edit(uid, v["hid"], v["changes"], v["effective"])
            self.service.session(uid, {})
            self._send(uid, self._text(uid, "setting_updated"), self._home_rows(uid))
        elif action == "note_existing":
            self._begin(uid, "note_existing", "note", {"hid": hid, "day": p.get("day")})
            self._send(uid, self._text(uid, "note_prompt"), self._nav(uid))
        elif action == "settings":
            self._show_settings(uid)
        elif action == "setting":
            field = p["field"]
            if field == "language":
                self._send(
                    uid,
                    self._text(uid, "onboard_lang"),
                    [[(self._text(uid, "language_" + lang), "language", {"value": lang}) for lang in LANGUAGES]]
                    + self._home_rows(uid),
                )
            elif field in {"tone", "reminders", "gamification", "ai", "ai_sensitive"}:
                if field == "tone":
                    options = [("calm", "tone_calm"), ("friendly", "tone_friendly"), ("strict", "tone_strict")]
                else:
                    options_bool = [(True, "yes"), (False, "no")]
                    self._send(
                        uid,
                        self._text(uid, "settings_prompt", field=self._text(uid, "setting_" + field)),
                        [
                            [
                                (self._text(uid, label), "setting_value", {"field": field, "value": value})
                                for value, label in options_bool
                            ]
                        ]
                        + self._home_rows(uid),
                    )
                    return
                self._send(
                    uid,
                    self._text(uid, "settings_prompt", field=self._text(uid, "setting_" + field)),
                    [
                        [
                            (self._text(uid, label), "setting_value", {"field": field, "value": value})
                            for value, label in options
                        ]
                    ]
                    + self._home_rows(uid),
                )
            else:
                self._begin(uid, "setting", "value", {"field": field})
                self._flow_prompt(uid)
        elif action == "language":
            self.service.settings(uid, lang=p["value"])
            self._show_settings(uid)
        elif action == "setting_value":
            field = p["field"]
            api = self.SETTINGS[field]
            self.service.settings(uid, **{api: p["value"]})
            self._send(uid, self._text(uid, "setting_updated"), self._home_rows(uid))
        elif action == "friends":
            self._friends(uid, int(p.get("page", 0)))
        elif action == "invite":
            token = self.service.extras.invite(uid)
            self._send(uid, self._text(uid, "friend_invite_text", token=token), self._home_rows(uid))
        elif action == "friend_accept":
            self._begin(uid, "friend_token", "token")
            self._flow_prompt(uid)
        elif action == "friend_detail":
            friend = int(p["friend"])
            shared = self.service.extras.shared(uid, friend)
            page = max(0, int(p.get("page", 0)))
            lines = [
                f"• {h['title']}: {self._text(uid, 'status_' + (h.get('latest') or {}).get('status', 'none'))}"
                for h in shared[page * 8 : (page + 1) * 8]
            ]
            own_consent = self.service.db.one(
                "SELECT enabled FROM verifier_consent WHERE owner=? AND friend=?", (friend, uid)
            )
            consent_label = (
                self._text(uid, "friend_agree")
                if not own_consent or not own_consent["enabled"]
                else self._text(uid, "friend_revoke_consent")
            )
            self._send(
                uid,
                "\n".join(lines) or self._text(uid, "friend_empty"),
                [
                    [(self._text(uid, "friend_visibility"), "visibility_menu", {"friend": friend})],
                    [
                        (
                            consent_label,
                            "verifier_consent",
                            {"friend": friend, "enabled": not bool(own_consent and own_consent["enabled"])},
                        )
                    ],
                    [(self._text(uid, "friend_revoke"), "friend_revoke", {"friend": friend})],
                    *self._pagination(uid, "friend_detail", page, len(shared), {"friend": friend}),
                    *self._home_rows(uid),
                ],
            )
        elif action == "visibility_menu":
            friend = int(p["friend"])
            rows = []
            habits = [h for h in self.service.habits(uid) if not h.get("sensitive")]
            page = max(0, int(p.get("page", 0)))
            for h in habits[page * 8 : (page + 1) * 8]:
                visible = self.service.db.one(
                    "SELECT visible FROM visibility WHERE owner=? AND friend=? AND habit_id=?", (uid, friend, h["id"])
                )
                label = ("✓ " if visible and visible["visible"] else "") + h["title"]
                rows.append(
                    [
                        (
                            label[:40],
                            "visibility_toggle",
                            {"friend": friend, "hid": h["id"], "visible": not bool(visible and visible["visible"])},
                        )
                    ]
                )
            self._send(
                uid,
                self._text(uid, "friend_visibility"),
                rows
                + self._pagination(uid, "visibility_menu", page, len(habits), {"friend": friend})
                + [*self._home_rows(uid)],
            )
        elif action == "visibility_toggle":
            self.service.extras.visibility(uid, hid, int(p["friend"]), bool(p["visible"]))
            self._friends(uid)
        elif action == "verifier_consent":
            if p.get("enabled", True):
                self._send(
                    uid,
                    self._text(uid, "friend_agree_prompt"),
                    [
                        [
                            (self._text(uid, "yes"), "verifier_confirm", {"friend": int(p["friend"])}),
                            (self._text(uid, "no"), "friends", {}),
                        ]
                    ],
                )
            else:
                self.service.extras.consent_verifier(uid, int(p["friend"]), False)
                self._friends(uid)
        elif action == "verifier_confirm":
            self.service.extras.consent_verifier(uid, int(p["friend"]), True)
            self._send(uid, self._text(uid, "consent_saved"), self._home_rows(uid))
        elif action == "friend_revoke":
            self.service.extras.revoke(uid, int(p["friend"]))
            self._send(uid, self._text(uid, "friend_revoke_done"), self._home_rows(uid))
        elif action == "share_card":
            self._send(uid, self._text(uid, "friend_card", card=self._share_card(uid)), self._home_rows(uid))
        elif action == "challenges":
            xs = self.service.extras.challenges(uid)
            page = int(p.get("page", 0))
            msg = "\n".join(
                f"• {x['title']} — {x['completed']}/{x['target']} · {x['start']}–{x['end']}"
                for x in xs[page * 8 : (page + 1) * 8]
            ) or self._text(uid, "challenge_empty")
            self._send(
                uid,
                msg,
                self._pagination(uid, "challenges", page, len(xs))
                + [[(self._text(uid, "challenge_title"), "challenge_create", {})], *self._home_rows(uid)],
            )
        elif action == "challenge_create":
            self._begin(uid, "challenge", "title", {})
            self._flow_prompt(uid)
        elif action == "challenge_confirm":
            s = self._session(uid)
            v = s["values"]
            if s.get("flow") != "challenge" or s.get("step") != "confirm":
                raise ValueError("error_stale")
            self.service.extras.challenge(uid, v["title"], v["target"], v["start"], v["end"])
            self.service.session(uid, {})
            self._send(uid, self._text(uid, "challenge_created"), self._home_rows(uid))
        elif action == "export":
            self.transport.document(uid, "kitmode-export.json", self.service.extras.export_json(uid).encode("utf-8"))
        elif action == "export_csv":
            self.transport.document(uid, "kitmode-export.csv", self.service.extras.export_csv(uid).encode("utf-8"))
        elif action == "import":
            self._send(uid, self._text(uid, "import_prompt"))
        elif action == "import_confirm":
            session = self._session(uid)
            payload = session.get("values", {}).get("payload")
            if not payload:
                raise ValueError("error_stale")
            self.service.extras.import_data(uid, payload)
            self.service.session(uid, {})
            self._send(uid, self._text(uid, "import_done"), self._home_rows(uid))
        elif action == "delete":
            self._begin(uid, "delete", "confirm")
            self._send(
                uid,
                self._text(uid, "delete_confirm"),
                [[(self._text(uid, "confirm"), "delete_confirm", {}), (self._text(uid, "cancel"), "menu", {})]],
            )
        elif action == "delete_confirm":
            lang = self.service.user(uid)["lang"]
            self.service.extras.delete_user(uid)
            self._send(uid, tr(lang, "delete_done"))
        elif action == "feedback":
            self._begin(uid, "feedback", "text", {})
            self._flow_prompt(uid)
        elif action == "admin":
            x = self.service.extras.admin(uid, self.admin_ids)
            self._send(
                uid,
                self._text(
                    uid,
                    "admin_summary",
                    users=x.get("users", 0),
                    habits=x.get("habits", 0),
                    feedback=x.get("open_feedback", 0),
                ),
                [
                    [
                        (self._text(uid, "feedback"), "admin_feedback", {}),
                        (self._text(uid, "admin_system"), "admin_system", {}),
                    ],
                    [
                        (self._text(uid, "admin_block"), "admin_block_begin", {"blocked": True}),
                        (self._text(uid, "admin_unblock"), "admin_block_begin", {"blocked": False}),
                    ],
                ],
            )
        elif action == "admin_feedback":
            items = self.service.extras.admin_feedback(uid, self.admin_ids)
            page = int(p.get("page", 0))
            rows = []
            lines = []
            for item in items[page * 8 : (page + 1) * 8]:
                lines.append(f"{item['created_at']} · {item['owner']}\n{item['message'][:400]}")
                rows.append([(self._text(uid, "admin_resolve"), "admin_resolve", {"feedback_id": item["id"]})])
            self._send(
                uid,
                "\n\n".join(lines) or self._text(uid, "friend_empty"),
                rows + self._pagination(uid, "admin_feedback", page, len(items)),
            )
        elif action == "admin_resolve":
            self.service.extras.resolve_feedback(uid, p["feedback_id"], self.admin_ids)
            self._callback_action(uid, {"action": "admin"})
        elif action == "admin_system":
            x = self.service.extras.system_status(uid, self.admin_ids)
            self._send(
                uid,
                self._text(
                    uid,
                    "admin_system_line",
                    database=x["database"],
                    jobs=x["pending_jobs"],
                    errors=sum(x["errors_24h"].values()),
                ),
            )
        elif action == "admin_block_begin":
            if uid not in self.admin_ids:
                raise ValueError("error_access")
            self._begin(uid, "admin_block", "id", {"blocked": bool(p["blocked"])})
            self._flow_prompt(uid)
        elif action == "independence":
            self._send(
                uid,
                self._text(uid, "independence_offer"),
                [
                    [
                        (self._text(uid, "independence_yes"), "independence_confirm", {"hid": hid}),
                        (self._text(uid, "independence_no"), "menu", {}),
                    ]
                ],
            )
        elif action == "independence_confirm":
            if not isinstance(hid, str):
                raise ValueError("error_input")
            self._independence(uid, hid)
        elif action == "pause":
            self.service.pause(uid, hid, self.service.day(uid), None)
            self._send(uid, self._text(uid, "habit_paused"), self._home_rows(uid))
        elif action == "archive":
            self.service.archive(uid, hid)
            self._send(uid, self._text(uid, "habit_archived"), self._home_rows(uid))
        elif action == "edit_begin":
            fields = {
                "title": "habit_title",
                "target": "create_target",
                "minimum": "habit_minimum",
                "reminders": "habit_reminders",
                **{
                    k: "field_" + k
                    for k in ("description", "category", "icon", "routine", "repeat", "end", "private_title", "verify")
                },
            }
            rows = [
                [(self._text(uid, label), "edit_field", {"hid": hid, "field": field})]
                for field, label in fields.items()
            ]
            self._send(
                uid,
                self._text(uid, "habit_edit"),
                rows + [[(self._text(uid, "habit_schedule"), "edit_schedule", {"hid": hid})], *self._home_rows(uid)],
            )
        elif action == "edit_field":
            if p["field"] == "verify":
                h = self.service.habit(uid, hid)
                choices = ["none", "text", "timer"] + ([] if h["sensitive"] else ["photo", "friend"])
                self._send(
                    uid,
                    self._text(uid, "create_verify"),
                    [
                        [(self._text(uid, "verify_" + method), "edit_verify", {"hid": hid, "method": method})]
                        for method in choices
                    ]
                    + self._home_rows(uid),
                )
            else:
                self._begin(uid, "edit", "value", {"hid": hid, "field": p["field"]})
                self._flow_prompt(uid)
        elif action == "edit_verify":
            self._edit_preview(uid, hid, {"verify": p["method"]})
        elif action == "edit_schedule":
            self._send(
                uid,
                self._text(uid, "create_schedule"),
                [
                    [
                        (self._text(uid, "schedule_" + x), "edit_schedule_type", {"hid": hid, "type": x})
                        for x in ("daily", "weekdays", "weekly", "interval")
                    ]
                ],
            )
        elif action == "edit_schedule_type":
            if p["type"] == "daily":
                self._edit_preview(uid, hid, {"schedule": {"type": "daily"}})
            else:
                state = self._session(uid)
                state.setdefault("values", {})["schedule_type"] = p["type"]
                self._advance(uid, "schedule_value", state)
        elif action == "resume":
            self.service.resume(uid, hid)
            self._send(uid, self._text(uid, "habit_resumed"), self._home_rows(uid))
        else:
            self._send(uid, self._text(uid, "unknown"), self._home_rows(uid))

    def _advance(self, uid: int, step: str, state: dict[str, Any] | None = None) -> None:
        s = state if state is not None else self._session(uid)
        s.setdefault("history", []).append(s.get("step"))
        s["step"] = step
        self._session(uid, s)
        self._flow_prompt(uid)

    def _back(self, uid: int) -> None:
        s = self._session(uid)
        history = s.get("history", [])
        if history:
            s["step"] = history.pop()
            self._session(uid, s)
            self._flow_prompt(uid)
        else:
            self.service.session(uid, {})
            self._send(uid, self._text(uid, "cancelled"), self._home_rows(uid))

    def _skip(self, uid: int) -> None:
        if self.ui.skip(uid):
            return
        s = self._session(uid)
        flow = s.get("flow")
        step = s.get("step")
        v = s.setdefault("values", {})
        if not isinstance(step, str):
            self._send(uid, self._text(uid, "error_stale"))
            return
        if flow == "onboard":
            nxt = {"name": "timezone", "timezone": "sleep", "sleep": "boundary", "boundary": "tone"}.get(step)
            if step == "tone":
                self._finish_onboard(uid, v)
                return
            if nxt is not None:
                self._advance(uid, nxt, s)
            else:
                self._send(uid, self._text(uid, "error_stale"))
            return
        if flow == "create":
            nxt = {
                "minimum": "start",
                "start": "end",
                "end": "sensitive",
                "reminder": "description",
                "description": "category",
                "category": "icon",
                "icon": "routine",
                "routine": "repeat",
                "repeat": "preview",
            }.get(step)
            if step == "minimum":
                v.pop("minimum", None)
            elif step == "start":
                v.pop("start", None)
            elif step == "end":
                v.pop("end", None)
            elif step == "reminder":
                v["reminders"] = []
            elif step in {"description", "category", "icon", "routine"}:
                v[step] = ""
            elif step == "repeat":
                v["repeat"] = 0
            if step == "sensitive":
                v["sensitive"] = False
                nxt = "verify"
            if nxt is not None:
                self._advance(uid, nxt, s)
            else:
                self._send(uid, self._text(uid, "error_stale"))
            return
        if flow == "edit":
            if step == "value" and v.get("field") == "end":
                self._edit_preview(uid, v["hid"], {"end": None})
                return
            if step == "value" and v.get("field") == "minimum":
                self._edit_preview(uid, v["hid"], {"minimum": None})
                return
            if step == "value" and v.get("field") == "reminders":
                self._edit_preview(uid, v["hid"], {"reminders": []})
                return
        if flow == "note":
            self._commit_note(uid, "")
            return
        if flow == "record_note":
            self._record_action(uid, v["hid"], v.get("status", "done"), v.get("day"))
            return
        if flow == "evidence":
            self.service.session(uid, {})
            self._send(uid, self._text(uid, "marked"), self._result_rows(uid, v["hid"], v.get("day")))
            return
        if flow == "urge":
            self._save_urge(uid, "")
            return
        self._send(uid, self._text(uid, "error_input"))

    def _pick(self, uid: int, value: Any) -> None:
        if self.ui.pick(uid, value):
            return
        s = self._session(uid)
        flow = s.get("flow")
        step = s.get("step")
        v = s.setdefault("values", {})
        if not isinstance(step, str):
            self._send(uid, self._text(uid, "error_stale"))
            return
        if flow == "onboard":
            key = {"language": "lang", "tone": "tone"}.get(step)
            if key:
                v[key] = value
            if step == "language":
                self.service.settings(uid, lang=value)
                self._advance(uid, "name", s)
            elif step == "tone":
                self._finish_onboard(uid, v)
        elif flow == "create":
            mapsteps = {
                "kind": ("kind", None),
                "unit": ("unit", "schedule"),
                "schedule": ("schedule", None),
                "sensitive": ("sensitive", "verify"),
                "verify": ("verify", "reminder"),
            }
            if step in mapsteps:
                field, nxt = mapsteps[step]
                v[field] = value
                if step == "unit" and value == "custom":
                    nxt = "custom_unit"
                elif step == "schedule":
                    nxt = {"weekdays": "days", "weekly": "n", "interval": "n", "daily": "minimum"}[value]
                elif step == "kind":
                    nxt = "target" if value != "binary" else "schedule"
                elif step == "verify":
                    nxt = "reminder"
                if nxt is not None:
                    self._advance(uid, nxt, s)
                else:
                    self._send(uid, self._text(uid, "error_stale"))
        elif flow == "urge":
            v["intensity"] = int(value)
            self._advance(uid, "trigger", s)

    def _parse_input(self, value: str) -> Any:
        value = value.strip()
        if value.lower() in {"/skip"}:
            return None
        return value

    def _message_input(self, uid: int, text: str) -> bool:
        s = self._session(uid)
        flow = s.get("flow")
        if not flow:
            return False
        if text.strip() == "/skip":
            self._skip(uid)
            return True
        if text.strip() == "/cancel":
            self.service.session(uid, {})
            self._send(uid, self._text(uid, "cancelled"), self._home_rows(uid))
            return True
        step = s.get("step")
        v = s.setdefault("values", {})
        clean = self._parse_input(text)
        try:
            if self.ui.input(uid, text):
                return True
            if flow == "onboard":
                if step == "name":
                    v["name"] = clean[:80]
                    self.service.settings(uid, name=v["name"])
                    self._advance(uid, "timezone", s)
                elif step == "timezone":
                    v["tz"] = clean
                    self.service.settings(uid, tz=clean)
                    self._advance(uid, "sleep", s)
                elif step == "sleep":
                    v["sleep"] = self._time(clean)
                    self.service.settings(uid, sleep=v["sleep"])
                    self._advance(uid, "boundary", s)
                elif step == "boundary":
                    v["boundary"] = self._time(clean)
                    self.service.settings(uid, boundary=v["boundary"])
                    self._advance(uid, "tone", s)
            elif flow == "create":
                if step == "title":
                    v["title"] = clean[:100]
                    self._advance(uid, "kind", s)
                elif step == "target":
                    v["target"] = float(clean)
                    self._advance(uid, "unit", s)
                elif step == "custom_unit":
                    v["unit"] = clean[:32]
                    self._advance(uid, "schedule", s)
                elif step == "days":
                    days = sorted({int(x.strip()) - 1 for x in clean.split(",")})
                    if not days or min(days) < 0 or max(days) > 6:
                        raise ValueError
                    v["days"] = days
                    self._advance(uid, "minimum", s)
                elif step == "n":
                    n = int(clean)
                    if n < 1 or n > 365:
                        raise ValueError
                    v["n"] = n
                    self._advance(uid, "minimum", s)
                elif step == "minimum":
                    if v.get("kind") == "binary":
                        v["minimum"] = 0.5
                        v["minimum_description"] = clean[:500]
                    else:
                        v["minimum"] = float(clean)
                    self._advance(uid, "start", s)
                elif step in {"start", "end"}:
                    date.fromisoformat(clean)
                    v[step] = clean
                    self._advance(uid, "end" if step == "start" else "sensitive", s)
                elif step == "reminder":
                    v["reminders"] = [self._time(x.strip()) for x in clean.split(",") if x.strip()]
                    self._advance(uid, "description", s)
                elif step in {"description", "category", "icon", "routine"}:
                    v[step] = clean[:500]
                    self._advance(
                        uid,
                        {"description": "category", "category": "icon", "icon": "routine", "routine": "repeat"}[step],
                        s,
                    )
                elif step == "repeat":
                    repeat = int(clean)
                    if repeat not in {0, 15, 30, 60, 120}:
                        raise ValueError
                    v[step] = repeat
                    self._advance(uid, "preview", s)
            elif flow == "setting":
                field = v["field"]
                api = self.SETTINGS[field]
                val = clean
                if field in {"reminders", "gamification", "ai", "ai_sensitive"}:
                    val = clean.lower() in {"1", "true", "yes", "on", "так", "да"}
                if field == "language" and val not in LANGUAGES:
                    raise ValueError
                if field in {"sleep", "boundary", "quiet", "quiet_end"}:
                    val = self._time(clean)
                self.service.settings(uid, **{api: val})
                self.service.session(uid, {})
                self._send(uid, self._text(uid, "setting_updated"), self._home_rows(uid))
            elif flow == "record_note":
                self._record_action(uid, v["hid"], v.get("status", "done"), v.get("day"), note=clean[:1000])
            elif flow == "evidence":
                if v.get("method") != "text":
                    raise ValueError("error_input")
                self.service.evidence(uid, v["hid"], "text", note=clean[:1000], day=v.get("day"))
                self.service.session(uid, {})
                self._send(uid, self._text(uid, "proof_saved"), self._result_rows(uid, v["hid"], v.get("day")))
            elif flow == "amount":
                hid = v["hid"]
                day = v.get("day") or self.service.day(uid)
                key = self._action_key(uid, hid, day, "quantity")
                habit = self.service.habit(uid, hid, day)
                amount = float(clean)
                if v.get("minimum_limit"):
                    amount = float(clean)
                self.service.record(uid, hid, key, status="done", amount=amount, day=day)
                self.service.session(uid, {})
                if v.get("timer_seconds") is not None and habit["spec"].get("verify") == "timer":
                    self.service.evidence(uid, hid, "timer", day=day, seconds=int(v["timer_seconds"]))
                elif habit["spec"].get("verify") == "text":
                    self._begin(uid, "evidence", "attach", {"hid": hid, "day": day, "method": "text"})
                    self._flow_prompt(uid)
                    return True
                elif habit["spec"].get("verify") == "photo":
                    self._begin(uid, "evidence", "attach", {"hid": hid, "day": day, "method": "photo"})
                    self._flow_prompt(uid)
                    return True
                self._send(
                    uid,
                    self._text(uid, "marked"),
                    [[(self._text(uid, "undo"), "undo", {"key": key})], *self._home_rows(uid)],
                )
            elif flow == "correct_amount":
                hid = v["hid"]
                day = v["day"]
                key = self._action_key(uid, hid, day, "correct")
                self.service.correct_record(uid, hid, key, status=v.get("status", "done"), amount=float(clean), day=day)
                self.service.session(uid, {})
                self._send(uid, self._text(uid, "marked"), self._home_rows(uid))
            elif flow == "urge":
                self._save_urge(uid, clean)
            elif flow == "friend_token":
                self.service.extras.accept_invite(uid, clean)
                self.service.session(uid, {})
                self._send(uid, self._text(uid, "friend_accepted"), self._home_rows(uid))
            elif flow == "feedback":
                self.service.extras.feedback(uid, clean[:4000])
                self.service.session(uid, {})
                self._send(uid, self._text(uid, "feedback_done"), self._home_rows(uid))
            elif flow == "custom_period":
                date.fromisoformat(clean)
                v[step] = clean
                if step == "start":
                    self._advance(uid, "end", s)
                else:
                    if v["end"] < v["start"]:
                        raise ValueError
                    self.service.session(uid, {})
                    self._progress(uid, "custom", v["start"], v["end"])
            elif flow == "challenge":
                if step == "title":
                    v[step] = clean[:100]
                    self._advance(uid, "target", s)
                elif step == "target":
                    v[step] = int(clean)
                    self._advance(uid, "start", s)
                elif step in {"start", "end"}:
                    date.fromisoformat(clean)
                    v[step] = clean
                    if step == "start":
                        self._advance(uid, "end", s)
                    else:
                        if v["end"] < v["start"] or not 1 <= v["target"] <= 365:
                            raise ValueError
                        s["step"] = "confirm"
                        self._session(uid, s)
                        self._send(
                            uid,
                            self._text(uid, "confirm_challenge", **v),
                            [[(self._text(uid, "confirm"), "challenge_confirm", {})], *self._nav(uid)],
                        )
            elif flow == "edit":
                hid = v["hid"]
                if step == "schedule_value":
                    rule = {"type": v["schedule_type"]}
                    if rule["type"] == "weekdays":
                        rule["days"] = sorted({int(x.strip()) - 1 for x in clean.split(",")})
                        if not rule["days"] or min(rule["days"]) < 0 or max(rule["days"]) > 6:
                            raise ValueError
                    else:
                        rule["n"] = int(clean)
                        if rule["n"] < 1 or rule["n"] > (7 if rule["type"] == "weekly" else 365):
                            raise ValueError
                    self._edit_preview(uid, hid, {"schedule": rule})
                else:
                    field = v["field"]
                    if field == "title":
                        changes = {"title": clean[:100]}
                    elif field == "target":
                        changes = {"target": float(clean)}
                    elif field == "minimum":
                        sp = self.service.habit(uid, hid)["spec"]
                        changes = (
                            {"minimum": 0.5, "minimum_description": clean[:500]}
                            if sp["kind"] == "binary"
                            else {"minimum": float(clean)}
                        )
                    elif field == "reminders":
                        changes = {"reminders": [self._time(x.strip()) for x in clean.split(",") if x.strip()]}
                    elif field in {"description", "category", "icon", "routine", "private_title"}:
                        changes = {field: clean}
                    elif field == "repeat":
                        changes = {field: int(clean)}
                    elif field == "end":
                        changes = {field: date.fromisoformat(clean).isoformat()}
                    elif field == "verify":
                        changes = {field: clean.strip().lower()}
                    else:
                        raise ValueError
                    self._edit_preview(uid, hid, changes)
            elif flow == "note_existing":
                self.service.note(uid, v["hid"], clean, day=v.get("day"))
                self.service.session(uid, {})
                self._send(uid, self._text(uid, "marked"), self._home_rows(uid))
            elif flow == "ai":
                config = self.ai_config or AIConfig()
                result = self.service.extras.ai(uid, clean, config, requester=uid, sensitive=bool(v.get("sensitive")))
                label = (
                    "ai_label_optional"
                    if config.enabled and config.key and self.service.user(uid).get("ai_consent")
                    else "ai_label_local"
                )
                self.service.session(uid, {})
                self._send(uid, self._text(uid, label) + "\n" + result, self._home_rows(uid))
            elif flow == "admin_block":
                target = int(clean)
                if target <= 0 or not self.service.db.one("SELECT id FROM users WHERE id=?", (target,)):
                    raise ValueError("error_input")
                self.service.extras.block(uid, target, self.admin_ids, bool(v["blocked"]))
                self.service.session(uid, {})
                self._send(uid, self._text(uid, "blocked_done"), self._home_rows(uid))
        except TransportError:
            raise
        except Exception as exc:
            self._send(uid, self._error(uid, exc), self._nav(uid))
        return True

    @staticmethod
    def _time(text: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(text)):
            raise ValueError("error_input")
        return str(text)

    def _save_urge(self, uid: int, trigger: str) -> None:
        s = self._session(uid)
        v = s.get("values", {})
        self.service.extras.urge(
            uid, v["hid"], int(v["intensity"]), trigger=trigger[:500], episode=bool(v.get("episode"))
        )
        self.service.session(uid, {})
        self._send(
            uid,
            self._text(uid, "episode_saved" if v.get("episode") else "urge_saved")
            + "\n"
            + self._text(uid, "urge_alternative"),
            self._home_rows(uid),
        )

    def _record_action(
        self,
        uid: int,
        hid: str,
        status: str,
        day: str | None = None,
        amount: Any = None,
        seconds: int | None = None,
        note: str = "",
    ) -> None:
        day = day or self.service.day(uid)
        habit = self.service.habit(uid, hid, day)
        spec = habit["spec"]
        if status in {"done", "minimum"} and spec.get("kind") == "limit":
            self._begin(uid, "amount", "amount", {"hid": hid, "day": day, "minimum_limit": True})
            self._flow_prompt(uid)
            return
        if status == "done" and spec.get("kind") == "quantity" and amount is None:
            rec = self.service.db.one("SELECT value FROM records WHERE habit_id=? AND day=?", (hid, day))
            amount = max(0, float(spec["target"]) - (rec["value"] if rec else 0))
            if amount == 0:
                self._send(uid, self._text(uid, "marked"), self._home_rows(uid))
                return
        key = self._action_key(uid, hid, day, status)
        self.service.record(uid, hid, key, status=status, amount=amount, note=note, day=day)
        if seconds is not None and spec.get("verify") == "timer":
            self.service.evidence(uid, hid, "timer", day=day, seconds=seconds)
        elif status in {"done", "minimum"} and spec.get("verify") == "text":
            self._begin(uid, "evidence", "attach", {"hid": hid, "day": day, "method": "text"})
            self._flow_prompt(uid)
            return
        elif status in {"done", "minimum"} and spec.get("verify") == "photo":
            self._begin(uid, "evidence", "attach", {"hid": hid, "day": day, "method": "photo"})
            self._flow_prompt(uid)
            return
        elif status in {"done", "minimum"} and spec.get("verify") == "friend":
            self._send(
                uid,
                self._text(uid, "marked"),
                [
                    [(self._text(uid, "friend_confirm"), "friend_verify_list", {"hid": hid, "day": day})],
                    *self._home_rows(uid),
                ],
            )
            return
        self._send(
            uid, self._text(uid, "marked"), [[(self._text(uid, "undo"), "undo", {"key": key})], *self._home_rows(uid)]
        )

    def _action_key(self, uid: int, hid: str, day: str, action: str) -> str:
        update_key = self._current_update_key or secrets.token_urlsafe(12)
        return f"tg:{uid}:{update_key}:{hid}:{day}:{action}"

    def _photo_update(self, uid: int, message: dict[str, Any]) -> None:
        session = self._session(uid)
        if session.get("flow") != "evidence" or session.get("values", {}).get("method") != "photo":
            self._send(uid, self._text(uid, "photo_not_expected"))
            return
        photos = message.get("photo") or []
        if not photos:
            self._send(uid, self._text(uid, "proof_photo_prompt"))
            return
        photo = max(photos, key=lambda item: int(item.get("file_size", 0)))
        values = session["values"]
        self.service.evidence(uid, values["hid"], "photo", file_id=photo["file_id"], day=values.get("day"))
        self.service.session(uid, {})
        self._send(uid, self._text(uid, "proof_saved"), self._result_rows(uid, values["hid"], values.get("day")))

    def _commit_note(self, uid: int, note: str) -> None:
        s = self._session(uid)
        v = s.get("values", {})
        self.service.record(uid, v["hid"], v["key"], status=v.get("status", "done"), note=note[:2000])
        self.service.session(uid, {})
        self._send(uid, self._text(uid, "marked"), self._home_rows(uid))

    def _independence(self, uid: int, hid: str) -> None:
        h = self.service.habit(uid, hid)
        current = h["spec"].get("reminders", [])
        # A second confirmation check prevents reducing reminders if another
        # screen was opened while this button was outstanding.
        stats = self.service.stats(uid, hid=hid)
        if hid not in stats.get("independence", []):
            self._send(uid, self._text(uid, "error_stale"), self._home_rows(uid))
            return
        changes = {"reminders": current[::2] if len(current) > 1 else [], "independent": True}
        self.service.edit(uid, hid, changes)
        self._send(uid, self._text(uid, "independence_done"), self._home_rows(uid))

    def _error(self, uid: int, exc: Exception) -> str:
        key = getattr(exc, "key", None)
        if not key and str(exc) in {
            "error_input",
            "error_access",
            "error_old",
            "error_timezone",
            "error_future",
            "error_stale",
            "error_sensitive",
            "error_limit",
        }:
            key = str(exc)
        return self._text(uid, key or "error_generic")

    def handle(self, update: dict[str, Any]) -> None:
        """Handle one Telegram update; duplicate IDs are ignored transactionally."""
        if not isinstance(update, dict):
            return
        uid = None
        message = update.get("message") or update.get("edited_message")
        callback = update.get("callback_query")
        if message:
            uid = (message.get("from") or {}).get("id")
            chat = message.get("chat") or {}
        elif callback:
            uid = (callback.get("from") or {}).get("id")
            msg = callback.get("message") or {}
            chat = msg.get("chat") or {}
        else:
            return
        if uid is None:
            return
        if not isinstance(message, dict) and not isinstance(callback, dict):
            return
        uid = int(uid)
        callback_id = callback.get("id") if callback else None
        try:
            with self.service.db.transaction():
                update_id = update.get("update_id")
                self._current_update_key = str(update_id) if update_id is not None else secrets.token_urlsafe(12)
                if update_id is not None:
                    inserted = self.service.db.execute(
                        "INSERT OR IGNORE INTO updates(id,created_at) VALUES(?,?)",
                        (int(update_id), self.service.now().isoformat()),
                    )
                    if getattr(inserted, "rowcount", 1) == 0:
                        return
                    self._current_update_key = str(update_id)
                if (chat.get("type") or "private") != "private":
                    self.transport.send(uid, self._text(uid, "private_only"))
                    return
                user = self.service.user(uid, (message.get("from") or {}).get("first_name", "") if message else "")
                text = (message.get("text") or "").strip() if message else ""
                allowed_blocked = text in {"/export", "/delete"}
                if callback:
                    row = self.service.db.one(
                        "SELECT payload FROM buttons WHERE token=? AND user_id=?",
                        (str(callback.get("data", ""))[2:], uid),
                    )
                    allowed_blocked = bool(
                        row and json.loads(row["payload"]).get("action") in {"export", "delete", "delete_confirm"}
                    )
                if user.get("blocked") and not allowed_blocked:
                    if callback_id:
                        self.transport.answer(callback_id, self._text(uid, "blocked"))
                    self._send(uid, self._text(uid, "blocked"))
                    return
                self.service.settings(uid, last_seen=self.service.now().isoformat(), inactivity_sent=False)
                if callback:
                    try:
                        payload = self.service.consume_button(
                            uid,
                            str(callback.get("data", ""))[2:]
                            if str(callback.get("data", "")).startswith("b:")
                            else str(callback.get("data", "")),
                        )
                        if callback_id:
                            self.transport.answer(callback_id)
                        self._active_message = (uid, int((callback.get("message") or {}).get("message_id", 0)))
                        self._active_photo = bool((callback.get("message") or {}).get("photo"))
                        self._callback_action(uid, payload)
                    except Exception as exc:
                        if isinstance(exc, TransportError):
                            raise
                        if callback_id:
                            self.transport.answer(callback_id, self._error(uid, exc))
                        self._send(uid, self._error(uid, exc), self._home_rows(uid))
                    finally:
                        self._active_message = None
                        self._active_photo = False
                    return
                if not isinstance(message, dict):
                    return
                text = (message.get("text") or "").strip()
                if text.startswith("/start"):
                    self._welcoming = True
                    user = self.service.user(uid, (message.get("from") or {}).get("first_name", ""))
                    if self._session(uid).get("flow") and not user.get("onboarded"):
                        self._flow_prompt(uid)
                    elif not user.get("onboarded"):
                        self._begin(
                            uid,
                            "onboard",
                            "language",
                            {"telegram_name": (message.get("from") or {}).get("first_name", "")},
                        )
                        self._flow_prompt(uid)
                    else:
                        self._main_menu(uid)
                elif text.startswith("/help"):
                    self._show_help(uid)
                elif text.startswith("/cancel"):
                    self.service.session(uid, {})
                    self._send(uid, self._text(uid, "cancelled"), self._home_rows(uid))
                elif text.startswith("/admin"):
                    if uid not in self.admin_ids:
                        self._send(uid, self._text(uid, "error_access"))
                    else:
                        self._callback_action(uid, {"action": "admin"})
                elif message.get("photo"):
                    self._photo_update(uid, message)
                elif not text:
                    self._document_update(uid, message)
                elif text in {
                    "/today",
                    "/habits",
                    "/progress",
                    "/friends",
                    "/settings",
                    "/support",
                    "/ai",
                    "/import",
                    "/export",
                    "/delete",
                    "/resume",
                    "/menu",
                    "/new",
                }:
                    if text == "/resume":
                        self._flow_prompt(uid)
                    else:
                        route = {
                            "/menu": "menu",
                            "/new": "create_begin",
                            "/today": "today",
                            "/habits": "habits",
                            "/progress": "progress",
                            "/friends": "friends",
                            "/settings": "settings",
                            "/support": "support",
                            "/ai": "ai_choose",
                            "/import": "import",
                            "/export": "export",
                            "/delete": "delete",
                        }
                        self._callback_action(uid, {"action": route[text]})
                elif not self._message_input(uid, text):
                    route = {
                        "/today": "today",
                        "/habits": "habits",
                        "/progress": "progress",
                        "/friends": "friends",
                        "/settings": "settings",
                    }
                    if text in route:
                        self._callback_action(uid, {"action": route[text]})
                    else:
                        self._send(uid, self._text(uid, "unknown"), self._home_rows(uid))
        except TransportError as exc:
            if exc.code != 403:
                raise
            self.service.settings(uid, reminders=False)
            if update.get("update_id") is not None:
                self.service.db.execute(
                    "INSERT OR IGNORE INTO updates VALUES(?,?)",
                    (int(update["update_id"]), self.service.now().isoformat()),
                )
        except Exception as exc:
            self.service.extras.record_error("handler")
            try:
                self.transport.send(uid, self._error(uid, exc))
            except Exception:
                pass

        finally:
            self._welcoming = False

    def _document_update(self, uid: int, message: dict[str, Any]) -> None:
        doc = message.get("document")
        if not doc:
            return
        try:
            raw = self.transport.file(doc["file_id"])
            if len(raw) > 1_000_000:
                raise ValueError("error_limit")
            preview = self.service.extras.preview_import(uid, raw.decode("utf-8"))
            if preview.get("duplicate") or not preview.get("can_import"):
                self._send(uid, self._text(uid, "error_stale"))
                return
            self._begin(uid, "import_confirm", "confirm", {"payload": raw.decode("utf-8")})
            self._send(
                uid,
                self._text(uid, "import_preview", habits=preview.get("habits", 0), records=preview.get("records", 0)),
                [[(self._text(uid, "confirm"), "import_confirm", {}), (self._text(uid, "cancel"), "flow_cancel", {})]],
            )
        except TransportError:
            raise
        except Exception as exc:
            self._send(uid, self._error(uid, exc))
