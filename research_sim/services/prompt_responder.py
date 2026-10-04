from __future__ import annotations

import httpx
import json
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
                            "content": incoming_text,
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

    async def propose_dialogue(
        self,
        *,
        task_prompt: str,
        persona: Dict[str, Any],
        target_replies: List[str],
    ) -> List[Dict[str, str]]:
        if not self.settings.llm_api_key:
            raise RuntimeError("LLM_API_KEY is required to propose a dialogue")
        if len(target_replies) != 5:
            raise ValueError("exactly five fixed target replies are required")
        instructions = (
            "Create a research dialogue proposal for a closed test between two "
            "owned accounts. Do not send anything. Return only JSON with key "
            "'sender_messages' containing exactly five short strings. The five "
            "fixed target replies are provided separately and must not be rewritten. "
            "Do not include the target replies in sender_messages."
        )
        input_data = {
            "task_prompt": task_prompt,
            "sender_persona": persona,
            "fixed_target_replies": target_replies,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                self.settings.llm_base_url + "/chat/completions",
                headers={"Authorization": "Bearer " + self.settings.llm_api_key},
                json={
                    "model": self.settings.llm_model,
                    "messages": [
                        {"role": "system", "content": instructions},
                        {"role": "user", "content": json.dumps(input_data, ensure_ascii=False)},
                    ],
                    "response_format": {"type": "json_object"},
                    "temperature": 0.7,
                    "max_tokens": 1200,
                },
            )
            response.raise_for_status()
            data = response.json()
        try:
            result = json.loads(data["choices"][0]["message"]["content"])
            sender_messages = result["sender_messages"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RuntimeError("LLM returned an invalid dialogue proposal") from exc
        if (
            not isinstance(sender_messages, list)
            or len(sender_messages) != 5
            or any(not isinstance(message, str) or not message.strip() for message in sender_messages)
        ):
            raise RuntimeError("LLM proposal must contain exactly five non-empty sender messages")
        return [
            {"sender": sender_message.strip(), "target": target_reply}
            for sender_message, target_reply in zip(sender_messages, target_replies)
        ]

    async def rewrite_for_persona(
        self,
        *,
        identity_prompt: str,
        text: str,
        word_accuracy_percent: int,
        punctuation_accuracy_percent: int,
    ) -> str:
        if not self.settings.llm_api_key:
            raise RuntimeError(
                "LLM_API_KEY is required when a scheduled sender has an identity prompt"
            )
        instruction = (
            "Rewrite one short message for a closed research test. Preserve its intent "
            "and factual content. Apply the described sender voice. Do not add claims "
            "or explain the rewrite. Return only the rewritten message."
        )
        user_content = json.dumps(
            {
                "sender_identity": identity_prompt,
                "word_accuracy_percent": word_accuracy_percent,
                "punctuation_accuracy_percent": punctuation_accuracy_percent,
                "draft": text,
            },
            ensure_ascii=False,
        )
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                self.settings.llm_base_url + "/chat/completions",
                headers={"Authorization": "Bearer " + self.settings.llm_api_key},
                json={
                    "model": self.settings.llm_model,
                    "messages": [
                        {"role": "system", "content": instruction},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.5,
                    "max_tokens": 300,
                },
            )
            response.raise_for_status()
            data = response.json()
        try:
            rewritten = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise RuntimeError("LLM endpoint returned an invalid persona rewrite") from exc
        if not rewritten:
            raise RuntimeError("LLM endpoint returned an empty persona rewrite")
        return rewritten[:4096]
