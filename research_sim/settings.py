from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Set

from dotenv import load_dotenv


def normalize_username(username: str) -> str:
    normalized = username.strip().removeprefix("@").casefold()
    if not normalized or not all(
        character.isascii()
        and (character.isalnum() or character == "_")
        for character in normalized
    ):
        raise ValueError("recipient must be a Telegram username")
    return normalized


@dataclass(frozen=True)
class Settings:
    database_path: Path
    api_token: Optional[str]
    telegram_api_id: Optional[int]
    telegram_api_hash: Optional[str]
    telegram_session_dir: Path
    allowed_recipients: Set[str]
    minimum_send_interval_seconds: int = 30
    llm_api_key: Optional[str] = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    pack_activation_endpoint: Optional[str] = None
    pack_activation_api_token: Optional[str] = None
    persona_corpus_path: Path = Path("result.json")

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv()
        raw_api_id = os.environ.get("TELEGRAM_API_ID", "").strip()
        try:
            api_id = int(raw_api_id) if raw_api_id else None
        except ValueError as exc:
            raise ValueError("TELEGRAM_API_ID must be an integer") from exc

        raw_interval = os.environ.get("MINIMUM_SEND_INTERVAL_SECONDS", "30")
        try:
            minimum_interval = int(raw_interval)
        except ValueError as exc:
            raise ValueError("MINIMUM_SEND_INTERVAL_SECONDS must be an integer") from exc
        if minimum_interval < 1:
            raise ValueError("MINIMUM_SEND_INTERVAL_SECONDS must be at least 1")

        recipients = {
            normalize_username(item)
            for item in os.environ.get("TELEGRAM_ALLOWED_RECIPIENTS", "").split(",")
            if item.strip()
        }
        database_path = Path(
            os.environ.get("DATABASE_PATH", "data/telegram-research.sqlite3")
        ).expanduser()
        session_dir = Path(
            os.environ.get(
                "TELEGRAM_SESSION_DIR",
                "~/.telegram-research-simulator/sessions",
            )
        ).expanduser()
        llm_base_url = os.environ.get(
            "LLM_BASE_URL",
            "https://api.openai.com/v1",
        ).rstrip("/")
        llm_model = os.environ.get("LLM_MODEL", "gpt-4o-mini").strip()

        return cls(
            database_path=database_path,
            api_token=os.environ.get("API_TOKEN") or None,
            telegram_api_id=api_id,
            telegram_api_hash=os.environ.get("TELEGRAM_API_HASH") or None,
            telegram_session_dir=session_dir,
            allowed_recipients=recipients,
            minimum_send_interval_seconds=minimum_interval,
            llm_api_key=os.environ.get("LLM_API_KEY") or None,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            pack_activation_endpoint=os.environ.get("PACK_ACTIVATION_ENDPOINT") or None,
            pack_activation_api_token=os.environ.get("PACK_ACTIVATION_API_TOKEN") or None,
            persona_corpus_path=Path(
                os.environ.get("PERSONA_CORPUS_PATH", "result.json")
            ).expanduser(),
        )
