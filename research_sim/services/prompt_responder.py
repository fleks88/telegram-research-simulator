from __future__ import annotations

import httpx
from typing import Any, Dict, List, Optional

from ..settings import Settings


class PromptResponder:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def create_reply(
        self,
        prompt: str,
        incoming_text: str,
        *,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        if not self.settings.llm_api_key:
            raise RuntimeError("LLM_API_KEY is required for automatic prompt replies")
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                self.settings.llm_base_url + "/chat/completions",
                headers={"Authorization": "Bearer " + self.settings.llm_api_key},
                json={
                    "model": self.settings.llm_model,
                    "messages": [
                        {"role": "system", "content": prompt},
                        *[
                            {
                                "role": "assistant" if row["direction"] == "outgoing" else "user",
                                "content": row["message_text"],
                            }
                            for row in history or []
                        ],
                        {
                            "role": "user",
                            "content": "Ответь на последнее сообщение центрального "
                            "тестового аккаунта:\n" + incoming_text,
                        },
                    ],
                    "temperature": 0.7,
                    "max_tokens": 500,
                },
            )
            response.raise_for_status()
            data = response.json()
        try:
            reply = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError, TypeError) as exc:
            raise RuntimeError("LLM endpoint returned an invalid chat completion") from exc
        if not reply:
            raise RuntimeError("LLM endpoint returned an empty response")
        return reply[:4096]