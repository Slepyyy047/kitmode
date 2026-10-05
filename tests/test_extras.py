from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from kitmode.core import DB, Service
from kitmode.extras import AIConfig


class ExtrasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = DB(":memory:")
        self.service = Service(self.db, clock=lambda: datetime(2026, 1, 8, 12, tzinfo=timezone.utc))

    def tearDown(self) -> None:
        self.db.close()

    def habit(self, uid: int, title: str = "Walk", sensitive: bool = False) -> str:
        return self.service.create(
            uid,
            {
                "title": title,
                "kind": "binary",
                "target": 1,
                "schedule": {"type": "daily"},
                "start": "2026-01-01",
                "sensitive": sensitive,
                "verify": "none",
            },
        )

    def seed_record(self, uid: int, hid: str, day: str) -> None:
        spec = self.service.habit(uid, hid, day)["spec"]
        self.db.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)",
            (
                hid,
                day,
                "full",
                1,
                "",
                "none",
                "{}",
                json.dumps(spec, ensure_ascii=False),
                self.service.now().isoformat(),
            ),
        )

    def test_invite_is_one_use_and_expiring(self) -> None:
        token = self.service.extras.invite(1)
        self.assertEqual(self.service.extras.accept_invite(2, token), 1)
        with self.assertRaises(Exception):
            self.service.extras.accept_invite(3, token)
        expired = self.service.extras.invite(1)
        self.service.clock = lambda: datetime(2026, 1, 20, tzinfo=timezone.utc)
        with self.assertRaises(Exception):
            self.service.extras.accept_invite(4, expired)

    def test_friend_read_and_verification_require_rights_and_consent(self) -> None:
        hid = self.habit(1)
        token = self.service.extras.invite(1)
        self.service.extras.accept_invite(2, token)
        self.seed_record(1, hid, "2026-01-08")
        with self.assertRaises(Exception):
            self.service.extras.shared(3, 1)
        with self.assertRaises(Exception):
            self.service.extras.visibility(1, hid, 3, True)
        with self.assertRaises(Exception):
            self.service.extras.revoke(3, 1)
        with self.assertRaises(Exception):
            self.service.extras.request_verification(1, hid, "2026-01-08", 2)
        self.service.extras.consent_verifier(1, 2)
        with self.assertRaises(Exception):
            self.service.extras.request_verification(1, hid, "2026-01-08", 2)
        self.service.extras.consent_verifier(2, 1)
        rid = self.service.extras.request_verification(1, hid, "2026-01-08", 2)
        with self.assertRaises(Exception):
            self.service.extras.decide_verification(3, rid, True)
        self.service.extras.decide_verification(2, rid, True)

    def test_sensitive_habit_cannot_be_shared_or_requested_for_verification(self) -> None:
        hid = self.habit(1, "Private", True)
        self.service.extras.accept_invite(2, self.service.extras.invite(1))
        with self.assertRaises(Exception):
            self.service.extras.visibility(1, hid, 2, True)
        self.service.extras.urge(1, hid, 7, "stress", True)
        self.assertEqual(len(self.service.extras.journal(1, hid)), 1)
        self.assertNotIn("Private", self.service.extras.share_card(1))

    def test_export_import_preview_and_duplicate_protection(self) -> None:
        hid = self.habit(1)
        self.seed_record(1, hid, "2026-01-08")
        self.service.edit(1, hid, {"title": "Walk 2"})
        self.service.pause(1, hid, "2026-01-08", "2026-01-10")
        private_hid = self.habit(1, "Private", True)
        self.service.extras.urge(1, private_hid, 7, "stress", True)
        self.service.archive(1, private_hid)
        text = self.service.extras.export_json(1)
        exported = json.loads(text)
        self.assertEqual(len(exported["versions"]), 3)
        self.assertEqual(len(exported["pauses"]), 1)
        self.assertEqual(len(exported["urges"]), 1)
        self.assertEqual(len(exported["archives"]), 1)
        preview = self.service.extras.preview_import(2, text)
        self.assertTrue(preview["can_import"])
        self.assertEqual(self.service.extras.import_data(2, text)["habits"], 2)
        self.assertTrue(self.service.extras.preview_import(2, text)["duplicate"])
        self.assertEqual(self.service.extras.import_data(2, text)["duplicate"], 1)
        imported = self.db.all("SELECT user_id FROM habits WHERE user_id=2")
        self.assertEqual(len(imported), 2)
        self.assertEqual(
            self.db.one("SELECT count(*) n FROM pauses p JOIN habits h ON h.id=p.habit_id WHERE h.user_id=2")["n"], 1
        )
        self.assertEqual(self.db.one("SELECT count(*) n FROM urges WHERE owner=2")["n"], 1)
        self.assertEqual(
            self.db.one("SELECT count(*) n FROM archives a JOIN habits h ON h.id=a.habit_id WHERE h.user_id=2")["n"], 1
        )
        imported_hid = self.db.one("SELECT id FROM habits WHERE user_id=2 AND sensitive=0")["id"]
        versions = self.db.all(
            "SELECT effective,spec FROM versions WHERE habit_id=? ORDER BY effective", (imported_hid,)
        )
        self.assertEqual([r["effective"] for r in versions], ["2026-01-01", "2026-01-09"])
        self.assertEqual(json.loads(versions[1]["spec"])["title"], "Walk 2")
        self.assertEqual(self.db.one("SELECT count(*) n FROM rewards WHERE user_id=2")["n"], 0)
        challenge_id = self.service.extras.challenge(2, "One completion", 1, "2026-01-08", "2026-01-08")
        challenge = next(c for c in self.service.extras.challenges(2) if c["id"] == challenge_id)
        self.assertEqual(challenge["completed"], 1)
        self.assertTrue(challenge["done"])

    def test_hostile_imports_are_rejected_before_writing(self) -> None:
        hid = self.habit(1)
        self.seed_record(1, hid, "2026-01-08")
        original = json.loads(self.service.extras.export_json(1))
        corruptions = []
        bad = json.loads(json.dumps(original))
        bad["records"][0]["day"] = "not-a-date"
        corruptions.append(bad)
        bad = json.loads(json.dumps(original))
        bad["records"][0]["status"] = "completed-ish"
        corruptions.append(bad)
        bad = json.loads(json.dumps(original))
        bad["records"][0]["value"] = float("nan")
        corruptions.append(bad)
        bad = json.loads(json.dumps(original))
        bad["records"][0]["value"] = float("inf")
        corruptions.append(bad)
        bad = json.loads(json.dumps(original))
        bad["records"][0]["snapshot"] = "{broken"
        corruptions.append(bad)
        bad = json.loads(json.dumps(original))
        bad["versions"][0]["spec"] = {"kind": "invalid"}
        corruptions.append(bad)
        bad = json.loads(json.dumps(original))
        bad["records"].append(dict(bad["records"][0]))
        corruptions.append(bad)
        bad = json.loads(json.dumps(original))
        bad["habits"].append(dict(bad["habits"][0]))
        corruptions.append(bad)
        bad = json.loads(json.dumps(original))
        bad["friends"] = [{"owner": 1, "friend": 2}]
        corruptions.append(bad)
        for payload in corruptions:
            with self.subTest(payload=payload):
                with self.assertRaises(Exception):
                    self.service.extras.preview_import(2, json.dumps(payload, allow_nan=True))
        with self.assertRaises(Exception):
            self.service.extras.preview_import(2, " " * (self.service.extras.MAX_IMPORT_BYTES + 1))
        self.assertEqual(self.db.one("SELECT count(*) n FROM habits WHERE user_id=2")["n"], 0)

    def test_import_snapshot_cannot_downgrade_sensitive_habit(self) -> None:
        hid = self.habit(1, "Sensitive", True)
        self.seed_record(1, hid, "2026-01-08")
        payload = json.loads(self.service.extras.export_json(1))
        snapshot = json.loads(payload["records"][0]["snapshot"])
        snapshot["sensitive"] = False
        payload["records"][0]["snapshot"] = json.dumps(snapshot)
        with self.assertRaises(Exception):
            self.service.extras.preview_import(2, json.dumps(payload))
        self.assertEqual(self.db.one("SELECT count(*) n FROM habits WHERE user_id=2")["n"], 0)

    def test_csv_escapes_formula_text_and_import_strips_evidence(self) -> None:
        hid = self.habit(1, "=2+2")
        spec = self.service.habit(1, hid)["spec"]
        self.db.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)",
            (
                hid,
                "2026-01-08",
                "full",
                1,
                "@SUM(A1:A2)",
                "none",
                json.dumps({"file_id": "private-photo"}),
                json.dumps(spec),
                self.service.now().isoformat(),
            ),
        )
        csv_text = self.service.extras.export_csv(1)
        self.assertIn("'=2+2", csv_text)
        self.assertIn("'@SUM(A1:A2)", csv_text)
        payload = self.service.extras.export_json(1)
        self.assertNotIn("private-photo", payload)
        self.service.extras.import_data(2, payload)
        evidence = self.db.one("SELECT evidence FROM records r JOIN habits h ON h.id=r.habit_id WHERE h.user_id=2")[
            "evidence"
        ]
        self.assertEqual(evidence, "{}")

    def test_real_service_record_and_admin_feedback_apis(self) -> None:
        hid = self.habit(1)
        record = self.service.record(1, hid, "record-live", day="2026-01-08")
        self.assertEqual(record["status"], "full")
        feedback_id = self.service.extras.feedback(1, "A bug report")
        self.service.extras.record_error("database")
        with self.assertRaises(Exception):
            self.service.extras.admin_feedback(1, [2])
        self.assertEqual(self.service.extras.admin_feedback(2, [2])[0]["message"], "A bug report")
        self.service.extras.resolve_feedback(2, feedback_id, [2])
        self.assertEqual(self.service.extras.admin_feedback(2, [2])[0]["status"], "resolved")
        self.assertEqual(self.service.extras.system_status(2, [2])["errors_24h"], {"database": 1})

    def test_delete_user_removes_owned_and_social_data(self) -> None:
        hid = self.habit(1)
        self.service.extras.accept_invite(2, self.service.extras.invite(1))
        self.service.extras.urge(1, self.habit(1, "Private", True), 5)
        self.service.extras.delete_user(1)
        for table in ("habits", "records", "friends", "invites", "urges", "timers", "verification_requests"):
            self.assertEqual(self.db.one(f"SELECT count(*) n FROM {table}")["n"], 0, table)
        self.assertIsNone(self.db.one("SELECT id FROM users WHERE id=1"))
        self.assertEqual(self.db.one("SELECT count(*) n FROM habits WHERE id=?", (hid,))["n"], 0)

    def test_admin_is_id_allowlisted_and_blocking_updates_user_state(self) -> None:
        self.service.user(7)
        with self.assertRaises(Exception):
            self.service.extras.admin(7, [8])
        stats = self.service.extras.admin(8, [8])
        self.assertEqual(stats["users"], 1)
        self.service.extras.block(8, 7, [8], True)
        self.assertTrue(self.service.user(7)["blocked"])
        self.service.extras.block(8, 7, [8], False)
        self.assertFalse(self.service.user(7)["blocked"])

    def test_ai_disabled_or_without_consent_uses_local_fallback(self) -> None:
        self.service.user(1)
        with patch("urllib.request.urlopen") as call:
            answer = self.service.extras.ai(1, "Help me plan", AIConfig(enabled=True, key="secret", model="gpt-test"))
        self.assertTrue(answer)
        call.assert_not_called()
        self.assertEqual(self.db.one("SELECT count(*) n FROM ai_usage")["n"], 0)

    def test_ai_provider_failure_falls_back(self) -> None:
        self.service.settings(1, ai_consent=True)
        with patch("urllib.request.urlopen", side_effect=TimeoutError):
            answer = self.service.extras.ai(1, "A small goal", AIConfig(enabled=True, key="secret", model="gpt-test"))
        self.assertTrue(answer)

    def test_ai_success_is_consented_bounded_and_not_stored(self) -> None:
        self.service.settings(1, ai_consent=True)
        response = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "One small step."}]}]}
        with patch("urllib.request.urlopen") as call:
            call.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
            answer = self.service.extras.ai(1, "Help me plan", AIConfig(enabled=True, key="secret", model="gpt-test"))
            request = call.call_args.args[0]
            body = json.loads(request.data)
        self.assertEqual(answer, "One small step.")
        self.assertFalse(body["store"])
        self.assertEqual(body["max_output_tokens"], 1000)
        self.assertEqual(body["input"][0]["content"][0]["text"], "Help me plan")
        self.assertNotIn("stress", request.data.decode())
        self.assertEqual(self.db.one("SELECT count FROM ai_usage WHERE owner=1")["count"], 1)

    def test_ai_sensitive_consent_and_zero_quota(self) -> None:
        self.service.settings(1, ai_consent=True)
        self.habit(1, "Private pattern", True)
        config = AIConfig(enabled=True, key="secret", model="gpt-test")
        with patch("urllib.request.urlopen") as call:
            self.service.extras.ai(1, "Help with Private pattern", config)
        call.assert_not_called()
        self.service.settings(1, ai_sensitive=True)
        response = {"output": [{"content": [{"type": "output_text", "text": "A calm next step."}]}]}
        with patch("urllib.request.urlopen") as call:
            call.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
            self.service.extras.ai(
                1, "Help with Private pattern", AIConfig(enabled=True, key="secret", model="gpt-test")
            )
        call.assert_called_once()
        with patch("urllib.request.urlopen") as call:
            self.service.extras.ai(
                1, "Generic question", AIConfig(enabled=True, key="secret", model="gpt-test", daily_limit=0)
            )
        call.assert_not_called()
        self.assertEqual(self.db.one("SELECT count FROM ai_usage WHERE owner=1")["count"], 1)

    def test_csv_and_share_preview_follow_selected_language(self) -> None:
        self.habit(1, "Walk")
        self.service.settings(1, lang="en")
        self.assertIn("habit_id,habit,sensitive,date,status", self.service.extras.export_csv(1))
        self.assertIn("Open habits", self.service.extras.share_card(1))
        self.service.settings(1, lang="ru")
        self.assertIn("привычка", self.service.extras.export_csv(1))
        self.assertIn("Открытых привычек", self.service.extras.share_card(1))


if __name__ == "__main__":
    unittest.main()
