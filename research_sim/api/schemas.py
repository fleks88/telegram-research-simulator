from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class SendMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=4096)
    account_index: int = Field(ge=1)


class SendMessageResponse(BaseModel):
    delivery_id: int
    recipient: str
    sender_account: str
    account_index: int
    status: str = "sent"
    text: str
    photo_attached: bool = False


class SenderAccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_key: str = Field(min_length=1, max_length=48)
    label: str = Field(min_length=1, max_length=80)


class SenderAccountResponse(BaseModel):
    account_index: int
    account_key: str
    label: str
    enabled: bool
    send_count: int
    last_used_at: Optional[float] = None


class SenderAccountEnabledUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class CampaignSettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    campaign_id: str = Field(min_length=1, max_length=80)
    enabled: bool = False
    start_date: date
    recipient: str = Field(min_length=1, max_length=64)
    phrases: List[str] = Field(min_length=1, max_length=100)
    day_slots: Dict[int, List[str]]
    auto_reply_enabled: bool = False
    reply_prompt: Optional[str] = Field(default=None, max_length=4000)


class CampaignTickResponse(BaseModel):
    status: str
    campaign_day: Optional[int] = None
    slot: Optional[str] = None
    delivery_id: Optional[int] = None
    sender_account: Optional[str] = None
    account_index: Optional[int] = None


class MessageHistoryItem(BaseModel):
    id: int
    sender_account: str
    recipient: str
    message_text: str
    photo_attached: bool
    created_at: float
    status: str
    error: Optional[str] = None


class HealthResponse(BaseModel):
    status: str