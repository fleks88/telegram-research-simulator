from __future__ import annotations

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
        if self.settings.telegram_api_id is None or not self.settings.telegram_api_hash:
            raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured")
        session_path = Path(self.settings.telegram_session_dir).expanduser() / account_key
        if self.client_factory is None and not Path(str(session_path) + ".session").exists():
            raise RuntimeError(
                f"authorized Telethon session is missing for account '{account_key}'"
            )
        session_path.parent.mkdir(parents=True, exist_ok=True)
        client = client_factory(
            session=str(session_path),
            api_id=self.settings.telegram_api_id,
            api_hash=self.settings.telegram_api_hash,
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
        if self.settings.telegram_api_id is None or not self.settings.telegram_api_hash:
            raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured")

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
        client = self._make_client(str(session_path))
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

    def _make_client(self, session_path: str) -> Any:
        client_factory = self.client_factory
        if client_factory is None:
            from telethon import TelegramClient

            client_factory = TelegramClient
        if self.settings.telegram_api_id is None or not self.settings.telegram_api_hash:
            raise RuntimeError("TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured")
        return client_factory(
            session=session_path,
            api_id=self.settings.telegram_api_id,
            api_hash=self.settings.telegram_api_hash,
        )