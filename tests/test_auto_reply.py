from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional

from research_sim.database import Database, DatabaseRequests
from research_sim.integrations.telegram import TelethonSender
from research_sim.services.auto_reply import AutoReplyRuntime
from research_sim.services.prompt_responder import PromptResponder
from research_sim.settings import Settings


class FakeMessage:
    def __init__(self, message_id: int, text: str) -> None:
        self.id = message_id
        self.message = text


class FakeEvent:
    def __init__(self, message_id: int, sender_id: int, text: str) -> None:
        self.message = FakeMessage(message_id, text)
        self.sender_id = sender_id


class FakeMessaging:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Optional[int]]] = []

    async def send_message(
        self,
        recipient: str,
        text: str,
        *,
        sender_account_index: Optional[int] = None,
    ) -> None:
        self.sent.append((recipient, text, sender_account_index))


class FakeResponder:
    def __init__(self) -> None:
        self.histories: list[list[dict[str, Any]]] = []

    async def create_reply(self, prompt: str, incoming_text: str, **kwargs: Any) -> str:
        self.histories.append(kwargs.get("history", []))
        return f"{prompt}: {incoming_text}"


class AutoReplyRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_session_json_can_supply_per_account_api_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session_dir = Path(directory) / "sessions"
            session_dir.mkdir()
            (session_dir / "personal.json").write_text(
                '{"app_id": 456, "app_hash": "personal-hash"}',
                encoding="utf-8",
            )
            settings = Settings(
                database_path=Path(directory) / "test.sqlite3",
                api_token="token",
                telegram_api_id=None,
                telegram_api_hash=None,
                telegram_session_dir=session_dir,
                allowed_recipients={"central_user"},
            )
            sender = TelethonSender(settings)
            self.assertEqual(
                sender._credentials_for("personal"),
                (456, "personal-hash"),
            )

    async def test_only_central_sender_is_answered_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.sqlite3")
            database.initialize()
            requests = DatabaseRequests(database)
            requests.add_sender_account("personal", "Personal")
            requests.save_campaign_settings(
                {
                    "auto_reply_enabled": True,
                    "reply_prompt": "Test prompt",
                    "reply_delay_min_minutes": 0,
                    "reply_delay_max_minutes": 0,
                    "recipient": "central_user",
                }
            )
            settings = Settings(
                database_path=Path(directory) / "test.sqlite3",
                api_token="token",
                telegram_api_id=123,
                telegram_api_hash="hash",
                telegram_session_dir=Path(directory) / "sessions",
                allowed_recipients={"central_user"},
                minimum_send_interval_seconds=0,
                llm_api_key="test-key",
            )
            messaging = FakeMessaging()
            responder = FakeResponder()
            runtime = AutoReplyRuntime(
                settings,
                requests,
                TelethonSender(settings, client_factory=lambda **kwargs: None),
                messaging,
                responder,
            )

            await runtime._handle_message(
                "personal",
                9001,
                FakeEvent(1, 7777, "not the central test account"),
            )
            self.assertEqual(messaging.sent, [])

            event = FakeEvent(2, 9001, "hello")
            await runtime._handle_message("personal", 9001, event)
            await runtime._handle_message("personal", 9001, event)
            self.assertEqual(messaging.sent, [])
            await runtime.process_due_replies()
            self.assertEqual(
                len(messaging.sent),
                1,
            )
            self.assertEqual(messaging.sent[0][0], "central_user")
            self.assertTrue(messaging.sent[0][1].startswith("Test prompt"))
            self.assertEqual(messaging.sent[0][2], 1)
            self.assertEqual(responder.histories[0], [])
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT reply_status FROM received_messages WHERE telegram_message_id = 2"
                ).fetchone()
            self.assertEqual(row["reply_status"], "replied")


if __name__ == "__main__":
    unittest.main()
