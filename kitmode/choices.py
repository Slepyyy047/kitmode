"""Button-first conversation choices; domain rules stay in Service."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .core import DomainError, number
from .schedules import scheduled

Row = list[tuple[str, str, dict[str, Any]]]
REGIONS = (
    ("region_kyiv", "Europe/Kyiv"),
    ("region_warsaw", "Europe/Warsaw"),
    ("region_berlin", "Europe/Berlin"),
    ("region_london", "Europe/London"),
    ("region_newyork", "America/New_York"),
    ("region_utc", "UTC"),
    ("region_paris", "Europe/Paris"),
    ("region_prague", "Europe/Prague"),
    ("region_rome", "Europe/Rome"),
    ("region_madrid", "Europe/Madrid"),
    ("region_vilnius", "Europe/Vilnius"),
    ("region_chisinau", "Europe/Chisinau"),
    ("region_athens", "Europe/Athens"),
    ("region_helsinki", "Europe/Helsinki"),
    ("region_istanbul", "Europe/Istanbul"),
    ("region_dubai", "Asia/Dubai"),
    ("region_toronto", "America/Toronto"),
    ("region_losangeles", "America/Los_Angeles"),
)


class ChoiceUI:
    def __init__(self, bot: Any) -> None:
        self.b = bot

    def button(self, uid: int, key: str, action: str, **data: Any) -> tuple[str, str, dict[str, Any]]:
        return self.b._text(uid, key), action, data

    def choices(self, uid: int, field: str, values: list[tuple[str, Any]], width: int = 2) -> list[Row]:
        buttons = [(label, "choice", {"field": field, "value": value}) for label, value in values]
        return [buttons[i : i + width] for i in range(0, len(buttons), width)]

    def custom(self, uid: int) -> list[Row]:
        return [[self.button(uid, "custom_value", "custom_input")]]

    def nav(self, uid: int, state: dict[str, Any]) -> list[Row]:
        return self.b._nav(uid, back=bool(state.get("history") or state.get("values", {}).get("_manual")))

    def welcome(self, uid: int) -> None:
        self.b._send(
            uid,
            self.b._text(uid, "onboard_done"),
            [
                [self.button(uid, "habits_add_short", "create_begin")],
                [self.button(uid, "preset_packs", "preset_menu")],
                [self.button(uid, "menu_today", "today"), self.button(uid, "menu_settings", "settings")],
            ],
        )

    def settings(self, uid: int, section: str | None = None) -> None:
        b = self.b
        user = b.service.user(uid)
        groups = {
            "profile": ("language", "name", "timezone", "tone"),
            "notifications": ("reminders", "sleep", "quiet", "quiet_end", "boundary"),
            "more": ("gamification", "ai", "ai_sensitive"),
        }
        rows: list[Row] = []
        if section == "data":
            rows = [
                [self.button(uid, "export", "export"), self.button(uid, "export_csv", "export_csv")],
                [self.button(uid, "import", "import")],
                [self.button(uid, "delete", "delete")],
            ]
        elif section in groups:
            rows = [[self.button(uid, "setting_" + field, "setting", field=field)] for field in groups[section]]
        else:
            rows = [
                [
                    self.button(uid, "settings_profile", "settings_section", section="profile"),
                    self.button(uid, "settings_notifications", "settings_section", section="notifications"),
                ],
                [
                    self.button(uid, "settings_more", "settings_section", section="more"),
                    self.button(uid, "settings_data", "settings_section", section="data"),
                ],
                [self.button(uid, "feedback", "feedback")],
            ]
        rows.append([self.button(uid, "back", "settings" if section else "menu")])
        zone = next((b._text(uid, key) for key, value in REGIONS if value == user["tz"]), user["tz"] or "—")
        b._send(
            uid,
            b._text(uid, "settings_title")
            + "\n"
            + b._text(uid, "settings_summary", name=user["name"] or "—", zone=zone, sleep=user["sleep"]),
            rows,
        )

    def prompt(self, uid: int, state: dict[str, Any]) -> bool:
        b = self.b
        flow = state.get("flow")
        step = state.get("step")
        v = state.get("values", {})
        field = str(v.get("field", step)) if flow in ("setting", "edit") else str(step)
        if v.get("_manual"):
            kind = b.service.habit(uid, v["hid"])["spec"]["kind"] if flow == "edit" and v.get("hid") else v.get("kind")
            if field == "minimum" and kind == "binary":
                b._send(uid, b._text(uid, "create_minimum_binary"), self.nav(uid, state))
                return True
            key = "custom_timezone_help" if field == "timezone" else "settings_prompt"
            label = (
                b._text(uid, "setting_" + field)
                if flow == "setting"
                else b._text(
                    uid,
                    {
                        "title": "create_title",
                        "target": "create_target",
                        "minimum": "create_minimum",
                        "start": "create_start",
                        "end": "create_end",
                        "reminder": "create_reminders",
                    }.get(field, "custom_value"),
                )
            )
            if field in ("start", "end"):
                key = "custom_date_hint"
            elif field in ("reminder", "reminders", "sleep", "quiet", "quiet_end", "boundary"):
                key = "custom_time_hint"
            elif field in ("target", "amount", "n", "repeat", "schedule_value") or (
                field == "minimum" and v.get("kind") != "binary"
            ):
                key = "custom_number_hint"
            b._send(uid, b._text(uid, key, field=label), self.nav(uid, state))
            return True
        if flow == "onboard" and step == "name":
            rows = []
            if v.get("telegram_name"):
                rows.append(
                    [
                        (
                            b._text(uid, "use_name", name=v["telegram_name"][:24]),
                            "use_telegram_name",
                            {"name": v["telegram_name"]},
                        )
                    ]
                )
            rows += self.custom(uid) + [[self.button(uid, "skip", "flow_skip")]] + self.nav(uid, state)
            b._send(uid, b._text(uid, "onboard_name"), rows)
            return True
        if flow in ("onboard", "setting") and field in ("timezone", "sleep", "boundary", "quiet", "quiet_end"):
            if field == "timezone":
                if "_region_page" in v:
                    page = int(v["_region_page"])
                    regions = REGIONS[6 + page * 6 : 12 + page * 6]
                    rows = self.choices(uid, field, [(b._text(uid, key), zone) for key, zone in regions])
                    rows.append(
                        [
                            self.button(uid, "habit_prev", "region_page", page=page - 1),
                            self.button(uid, "habit_next", "region_page", page=(page + 1) % 2),
                        ]
                    )
                    rows += self.custom(uid)
                else:
                    rows = self.choices(uid, field, [(b._text(uid, key), zone) for key, zone in REGIONS[:6]])
                    rows.append([self.button(uid, "region_other", "region_page", page=0)])
                prompt = b._text(uid, "region_prompt")
            else:
                times = {
                    "sleep": ("21:00", "22:00", "23:00", "00:00", "01:00", "02:00"),
                    "boundary": ("00:00", "02:00", "04:00", "06:00"),
                    "quiet": ("21:00", "22:00", "23:00", "00:00"),
                    "quiet_end": ("06:00", "07:00", "08:00", "09:00"),
                }
                rows = self.choices(uid, field, [(t, t) for t in times[field]]) + self.custom(uid)
                prompt = b._text(uid, "onboard_sleep" if field == "sleep" else "choose_time")
            if flow == "onboard":
                rows.append([self.button(uid, "keep_default" if field != "timezone" else "skip", "flow_skip")])
            b._send(uid, prompt, rows + self.nav(uid, state))
            return True
        if flow in ("amount", "correct_amount"):
            habit = b.service.habit(uid, v["hid"], v.get("day"))
            spec = habit["spec"]
            values = [0, 1, 2, 3] if spec["kind"] == "limit" or flow == "correct_amount" else [1, 5, 10]
            rows = self.choices(
                uid, "amount", [(f"{n:g} {b._unit_label(uid, spec['unit'])}".strip(), n) for n in values], 2
            )
            if flow == "amount" and spec["kind"] == "quantity":
                rows.append([self.button(uid, "all_target", "amount_all")])
            key = (
                "amount_limit"
                if spec["kind"] == "limit"
                else ("amount_correct" if flow == "correct_amount" else "amount_prompt")
            )
            b._send(uid, b._text(uid, key), rows + self.custom(uid) + self.nav(uid, state))
            return True
        if flow == "edit" and step == "schedule_type":
            options = [
                (b._text(uid, "schedule_everyday"), {"type": "daily"}),
                (b._text(uid, "schedule_workdays"), {"type": "weekdays", "days": [0, 1, 2, 3, 4]}),
                (b._text(uid, "schedule_weekend"), {"type": "weekdays", "days": [5, 6]}),
                (b._text(uid, "schedule_n_count", n=3), {"type": "weekly", "n": 3}),
            ]
            rows = self.choices(uid, "schedule_type", options)
            rows += [
                [(b._text(uid, "schedule_" + kind), "edit_schedule_type", {"hid": v["hid"], "type": kind})]
                for kind in ("weekdays", "weekly", "interval")
            ]
            b._send(uid, b._text(uid, "next_schedule"), rows + self.nav(uid, state))
            return True
        if flow == "edit" and step == "schedule_value":
            if v["schedule_type"] == "weekdays":
                names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
                buttons = [
                    (
                        b._text(uid, "weekday_" + name) + (" ✓" if i in v.get("days", []) else ""),
                        "edit_day_toggle",
                        {"day": i},
                    )
                    for i, name in enumerate(names)
                ]
                rows = [buttons[i : i + 3] for i in range(0, 7, 3)]
                rows.append([self.button(uid, "done", "edit_days_done")])
            else:
                counts = [1, 2, 3, 4, 5, 7] if v["schedule_type"] == "weekly" else [2, 3, 7, 14]
                rows = self.choices(uid, "schedule_value", [(str(n), n) for n in counts]) + self.custom(uid)
            b._send(
                uid,
                b._text(uid, "schedule_days" if v["schedule_type"] == "weekdays" else "schedule_n"),
                rows + self.nav(uid, state),
            )
            return True
        if flow == "edit" and step == "value" and field in ("target", "minimum", "repeat", "end", "reminders"):
            h = b.service.habit(uid, v["hid"])
            spec = h["spec"]
            if field == "minimum" and spec["kind"] == "binary":
                rows = self.choices(uid, field, [(b._text(uid, "no_minimum"), None)]) + self.custom(uid)
                b._send(uid, b._text(uid, "create_minimum_binary"), rows + self.nav(uid, state))
                return True
            if field in ("target", "minimum", "repeat"):
                numbers: list[float] = (
                    [0, 15, 30, 60]
                    if field == "repeat"
                    else ([0, 1, 3, 5] if spec["kind"] == "limit" and field == "target" else [1, 5, 10, 20])
                )
                if field == "minimum":
                    numbers = (
                        [spec["target"], spec["target"] + 1]
                        if spec["kind"] == "limit"
                        else [spec["target"] / 2, spec["target"]]
                    )
                rows = self.choices(uid, field, [(f"{n:g}", n) for n in numbers])
                if field == "minimum":
                    rows += self.choices(uid, field, [(b._text(uid, "no_minimum"), None)])
            elif field == "end":
                rows = self.date_rows(uid, "end")
            else:
                rows = (
                    self.choices(uid, "reminders", [(t, [t]) for t in ("08:00", "09:00", "12:00", "18:00")])
                    if b.service.user(uid)["tz"]
                    else []
                )
                rows += self.choices(uid, "reminders", [(b._text(uid, "skip_reminders"), [])])
            b._send(
                uid,
                b._text(
                    uid,
                    "settings_prompt",
                    field=b._text(
                        uid,
                        {
                            "target": "value_target",
                            "minimum": "value_minimum",
                            "repeat": "field_repeat",
                            "end": "field_end",
                            "reminders": "habit_reminders",
                        }[field],
                    ),
                ),
                rows + self.custom(uid) + self.nav(uid, state),
            )
            return True
        if flow != "create" or not v.get("_quick"):
            return False
        rows = []
        prompt = ""
        if step == "title":
            rows = [
                [self.button(uid, key, "preset", name=key)] for key in ("preset_read", "preset_walk", "preset_water")
            ]
            prompt = b._text(uid, "title_prompt_short")
        elif step == "unit":
            rows = [
                [(b._text(uid, "unit_" + unit), "flow_pick", {"value": unit}) for unit in ("minutes", "pages")],
                [
                    (b._text(uid, "unit_reps"), "flow_pick", {"value": "reps"}),
                    (b._text(uid, "unit_custom"), "flow_pick", {"value": "custom"}),
                ],
                [(b._text(uid, "unit_" + unit), "flow_pick", {"value": unit}) for unit in ("glasses", "km")],
            ]
            prompt = b._text(uid, "create_unit")
        elif step in ("target", "minimum", "n", "repeat"):
            if step == "minimum" and v.get("kind") == "binary":
                rows = self.choices(uid, step, [(b._text(uid, "no_minimum"), None)]) + self.custom(uid)
                prompt = b._text(uid, "create_minimum_binary")
            else:
                numbers = [0, 1, 3, 5] if v.get("kind") == "limit" and step == "target" else [1, 5, 10, 20]
                if step == "n":
                    numbers = [1, 2, 3, 4, 5, 7] if v.get("schedule") == "weekly" else [2, 3, 7, 14]
                if step == "repeat":
                    numbers = [0, 15, 30, 60]
                if step == "minimum" and v.get("kind") == "quantity":
                    numbers = sorted({float(v.get("target", 1)) / 2, float(v.get("target", 1))})
                if step == "minimum" and v.get("kind") == "limit":
                    numbers = sorted({float(v.get("target", 0)), float(v.get("target", 0)) + 1})
                rows = self.choices(uid, step, [(f"{n:g}", n) for n in numbers]) + self.custom(uid)
                if step == "minimum":
                    rows += self.choices(uid, step, [(b._text(uid, "no_minimum"), None)])
                prompt = b._text(
                    uid,
                    {
                        "target": "value_target",
                        "minimum": "value_minimum",
                        "n": "schedule_n",
                        "repeat": "create_repeat",
                    }[step],
                )
                if step in ("target", "minimum"):
                    prompt += " · " + b._unit_label(uid, v.get("unit", ""))
        elif step == "schedule" and not v.get("_schedule_more"):
            options = [
                (b._text(uid, "schedule_everyday"), {"type": "daily"}),
                (b._text(uid, "schedule_workdays"), {"type": "weekdays", "days": [0, 1, 2, 3, 4]}),
                (b._text(uid, "schedule_weekend"), {"type": "weekdays", "days": [5, 6]}),
                (b._text(uid, "schedule_n_count", n=3), {"type": "weekly", "n": 3}),
            ]
            rows = self.choices(uid, "schedule", options)
            rows.append([self.button(uid, "advanced_options", "schedule_more")])
            prompt = b._text(uid, "next_schedule")
        elif step == "reminder":
            if b.service.user(uid)["tz"]:
                rows = self.choices(uid, step, [(t, t) for t in ("08:00", "09:00", "12:00", "18:00", "20:00")])
                prompt = b._text(uid, "reminder_choose")
            else:
                prompt = b._text(uid, "no_reminders_timezone")
            rows += self.choices(uid, step, [(b._text(uid, "skip_reminders"), "none")])
            if b.service.user(uid)["tz"]:
                rows += self.custom(uid)
        elif step in ("start", "end"):
            rows = self.date_rows(uid, step) + self.custom(uid)
            prompt = b._text(uid, "create_" + step)
        elif step in ("advanced", "metadata"):
            fields = (
                {
                    "minimum": "value_minimum",
                    "sensitive": "create_sensitive",
                    "verify": "field_verify",
                    "start": "create_start",
                    "end": "field_end",
                    "reminder": "habit_reminders",
                }
                if step == "advanced"
                else {
                    "description": "field_description",
                    "category": "field_category",
                    "icon": "field_icon",
                    "routine": "field_routine",
                    "repeat": "field_repeat",
                    "private_title": "field_private_title",
                }
            )
            rows = [[self.button(uid, key, "advanced_field", field=field)] for field, key in fields.items()]
            if step == "advanced":
                rows.append([self.button(uid, "more_actions", "advanced_menu", section="metadata")])
            rows.append([self.button(uid, "done", "preview_return")])
            prompt = b._text(uid, "advanced_intro")
        elif step == "preview":
            spec = b.service._validate(b._create_spec(uid, v))
            prompt = b._text(
                uid,
                "quick_preview",
                title=spec["title"],
                target=spec["target"] if spec["kind"] != "binary" else b._text(uid, "kind_binary"),
                unit=b._unit_label(uid, spec["unit"]),
                schedule=b._schedule_label(uid, spec["schedule"]),
                reminders=", ".join(spec["reminders"]) or b._text(uid, "skip_reminders"),
            )
            if spec["minimum"] is not None:
                prompt += (
                    "\n" + b._text(uid, "value_minimum") + ": " + str(spec["minimum_description"] or spec["minimum"])
                )
            if spec["sensitive"]:
                prompt += "\n🔒 " + b._text(uid, "private_habit")
            if spec["verify"] != "none":
                prompt += "\n" + b._text(uid, "verify_" + spec["verify"])
            if spec["start"] != b.service.day(uid) or spec["end"]:
                prompt += f"\n{spec['start']} → {spec['end'] or '—'}"
            rows = [
                [self.button(uid, "create_confirm", "create_save")],
                [self.button(uid, "advanced_options", "advanced_menu")],
            ]
        else:
            return False
        b._send(uid, prompt, rows + self.nav(uid, state))
        return True

    def date_rows(self, uid: int, field: str) -> list[Row]:
        today = date.fromisoformat(self.b.service.day(uid))
        options: list[tuple[str, Any]] = [
            (self.b._text(uid, "date_today"), today.isoformat()),
            (self.b._text(uid, "date_tomorrow"), (today + timedelta(days=1)).isoformat()),
            (self.b._text(uid, "date_next_monday"), (today + timedelta(days=7 - today.weekday())).isoformat()),
        ]
        if field == "end":
            options = options[1:] + [(self.b._text(uid, "no_end"), None)]
        return self.choices(uid, field, options)

    def action(self, uid: int, payload: dict[str, Any]) -> bool:
        b = self.b
        action = payload.get("action")
        state = b._session(uid)
        v = state.get("values", {})
        if action == "region_page":
            page = int(payload["page"])
            if page < 0:
                v.pop("_region_page", None)
            else:
                v["_region_page"] = page
            b._session(uid, state)
            b._flow_prompt(uid)
            return True
        if action == "resume_flow":
            b._flow_prompt(uid)
            return True
        if action in ("edit_begin", "edit_more"):
            fields = {
                "title": "habit_title",
                "target": "value_target",
                "minimum": "value_minimum",
                "reminders": "habit_reminders",
            }
            if action == "edit_more":
                fields = {
                    key: "field_" + key
                    for key in (
                        "description",
                        "category",
                        "icon",
                        "routine",
                        "repeat",
                        "end",
                        "private_title",
                        "verify",
                    )
                }
            h = b.service.habit(uid, payload["hid"])
            rows = [
                [self.button(uid, label, "edit_field", hid=h["id"], field=field)]
                for field, label in fields.items()
                if not (field == "target" and h["spec"]["kind"] == "binary")
            ]
            if action == "edit_begin":
                rows += [
                    [self.button(uid, "habit_schedule", "edit_schedule", hid=h["id"])],
                    [self.button(uid, "more_actions", "edit_more", hid=h["id"])],
                ]
            rows.append(
                [self.button(uid, "back", "edit_begin" if action == "edit_more" else "habit_more", hid=h["id"])]
            )
            b._send(uid, b._text(uid, "habit_edit") + ": " + b._habit_label(uid, h), rows + b._home_rows(uid))
            return True
        if action == "edit_schedule":
            b._begin(uid, "edit", "schedule_type", {"hid": payload["hid"]})
            b._flow_prompt(uid)
            return True
        if action == "edit_day_toggle":
            days = set(v.get("days", []))
            day = int(payload["day"])
            if day in days:
                days.remove(day)
            else:
                days.add(day)
            v["days"] = sorted(days)
            b._session(uid, state)
            b._flow_prompt(uid)
            return True
        if action == "edit_days_done":
            if not v.get("days"):
                raise DomainError()
            b._edit_preview(uid, v["hid"], {"schedule": {"type": "weekdays", "days": v["days"]}})
            return True
        if action in ("detail", "habit_more"):
            self.detail(uid, payload["hid"], more=action == "habit_more")
            return True
        if action == "settings_section":
            self.settings(uid, payload["section"])
            return True
        if action == "custom_input":
            v["_manual"] = True
            b._session(uid, state)
            b._flow_prompt(uid)
            return True
        if action == "flow_back" and v.get("_manual"):
            v.pop("_manual")
            b._session(uid, state)
            b._flow_prompt(uid)
            return True
        if action == "choice":
            self.apply(uid, payload["field"], payload["value"])
            return True
        if action == "schedule_more":
            v["_schedule_more"] = True
            b._session(uid, state)
            b._flow_prompt(uid)
            return True
        if action == "advanced_menu":
            b._advance(uid, payload.get("section", "advanced"), state)
            return True
        if action == "advanced_field":
            v["_return"] = state["step"]
            b._advance(uid, payload["field"], state)
            return True
        if action == "preview_return":
            v.pop("_return", None)
            v.pop("_manual", None)
            b._advance(uid, "preview", state)
            return True
        if action == "amount_all":
            b.service.session(uid, {})
            b._record_action(uid, v["hid"], "done", v.get("day"), seconds=v.get("timer_seconds"))
            return True
        return False

    def detail(self, uid: int, hid: str, more: bool = False) -> None:
        b = self.b
        h = b.service.habit(uid, hid)
        spec = h["spec"]
        day = b.service.day(uid)
        rec = b.service.db.one("SELECT * FROM records WHERE habit_id=? AND day=?", (hid, day))
        paused = b.service._paused(hid, day)
        due = scheduled(spec, date.fromisoformat(day))
        text = b._habit_label(uid, h) + "\n" + b._schedule_label(uid, spec["schedule"])
        if spec["kind"] != "binary":
            text += "\n" + b._text(uid, "value_target") + f": {spec['target']:g} {b._unit_label(uid, spec['unit'])}"
        if spec["minimum"] is not None:
            text += "\n" + b._text(uid, "value_minimum") + ": " + str(spec["minimum_description"] or spec["minimum"])
        if paused:
            text += "\n" + b._text(uid, "status_paused")
        elif not due:
            text += "\n" + b._text(uid, "not_today")
        if rec:
            text += (
                "\n"
                + b._text(uid, "status_" + rec["status"])
                + " · "
                + b._text(uid, "verification_" + rec["verification"])
            )
        rows: list[Row] = []
        if more:
            for field in ("description", "routine", "category"):
                if spec[field]:
                    text += "\n" + spec[field]
            rows.append([self.button(uid, "history", "history", hid=hid)])
            if not h["archived"]:
                rows.append(
                    [
                        self.button(uid, "habit_edit", "edit_begin", hid=hid),
                        self.button(
                            uid, "habit_resume" if paused else "pause", "resume" if paused else "pause", hid=hid
                        ),
                    ]
                )
                rows.append([self.button(uid, "archive", "archive", hid=hid)])
                if due and (not paused or rec):
                    rows.append(
                        [
                            self.button(uid, "mark_fail", "record", hid=hid, status="fail"),
                            self.button(uid, "mark_skip", "record", hid=hid, status="skip"),
                        ]
                    )
            if rec:
                rows.append([self.button(uid, "note_prompt", "note_existing", hid=hid)])
            if h.get("sensitive"):
                rows.append(
                    [
                        self.button(uid, "urge", "urge_begin", hid=hid),
                        self.button(uid, "episode", "urge_begin", hid=hid, episode=True),
                    ]
                )
                rows.append([self.button(uid, "journal", "journal", hid=hid)])
            rows.append([self.button(uid, "back", "detail", hid=hid)])
        else:
            if due and not paused and not h["archived"]:
                if spec["kind"] == "binary":
                    rows.append([self.button(uid, "mark_done", "record", hid=hid, status="done")])
                else:
                    key = "amount_total" if spec["kind"] == "limit" else "amount_add"
                    rows.append(
                        [(b._text(uid, key, unit=b._unit_label(uid, spec["unit"])), "record_amount", {"hid": hid})]
                    )
                if spec["minimum"] is not None and spec["kind"] != "limit":
                    rows.append([self.button(uid, "mark_minimum", "record", hid=hid, status="minimum")])
                if spec["verify"] == "timer":
                    rows.append([self.button(uid, "timer_begin", "timer_start", hid=hid)])
            rows.append([self.button(uid, "more_actions", "habit_more", hid=hid)])
            rows.append([self.button(uid, "menu_today", "today")])
        b._send(uid, text, rows + b._home_rows(uid))

    def pick(self, uid: int, value: Any) -> bool:
        b = self.b
        state = b._session(uid)
        v = state.get("values", {})
        step = state.get("step")
        if state.get("flow") != "create" or not v.get("_quick"):
            return False
        if step == "kind":
            if value not in ("binary", "quantity", "limit"):
                raise DomainError()
            v["kind"] = value
            v["target"] = 1
            v["minimum"] = None
            v["unit"] = ""
            b._advance(uid, "schedule" if value == "binary" else "unit", state)
        elif step == "unit":
            if value not in ("minutes", "pages", "reps", "glasses", "km", "custom"):
                raise DomainError()
            v["unit"] = value
            b._advance(uid, "custom_unit" if value == "custom" else "target", state)
        elif step == "schedule":
            if value not in ("daily", "weekdays", "weekly", "interval"):
                raise DomainError()
            v["schedule"] = value
            if value == "daily":
                self.next_create(uid, state)
            else:
                b._advance(uid, "days" if value == "weekdays" else "n", state)
        elif step in ("sensitive", "verify"):
            self.apply(uid, step, value)
        else:
            return False
        return True

    def next_create(self, uid: int, state: dict[str, Any], ordinary: str = "reminder") -> None:
        v = state["values"]
        v.pop("_manual", None)
        self.b._advance(uid, v.pop("_return", ordinary), state)

    def apply(self, uid: int, field: str, value: Any) -> None:
        b = self.b
        state = b._session(uid)
        flow = state.get("flow")
        v = state.get("values", {})
        step = state.get("step")
        expected = (
            v.get("field", step)
            if flow in ("setting", "edit")
            else ("amount" if flow in ("amount", "correct_amount") else step)
        )
        if expected != field:
            raise DomainError("error_stale")
        if flow in ("amount", "correct_amount"):
            v.pop("_manual", None)
            b._session(uid, state)
            b._message_input(uid, str(value))
            return
        if flow == "setting":
            b.service.settings(uid, **{b.SETTINGS[field]: value})
            b.service.session(uid, {})
            self.settings(uid, "profile" if field == "timezone" else "notifications")
            return
        if flow == "edit":
            changes = {field: value}
            if field == "schedule_type":
                changes = {"schedule": value}
            elif field == "schedule_value":
                n = number(value, positive=True)
                if n != int(n) or n > (7 if v["schedule_type"] == "weekly" else 365):
                    raise DomainError()
                changes = {"schedule": {"type": v["schedule_type"], "n": int(n)}}
            b._edit_preview(uid, v["hid"], changes)
            return
        if flow == "onboard":
            key = {"timezone": "tz", "sleep": "sleep", "boundary": "boundary", "name": "name"}[field]
            b.service.settings(uid, **{key: value})
            v[key] = value
            v.pop("_manual", None)
            b._advance(
                uid, {"timezone": "sleep", "sleep": "tone", "boundary": "tone", "name": "timezone"}[field], state
            )
            return
        if flow != "create" or not v.get("_quick"):
            raise DomainError("error_stale")
        v.pop("_manual", None)
        if field == "title":
            if not isinstance(value, str) or not value.strip() or len(value) > 100:
                raise DomainError()
            v["title"] = value.strip()
            b._advance(uid, "kind", state)
        elif field == "custom_unit":
            if not isinstance(value, str) or not value.strip() or len(value) > 40:
                raise DomainError()
            v["unit"] = value.strip()
            b._advance(uid, "target", state)
        elif field == "target":
            v["target"] = number(value, positive=v.get("kind") != "limit")
            b._advance(uid, "schedule", state)
        elif field == "schedule":
            if not isinstance(value, dict):
                raise DomainError()
            v["schedule"] = value["type"]
            v["days"] = value.get("days", [])
            v["n"] = value.get("n", 1)
            v.pop("_schedule_more", None)
            self.next_create(uid, state)
        elif field == "n":
            n = number(value, positive=True)
            if n != int(n) or n > (7 if v["schedule"] == "weekly" else 365):
                raise DomainError()
            v["n"] = int(n)
            self.next_create(uid, state)
        elif field == "reminder":
            if value != "none" and not b.service.user(uid)["tz"]:
                raise DomainError("error_timezone")
            times = [] if value == "none" else [b._time(t.strip()) for t in str(value).split(",")]
            v["reminders"] = times
            self.next_create(uid, state, "preview")
        elif field in ("start", "end"):
            if value is not None:
                date.fromisoformat(str(value))
            proposed = b._create_spec(uid, v) | {field: value}
            b.service._validate(proposed)
            v[field] = value
            self.next_create(uid, state, "advanced")
        elif field == "minimum":
            if value is None:
                v["minimum"] = None
                v["minimum_description"] = ""
            elif v.get("kind") == "binary":
                v["minimum"] = 0.5
                v["minimum_description"] = str(value)[:500]
            else:
                value = number(value)
                b.service._validate(b._create_spec(uid, v) | {"minimum": value})
                v["minimum"] = value
            self.next_create(uid, state, "advanced")
        elif field in ("sensitive", "verify"):
            proposed = b._create_spec(uid, v) | {field: value}
            if field == "sensitive" and value is True and proposed["verify"] in ("photo", "friend"):
                proposed["verify"] = "none"
                v["verify"] = "none"
            b.service._validate(proposed)
            v[field] = value
            self.next_create(uid, state, "advanced")
        elif field in ("description", "category", "icon", "routine", "repeat", "private_title"):
            if field == "repeat":
                value = int(value)
            b.service._validate(b._create_spec(uid, v) | {field: value})
            v[field] = value
            self.next_create(uid, state, "metadata")
        else:
            raise DomainError()

    def input(self, uid: int, text: str) -> bool:
        b = self.b
        state = b._session(uid)
        flow = state.get("flow")
        v = state.get("values", {})
        step = state.get("step")
        if flow == "create" and v.get("_quick"):
            if step in (
                "title",
                "custom_unit",
                "target",
                "minimum",
                "n",
                "start",
                "end",
                "reminder",
                "description",
                "category",
                "icon",
                "routine",
                "repeat",
                "private_title",
            ):
                self.apply(uid, step, text)
                return True
            b._flow_prompt(uid)
            return True
        if flow == "onboard" and step in ("timezone", "sleep", "boundary", "name"):
            self.apply(uid, step, text)
            return True
        return False

    def skip(self, uid: int) -> bool:
        b = self.b
        state = b._session(uid)
        flow = state.get("flow")
        v = state.get("values", {})
        step = state.get("step")
        if flow == "onboard" and step == "sleep":
            b._advance(uid, "tone", state)
            return True
        if flow == "create" and v.get("_quick"):
            defaults = {
                "minimum": None,
                "start": b.service.day(uid),
                "end": None,
                "reminder": "none",
                "description": "",
                "category": "",
                "icon": "🐾",
                "routine": "",
                "repeat": 0,
                "private_title": b._text(uid, "private_habit"),
            }
            if step in defaults:
                self.apply(uid, step, defaults[step])
                return True
        return False
