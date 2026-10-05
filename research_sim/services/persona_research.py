from __future__ import annotations

from typing import Any, Dict, List

from ..database.requests import DatabaseRequests
from .personas import PersonaService
from .prompt_responder import PromptResponder


class PersonaResearchService:
    def __init__(
        self,
        requests: DatabaseRequests,
        personas: PersonaService,
        responder: PromptResponder,
    ) -> None:
        self.requests = requests
        self.personas = personas
        self.responder = responder

    def get_profile(self, account_key: str) -> Dict[str, Any]:
        if not any(account.account_key == account_key for account in self.requests.list_sender_accounts()):
            raise KeyError("sender account not found")
        return self.personas.get(account_key)

    def save_profile(self, account_key: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        return self.personas.save(account_key, profile)

    def randomize_profile(self, account_key: str) -> Dict[str, Any]:
        if not any(
            account.account_key == account_key
            for account in self.requests.list_sender_accounts()
        ):
            raise KeyError("sender account not found")
        return self.personas.randomize(account_key)

    async def propose_dialogue(
        self,
        *,
        account_key: str,
        task_prompt: str,
        target_replies: List[str],
    ) -> Dict[str, Any]:
        if not any(account.account_key == account_key for account in self.requests.list_sender_accounts()):
            raise KeyError("sender account not found")
        if not task_prompt.strip():
            raise ValueError("task_prompt must not be empty")
        if len(target_replies) != 5 or any(not reply.strip() for reply in target_replies):
            raise ValueError("provide exactly five non-empty fixed target replies")
        persona = self.personas.get(account_key)
        dialogue = await self.responder.propose_dialogue(
            task_prompt=task_prompt,
            persona=persona,
            target_replies=target_replies,
        )
        proposal_id = self.requests.save_dialogue_proposal(
            account_key,
            task_prompt,
            dialogue,
        )
        return {
            "proposal_id": proposal_id,
            "account_key": account_key,
            "dialogue": dialogue,
        }

    def list_proposals(self, account_key: str, limit: int = 10) -> List[Dict[str, Any]]:
        if not any(account.account_key == account_key for account in self.requests.list_sender_accounts()):
            raise KeyError("sender account not found")
        return self.requests.list_dialogue_proposals(account_key, limit=limit)

    async def preview_reply(
        self,
        *,
        account_key: str,
        incoming_text: str,
    ) -> Dict[str, str]:
        if not any(
            account.account_key == account_key
            for account in self.requests.list_sender_accounts()
        ):
            raise KeyError("sender account not found")
        message = incoming_text.strip()
        if not message:
            raise ValueError("incoming_text must not be empty")
        campaign = self.requests.get_campaign_settings()
        reply_prompt = (campaign.config.get("reply_prompt") if campaign else None) or ""
        if not reply_prompt.strip():
            raise ValueError("configure the default reply prompt first")
        effective_prompt = (
            reply_prompt.strip()
            + "\n\n"
            + self.personas.prompt_fragment(account_key)
        )
        reply = await self.responder.create_reply(
            effective_prompt,
            message,
            history=self.requests.get_conversation_context(account_key, limit=12),
        )
        reply = self.personas.apply_reply_habits(
            account_key,
            reply,
            seed=f"preview:{account_key}:{message}",
        )
        return {
            "account_key": account_key,
            "reply_prompt": reply_prompt.strip(),
            "incoming_text": message,
            "reply_text": reply,
        }
