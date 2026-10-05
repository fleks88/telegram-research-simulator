from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Dict

from telethon import events

from ..database.requests import DatabaseRequests
from ..integrations.telegram import TelethonSender
from ..services.messaging import MessagingService, SendRateLimitExceeded
from ..settings import Settings
from .prompt_responder import PromptResponder
from .personas import PersonaService


LOGGER = logging.getLogger(__name__)


class AutoReplyRuntime:
    """Listen only to the single configured central test account."""

    def __init__(
        self,
        settings: Settings,
        requests: DatabaseRequests,
        sender: TelethonSender,
        messaging: MessagingService,
        responder: PromptResponder,
    ) -> None:
        self.settings = settings
        self.requests = requests
        self.sender = sender
        self.messaging = messaging
        self.responder = responder
        self.personas = PersonaService(requests)
        self._task: asyncio.Task[None] | None = None
        self._central_sender_ids: Dict[str, int] = {}
        self._handlers: Dict[str, Any] = {}

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for account_key in list(self._handlers):
            await self._remove_account(account_key)

    async def _run(self) -> None:
        while True:
            try:
                await self._sync_accounts()
                await self.process_due_replies()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Failed to synchronize Telethon listeners")
            await asyncio.sleep(15)

    async def _sync_accounts(self) -> None:
        campaign_record = self.requests.get_campaign_settings()
        config = campaign_record.config if campaign_record else {}
        should_listen = bool(
            config.get("auto_reply_enabled")
            and config.get("reply_prompt")
        )
        if not should_listen:
            for account_key in list(self._handlers):
                await self._remove_account(account_key)
            return
        if len(self.settings.allowed_recipients) != 1:
            LOGGER.error("Auto-replies require exactly one allowlisted central recipient")
            for account_key in list(self._handlers):
                await self._remove_account(account_key)
            return
        if not self.settings.llm_api_key:
            LOGGER.error("Auto-replies enabled but LLM_API_KEY is not configured")
            for account_key in list(self._handlers):
                await self._remove_account(account_key)
            return

        recipient = next(iter(self.settings.allowed_recipients))
        accounts = {
            account.account_key
            for account in self.requests.list_sender_accounts()
            if account.enabled
        }
        for account_key in set(self._handlers) - accounts:
            await self._remove_account(account_key)
        for account_key in accounts - set(self._handlers):
            try:
                client = await self.sender.connect_session(account_key)
                peer = await client.get_entity("@" + recipient)
                central_sender_id = int(peer.id)

                async def handle_message(
                    event: Any,
                    key: str = account_key,
                    expected_sender_id: int = central_sender_id,
                ) -> None:
                    await self._handle_message(key, expected_sender_id, event)

                client.add_event_handler(
                    handle_message,
                    events.NewMessage(incoming=True, from_users=peer),
                )
                self._handlers[account_key] = (client, handle_message)
            except Exception:
                LOGGER.exception("Could not start auto-reply listener for %s", account_key)

    async def _remove_account(self, account_key: str) -> None:
        registered = self._handlers.pop(account_key, None)
        self._central_sender_ids.pop(account_key, None)
        if registered is not None:
            client, callback = registered
            client.remove_event_handler(callback)
        await self.sender.disconnect_session(account_key)

    async def _handle_message(
        self,
        account_key: str,
        expected_sender_id: int,
        event: Any,
    ) -> None:
        if getattr(event, "is_private", True) is not True:
            return
        message = event.message
        message_text = (message.message or "").strip()
        sender_id = event.sender_id
        if (
            not message_text
            or sender_id is None
            or int(sender_id) != expected_sender_id
        ):
            return
        claimed = self.requests.claim_received_message(
            account_key=account_key,
            telegram_message_id=int(message.id),
            sender_id=int(sender_id),
            message_text=message_text,
        )
        if not claimed:
            return

        record = self.requests.get_campaign_settings()
        config = record.config if record else {}
        if not (
            config.get("auto_reply_enabled")
            and config.get("reply_prompt")
        ):
            self.requests.set_received_message_status(
                account_key,
                int(message.id),
                "ignored",
            )
            return

        minimum = int(config.get("reply_delay_min_minutes", 2))
        maximum = int(config.get("reply_delay_max_minutes", 180))
        delay_seconds = random.randint(minimum * 60, maximum * 60)
        prompt = self._compose_prompt(account_key, config["reply_prompt"])
        history = self.requests.get_conversation_context(account_key, limit=12)
        if (
            history
            and history[-1]["direction"] == "incoming"
            and history[-1]["message_text"] == message_text
        ):
            history = history[:-1]
        try:
            reply = await self.responder.create_reply(
                prompt,
                message_text,
                history=history,
            )
        except Exception:
            self.requests.set_received_message_status(
                account_key,
                int(message.id),
                "failed",
            )
            LOGGER.exception("Could not prepare automatic reply for %s", account_key)
            return
        self.requests.enqueue_auto_reply(
            account_key=account_key,
            telegram_message_id=int(message.id),
            due_at=time.time() + delay_seconds,
            reply_text=reply,
        )

    def _compose_prompt(self, account_key: str, prompt: str) -> str:
        return prompt.strip() + "\n\n" + self.personas.prompt_fragment(account_key)

    async def process_due_replies(self, *, now: float | None = None) -> int:
        rows = self.requests.claim_due_auto_replies(now=now, limit=10)
        sent = 0
        for row in rows:
            record = self.requests.get_campaign_settings()
            config = record.config if record else {}
            if not (config.get("auto_reply_enabled") and config.get("reply_prompt")):
                self.requests.requeue_auto_reply(row["id"], due_at=(now or time.time()) + 60)
                continue
            accounts = self.requests.list_sender_accounts()
            account = next(
                (
                    item
                    for item in accounts
                    if item.account_key == row["account_key"] and item.enabled
                ),
                None,
            )
            if account is None:
                self.requests.requeue_auto_reply(row["id"], due_at=(now or time.time()) + 300)
                continue
            try:
                reply = row.get("reply_text")
                if not reply:
                    reply = await self.responder.create_reply(
                        self._compose_prompt(
                            row["account_key"], config["reply_prompt"]
                        ),
                        row["message_text"],
                        history=self.requests.get_conversation_context(
                            row["account_key"], limit=12
                        ),
                    )
                await self.messaging.send_message(
                    next(iter(self.settings.allowed_recipients)),
                    reply,
                    sender_account_index=account.account_index,
                )
            except SendRateLimitExceeded:
                self.requests.requeue_auto_reply(
                    row["id"], due_at=(now or time.time()) + 60
                )
                continue
            except Exception as exc:
                self.requests.finish_auto_reply(row["id"], status="failed", error=str(exc))
                self.requests.set_received_message_status(
                    row["account_key"], row["telegram_message_id"], "failed"
                )
                LOGGER.exception(
                    "Automatic reply failed for account %s", row["account_key"]
                )
                continue
            self.requests.finish_auto_reply(row["id"], status="sent")
            self.requests.set_received_message_status(
                row["account_key"], row["telegram_message_id"], "replied"
            )
            sent += 1
        return sent
