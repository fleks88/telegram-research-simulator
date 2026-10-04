from __future__ import annotations

import random
import re
from typing import Any, Dict


WORD_PATTERN = re.compile(r"[A-Za-zА-Яа-яЁё]{4,}")
PUNCTUATION_PATTERN = re.compile(r"[,;:!?\.]")


class PersonaService:
    DEFAULT_PROFILE = {
        "identity_prompt": "",
        "word_accuracy_percent": 100,
        "punctuation_accuracy_percent": 100,
        "literacy_level": 5,
        "aggression_level": 1,
        "friendliness_level": 3,
        "verbosity_level": 3,
        "humor_level": 2,
        "emoji_level": 1,
        "initiative_level": 3,
    }

    def __init__(self, requests: Any) -> None:
        self.requests = requests

    def get(self, account_key: str) -> Dict[str, Any]:
        saved = self.requests.get_sender_persona(account_key) or {}
        return {**self.DEFAULT_PROFILE, **saved}

    def save(self, account_key: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        merged = {**self.DEFAULT_PROFILE, **profile}
        normalized = {
            "identity_prompt": str(merged["identity_prompt"]).strip(),
            "word_accuracy_percent": int(merged["word_accuracy_percent"]),
            "punctuation_accuracy_percent": int(merged["punctuation_accuracy_percent"]),
            **{
                key: int(merged[key])
                for key in (
                    "literacy_level",
                    "aggression_level",
                    "friendliness_level",
                    "verbosity_level",
                    "humor_level",
                    "emoji_level",
                    "initiative_level",
                )
            },
        }
        for key in ("word_accuracy_percent", "punctuation_accuracy_percent"):
            if not 0 <= normalized[key] <= 100:
                raise ValueError(f"{key} must be between 0 and 100")
        if len(normalized["identity_prompt"]) > 2000:
            raise ValueError("identity_prompt must be 2000 characters or fewer")
        for key in (
            "literacy_level",
            "aggression_level",
            "friendliness_level",
            "verbosity_level",
            "humor_level",
            "emoji_level",
            "initiative_level",
        ):
            if not 1 <= normalized[key] <= 5:
                raise ValueError(f"{key} must be between 1 and 5")
        if not self.requests.save_sender_persona(account_key, normalized):
            raise KeyError("sender account not found")
        return normalized

    def prompt_fragment(self, account_key: str) -> str:
        profile = self.get(account_key)
        labels = {
            "literacy_level": "Грамотность",
            "aggression_level": "Резкость и напористость",
            "friendliness_level": "Дружелюбность",
            "verbosity_level": "Разговорчивость",
            "humor_level": "Юмор",
            "emoji_level": "Использование эмодзи",
            "initiative_level": "Инициативность",
        }
        lines = ["Профиль текущего отправителя:"]
        lines.extend(f"- {label}: {profile[key]} из 5" for key, label in labels.items())
        if profile["identity_prompt"]:
            lines.append("- Дополнительное описание: " + profile["identity_prompt"])
        lines.append("Резкость не разрешает угрозы, травлю или оскорбления.")
        return "\n".join(lines)

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
