from __future__ import annotations

import random
import re
from typing import Any, Dict


WORD_PATTERN = re.compile(r"[A-Za-zА-Яа-яЁё]{4,}")
PUNCTUATION_PATTERN = re.compile(r"[,;:!?\.]")


class PersonaService:
    def __init__(self, requests: Any) -> None:
        self.requests = requests

    def get(self, account_key: str) -> Dict[str, Any]:
        return self.requests.get_sender_persona(account_key) or {
            "identity_prompt": "",
            "word_accuracy_percent": 100,
            "punctuation_accuracy_percent": 100,
        }

    def save(self, account_key: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        normalized = {
            "identity_prompt": profile["identity_prompt"].strip(),
            "word_accuracy_percent": int(profile["word_accuracy_percent"]),
            "punctuation_accuracy_percent": int(profile["punctuation_accuracy_percent"]),
        }
        for key in ("word_accuracy_percent", "punctuation_accuracy_percent"):
            if not 0 <= normalized[key] <= 100:
                raise ValueError(f"{key} must be between 0 and 100")
        if len(normalized["identity_prompt"]) > 2000:
            raise ValueError("identity_prompt must be 2000 characters or fewer")
        if not self.requests.save_sender_persona(account_key, normalized):
            raise KeyError("sender account not found")
        return normalized

    def stylize_scheduled_text(
        self,
        account_key: str,
        text: str,
        *,
        seed: str,
        profile: Dict[str, Any] | None = None,
    ) -> str:
        profile = profile or self.get(account_key)
        rng = random.Random(seed)
        accuracy = profile["word_accuracy_percent"]
        punctuation_accuracy = profile["punctuation_accuracy_percent"]

        def vary_word(match: re.Match[str]) -> str:
            word = match.group(0)
            if rng.randrange(100) < accuracy:
                return word
            index = rng.randrange(1, len(word) - 1)
            letters = list(word)
            letters[index], letters[index + 1] = letters[index + 1], letters[index]
            return "".join(letters)

        varied = WORD_PATTERN.sub(vary_word, text)

        def vary_punctuation(match: re.Match[str]) -> str:
            mark = match.group(0)
            if rng.randrange(100) < punctuation_accuracy:
                return mark
            if mark in ".!?":
                return rng.choice([".", "!", "?", ""])
            return rng.choice([",", ";", "", "."])

        return PUNCTUATION_PATTERN.sub(vary_punctuation, varied)