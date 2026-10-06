from __future__ import annotations

import string
from typing import Any, Dict, Optional

from ..database.requests import DatabaseRequests
from ..settings import Settings, normalize_username
from .messaging import MessagingService
from .conversation_prompts import shared_prompt


SUPPORTED_PACK_SIZES = {8100, 3650, 1800, 660, 325, 60}


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
        if record is None:
            return None
        return {**record.config, "reply_prompt": shared_prompt(record.config.get("reply_prompt"))}

    def save_settings(self, config: Dict[str, Any]) -> Dict[str, Any]:
        recipient = normalize_username(config["recipient"])
        config = dict(config)
        config["recipient"] = recipient
        config["enabled"] = False
        config["day_slots"] = {}
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
        config["reply_prompt"] = shared_prompt(config.get("reply_prompt"))
        config.setdefault("reply_delay_min_minutes", 2)
        config.setdefault("reply_delay_max_minutes", 180)
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
        if config["reply_prompt"] is not None and len(config["reply_prompt"]) > 4000:
            raise ValueError("reply_prompt must be 4000 characters or fewer")
        if not (
            0 <= int(config["reply_delay_min_minutes"]) <= 1440
            and 0 <= int(config["reply_delay_max_minutes"]) <= 1440
        ):
            raise ValueError("reply delays must be between 0 and 1440 minutes")
        if config["reply_delay_min_minutes"] > config["reply_delay_max_minutes"]:
            raise ValueError("reply_delay_min_minutes must not exceed reply_delay_max_minutes")
        self.requests.save_campaign_settings(config)
        return config
