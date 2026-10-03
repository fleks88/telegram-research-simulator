from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class CampaignSettingsRecord:
    config: Dict[str, Any]


@dataclass(frozen=True)
class DeliveryReservation:
    delivery_id: Optional[int]
    reason: Optional[str] = None

    @property
    def acquired(self) -> bool:
        return self.delivery_id is not None


@dataclass(frozen=True)
class SenderAccount:
    account_index: int
    account_key: str
    label: str
    enabled: bool
    send_count: int
    last_used_at: Optional[float]


@dataclass(frozen=True)
class SenderAccountReservation:
    account: Optional[SenderAccount]
    reason: Optional[str] = None