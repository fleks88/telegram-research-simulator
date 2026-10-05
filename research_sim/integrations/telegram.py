from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Optional

from ..settings import Settings


class TelethonSender:
    def __init__(
        self,
        settings: Settings,
        client_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.settings = settings
        self.client_factory = client_factory
        self._clients: dict[str, Any] = {}

    async def connect_session(self, account_key: str) -> Any:
        existing = self._clients.get(account_key)
        if existing is not None:
            return existing
        client_factory = self.client_factory
        if client_factory is None:
            try:
                from telethon import TelegramClient
            except ImportError as exc:
                raise RuntimeError("Install Telethon to listen for Telegram messages") from exc
            client_factory = TelegramClient
        api_id, api_hash = self._credentials_for(account_key)
        session_path = Path(self.settings.telegram_session_dir).expanduser() / account_key
        if self.client_factory is None and not Path(str(session_path) + ".session").exists():
            raise RuntimeError(
                f"authorized Telethon session is missing for account '{account_key}'"
            )
        session_path.parent.mkdir(parents=True, exist_ok=True)
        client = client_factory(
            session=str(session_path),
            api_id=api_id,
            api_hash=api_hash,
        )
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise RuntimeError(f"Telethon session is not authorized for '{account_key}'")
        self._clients[account_key] = client
        return client

    async def disconnect_session(self, account_key: str) -> None:
        client = self._clients.pop(account_key, None)
        if client is not None:
            await client.disconnect()

    async def send_message(
        self,
        account_key: str,
        recipient: str,
        text: str,
        photo: Optional[bytes] = None,
    ) -> None:
        client = self._clients.get(account_key)
        if client is not None:
            if photo is None:
                await client.send_message("@" + recipient, text)
            else:
                photo_stream = BytesIO(photo)
                photo_stream.name = "photo.jpg"
                await client.send_file(
                    "@" + recipient,
                    file=photo_stream,
                    caption=text,
                )
            return

        session_path = Path(self.settings.telegram_session_dir).expanduser() / account_key
        if self.client_factory is None and not Path(str(session_path) + ".session").exists():
            raise RuntimeError(
                f"authorized Telethon session is missing for account '{account_key}'"
            )
        client = self._make_client(str(session_path), account_key)
        async with client:
            if photo is None:
                await client.send_message("@" + recipient, text)
            else:
                photo_stream = BytesIO(photo)
                photo_stream.name = "photo.jpg"
                await client.send_file(
                    "@" + recipient,
                    file=photo_stream,
                    caption=text,
                )

    def _make_client(self, session_path: str, account_key: str) -> Any:
        client_factory = self.client_factory
        if client_factory is None:
            from telethon import TelegramClient

            client_factory = TelegramClient
        api_id, api_hash = self._credentials_for(account_key)
        return client_factory(
            session=session_path,
            api_id=api_id,
            api_hash=api_hash,
        )

    def _credentials_for(self, account_key: str) -> tuple[int, str]:
        metadata_path = (
            Path(self.settings.telegram_session_dir).expanduser()
            / f"{account_key}.json"
        )
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"invalid session metadata for '{account_key}'"
                ) from exc
            telegram = metadata.get("telegram", {})
            if not isinstance(telegram, dict):
                telegram = {}
            raw_api_id = (
                metadata.get("api_id")
                or metadata.get("app_id")
                or telegram.get("api_id")
                or telegram.get("app_id")
            )
            api_hash = (
                metadata.get("api_hash")
                or metadata.get("app_hash")
                or telegram.get("api_hash")
                or telegram.get("app_hash")
            )
            if raw_api_id is not None or api_hash is not None:
                try:
                    api_id = int(raw_api_id)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"invalid api_id in metadata for '{account_key}'"
                    ) from exc
                if api_id <= 0 or not isinstance(api_hash, str) or not api_hash.strip():
                    raise RuntimeError(
                        f"api_id and api_hash must both be valid for '{account_key}'"
                    )
                return api_id, api_hash.strip()
        if self.settings.telegram_api_id is None or not self.settings.telegram_api_hash:
            raise RuntimeError(
                "TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured globally "
                f"or supplied in {account_key}.json"
            )
        return self.settings.telegram_api_id, self.settings.telegram_api_hash
