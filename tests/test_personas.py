from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from research_sim.database import Database, DatabaseRequests
from research_sim.services.persona_research import PersonaResearchService
from research_sim.services.personas import PersonaService


class FakeDialogueResponder:
    def __init__(self) -> None:
        self.received_persona: Dict[str, Any] | None = None

    async def propose_dialogue(
        self,
        *,
        task_prompt: str,
        persona: Dict[str, Any],
        target_replies: List[str],
    ) -> List[Dict[str, str]]:
        self.received_persona = persona
        return [
            {"sender": f"proposal {index}", "target": reply}
            for index, reply in enumerate(target_replies, start=1)
        ]


class PersonaResearchTest(unittest.IsolatedAsyncioTestCase):
    async def test_persona_and_five_turn_preview_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "research.sqlite3")
            database.initialize()
            requests = DatabaseRequests(database)
            requests.add_sender_account("research_a", "Research A")
            personas = PersonaService(requests)
            profile = personas.save(
                "research_a",
                {
                    "identity_prompt": "Short, curious style",
                    "word_accuracy_percent": 96,
                    "punctuation_accuracy_percent": 82,
                },
            )
            responder = FakeDialogueResponder()
            service = PersonaResearchService(requests, personas, responder)
            targets = [f"Static test reply {index}" for index in range(1, 6)]

            proposal = await service.propose_dialogue(
                account_key="research_a",
                task_prompt="A short lab scenario",
                target_replies=targets,
            )

            self.assertEqual(responder.received_persona, profile)
            self.assertEqual(len(proposal["dialogue"]), 5)
            self.assertEqual(
                [turn["target"] for turn in proposal["dialogue"]],
                targets,
            )
            history = service.list_proposals("research_a")
            self.assertEqual(history[0]["id"], proposal["proposal_id"])
            self.assertEqual(len(history[0]["dialogue"]), 5)

    def test_text_accuracy_transform_is_seeded_and_hundred_percent_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "persona.sqlite3")
            database.initialize()
            requests = DatabaseRequests(database)
            requests.add_sender_account("plain", "Plain")
            personas = PersonaService(requests)

            source = "Testing the dialogue, carefully."
            self.assertEqual(
                personas.stylize_scheduled_text("plain", source, seed="fixed"),
                source,
            )
            personas.save(
                "plain",
                {
                    "identity_prompt": "",
                    "word_accuracy_percent": 0,
                    "punctuation_accuracy_percent": 0,
                },
            )
            first = personas.stylize_scheduled_text("plain", source, seed="fixed")
            second = personas.stylize_scheduled_text("plain", source, seed="fixed")
            self.assertEqual(first, second)
            self.assertNotEqual(first, source)


if __name__ == "__main__":
    unittest.main()