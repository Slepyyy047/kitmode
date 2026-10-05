from __future__ import annotations

import json
import unittest

try:
    from tests import test_flows as flow_helpers
except ModuleNotFoundError:
    import test_flows as flow_helpers


class ChoiceUXTests(unittest.TestCase):
    setUp = flow_helpers.TelegramFlowTests.setUp
    tearDown = flow_helpers.TelegramFlowTests.tearDown
    message = flow_helpers.TelegramFlowTests.message
    tap = flow_helpers.TelegramFlowTests.tap
    create_binary = flow_helpers.TelegramFlowTests.create_binary

    def buttons(self):
        return [
            (
                button,
                json.loads(
                    self.db.one("SELECT payload FROM buttons WHERE token=?", (button["callback_data"][2:],))["payload"]
                ),
            )
            for row in self.transport.events[-1].get("keyboard", [])
            for button in row
        ]

    def test_binary_fast_create_requires_only_title_text(self):
        hid = self.create_binary("Morning stretch")
        habit = self.service.habit(self.uid, hid)
        self.assertEqual(habit["title"], "Morning stretch")
        self.assertEqual(habit["spec"]["kind"], "binary")
        self.assertEqual(habit["spec"]["schedule"]["type"], "daily")

    def test_preset_buttons_are_human_labels_in_both_entrypoints_and_all_languages(self):
        expected = {
            "uk": ("📚 Читання", "🚶 Прогулянка", "💧 Випити води"),
            "ru": ("📚 Чтение", "🚶 Прогулка", "💧 Выпить воды"),
            "en": ("📚 Reading", "🚶 Walk", "💧 Drink water"),
        }
        for lang, labels in expected.items():
            for entry in ("create_begin", "preset_menu"):
                with self.subTest(lang=lang, entry=entry):
                    self.service.session(self.uid, {})
                    self.service.settings(self.uid, lang=lang)
                    self.message("/habits")
                    self.tap(entry)
                    presets = [(button, data) for button, data in self.buttons() if data["action"] == "preset"]
                    self.assertEqual(tuple(button["text"] for button, _ in presets), labels)
                    self.assertEqual(
                        tuple(data["name"] for _, data in presets), ("preset_read", "preset_walk", "preset_water")
                    )
                    self.tap("preset")
                    self.assertEqual(self.service.session(self.uid)["step"], "preview")
                    self.assertEqual(self.service.habits(self.uid), [])
                    self.assertNotIn("preset_", self.transport.events[-1]["text"])

    def test_weekday_buttons_and_pause_resume_show_translated_labels(self):
        for lang, labels, resume in (
            ("uk", ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"), "▶️ Продовжити звичку"),
            ("ru", ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"), "▶️ Продолжить привычку"),
            ("en", ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"), "▶️ Resume habit"),
        ):
            with self.subTest(lang=lang):
                self.service.session(self.uid, {})
                self.service.settings(self.uid, lang=lang)
                self.message("/new")
                self.message("Stretch")
                self.tap("flow_pick", value="binary")
                self.tap("schedule_more")
                self.tap("flow_pick", value="weekdays")
                actual = tuple(button["text"] for button, data in self.buttons() if data["action"] == "day_toggle")
                self.assertEqual(actual, labels)
                self.service.session(self.uid, {})
                hid = self.service.create(self.uid, {"title": "Stretch"})
                self.service.pause(self.uid, hid, self.service.day(self.uid), None)
                self.bot._callback_action(self.uid, {"action": "habit_more", "hid": hid})
                actual_resume = next(button["text"] for button, data in self.buttons() if data["action"] == "resume")
                self.assertEqual(actual_resume, resume)

    def test_unknown_translation_cannot_leak_a_technical_key(self):
        from kitmode.i18n import LANGUAGES, tr

        for lang in LANGUAGES:
            with self.assertRaises(KeyError):
                tr(lang, "internal_key_that_has_no_translation")

    def test_timezone_buttons_save_explicit_iana_zone(self):
        self.service.settings(self.uid, onboarded=False)
        self.message("/start")
        self.tap("flow_pick", value="en")
        self.tap("use_telegram_name")
        zones = {data.get("value") for _, data in self.buttons() if data.get("action") == "choice"}
        self.assertIn("Europe/Kyiv", zones)
        self.assertIn("Europe/Warsaw", zones)
        self.tap("choice", field="timezone", value="Europe/Warsaw")
        self.tap("choice", field="sleep", value="22:00")
        self.tap("flow_pick", value="strict")
        user = self.service.user(self.uid)
        self.assertEqual(user["tz"], "Europe/Warsaw")
        self.assertEqual(user["boundary"], "04:00")
        self.assertTrue(user["onboarded"])

    def test_limit_zero_is_an_explicit_valid_target(self):
        self.message("/habits")
        self.tap("create_begin")
        self.message("No soda")
        self.tap("flow_pick", value="limit")
        self.tap("flow_pick", value="reps")
        self.tap("choice", field="target", value=0)
        self.tap("choice", field="schedule", value={"type": "daily"})
        self.tap("choice", field="reminder", value="none")
        self.assertEqual(self.service.habits(self.uid), [])
        self.tap("create_save")
        self.assertEqual(self.service.habits(self.uid)[0]["spec"]["target"], 0)

    def test_grouped_settings_keep_time_preferences_and_existing_habits(self):
        hid = self.service.create(self.uid, {"title": "Existing"})
        self.message("/settings")
        groups = {data.get("section") for _, data in self.buttons() if data.get("action") == "settings_section"}
        self.assertEqual(groups, {"profile", "notifications", "more", "data"})
        self.tap("settings_section", section="notifications")
        self.tap("setting", field="boundary")
        self.tap("choice", field="boundary", value="06:00")
        self.assertEqual(self.service.user(self.uid)["boundary"], "06:00")
        self.assertEqual(self.service.habit(self.uid, hid)["title"], "Existing")
        for event in self.transport.events:
            self.assertNotRegex(event.get("text", ""), r"\{[a-z_]+\}")

    def test_custom_target_rejects_invalid_number_and_back_restores_buttons(self):
        self.message("/habits")
        self.tap("create_begin")
        self.message("Read")
        self.tap("flow_pick", value="quantity")
        self.tap("flow_pick", value="pages")
        self.tap("custom_input")
        self.message("not a number")
        self.assertEqual(self.service.session(self.uid)["step"], "target")
        self.assertEqual(self.service.habits(self.uid), [])
        self.tap("flow_back")
        self.tap("choice", field="target", value=10)
        self.assertEqual(self.service.session(self.uid)["step"], "schedule")

    def test_daily_binary_has_one_habit_row_and_direct_done(self):
        hid = self.service.create(self.uid, {"title": "Stretch"})
        self.message("/today")
        controls = [data for _, data in self.buttons() if data.get("hid") == hid]
        self.assertEqual([item["action"] for item in controls], ["detail", "record"])
        self.tap("detail")
        actions = {data["action"] for _, data in self.buttons()}
        self.assertIn("record", actions)
        self.assertIn("habit_more", actions)
        self.assertNotIn("edit_begin", actions)
        self.tap("habit_more")
        actions = {data["action"] for _, data in self.buttons()}
        self.assertIn("edit_begin", actions)
        self.assertIn("pause", actions)
        self.assertNotIn("resume", actions)
        self.tap("pause")
        self.message("/habits")
        self.tap("detail")
        self.tap("habit_more")
        actions = {data["action"] for _, data in self.buttons()}
        self.assertIn("resume", actions)
        self.assertNotIn("pause", actions)

    def test_quantity_and_limit_details_offer_amount_without_binary_done(self):
        for kind in ("quantity", "limit"):
            with self.subTest(kind=kind):
                self.service.create(self.uid, {"title": kind, "kind": kind, "target": 10, "unit": "pages"})
                self.message("/today")
                self.tap("detail")
                actions = {data["action"] for _, data in self.buttons()}
                self.assertIn("record_amount", actions)
                self.assertNotIn("record", actions)
                self.tap("record_amount")
                self.tap("choice", field="amount", value=1)
                self.uid += 1
                self.service.settings(self.uid, lang="en", tz="UTC", onboarded=True)

    def test_start_exposes_resume_without_losing_saved_creation(self):
        self.message("/habits")
        self.tap("create_begin")
        self.message("Read")
        saved = self.service.session(self.uid)
        self.message("/start")
        self.assertEqual(self.service.session(self.uid), saved)
        self.tap("resume_flow")
        self.tap("flow_pick", value="quantity")
        self.assertEqual(self.service.session(self.uid)["values"]["title"], "Read")
        self.assertEqual(self.service.session(self.uid)["step"], "unit")

    def tap_payload(self, action, **expected):
        button = next(
            (
                button
                for button, payload in self.buttons()
                if payload.get("action") == action and all(payload.get(k) == v for k, v in expected.items())
            ),
            None,
        )
        self.assertIsNotNone(button, (action, expected))
        self.send_callback(button["callback_data"])

    def send_callback(self, data):
        self.update_id += 1
        self.bot.handle(
            {
                "update_id": self.update_id,
                "callback_query": {
                    "id": str(self.update_id),
                    "from": {"id": self.uid},
                    "message": {"chat": {"id": self.uid, "type": "private"}},
                    "data": data,
                },
            }
        )

    def open_schedule_editor(self, hid):
        self.message("/habits")
        self.tap_payload("detail", hid=hid)
        self.tap("habit_more")
        self.tap("edit_begin")
        fields = {p.get("field") for _, p in self.buttons() if p["action"] == "edit_field"}
        self.assertEqual(fields, {"title", "target", "minimum", "reminders"})
        self.tap("edit_schedule")

    def assert_schedule_preview(self, hid, previous, expected):
        state = self.service.session(self.uid)
        self.assertEqual(state["flow"], "edit_confirm")
        effective = state["values"]["effective"]
        self.assertEqual(self.service.habit(self.uid, hid, effective)["spec"]["schedule"], previous)
        self.tap("edit_confirm")
        actual = self.service.habit(self.uid, hid, effective)["spec"]["schedule"]
        for key, value in expected.items():
            self.assertEqual(actual[key], value)
        self.assertEqual(self.service.habit(self.uid, hid, "2026-10-05")["spec"]["schedule"], previous)

    def test_edit_schedule_presets_preview_then_preserve_history(self):
        schedules = [
            {"type": "daily"},
            {"type": "weekdays", "days": [0, 1, 2, 3, 4]},
            {"type": "weekdays", "days": [5, 6]},
            {"type": "weekly", "n": 3},
        ]
        for schedule in schedules:
            with self.subTest(schedule=schedule):
                hid = self.service.create(self.uid, {"title": "Read", "kind": "quantity", "target": 10})
                previous = self.service.habit(self.uid, hid)["spec"]["schedule"]
                self.open_schedule_editor(hid)
                self.tap("choice", field="schedule_type", value=schedule)
                self.assert_schedule_preview(hid, previous, schedule)

    def test_edit_schedule_weekdays_weekly_and_interval_use_buttons(self):
        for kind in ("weekdays", "weekly", "interval"):
            with self.subTest(kind=kind):
                hid = self.service.create(self.uid, {"title": kind, "kind": "quantity", "target": 10})
                previous = self.service.habit(self.uid, hid)["spec"]["schedule"]
                self.open_schedule_editor(hid)
                self.tap_payload("edit_schedule_type", type=kind)
                if kind == "weekdays":
                    self.tap_payload("edit_day_toggle", day=0)
                    self.tap_payload("edit_day_toggle", day=4)
                    self.tap("edit_days_done")
                    expected = {"type": kind, "days": [0, 4]}
                else:
                    self.tap("choice", field="schedule_value", value=2)
                    expected = {"type": kind, "n": 2}
                self.assert_schedule_preview(hid, previous, expected)

    def test_ui_migration_invalidates_old_buttons_once_and_preserves_data(self):
        hid = self.service.create(self.uid, {"title": "Existing"})
        self.message("/habits")
        self.tap("create_begin")
        self.message("New draft")
        old = next(button["callback_data"] for button, data in self.buttons() if data.get("value") == "quantity")
        profile = self.service.user(self.uid)
        session = self.service.session(self.uid)
        self.db.conn.execute("UPDATE system SET value='1' WHERE key='ui_version'")
        self.db.conn.commit()
        self.bot = flow_helpers.Bot(self.service, self.transport)
        self.send_callback(old)
        self.assertEqual(self.service.session(self.uid), session)
        self.assertEqual(self.service.user(self.uid), profile)
        self.assertEqual(self.service.habit(self.uid, hid)["title"], "Existing")
        self.message("/start")
        self.tap("resume_flow")
        self.bot = flow_helpers.Bot(self.service, self.transport)
        self.tap("flow_pick", value="quantity")
        self.assertEqual(self.service.session(self.uid)["step"], "unit")

    def test_optional_advanced_minimum_can_be_added_and_cleared_before_save(self):
        self.message("/habits")
        self.tap("create_begin")
        self.message("Read")
        self.tap("flow_pick", value="quantity")
        self.tap("flow_pick", value="pages")
        self.tap("choice", field="target", value=10)
        self.tap("choice", field="schedule", value={"type": "daily"})
        self.tap("choice", field="reminder", value="none")
        self.tap("advanced_menu")
        self.tap("advanced_field", field="minimum")
        self.tap("choice", field="minimum", value=5)
        self.tap("preview_return")
        self.assertIn("5", self.transport.events[-1]["text"])
        self.assertEqual(self.service.habits(self.uid), [])
        self.tap("advanced_menu")
        self.tap("advanced_field", field="minimum")
        self.tap("choice", field="minimum", value=None)
        self.tap("preview_return")
        self.tap("create_save")
        spec = self.service.habits(self.uid)[0]["spec"]
        self.assertIsNone(spec["minimum"])
        self.assertEqual(spec["reminders"], [])

    def test_localized_choice_screens_have_labels_without_raw_keys(self):
        from kitmode.i18n import LANGUAGES, _ROWS

        for lang in LANGUAGES:
            with self.subTest(lang=lang):
                self.service.session(self.uid, {})
                self.service.settings(self.uid, lang=lang)
                self.transport.events.clear()
                self.message("/settings")
                self.tap("settings_section", section="notifications")
                self.tap("setting", field="sleep")
                self.tap("choice", field="sleep", value="22:00")
                self.message("/habits")
                self.tap("create_begin")
                self.message("Read")
                self.tap("flow_pick", value="quantity")
                self.tap("flow_pick", value="pages")
                self.tap("choice", field="target", value=10)
                self.tap("choice", field="schedule", value={"type": "weekdays", "days": [5, 6]})
                self.tap("choice", field="reminder", value="none")
                self.tap("advanced_menu")
                for event in self.transport.events:
                    labels = [button["text"] for row in event.get("keyboard", []) for button in row]
                    for text in [event.get("text", ""), *labels]:
                        self.assertNotIn(text, _ROWS[lang])
                        self.assertNotRegex(text, r"\{[a-z_]+\}")

    def test_other_cities_are_selectable_without_typing_a_zone(self):
        self.message("/settings")
        self.tap("settings_section", section="profile")
        self.tap("setting", field="timezone")
        self.tap("region_page")
        self.tap("region_page", value=...)
        self.tap("choice", field="timezone", value="Europe/Kyiv")
        self.assertEqual(self.service.user(self.uid)["tz"], "Europe/Kyiv")
        self.message("/settings")
        self.tap("settings_section", section="profile")
        self.tap("setting", field="timezone")
        self.tap("region_page")
        self.tap("choice", field="timezone", value="Europe/Paris")
        self.assertEqual(self.service.user(self.uid)["tz"], "Europe/Paris")

    def test_all_literal_ui_translation_keys_exist_in_every_language(self):
        import ast
        from pathlib import Path
        from kitmode.i18n import STRINGS

        for name in ("telegram.py", "choices.py"):
            source = Path(__file__).resolve().parent.parent / "kitmode" / name
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"_text", "button"}
                    and len(node.args) > 1
                ):
                    continue
                key = node.args[1]
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and not key.value.endswith("_"):
                    for lang, strings in STRINGS.items():
                        self.assertIn(key.value, strings, (lang, name, node.lineno))

    def test_non_scheduled_day_hides_record_actions(self):
        self.service.create(self.uid, {"title": "Weekend", "schedule": {"type": "weekdays", "days": [5, 6]}})
        self.message("/habits")
        self.tap("detail")
        self.assertFalse(
            any(data["action"] in {"record", "record_amount", "timer_start"} for _, data in self.buttons())
        )
        self.tap("habit_more")
        self.assertFalse(any(data["action"] == "record" for _, data in self.buttons()))
        self.assertTrue(any(data["action"] == "history" for _, data in self.buttons()))

    def test_global_reminder_enable_and_disable_keep_habit_times(self):
        hid = self.service.create(self.uid, {"title": "Read", "reminders": ["12:00"]})
        for enabled in (False, True):
            self.message("/settings")
            self.tap("settings_section", section="notifications")
            self.tap("setting", field="reminders")
            self.tap("setting_value", field="reminders", value=enabled)
            self.assertIs(self.service.user(self.uid)["reminders"], enabled)
            self.assertEqual(self.service.habit(self.uid, hid)["spec"]["reminders"], ["12:00"])

    def test_utc_button_is_explicitly_labeled(self):
        self.message("/settings")
        self.tap("settings_section", section="profile")
        self.tap("setting", field="timezone")
        utc = next(button for button, data in self.buttons() if data.get("value") == "UTC")
        self.assertEqual(utc["text"], "UTC")

    def test_quantity_all_target_preserves_completed_timer_evidence(self):
        from datetime import timedelta

        hid = self.service.create(
            self.uid, {"title": "Walk", "kind": "quantity", "target": 10, "unit": "minutes", "verify": "timer"}
        )
        self.message("/habits")
        self.tap("detail")
        self.tap("timer_start")
        later = self.service.now() + timedelta(seconds=600)
        self.service.clock = lambda: later
        self.tap("timer_finish")
        self.tap("amount_all")
        record = self.service.history(self.uid, hid)[-1]["record"]
        self.assertEqual(record["value"], 10)
        self.assertEqual(json.loads(record["evidence"])["seconds"], 600)

    def test_progress_summary_opens_full_calendar_details(self):
        hid = self.service.create(self.uid, {"title": "Read"})
        self.service.record(self.uid, hid, "fixture")
        self.message("/progress")
        brief = self.transport.events[-1]["text"]
        self.tap("progress", value=...)  # Period selectors return the brief view.
        details = next(button["callback_data"] for button, data in self.buttons() if data.get("detailed"))
        self.update_id += 1
        self.bot.handle(
            {
                "update_id": self.update_id,
                "callback_query": {
                    "id": str(self.update_id),
                    "from": {"id": self.uid},
                    "message": {"chat": {"id": self.uid, "type": "private"}},
                    "data": details,
                },
            }
        )
        self.assertGreater(len(self.transport.events[-1]["text"]), len(brief))
        self.assertIn("Calendar", self.transport.events[-1]["text"])

    def test_old_target_button_cannot_change_later_step(self):
        self.message("/habits")
        self.tap("create_begin")
        self.message("Read")
        self.tap("flow_pick", value="quantity")
        self.tap("flow_pick", value="pages")
        old = next(
            button["callback_data"]
            for button, data in self.buttons()
            if data.get("field") == "target" and data.get("value") == 10
        )
        self.tap("choice", field="target", value=10)
        before = self.service.session(self.uid)
        self.update_id += 1
        self.bot.handle(
            {
                "update_id": self.update_id,
                "callback_query": {
                    "id": str(self.update_id),
                    "from": {"id": self.uid},
                    "message": {"chat": {"id": self.uid, "type": "private"}},
                    "data": old,
                },
            }
        )
        self.assertEqual(self.service.session(self.uid), before)
        self.assertEqual(self.service.habits(self.uid), [])


if __name__ == "__main__":
    unittest.main()
