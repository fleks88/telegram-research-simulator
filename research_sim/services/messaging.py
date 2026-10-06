from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..database.requests import DatabaseRequests
from ..integrations.telegram import TelethonSender
from ..settings import Settings, normalize_username
from .conversation_prompts import compose_prompt
from .personas import PersonaService
from .prompt_responder import PromptResponder


class RecipientNotAllowed(ValueError):
    pass


class SendRateLimitExceeded(ValueError):
    pass


class DeliveryAlreadyProcessed(ValueError):
    pass


class SenderAccountNotAvailable(ValueError):
    pass


@dataclass(frozen=True)
class SendResult:
    delivery_id: int
    recipient: str
    text: str
    sender_account: str
    sender_account_index: int
    photo_attached: bool


class MessagingService:
    def __init__(
        self,
        settings: Settings,
        requests: DatabaseRequests,
        sender: TelethonSender,
        personas: PersonaService | None = None,
        prompt_responder: PromptResponder | None = None,
    ) -> None:
        self.settings = settings
        self.requests = requests
        self.sender = sender
        self.personas = personas or PersonaService(requests)
        self.prompt_responder = prompt_responder

    def validate_recipient(self, recipient: str) -> str:
        normalized = normalize_username(recipient)
        campaign = self.requests.get_campaign_settings()
        configured = (campaign.config.get("recipient") if campaign else "") or ""
        try:
            configured = normalize_username(configured) if configured else ""
        except ValueError:
            configured = ""
        allowed = {configured} if configured else self.settings.allowed_recipients
        if normalized not in allowed:
            raise RecipientNotAllowed("recipient is not the bot-configured allowlist user")
        return normalized

    async def send_message(
        self,
        recipient: str,
        text: str,
        *,
        campaign_id: Optional[str] = None,
        campaign_day: Optional[int] = None,
        campaign_slot: Optional[str] = None,
        sender_account_index: Optional[int] = None,
        photo: Optional[bytes] = None,
        apply_persona: bool = False,
    ) -> SendResult:
        normalized_recipient = self.validate_recipient(recipient)
        message = text.strip()
        if not message:
            raise ValueError("message must not be empty")
        if len(message) > 4096:
            raise ValueError("message must be 4096 characters or fewer")
        if photo is not None:
            if len(photo) > 10 * 1024 * 1024:
                raise ValueError("photo must be 10 MB or smaller")
            if not photo.startswith(b"\xff\xd8\xff"):
                raise ValueError("photo must be a JPEG image")
            if len(message) > 1024:
                raise ValueError("photo caption must be 1024 characters or fewer")

        reservation = self.requests.reserve_delivery(
            recipient=normalized_recipient,
            minimum_interval_seconds=self.settings.minimum_send_interval_seconds,
            campaign_id=campaign_id,
            campaign_day=campaign_day,
            campaign_slot=campaign_slot,
        )
        if not reservation.acquired:
            if reservation.reason == "already_processed":
                raise DeliveryAlreadyProcessed("campaign slot was already processed")
            raise SendRateLimitExceeded("a message was sent recently; retry later")

        delivery_id = reservation.delivery_id
        try:
            if sender_account_index is None:
                account_reservation = self.requests.reserve_next_sender_account()
            else:
                account_reservation = self.requests.reserve_sender_account_by_index(
                    sender_account_index
                )
            sender_account = account_reservation.account
            if sender_account is None:
                if account_reservation.reason == "disabled":
                    raise SenderAccountNotAvailable("selected sender account is disabled")
                if sender_account_index is not None:
                    raise SenderAccountNotAvailable("sender account index does not exist")
                raise RuntimeError("no active sender accounts are registered")
            if campaign_id is not None or apply_persona:
                persona = self.personas.get(sender_account.account_key)
                if self.prompt_responder is not None and self.settings.llm_api_key:
                    campaign = self.requests.get_campaign_settings()
                    message = await self.prompt_responder.rewrite_for_persona(
                        identity_prompt=compose_prompt(
                            campaign.config.get("reply_prompt") if campaign else None,
                            self.personas.prompt_fragment(sender_account.account_key),
                            mode="activation",
                        ),
                        text=message,
                        word_accuracy_percent=persona["word_accuracy_percent"],
                        punctuation_accuracy_percent=persona["punctuation_accuracy_percent"],
                        history=self.requests.get_conversation_context(
                            sender_account.account_key, limit=12, recipient=normalized_recipient,
                        ),
                    )
                message = self.personas.stylize_scheduled_text(
                    sender_account.account_key,
                    message,
                    seed=f"{campaign_id}:{campaign_day}:{campaign_slot}:{sender_account.account_key}",
                    profile=persona,
                )
            self.requests.set_delivery_content(
                delivery_id,
                sender_account=sender_account.account_key,
                message_text=message,
                photo_attached=photo is not None,
            )
            await self.sender.send_message(
                sender_account.account_key,
                normalized_recipient,
                message,
                photo=photo,
            )
            self.requests.record_sender_account_success(sender_account.account_key)
        except Exception as exc:
            self.requests.finish_delivery(delivery_id, status="failed", error=str(exc))
            raise
        self.requests.finish_delivery(delivery_id, status="sent")
        return SendResult(
            delivery_id,
            normalized_recipient,
            message,
            sender_account.account_key,
            sender_account.account_index,
            photo is not None,
        )
