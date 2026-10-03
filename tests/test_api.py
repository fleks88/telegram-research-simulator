from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime
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

        disabled = self.client.patch(
            "/api/v1/accounts/business",
            headers=self.auth,
            json={"enabled": False},
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["enabled"])
        listed = self.client.get("/api/v1/accounts", headers=self.auth)
        self.assertEqual(listed.json()[0]["account_key"], "business")

    def test_campaign_settings_and_scheduled_slot_are_idempotent(self) -> None:
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
        account_response = self.client.post(
            "/api/v1/accounts",
            headers=self.auth,
            json={"account_key": "personal", "label": "Personal"},
        )
        self.assertEqual(account_response.status_code, 201)

        campaign_service = self.app.state.campaign_service
        now = datetime(2026, 10, 3, 10, 0)
        first = asyncio.run(campaign_service.tick(now))
        second = asyncio.run(campaign_service.tick(now))
        self.assertEqual(first["status"], "sent")
        self.assertEqual(second["status"], "already_processed")
        self.assertEqual(
            self.sent_messages,
            [("personal", "@partner_user", "Approved scheduled test day 2", None)],
        )

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


if __name__ == "__main__":
    unittest.main()