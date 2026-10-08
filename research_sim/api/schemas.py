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


class PersonaProfilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_version: int = Field(default=4, ge=1)
    identity_prompt: str = Field(default="", max_length=2000)
    word_accuracy_percent: int = Field(default=98, ge=0, le=100)
    punctuation_accuracy_percent: int = Field(default=98, ge=0, le=100)
    literacy_level: int = Field(default=5, ge=1, le=5)
    aggression_level: int = Field(default=1, ge=1, le=5)
    friendliness_level: int = Field(default=3, ge=1, le=5)
    verbosity_level: int = Field(default=3, ge=1, le=5)
    humor_level: int = Field(default=2, ge=1, le=5)
    emoji_level: int = Field(default=1, ge=1, le=5)
    initiative_level: int = Field(default=3, ge=1, le=5)
    address_style: str = Field(default="ты", pattern="^(ты|вы)$")
    terminal_period_percent: int = Field(default=1, ge=0, le=100)
    lowercase_start_percent: int = Field(default=10, ge=0, le=100)


class DialogueProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_key: str = Field(min_length=1, max_length=48)
    task_prompt: str = Field(min_length=1, max_length=2000)
    target_replies: List[str] = Field(min_length=5, max_length=5)


class DialogueTurn(BaseModel):
    sender: str
    target: str


class DialogueProposalResponse(BaseModel):
    proposal_id: int
    account_key: str
    dialogue: List[DialogueTurn]


class ReplyPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_key: str = Field(min_length=1, max_length=48)
    incoming_text: str = Field(min_length=1, max_length=4096)


class ReplyPreviewResponse(BaseModel):
    account_key: str
    reply_prompt: str
    incoming_text: str
    reply_text: str


class ManualOperatorAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator_id: int = Field(gt=0)


class ManualReplyRequest(ManualOperatorAction):
    text: str = Field(min_length=1, max_length=4096)


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
    reply_delay_min_minutes: int = Field(default=2, ge=0, le=1440)
    reply_delay_max_minutes: int = Field(default=180, ge=0, le=1440)
    activation_enabled: bool = False
    activation_rules: Dict[int, int] = Field(default_factory=dict)


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
