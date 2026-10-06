from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from research_sim.bot.app import (
    EDIT_REPLY_PROMPT, edit_reply_prompt_start, edit_reply_prompt_entered,
    reset_reply_prompt,
)
from research_sim.database import Database, DatabaseRequests
from research_sim.services.auto_reply import AutoReplyRuntime
from research_sim.services.campaign import CampaignService
from research_sim.services.conversation_prompts import (
    DEFAULT_REPLY_PROMPT, AUTO_REPLY_INSTRUCTION, ACTIVATION_INSTRUCTION,
)
from research_sim.services.messaging import MessagingService
from research_sim.settings import Settings


class SharedPromptTest(unittest.IsolatedAsyncioTestCase):
    async def test_saved_prompt_is_used_by_activation_and_auto_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shared.sqlite3"
            database = Database(path)
            database.initialize()
            requests = DatabaseRequests(database)
            requests.add_sender_account("buyer", "Buyer")
            settings = Settings(
                database_path=path, api_token="test", telegram_api_id=123,
                telegram_api_hash="hash", telegram_session_dir=Path(directory),
                allowed_recipients={"seller"}, minimum_send_interval_seconds=0,
                llm_api_key="test",
            )
            sender = SimpleNamespace(send_message=AsyncMock())
            responder = SimpleNamespace(rewrite_for_persona=AsyncMock(return_value="Нужны 1800 UC"))
            messaging = MessagingService(settings, requests, sender, prompt_responder=responder)
            campaign = CampaignService(settings, requests, messaging)
            config = campaign.save_settings({
                "campaign_id": "test", "recipient": "seller",
                "phrases": ["Хочу {pack} UC"],
            })
            self.assertEqual(config["reply_prompt"], DEFAULT_REPLY_PROMPT)
            # Send once so the next activation has actual outgoing history.
            await messaging.send_message("seller", "Привет", sender_account_index=1)
            for custom in ("Интересуйся покупкой 1800 UC", "Уточняй цену перед покупкой"):
                config = campaign.save_settings({**config, "reply_prompt": custom})
                await messaging.send_message(
                    "seller", "Хочу 1800 UC", sender_account_index=1, apply_persona=True,
                )
                arguments = responder.rewrite_for_persona.call_args.kwargs
                activation_prompt = arguments["identity_prompt"]
                self.assertTrue(activation_prompt.startswith(custom))
                self.assertIn(ACTIVATION_INSTRUCTION, activation_prompt)
                self.assertNotIn(AUTO_REPLY_INSTRUCTION, activation_prompt)
                self.assertTrue(arguments["history"])
                self.assertEqual(arguments["text"], "Хочу 1800 UC")
                runtime = AutoReplyRuntime(settings, requests, sender, messaging, responder)
                reply_prompt = runtime._compose_prompt("buyer", config["reply_prompt"])
                self.assertTrue(reply_prompt.startswith(custom))
                self.assertIn(AUTO_REPLY_INSTRUCTION, reply_prompt)
                self.assertNotIn(ACTIVATION_INSTRUCTION, reply_prompt)
            self.assertEqual(campaign.get_settings()["reply_prompt"], custom)

    async def test_editor_shows_full_current_prompt_and_saves_replacement(self) -> None:
        current = "Текущий промпт " + "а" * 3900
        query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
        message = SimpleNamespace(reply_text=AsyncMock(), text="Новый общий промпт")
        update = SimpleNamespace(callback_query=query, effective_message=message)
        api = SimpleNamespace(
            get_campaign=AsyncMock(return_value={"reply_prompt": current}),
            save_campaign=AsyncMock(),
        )
        with patch("research_sim.bot.app._is_authorized", return_value=True), patch(
            "research_sim.bot.app._api", return_value=api,
        ):
            self.assertEqual(await edit_reply_prompt_start(update, None), EDIT_REPLY_PROMPT)
            message.reply_text.assert_awaited_with(current)
            await edit_reply_prompt_entered(update, None)
            self.assertEqual(api.save_campaign.call_args.args[0]["reply_prompt"], message.text)

    async def test_editor_can_restore_default(self) -> None:
        query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_message=message)
        api = SimpleNamespace(
            get_campaign=AsyncMock(return_value={"reply_prompt": "Свой текст"}),
            save_campaign=AsyncMock(),
        )
        with patch("research_sim.bot.app._is_authorized", return_value=True), patch(
            "research_sim.bot.app._api", return_value=api,
        ):
            await reset_reply_prompt(update, None)
        self.assertEqual(api.save_campaign.call_args.args[0]["reply_prompt"], DEFAULT_REPLY_PROMPT)
        message.reply_text.assert_awaited_with(DEFAULT_REPLY_PROMPT)
