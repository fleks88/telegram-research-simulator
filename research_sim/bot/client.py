from __future__ import annotations

from typing import Any, Dict, Optional

import httpx


class ApiClient:
    def __init__(self, base_url: str, api_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(
            timeout=25,
            headers={"Authorization": "Bearer " + api_token},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.client.request(
            method,
            self.base_url + path,
            **kwargs,
        )
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise RuntimeError(f"API {response.status_code}: {detail}")
        if response.status_code == 204:
            return None
        return response.json()

    async def accounts(self) -> list[Dict[str, Any]]:
        return await self.request("GET", "/api/v1/accounts")

    async def add_account(self, account_key: str, label: str) -> Dict[str, Any]:
        return await self.request(
            "POST",
            "/api/v1/accounts",
            json={"account_key": account_key, "label": label},
        )

    async def set_account_enabled(self, account_key: str, enabled: bool) -> Dict[str, Any]:
        return await self.request(
            "PATCH",
            "/api/v1/accounts/" + account_key,
            json={"enabled": enabled},
        )

    async def account_persona(self, account_key: str) -> Dict[str, Any]:
        return await self.request("GET", "/api/v1/accounts/" + account_key + "/persona")

    async def save_account_persona(
        self,
        account_key: str,
        profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        return await self.request(
            "PUT",
            "/api/v1/accounts/" + account_key + "/persona",
            json=profile,
        )

    async def propose_dialogue(
        self,
        *,
        account_key: str,
        task_prompt: str,
        target_replies: list[str],
    ) -> Dict[str, Any]:
        return await self.request(
            "POST",
            "/api/v1/research/dialogues/propose",
            json={
                "account_key": account_key,
                "task_prompt": task_prompt,
                "target_replies": target_replies,
            },
        )

    async def preview_reply(
        self,
        *,
        account_key: str,
        incoming_text: str,
    ) -> Dict[str, Any]:
        return await self.request(
            "POST",
            "/api/v1/research/reply-preview",
            json={"account_key": account_key, "incoming_text": incoming_text},
        )

    async def account_dialogues(self, account_key: str, limit: int = 10) -> list[Dict[str, Any]]:
        return await self.request(
            "GET",
            "/api/v1/accounts/" + account_key + "/dialogues",
            params={"limit": limit},
        )

    async def account_history(
        self,
        account_key: str,
        *,
        limit: int = 10,
        before_id: Optional[int] = None,
    ) -> list[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": limit}
        if before_id is not None:
            params["before_id"] = before_id
        return await self.request(
            "GET",
            "/api/v1/accounts/" + account_key + "/history",
            params=params,
        )

    async def account_timeline(
        self,
        account_key: str,
        *,
        limit: int = 50,
    ) -> list[Dict[str, Any]]:
        return await self.request(
            "GET",
            "/api/v1/accounts/" + account_key + "/timeline",
            params={"limit": limit},
        )

    async def send_message(
        self,
        *,
        recipient: str,
        text: str,
        account_index: int,
    ) -> Dict[str, Any]:
        return await self.request(
            "POST",
            "/api/v1/messages/send",
            json={
                "recipient": recipient,
                "text": text,
                "account_index": account_index,
            },
        )

    async def send_photo(
        self,
        *,
        recipient: str,
        text: str,
        account_index: int,
        photo: bytes,
        filename: str = "photo.jpg",
    ) -> Dict[str, Any]:
        return await self.request(
            "POST",
            "/api/v1/messages/send-with-photo",
            data={
                "recipient": recipient,
                "text": text,
                "account_index": str(account_index),
            },
            files={"photo": (filename, photo, "image/jpeg")},
        )

    async def get_campaign(self) -> Optional[Dict[str, Any]]:
        return await self.request("GET", "/api/v1/settings/campaign")

    async def save_campaign(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return await self.request(
            "PUT",
            "/api/v1/settings/campaign",
            json=config,
        )

    async def activation_status(self) -> Dict[str, Any]:
        return await self.request("GET", "/api/v1/activations/status")

    async def auto_reply_status(self) -> Dict[str, Any]:
        return await self.request("GET", "/api/v1/auto-replies/status")
