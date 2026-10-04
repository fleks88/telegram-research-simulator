from __future__ import annotations

import random
import string
from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from ..database.models import CampaignSettingsRecord
from ..database.requests import DatabaseRequests
from ..settings import Settings
from .messaging import (
    DeliveryAlreadyProcessed,
    MessagingService,
    SendRateLimitExceeded,
)


MOSCOW_TIMEZONE = ZoneInfo("Europe/Moscow")
SUPPORTED_PACK_SIZES = {8100, 3650, 1800, 660, 325, 60}


class CampaignNotConfigured(ValueError):
    pass


class CampaignDisabled(ValueError):
    pass


class CampaignService:
    def __init__(
        self,
        settings: Settings,
        requests: DatabaseRequests,
        messaging: MessagingService,
    ) -> None:
        self.settings = settings
        self.requests = requests
        self.messaging = messaging

    def get_settings(self) -> Optional[Dict[str, Any]]:
        record = self.requests.get_campaign_settings()
        return record.config if record is not None else None

    def save_settings(self, config: Dict[str, Any]) -> Dict[str, Any]:
        recipient = self.messaging.validate_recipient(config["recipient"])
        config = dict(config)
        config["recipient"] = recipient
        slots = config["day_slots"]
        for raw_day, day_times in slots.items():
            day = int(raw_day)
            if day < 1 or day > 3:
                raise ValueError("day_slots supports campaign days 1 through 3")
            if not 1 <= len(day_times) <= 3:
                raise ValueError("each campaign day must have 1 to 3 send slots")
            parsed_times = [datetime.strptime(value, "%H:%M") for value in day_times]
            if any(value.strftime("%H:%M") != original for value, original in zip(parsed_times, day_times)):
                raise ValueError("schedule slots must use zero-padded HH:MM format")
            minutes = [value.hour * 60 + value.minute for value in parsed_times]
            if minutes != sorted(set(minutes)):
                raise ValueError("schedule slots must be unique and ordered")
            if any(later - earlier < 30 for earlier, later in zip(minutes, minutes[1:])):
                raise ValueError("schedule slots must be at least 30 minutes apart")
        if not config["phrases"] or any(not phrase.strip() for phrase in config["phrases"]):
            raise ValueError("phrases must contain non-empty strings")
        allowed_template_fields = {"date", "day", "slot", "pack", "activations"}
        for phrase in config["phrases"]:
            try:
                fields = {
                    field_name
                    for _, field_name, _, _ in string.Formatter().parse(phrase)
                    if field_name is not None
                }
            except ValueError as exc:
                raise ValueError("phrase template has invalid format syntax") from exc
            if not fields <= allowed_template_fields:
                raise ValueError(
                    "templates support only {date}, {day}, {slot}, {pack}, and {activations}"
                )
        config.setdefault("auto_reply_enabled", False)
        config.setdefault("reply_prompt", None)
        config.setdefault("activation_enabled", False)
        config.setdefault("activation_rules", {})
        if not isinstance(config["activation_rules"], dict):
            raise ValueError("activation_rules must be a pack-to-multiplier object")
        for pack_key, multiplier in config["activation_rules"].items():
            try:
                pack_size = int(pack_key)
                numeric_multiplier = int(multiplier)
            except (TypeError, ValueError) as exc:
                raise ValueError("activation_rules must map pack sizes to integer multipliers") from exc
            if pack_size not in SUPPORTED_PACK_SIZES:
                raise ValueError(f"unsupported activation pack: {pack_size}")
            if not 1 <= numeric_multiplier <= 1_000_000:
                raise ValueError("activation multiplier must be between 1 and 1000000")
        if config["activation_enabled"] and not config["activation_rules"]:
            raise ValueError("activation_rules are required when activation sending is enabled")
        if config["auto_reply_enabled"] and not (config["reply_prompt"] or "").strip():
            raise ValueError("reply_prompt is required when automatic replies are enabled")
        if config["reply_prompt"] is not None and len(config["reply_prompt"]) > 4000:
            raise ValueError("reply_prompt must be 4000 characters or fewer")
        self.requests.save_campaign_settings(config)
        return config

    @staticmethod
    def find_due_slot(config: Dict[str, Any], now: datetime) -> Optional[Tuple[int, str]]:
        start_date = date.fromisoformat(config["start_date"])
        campaign_day = (now.date() - start_date).days + 1
        day_times = config["day_slots"].get(str(campaign_day), [])
        current_time = now.strftime("%H:%M")
        if current_time in day_times:
            return campaign_day, current_time
        return None

    async def tick(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        record = self.requests.get_campaign_settings()
        if record is None:
            raise CampaignNotConfigured("configure a campaign before requesting a tick")
        config = record.config
        if not config["enabled"]:
            raise CampaignDisabled("campaign is disabled")

        current = now or datetime.now(MOSCOW_TIMEZONE)
        if current.tzinfo is not None:
            current = current.astimezone(MOSCOW_TIMEZONE)
        slot = self.find_due_slot(config, current)
        if slot is None:
            return {"status": "not_due"}
        campaign_day, campaign_slot = slot
        phrase = random.choice(config["phrases"])
        try:
            phrase = phrase.format(
                date=current.strftime("%Y-%m-%d"),
                day=campaign_day,
                slot=campaign_slot,
                pack="",
                activations=0,
            )
        except (KeyError, ValueError) as exc:
            raise ValueError("phrase template supports only {date}, {day}, and {slot}") from exc
        try:
            result = await self.messaging.send_message(
                config["recipient"],
                phrase,
                campaign_id=config["campaign_id"],
                campaign_day=campaign_day,
                campaign_slot=campaign_slot,
            )
        except DeliveryAlreadyProcessed:
            return {
                "status": "already_processed",
                "campaign_day": campaign_day,
                "slot": campaign_slot,
            }
        except SendRateLimitExceeded:
            return {
                "status": "rate_limited",
                "campaign_day": campaign_day,
                "slot": campaign_slot,
            }
        return {
            "status": "sent",
            "campaign_day": campaign_day,
            "slot": campaign_slot,
            "delivery_id": result.delivery_id,
            "sender_account": result.sender_account,
            "account_index": result.sender_account_index,
        }