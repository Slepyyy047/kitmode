from __future__ import annotations

import re
import json
import unittest
from unittest.mock import patch

from kitmode.branding import COMMANDS, PROFILE, apply_profile, command_list
from kitmode.transport import TelegramAPI

try:
    from tests import test_flows as flow_helpers
except ModuleNotFoundError:
    import test_flows as flow_helpers


class HumanMenuTests(unittest.TestCase):
    setUp = flow_helpers.TelegramFlowTests.setUp
    tearDown = flow_helpers.TelegramFlowTests.tearDown
    message = flow_helpers.TelegramFlowTests.message
    tap = flow_helpers.TelegramFlowTests.tap

    def test_start_prioritizes_daily_actions_and_more_has_no_cat_support(self):
        self.message("/start")
        self.tap("create_begin")
        self.assertEqual(self.service.session(self.uid)["step"], "title")
        self.message("/menu")
        self.assertFalse(self.service.session(self.uid))
        self.tap("more_menu")
        self.assertNotIn("support", self.actions())
        self.assertTrue(self.transport.events[-1]["keyboard"])

    def actions(self):
        return [
            json.loads(self.db.one("SELECT payload FROM buttons WHERE token=?", (b["callback_data"][2:],))[0])["action"]
            for row in self.transport.events[-1]["keyboard"] for b in row
        ]

    def test_welcome_is_one_photo_with_working_buttons_and_cached_file(self):
        for lang in ("uk", "ru", "en"):
            self.service.settings(self.uid, lang=lang)
            self.transport.events.clear()
            self.message("/start")
            self.assertEqual(len(self.transport.events), 1)
            self.assertEqual(self.transport.events[0]["method"], "photo")
            self.assertIn("create_begin", self.actions())
            self.tap("create_begin")
            self.assertEqual(self.transport.events[-1]["method"], "edit_caption")
        self.assertEqual(self.transport.events[0]["path"], "fake-welcome-photo")

    def test_removed_support_commands_and_old_buttons_open_menu(self):
        for action in ("support", "pause_choose", "pause_start", "pause_finish"):
            self.bot._callback_action(self.uid, {"action": action})
            self.assertIn("create_begin", self.actions())
        self.message("/support")
        self.assertIn("create_begin", self.actions())

    def test_existing_support_pause_session_can_be_resumed_safely(self):
        self.service.session(self.uid, {"flow": "urge_pause", "step": "waiting", "values": {}})
        self.message("/resume")
        self.assertFalse(self.service.session(self.uid))
        self.assertIn("create_begin", self.actions())

    def test_new_from_command_drawer_starts_a_new_habit_flow(self):
        self.message("/settings")
        self.tap("settings_section", section="profile")
        self.tap("setting", field="name")
        self.message("/new")
        state = self.service.session(self.uid)
        self.assertEqual((state["flow"], state["step"]), ("create", "title"))
        self.assertEqual(self.service.habits(self.uid), [])

    def test_every_published_command_has_a_working_route(self):
        for command in COMMANDS:
            with self.subTest(command=command):
                self.service.session(self.uid, {})
                self.transport.events.clear()
                self.message("/" + command)
                self.assertTrue(self.transport.events[-1].get("text"))
                self.assertTrue(self.transport.events[-1].get("keyboard"))
                if command == "new":
                    self.assertEqual(self.service.session(self.uid)["flow"], "create")
                else:
                    self.assertFalse(self.service.session(self.uid).get("flow"))

    def test_checkin_has_direct_return_to_today_and_undo(self):
        self.service.create(self.uid, {"title": "Stretch"})
        self.message("/today")
        self.tap("record")
        self.tap("undo")
        self.assertTrue(self.transport.events[-1]["keyboard"])


class PublicProfileTests(unittest.TestCase):
    def test_profile_fits_telegram_limits_and_publishes_only_human_commands(self):
        self.assertEqual(len(COMMANDS), len(set(COMMANDS)))
        self.assertNotIn("admin", COMMANDS)
        for lang, copy in PROFILE.items():
            self.assertLessEqual(len(copy["name"]), 64)
            self.assertLessEqual(len(copy["description"]), 512)
            self.assertLessEqual(len(copy["short_description"]), 120)
            for command in command_list(lang):
                self.assertRegex(command["command"], re.compile(r"^[a-z0-9_]{1,32}$"))
                self.assertTrue(1 <= len(command["description"]) <= 256)

    def test_profile_setup_verifies_each_language_and_command_scope(self):
        api = TelegramAPI("123456:synthetic-test-token")
        saved = {}

        def request(method, payload, upload=None):
            if method.startswith("set"):
                saved[(method[3:], str(payload.get("language_code", "")), str(payload.get("scope", {})))] = payload
                return True
            record = saved[(method[3:], str(payload.get("language_code", "")), str(payload.get("scope", {})))]
            if method == "getMyCommands":
                return record["commands"]
            if method == "getChatMenuButton":
                return record["menu_button"]
            return record

        with patch.object(api, "_request", side_effect=request) as mocked:
            result = apply_profile(api)
        self.assertEqual(result["menu"], "commands")
        calls = [c.args for c in mocked.call_args_list]
        for language in ("", "uk", "ru", "en"):
            for scope in ("default", "all_private_chats"):
                self.assertTrue(
                    any(
                        m == "getMyCommands" and p["language_code"] == language and p["scope"] == {"type": scope}
                        for m, p in calls
                    )
                )
        self.assertFalse(any(m == "getUpdates" for m, _ in calls))

    def test_profile_readback_mismatch_is_not_reported_as_success(self):
        api = TelegramAPI("123456:synthetic-test-token")
        with patch.object(
            api, "_request", side_effect=lambda method, payload: True if method.startswith("set") else {}
        ):
            with self.assertRaisesRegex(RuntimeError, "verification failed"):
                apply_profile(api)


if __name__ == "__main__":
    unittest.main()
