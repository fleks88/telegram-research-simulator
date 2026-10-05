from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from research_sim.api.app import create_app
from research_sim.database import Database
from research_sim.settings import Settings


class FakeTelegramClient:
    def __init__(
        self,
        account_key: str,
        sent_messages: list[tuple[str, str, str, bytes | None]],
    ) -> None:
        self.account_key = account_key
        self.sent_messages = sent_messages

    async def __aenter__(self) -> "FakeTelegramClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def send_message(self, recipient: str, text: str) -> None:
        self.sent_messages.append((self.account_key, recipient, text, None))

    async def send_file(self, recipient: str, file: bytes, caption: str) -> None:
        image_bytes = file.read()
        self.sent_messages.append((self.account_key, recipient, caption, image_bytes))


class ApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        root = Path(self.temp_directory.name)
        self.sent_messages: list[tuple[str, str, str, bytes | None]] = []
        settings = Settings(
            database_path=root / "api.sqlite3",
            api_token="test-api-token",
            telegram_api_id=123,
            telegram_api_hash="test-api-hash",
            telegram_session_dir=root / "sessions",
            allowed_recipients={"partner_user"},
            minimum_send_interval_seconds=0,
        )
        self.app = create_app(
            settings=settings,
            database=Database(settings.database_path),
            client_factory=lambda **kwargs: FakeTelegramClient(
                Path(kwargs["session"]).name,
                self.sent_messages,
            ),
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.auth = {"Authorization": "Bearer test-api-token"}

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.temp_directory.cleanup()

    def test_health_is_public_but_settings_require_token(self) -> None:
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        response = self.client.get("/api/v1/settings/campaign")
        self.assertEqual(response.status_code, 401)

    def test_activation_status_reports_endpoint_and_empty_snapshots(self) -> None:
        response = self.client.get("/api/v1/activations/status", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["endpoint_configured"])
        self.assertFalse(response.json()["enabled"])
        self.assertIsNone(response.json()["first_response"])
        self.assertEqual(response.json()["pending"], 0)
        replies = self.client.get("/api/v1/auto-replies/status", headers=self.auth)
        self.assertEqual(replies.status_code, 200)
        self.assertEqual(replies.json(), {"pending": 0, "items": []})

    def test_sender_settings_allow_only_one_central_recipient(self) -> None:
        from unittest.mock import patch

        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_ALLOWED_RECIPIENTS": "central_one,central_two",
                "DATABASE_PATH": str(Path(self.temp_directory.name) / "other.sqlite3"),
            },
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "exactly one central account"):
                Settings.from_environment()

    def test_send_api_enforces_allowlist(self) -> None:
        for account_key, label in (("business", "Business"), ("personal", "Personal")):
            response = self.client.post(
                "/api/v1/accounts",
                headers=self.auth,
                json={"account_key": account_key, "label": label},
            )
            self.assertEqual(response.status_code, 201, response.text)

        allowed = self.client.post(
            "/api/v1/messages/send",
            headers=self.auth,
            json={"recipient": "@Partner_User", "text": "Approved test", "account_index": 1},
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["recipient"], "partner_user")
        self.assertEqual(allowed.json()["sender_account"], "business")
        self.assertEqual(allowed.json()["account_index"], 1)
        self.assertEqual(
            self.sent_messages,
            [("business", "@partner_user", "Approved test", None)],
        )

        second = self.client.post(
            "/api/v1/messages/send",
            headers=self.auth,
            json={"recipient": "partner_user", "text": "Second test", "account_index": 2},
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["sender_account"], "personal")
        self.assertEqual(second.json()["account_index"], 2)

        blocked = self.client.post(
            "/api/v1/messages/send",
            headers=self.auth,
            json={"recipient": "@another_user", "text": "Must not send", "account_index": 1},
        )
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(len(self.sent_messages), 2)

    def test_photo_send_and_account_history(self) -> None:
        account = self.client.post(
            "/api/v1/accounts",
            headers=self.auth,
            json={"account_key": "business", "label": "Business"},
        )
        self.assertEqual(account.status_code, 201)
        photo_bytes = b"\xff\xd8\xff\xe0test-jpeg"
        response = self.client.post(
            "/api/v1/messages/send-with-photo",
            headers=self.auth,
            data={
                "recipient": "partner_user",
                "text": "Photo caption",
                "account_index": "1",
            },
            files={"photo": ("test.jpg", photo_bytes, "image/jpeg")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["photo_attached"])
        self.assertEqual(
            self.sent_messages,
            [("business", "@partner_user", "Photo caption", photo_bytes)],
        )
        history = self.client.get(
            "/api/v1/accounts/business/history",
            headers=self.auth,
        )
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.json()[0]["message_text"], "Photo caption")
        self.assertTrue(history.json()[0]["photo_attached"])

    def test_photo_endpoint_rejects_non_jpeg(self) -> None:
        self.client.post(
            "/api/v1/accounts",
            headers=self.auth,
            json={"account_key": "business", "label": "Business"},
        )
        response = self.client.post(
            "/api/v1/messages/send-with-photo",
            headers=self.auth,
            data={
                "recipient": "partner_user",
                "text": "Not an image",
                "account_index": "1",
            },
            files={"photo": ("test.png", b"not jpeg", "image/png")},
        )
        self.assertEqual(response.status_code, 415)
        self.assertEqual(self.sent_messages, [])

    def test_legacy_delivery_table_is_migrated(self) -> None:
        legacy_path = Path(self.temp_directory.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy_path) as connection:
            connection.execute(
                """CREATE TABLE message_deliveries (
                    id INTEGER PRIMARY KEY,
                    recipient TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    campaign_id TEXT,
                    campaign_day INTEGER,
                    campaign_slot TEXT
                )"""
            )
        legacy_database = Database(legacy_path)
        legacy_database.initialize()
        with legacy_database.connect() as connection:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(message_deliveries)"
                ).fetchall()
            }
        self.assertTrue({"sender_account", "message_text", "photo_attached"} <= columns)

    def test_account_inventory_can_be_listed_and_disabled(self) -> None:
        added = self.client.post(
            "/api/v1/accounts",
            headers=self.auth,
            json={"account_key": "business", "label": "Business account"},
        )
        self.assertEqual(added.status_code, 201)
        self.assertNotIn("session_path", added.json())
        generated = self.client.get(
            "/api/v1/accounts/business/persona",
            headers=self.auth,
        ).json()
        self.assertTrue(generated["identity_prompt"])
        self.assertIn(generated["address_style"], {"ты", "вы"})

        disabled = self.client.patch(
            "/api/v1/accounts/business",
            headers=self.auth,
            json={"enabled": False},
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["enabled"])
        listed = self.client.get("/api/v1/accounts", headers=self.auth)
        self.assertEqual(listed.json()[0]["account_key"], "business")

    def test_persona_profile_is_saved_per_account(self) -> None:
        self.client.post(
            "/api/v1/accounts",
            headers=self.auth,
            json={"account_key": "business", "label": "Business"},
        )
        profile = {
            "identity_prompt": "Calm and concise test persona",
            "word_accuracy_percent": 93,
            "punctuation_accuracy_percent": 81,
        }
        response = self.client.put(
            "/api/v1/accounts/business/persona",
            headers=self.auth,
            json=profile,
        )
        self.assertEqual(response.status_code, 200, response.text)
        saved_profile = self.client.get(
            "/api/v1/accounts/business/persona",
            headers=self.auth,
        ).json()
        self.assertEqual(saved_profile["identity_prompt"], profile["identity_prompt"])
        self.assertEqual(saved_profile["word_accuracy_percent"], 93)
        self.assertEqual(saved_profile["literacy_level"], 5)
        self.assertEqual(saved_profile["aggression_level"], 1)
        invalid = dict(profile, word_accuracy_percent=101)
        response = self.client.put(
            "/api/v1/accounts/business/persona",
            headers=self.auth,
            json=invalid,
        )
        self.assertEqual(response.status_code, 422)

        randomized = self.client.post(
            "/api/v1/accounts/business/persona/randomize",
            headers=self.auth,
        )
        self.assertEqual(randomized.status_code, 200, randomized.text)
        self.assertIn(randomized.json()["address_style"], {"ты", "вы"})
        self.assertLessEqual(randomized.json()["terminal_period_percent"], 6)

    def test_dialogue_proposal_uses_fixed_target_replies_and_is_saved(self) -> None:
        self.client.post(
            "/api/v1/accounts",
            headers=self.auth,
            json={"account_key": "research_a", "label": "Research A"},
        )

        class FakeDialogueResponder:
            async def propose_dialogue(self, *, task_prompt, persona, target_replies):
                return [
                    {"sender": f"Draft {index}", "target": target}
                    for index, target in enumerate(target_replies, start=1)
                ]

        self.app.state.persona_research_service.responder = FakeDialogueResponder()
        targets = [f"Static target reply {index}" for index in range(1, 6)]
        response = self.client.post(
            "/api/v1/research/dialogues/propose",
            headers=self.auth,
            json={
                "account_key": "research_a",
                "task_prompt": "Closed test scenario",
                "target_replies": targets,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["dialogue"]), 5)
        self.assertEqual(
            [turn["target"] for turn in response.json()["dialogue"]],
            targets,
        )
        saved = self.client.get(
            "/api/v1/accounts/research_a/dialogues",
            headers=self.auth,
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()[0]["id"], response.json()["proposal_id"])

    def test_reply_preview_uses_configured_prompt_without_sending(self) -> None:
        self.client.post(
            "/api/v1/accounts",
            headers=self.auth,
            json={"account_key": "acc1", "label": "acc1"},
        )
        settings = {
            "campaign_id": "activation-messages",
            "enabled": False,
            "start_date": "2026-10-05",
            "recipient": "partner_user",
            "phrases": ["Pack {pack}: {activations}"],
            "day_slots": {},
            "reply_prompt": "Configured default prompt",
        }
        self.assertEqual(
            self.client.put(
                "/api/v1/settings/campaign", headers=self.auth, json=settings
            ).status_code,
            200,
        )

        class FakeReplyResponder:
            async def create_reply(self, prompt, incoming_text, **kwargs):
                self.prompt = prompt
                return "Preview answer"

        responder = FakeReplyResponder()
        self.app.state.persona_research_service.responder = responder
        response = self.client.post(
            "/api/v1/research/reply-preview",
            headers=self.auth,
            json={"account_key": "acc1", "incoming_text": "Human question"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["reply_prompt"], "Configured default prompt")
        self.assertEqual(response.json()["reply_text"], "Preview answer")
        self.assertIn("Профиль текущего отправителя", responder.prompt)
        self.assertEqual(self.sent_messages, [])

    def test_timeline_contains_incoming_and_planned_reply(self) -> None:
        self.client.post(
            "/api/v1/accounts",
            headers=self.auth,
            json={"account_key": "acc1", "label": "acc1"},
        )
        requests = self.app.state.database_requests
        requests.claim_received_message(
            account_key="acc1",
            telegram_message_id=99,
            sender_id=123,
            message_text="Human answer",
        )
        requests.enqueue_auto_reply(
            account_key="acc1",
            telegram_message_id=99,
            due_at=2_000_000_000,
            reply_text="Planned account reply",
        )
        response = self.client.get(
            "/api/v1/accounts/acc1/timeline",
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200, response.text)
        by_kind = {item["kind"]: item for item in response.json()}
        self.assertEqual(by_kind["incoming"]["message_text"], "Human answer")
        self.assertEqual(by_kind["planned"]["message_text"], "Planned account reply")
        self.assertEqual(by_kind["planned"]["due_at"], 2_000_000_000)

    def test_time_scheduled_delivery_is_removed(self) -> None:
        payload = {
            "campaign_id": "partner-test",
            "enabled": True,
            "start_date": "2026-10-02",
            "recipient": "partner_user",
            "phrases": ["Approved scheduled test day {day}"],
            "day_slots": {
                "1": ["10:00"],
                "2": ["10:00", "15:00"],
                "3": ["10:00"],
            },
        }
        saved = self.client.put(
            "/api/v1/settings/campaign",
            headers=self.auth,
            json=payload,
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertFalse(saved.json()["enabled"])
        self.assertEqual(saved.json()["day_slots"], {})
        tick = self.client.post("/api/v1/campaign/tick", headers=self.auth)
        self.assertEqual(tick.status_code, 404)
        self.assertEqual(self.sent_messages, [])

    def test_campaign_requires_prompt_for_auto_reply_and_valid_templates(self) -> None:
        base_payload = {
            "campaign_id": "prompt-validation",
            "enabled": False,
            "start_date": "2026-10-05",
            "recipient": "partner_user",
            "phrases": ["Test {day}"],
            "day_slots": {"1": ["10:00"]},
        }
        missing_prompt = dict(base_payload, auto_reply_enabled=True)
        response = self.client.put(
            "/api/v1/settings/campaign",
            headers=self.auth,
            json=missing_prompt,
        )
        self.assertEqual(response.status_code, 422)

        invalid_template = dict(base_payload, phrases=["Test {unknown}"])
        response = self.client.put(
            "/api/v1/settings/campaign",
            headers=self.auth,
            json=invalid_template,
        )
        self.assertEqual(response.status_code, 422)

        invalid_delay = dict(
            base_payload,
            reply_delay_min_minutes=181,
            reply_delay_max_minutes=180,
        )
        response = self.client.put(
            "/api/v1/settings/campaign",
            headers=self.auth,
            json=invalid_delay,
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
