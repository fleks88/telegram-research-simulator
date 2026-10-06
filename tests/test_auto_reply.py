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


class FakeReplySender:
    def __init__(self) -> None:
        self.prepared: list[tuple[str, str, int]] = []

    async def mark_read_and_type(
        self,
        account_key: str,
        recipient: str,
        *,
        typing_seconds: int,
    ) -> None:
        self.prepared.append((account_key, recipient, typing_seconds))


class FakeResponder:
    def __init__(self) -> None:
        self.histories: list[list[dict[str, Any]]] = []

    async def create_reply(self, prompt: str, incoming_text: str, **kwargs: Any) -> str:
        self.histories.append(kwargs.get("history", []))
        return f"{prompt}: {incoming_text}"


class LearningResponder(FakeResponder):
    async def create_reply_analysis(
        self,
        prompt: str,
        incoming_text: str,
        **kwargs: Any,
    ) -> dict[str, Optional[str]]:
        self.histories.append(kwargs.get("history", []))
        if "фробус" in incoming_text and not incoming_text.startswith("это"):
            return {"reply": "что такое фробус?", "unknown_term": "фробус"}
        return {"reply": "понял", "unknown_term": None}

    async def extract_term_explanation(
        self,
        *,
        term: str,
        incoming_text: str,
    ) -> Optional[str]:
        if term == "фробус" and incoming_text.startswith("это"):
            return "тестовый пакет игровой валюты"
        return None

    async def create_reply(self, prompt: str, incoming_text: str, **kwargs: Any) -> str:
        return "нашел значение в общей базе"


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
            sender = FakeReplySender()
            runtime = AutoReplyRuntime(
                settings,
                requests,
                sender,
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
            self.assertTrue(messaging.sent[0][1])
            self.assertEqual(messaging.sent[0][2], 1)
            self.assertEqual(sender.prepared[0][:2], ("personal", "central_user"))
            self.assertGreaterEqual(sender.prepared[0][2], 6)
            self.assertLessEqual(sender.prepared[0][2], 11)
            self.assertEqual(responder.histories[0], [])
            with database.connect() as connection:
                row = connection.execute(
                    "SELECT reply_status FROM received_messages WHERE telegram_message_id = 2"
                ).fetchone()
            self.assertEqual(row["reply_status"], "replied")

    async def test_queued_reply_is_cancelled_after_recipient_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "recipient-change.sqlite3")
            database.initialize()
            requests = DatabaseRequests(database)
            requests.add_sender_account("personal", "Personal")
            requests.save_campaign_settings(
                {
                    "auto_reply_enabled": True,
                    "reply_prompt": "Test prompt",
                    "reply_delay_min_minutes": 0,
                    "reply_delay_max_minutes": 0,
                    "recipient": "old_partner",
                }
            )
            settings = Settings(
                database_path=Path(directory) / "recipient-change.sqlite3",
                api_token="token",
                telegram_api_id=123,
                telegram_api_hash="hash",
                telegram_session_dir=Path(directory) / "sessions",
                allowed_recipients={"legacy_partner"},
                minimum_send_interval_seconds=0,
                llm_api_key="test-key",
            )
            messaging = FakeMessaging()
            runtime = AutoReplyRuntime(
                settings,
                requests,
                FakeReplySender(),
                messaging,
                FakeResponder(),
            )

            await runtime._handle_message(
                "personal",
                9001,
                FakeEvent(1, 9001, "hello"),
                recipient="old_partner",
            )
            requests.save_campaign_settings(
                {
                    "auto_reply_enabled": True,
                    "reply_prompt": "Test prompt",
                    "reply_delay_min_minutes": 0,
                    "reply_delay_max_minutes": 0,
                    "recipient": "new_partner",
                }
            )
            await runtime.process_due_replies()

            self.assertEqual(messaging.sent, [])
            with database.connect() as connection:
                queued = connection.execute(
                    "SELECT status FROM pending_auto_replies WHERE telegram_message_id = 1"
                ).fetchone()
                received = connection.execute(
                    "SELECT reply_status FROM received_messages WHERE telegram_message_id = 1"
                ).fetchone()
            self.assertEqual(queued["status"], "cancelled")
            self.assertEqual(received["reply_status"], "ignored")

    async def test_unknown_term_is_learned_and_shared_after_question_is_sent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "knowledge.sqlite3")
            database.initialize()
            requests = DatabaseRequests(database)
            requests.add_sender_account("acc1", "Account 1")
            requests.add_sender_account("acc2", "Account 2")
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
                database_path=Path(directory) / "knowledge.sqlite3",
                api_token="token",
                telegram_api_id=123,
                telegram_api_hash="hash",
                telegram_session_dir=Path(directory) / "sessions",
                allowed_recipients={"central_user"},
                minimum_send_interval_seconds=0,
                llm_api_key="test-key",
            )
            messaging = FakeMessaging()
            sender = FakeReplySender()
            runtime = AutoReplyRuntime(
                settings,
                requests,
                sender,
                messaging,
                LearningResponder(),
            )

            await runtime._handle_message(
                "acc1", 9001, FakeEvent(1, 9001, "что по фробусу")
            )
            self.assertIsNone(requests.get_pending_term_question("acc1"))
            await runtime.process_due_replies()
            self.assertEqual(
                requests.get_pending_term_question("acc1")["display_term"],
                "фробус",
            )

            await runtime._handle_message(
                "acc1",
                9001,
                FakeEvent(2, 9001, "это тестовый пакет игровой валюты"),
            )
            self.assertIsNone(requests.get_pending_term_question("acc1"))
            self.assertEqual(
                requests.get_learned_term("фробус")["definition"],
                "тестовый пакет игровой валюты",
            )

            await runtime._handle_message(
                "acc2", 9001, FakeEvent(3, 9001, "что по фробусу")
            )
            await runtime.process_due_replies()
            self.assertIsNone(requests.get_pending_term_question("acc2"))
            self.assertEqual(messaging.sent[-1][1], "нашел значение в общей базе")


if __name__ == "__main__":
    unittest.main()
