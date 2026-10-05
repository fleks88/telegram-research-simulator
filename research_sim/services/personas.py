from __future__ import annotations

import random
import re
import secrets
from pathlib import Path
from typing import Any, Dict

from .persona_corpus import CorpusStyle, analyze_telegram_export


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
        "address_style": "ты",
        "terminal_period_percent": 1,
        "lowercase_start_percent": 5,
    }

    def __init__(self, requests: Any, corpus_path: Path | None = None) -> None:
        self.requests = requests
        self.corpus_path = corpus_path
        self._corpus_style: CorpusStyle | None = None

    def get(self, account_key: str) -> Dict[str, Any]:
        saved = self.requests.get_sender_persona(account_key) or {}
        return {**self.DEFAULT_PROFILE, **saved}

    def save(self, account_key: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        merged = {**self.DEFAULT_PROFILE, **profile}
        normalized = {
            "identity_prompt": str(merged["identity_prompt"]).strip(),
            "word_accuracy_percent": int(merged["word_accuracy_percent"]),
            "punctuation_accuracy_percent": int(merged["punctuation_accuracy_percent"]),
            "address_style": str(merged["address_style"]).strip().casefold(),
            "terminal_period_percent": int(merged["terminal_period_percent"]),
            "lowercase_start_percent": int(merged["lowercase_start_percent"]),
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
        for key in (
            "word_accuracy_percent",
            "punctuation_accuracy_percent",
            "terminal_period_percent",
            "lowercase_start_percent",
        ):
            if not 0 <= normalized[key] <= 100:
                raise ValueError(f"{key} must be between 0 and 100")
        if normalized["address_style"] not in {"ты", "вы"}:
            raise ValueError("address_style must be ты or вы")
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

    def randomize(self, account_key: str) -> Dict[str, Any]:
        style = self._style()
        rng = secrets.SystemRandom()
        address_style = (
            "ты" if rng.randrange(100) < style.informal_address_percent else "вы"
        )
        literacy = rng.choices([2, 3, 4, 5], weights=[10, 35, 40, 15])[0]
        verbosity = rng.choices([1, 2, 3], weights=[45, 45, 10])[0]
        emoji_level = rng.choices([1, 2, 3], weights=[72, 24, 4])[0]
        terminal_period = max(
            0,
            min(6, style.terminal_period_percent + rng.randint(-1, 3)),
        )
        lowercase_start = max(
            0,
            min(25, style.lowercase_start_percent + rng.randint(-3, 8)),
        )
        length_hint = max(2, min(8, style.median_words + rng.randint(-1, 3)))
        temperament = rng.choice(
            [
                "спокойно и по делу",
                "мягко и доброжелательно",
                "прямо, иногда немного сухо",
                "живым разговорным языком",
                "сдержанно, но вовлечённо",
            ]
        )
        emoji_hint = (
            "почти не используй эмодзи"
            if emoji_level == 1
            else "используй эмодзи редко"
        )
        identity = (
            f"Пиши {temperament}. Обычно отвечай коротко, примерно 1–{length_hint} слов. "
            f"Обращайся к собеседнику только на «{address_style}». "
            "В конце коротких реплик обычно не ставь точку. "
            f"{emoji_hint.capitalize()}. Сохраняй этот стиль во всём диалоге."
        )
        accuracy = {2: 80, 3: 89, 4: 96, 5: 100}[literacy]
        return self.save(
            account_key,
            {
                "identity_prompt": identity,
                "word_accuracy_percent": accuracy,
                "punctuation_accuracy_percent": accuracy,
                "literacy_level": literacy,
                "aggression_level": rng.choices([1, 2, 3, 4], [25, 40, 28, 7])[0],
                "friendliness_level": rng.choices([2, 3, 4, 5], [12, 40, 38, 10])[0],
                "verbosity_level": verbosity,
                "humor_level": rng.choices([1, 2, 3, 4], [28, 40, 27, 5])[0],
                "emoji_level": emoji_level,
                "initiative_level": rng.choices([1, 2, 3, 4], [15, 35, 38, 12])[0],
                "address_style": address_style,
                "terminal_period_percent": terminal_period,
                "lowercase_start_percent": lowercase_start,
            },
        )

    def _style(self) -> CorpusStyle:
        if self._corpus_style is None:
            try:
                self._corpus_style = (
                    analyze_telegram_export(self.corpus_path)
                    if self.corpus_path
                    else CorpusStyle()
                )
            except (OSError, ValueError):
                self._corpus_style = CorpusStyle()
        return self._corpus_style

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
        lines.append(f"- Обращение к собеседнику: только на «{profile['address_style']}»")
        lines.append(
            "- Точка в конце сообщения: примерно "
            f"{profile['terminal_period_percent']}% коротких реплик"
        )
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

        varied = PUNCTUATION_PATTERN.sub(vary_punctuation, varied)
        return self.apply_reply_habits(account_key, varied, seed=seed, profile=profile)

    def apply_reply_habits(
        self,
        account_key: str,
        text: str,
        *,
        seed: str,
        profile: Dict[str, Any] | None = None,
    ) -> str:
        profile = profile or self.get(account_key)
        result = text.strip()
        if not result:
            return result
        rng = random.Random(seed + ":habits")
        period_percent = int(profile["terminal_period_percent"])
        if result.endswith(".") and not result.endswith("..."):
            if rng.randrange(100) >= period_percent:
                result = result[:-1].rstrip()
        elif result[-1] not in "!?…" and rng.randrange(100) < period_percent:
            result += "."
        if result[0].isalpha() and rng.randrange(100) < int(
            profile["lowercase_start_percent"]
        ):
            result = result[0].lower() + result[1:]
        return result
