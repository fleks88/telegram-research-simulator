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