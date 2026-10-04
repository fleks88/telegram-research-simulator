from __future__ import annotations

import logging
import random
import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional
from zoneinfo import ZoneInfo

import httpx

from ..database.requests import DatabaseRequests
from ..services.messaging import MessagingService, SendRateLimitExceeded
from ..settings import Settings
from .campaign import SUPPORTED_PACK_SIZES


LOGGER = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")
SYNC_INTERVAL_SECONDS = 300
MAX_QUEUED_MESSAGES_PER_SYNC = 1000


class ActivationSyncService:
    """Poll 24-hour pack statistics and enqueue one send per configured threshold."""

    def __init__(
        self,
        settings: Settings,
        requests: DatabaseRequests,
        messaging: MessagingService,
        *,
        fetcher: Optional[Callable[..., Awaitable[Dict[str, Any]]]] = None,
    ) -> None:
        self.settings = settings
        self.requests = requests
        self.messaging = messaging
        self.fetcher = fetcher
        self._task: Optional[asyncio.Task[None]] = None

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

    async def _run(self) -> None:
        while True:
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Pack activation synchronization failed")
            await asyncio.sleep(SYNC_INTERVAL_SECONDS)

    async def sync_once(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        config_record = self.requests.get_campaign_settings()
        config = config_record.config if config_record else {}
        if not config.get("activation_enabled"):
            return {"status": "disabled"}
        if not self.settings.pack_activation_endpoint:
            return {"status": "endpoint_not_configured"}
        if len(self.settings.allowed_recipients) != 1:
            return {"status": "invalid_recipient_config"}

        current_time = now if now is not None else datetime.now(timezone.utc).timestamp()
        state = self.requests.get_activation_sync_state() or {
            "first_response": None,
            "last_response": None,
            "last_sync_at": None,
            "remainders": {},
            "pending": [],
        }

        polled = False
        is_baseline = state.get("first_response") is None
        if self._poll_is_due(state.get("last_sync_at"), current_time):
            response = await self._fetch_snapshot(state.get("last_sync_at"))
            self._validate_snapshot(response)
            polled = True
            if state["first_response"] is None:
                state["first_response"] = response
            else:
                self._apply_deltas(state, config, response)
            state["last_response"] = response
            state["last_sync_at"] = self._parse_timestamp(response["as_of"])
            self.requests.save_activation_sync_state(state)

        if not state["pending"]:
            return {
                "status": "baseline_saved" if polled and is_baseline else "synced",
                "polled": polled,
                "pending": 0,
            }

        result = await self._send_next_pending(config, state)
        return {"polled": polled, "pending": len(state["pending"]), **result}

    @staticmethod
    def _poll_is_due(last_sync_at: Optional[float], current_time: float) -> bool:
        return last_sync_at is None or current_time - float(last_sync_at) >= SYNC_INTERVAL_SECONDS

    async def _fetch_snapshot(self, since: Optional[float]) -> Dict[str, Any]:
        params: Dict[str, Any] = {"window_hours": 24}
        if since is not None:
            params["since"] = datetime.fromtimestamp(float(since), timezone.utc).isoformat()
        if self.fetcher is not None:
            return await self.fetcher(params=params)

        headers: Dict[str, str] = {}
        if self.settings.pack_activation_api_token:
            headers["Authorization"] = "Bearer " + self.settings.pack_activation_api_token
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                self.settings.pack_activation_endpoint or "",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _parse_timestamp(value: str) -> float:
        if not isinstance(value, str):
            raise ValueError("activation response as_of must be an ISO-8601 timestamp")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("activation response as_of must include a timezone")
        return parsed.astimezone(timezone.utc).timestamp()

    @classmethod
    def _validate_snapshot(cls, response: Dict[str, Any]) -> None:
        if response.get("window_hours") != 24:
            raise ValueError("activation endpoint must return window_hours=24")
        cls._parse_timestamp(response.get("as_of"))
        packs = response.get("packs")
        if not isinstance(packs, list):
            raise ValueError("activation endpoint packs must be an array")
        seen: set[int] = set()
        for item in packs:
            if not isinstance(item, dict):
                raise ValueError("each activation pack item must be an object")
            pack_size = item.get("pack_size")
            if pack_size not in SUPPORTED_PACK_SIZES or pack_size in seen:
                raise ValueError("activation endpoint contains an unsupported or duplicate pack_size")
            seen.add(pack_size)
            for key in ("activations_24h", "activations_since_previous_sync"):
                value = item.get(key)
                if not isinstance(value, int) or value < 0:
                    raise ValueError(f"{key} must be a non-negative integer")
        if seen != SUPPORTED_PACK_SIZES:
            raise ValueError("activation endpoint must return all six supported pack sizes")

    @staticmethod
    def _apply_deltas(
        state: Dict[str, Any],
        config: Dict[str, Any],
        response: Dict[str, Any],
    ) -> None:
        rules = {int(pack): int(multiplier) for pack, multiplier in config.get("activation_rules", {}).items()}
        remainders = state.setdefault("remainders", {})
        active_rule_keys = {f"{pack_size}x{multiplier}" for pack_size, multiplier in rules.items()}
        for stale_key in set(remainders) - active_rule_keys:
            remainders.pop(stale_key, None)
        pending = state.setdefault("pending", [])
        deltas = {
            item["pack_size"]: item["activations_since_previous_sync"]
            for item in response["packs"]
        }
        queued_count = 0
        for pack_size, multiplier in rules.items():
            rule_key = f"{pack_size}x{multiplier}"
            threshold = pack_size * multiplier
            accumulated = int(remainders.get(rule_key, 0)) + int(deltas.get(pack_size, 0))
            crossed = min(accumulated // threshold, MAX_QUEUED_MESSAGES_PER_SYNC - queued_count)
            for occurrence in range(crossed):
                pending.append(
                    {
                        "pack_size": pack_size,
                        "multiplier": multiplier,
                        "threshold": threshold,
                        "as_of": response["as_of"],
                        "occurrence": occurrence + 1,
                    }
                )
            queued_count += crossed
            remainders[rule_key] = accumulated - crossed * threshold

    async def _send_next_pending(
        self,
        config: Dict[str, Any],
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        item = state["pending"][0]
        phrases = config.get("phrases") or []
        if not phrases:
            return {"status": "no_templates", "pending": len(state["pending"])}
        phrase = random.choice(phrases)
        at = datetime.fromisoformat(item["as_of"].replace("Z", "+00:00")).astimezone(MOSCOW)
        text = phrase.format(
            date=at.strftime("%Y-%m-%d"),
            day=(at.date() - datetime.fromisoformat(config["start_date"]).date()).days + 1,
            slot=at.strftime("%H:%M"),
            pack=item["pack_size"],
            activations=item["threshold"],
        )
        try:
            result = await self.messaging.send_message(
                config["recipient"],
                text,
                apply_persona=True,
            )
        except SendRateLimitExceeded:
            return {"status": "rate_limited", "pending": len(state["pending"])}
        state["pending"].pop(0)
        self.requests.save_activation_sync_state(state)
        return {
            "status": "sent",
            "pack_size": item["pack_size"],
            "threshold": item["threshold"],
            "sender_account": result.sender_account,
            "pending": len(state["pending"]),
        }