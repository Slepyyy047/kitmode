from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from kitmode.core import Service
from kitmode.db import DB
from kitmode.telegram import Bot
from kitmode.transport import FakeTransport


class TelegramFlowTests(unittest.TestCase):
    """Exercise user-visible flows through Bot.handle without Telegram I/O."""

    def setUp(self) -> None:
        self.db = DB(":memory:")
        self.service = Service(self.db, lambda: datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
        self.transport = FakeTransport()
        self.bot = Bot(self.service, self.transport, admin_ids=(99,))
        self.uid = 41
        self.service.user(self.uid)
        self.service.settings(self.uid, lang="en", tz="UTC", onboarded=True)
        self.update_id = 100

    def tearDown(self) -> None:
        self.db.close()

    def message(self, text: str | None = None, **extra: object) -> None:
        self.update_id += 1
        msg: dict[str, object] = {
            "from": {"id": self.uid, "first_name": "Alex"},
            "chat": {"id": self.uid, "type": "private"},
            **extra,
        }
        if text is not None:
            msg["text"] = text
        self.bot.handle({"update_id": self.update_id, "message": msg})

    def tap(
        self,
        action: str,
        *,
        value: object = ...,
        field: str | None = None,
        section: str | None = None,
        sensitive: bool | None = None,
        blocked: bool | None = None,
    ) -> None:
        event = self.transport.events[-1]
        keyboard = event.get("keyboard") or []
        for row in keyboard:
            for button in row:
                token = button["callback_data"][2:]
                record = self.db.one("SELECT payload FROM buttons WHERE token=?", (token,))
                if not record:
                    continue
                payload = json.loads(record["payload"])
                if (
                    payload.get("action") == action
                    and (value is ... or payload.get("value") == value)
                    and (field is None or payload.get("field") == field)
                    and (section is None or payload.get("section") == section)
                    and (sensitive is None or payload.get("sensitive") is sensitive)
                    and (blocked is None or payload.get("blocked") is blocked)
                ):
                    self.update_id += 1
                    self.bot.handle(
                        {
                            "update_id": self.update_id,
                            "callback_query": {
                                "id": f"flow-{self.update_id}",
                                "from": {"id": self.uid},
                                "message": {
                                    "chat": {"id": self.uid, "type": "private"},
                                    "message_id": event.get("message_id", 0),
                                    "photo": [{}] if event.get("method") in {"photo", "edit_caption"} else [],
                                },
                                "data": button["callback_data"],
                            },
                        }
                    )
                    return
        self.fail(f"No current {action=} {value=} button in {event.get('text')!r}")

    def create_binary(self, title: str = "Stretch") -> str:
        self.message("/habits")
        self.tap("create_begin")
        self.message(title)
        self.tap("flow_pick", value="binary")
        self.tap("choice", field="schedule", value={"type": "daily"})
        self.tap("choice", field="reminder", value="none")
        self.assertEqual(self.service.session(self.uid).get("step"), "preview")
        self.tap("create_save")
        return self.service.habits(self.uid)[-1]["id"]

    def test_quantity_create_flow_reaches_preview_before_persisting(self) -> None:
        self.message("/habits")
        self.tap("create_begin")
        self.message("Read")
        self.tap("flow_pick", value="quantity")
        self.tap("flow_pick", value="pages")
        self.tap("choice", field="target", value=10)
        self.tap("choice", field="schedule", value={"type": "daily"})
        self.tap("choice", field="reminder", value="none")
        self.assertEqual(self.service.session(self.uid).get("step"), "preview")
        self.assertIn("Read", self.transport.events[-1]["text"])
        self.assertEqual(self.service.habits(self.uid), [])
        self.tap("create_save")
        spec = self.service.habits(self.uid)[0]["spec"]
        self.assertEqual((spec["kind"], spec["target"], spec["unit"]), ("quantity", 10, "pages"))

    def test_quantity_updates_are_idempotent_per_update_but_add_distinct_entries(self) -> None:
        hid = self.service.create(
            self.uid,
            {
                "title": "Read",
                "kind": "quantity",
                "target": 10,
                "unit": "pages",
                "minimum": 2,
                "schedule": {"type": "daily"},
                "start": "2026-10-05",
            },
        )
        self.message("/habits")
        self.tap("detail")
        self.tap("record_amount")
        self.message("3")
        first_update = self.update_id
        self.bot.handle(
            {
                "update_id": first_update,
                "message": {"from": {"id": self.uid}, "chat": {"id": self.uid, "type": "private"}, "text": "3"},
            }
        )
        self.assertEqual(self.service.history(self.uid, hid, days=1)[0]["record"]["value"], 3)
        self.message("/habits")
        self.tap("detail")
        self.tap("record_amount")
        self.message("4")
        self.assertEqual(self.service.history(self.uid, hid, days=1)[0]["record"]["value"], 7)

    def test_wizard_back_preserves_input_and_session_survives_bot_restart(self) -> None:
        self.message("/habits")
        self.tap("create_begin")
        self.message("Walk")
        self.tap("flow_pick", value="quantity")
        self.assertEqual(self.service.session(self.uid)["step"], "unit")
        self.tap("flow_back")
        self.assertEqual(self.service.session(self.uid)["step"], "kind")
        self.assertEqual(self.service.session(self.uid)["values"]["title"], "Walk")
        self.bot = Bot(self.service, self.transport, admin_ids=(99,))
        self.tap("flow_pick", value="quantity")
        self.assertEqual(self.service.session(self.uid)["step"], "unit")
        self.assertEqual(self.service.session(self.uid)["values"]["title"], "Walk")

    def test_record_undo_and_stats_work_after_bot_restart(self) -> None:
        hid = self.service.create(
            self.uid,
            {"title": "Stretch", "kind": "binary", "schedule": {"type": "daily"}, "start": "2026-10-05"},
        )
        self.message("/today")
        self.tap("record")
        self.assertEqual(self.service.stats(self.uid, hid=hid, start="2026-10-05", end="2026-10-05")["full"], 1)
        self.bot = Bot(self.service, self.transport, admin_ids=(99,))
        self.tap("undo")
        self.assertEqual(self.service.stats(self.uid, hid=hid, start="2026-10-05", end="2026-10-05")["full"], 0)

    def test_photo_verification_accepts_file_id_without_downloading_photo(self) -> None:
        hid = self.service.create(
            self.uid,
            {
                "title": "Walk",
                "kind": "binary",
                "schedule": {"type": "daily"},
                "start": "2026-10-05",
                "verify": "photo",
            },
        )
        self.message("/habits")
        self.tap("detail")
        self.tap("record")
        self.assertEqual(self.service.session(self.uid).get("flow"), "evidence")
        self.message(None, photo=[{"file_id": "telegram-file-id", "file_size": 42}])
        proof = self.db.one("SELECT verification,evidence FROM records WHERE habit_id=?", (hid,))
        self.assertEqual(proof["verification"], "submitted")
        self.assertEqual(json.loads(proof["evidence"])["file_id"], "telegram-file-id")

    def test_edit_title_shows_unapplied_preview_until_confirmed(self) -> None:
        hid = self.service.create(
            self.uid,
            {"title": "Stretch", "kind": "binary", "schedule": {"type": "daily"}, "start": "2026-10-05"},
        )
        self.message("/habits")
        self.tap("detail")
        self.tap("habit_more")
        self.tap("edit_begin")
        # The edit menu has a distinct field in each opaque callback payload.
        self.tap("edit_field", field="title")
        self.message("Mobility")
        self.assertEqual(self.service.habit(self.uid, hid)["title"], "Stretch")
        self.assertEqual(self.service.session(self.uid).get("flow"), "edit_confirm")
        self.assertIn("Mobility", self.transport.events[-1]["text"])
        effective = self.service.session(self.uid)["values"]["effective"]
        self.assertEqual(self.service.habit(self.uid, hid, effective)["title"], "Stretch")
        self.tap("edit_confirm")
        self.assertEqual(self.service.habit(self.uid, hid)["title"], "Stretch")
        self.assertEqual(self.service.habit(self.uid, hid, effective)["title"], "Mobility")

    def test_optional_ai_command_offers_local_help_without_external_api(self) -> None:
        self.message("/ai")
        self.tap("ai_begin", sensitive=False)
        self.message("Help me keep a steady reading habit")
        self.assertIn("local", self.transport.events[-1]["text"].lower())

    def test_admin_and_blocked_user_routes_preserve_data_access(self) -> None:
        self.service.user(52)
        self.service.settings(52, lang="en", tz="UTC", onboarded=True)
        self.uid = 99
        self.service.user(99)
        self.service.settings(99, lang="en", tz="UTC", onboarded=True)
        self.message("/admin")
        self.assertIn("users", self.transport.events[-1]["text"].lower())
        self.tap("admin_block_begin", blocked=True)
        self.message("52")
        self.assertTrue(self.service.user(52)["blocked"])
        self.uid = 52
        self.message("/help")
        self.assertIn("restricted", self.transport.events[-1]["text"].lower())
        token = self.service.button(52, {"action": "export"})
        self.update_id += 1
        self.bot.handle(
            {
                "update_id": self.update_id,
                "callback_query": {
                    "id": f"flow-{self.update_id}",
                    "from": {"id": 52},
                    "message": {"chat": {"id": 52, "type": "private"}, "message_id": 0},
                    "data": "b:" + token,
                },
            }
        )
        self.assertEqual(self.transport.events[-1]["method"], "document")

    def test_sensitive_habit_title_is_hidden_from_list_and_share_card(self) -> None:
        self.service.create(
            self.uid,
            {
                "title": "Private detail",
                "kind": "binary",
                "sensitive": True,
                "schedule": {"type": "daily"},
                "start": "2026-10-05",
            },
        )
        self.message("/habits")
        self.assertNotIn("Private detail", self.transport.events[-1]["text"])
        self.assertIn("private", self.transport.events[-1]["text"].lower())
        self.message("/friends")
        self.tap("share_card")
        self.assertNotIn("Private detail", self.transport.events[-1]["text"])


if __name__ == "__main__":
    unittest.main()
