from __future__ import annotations

import random
import re
import secrets
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict

from .persona_corpus import CorpusStyle, analyze_telegram_export
from .term_knowledge import TermKnowledgeService


WORD_PATTERN = re.compile(r"[A-Za-zА-Яа-яЁё]{4,}")
PUNCTUATION_PATTERN = re.compile(r"[,;:!?\.]")
CYRILLIC_LETTERS = "абвгдежзийклмнопрстуфхцчшщыэюя"
PAYMENT_NOTICE_MARKERS = ("заявк", "сумм")
PUBG_DOMAIN_CONTEXT = """Предметный контекст разговора:
- UC («юц») — внутриигровая валюта PUBG.
- «Вход», «по входу», «UC по/через входу» — продавец входит в PUBG-аккаунт покупателя и пополняет UC через игровой клиент. Вход бывает по QR-коду либо по почте и коду.
- «По ID», «UC по ID» — пополнение по PUBG ID клиента без входа в аккаунт.
- PUBG ID — внутриигровой UID аккаунта.
- «Ссылка на оплату» — созданная продавцом ссылка для оплаты через СБП или российской картой.
- «UC по QR-коду без входа» — покупатель сканирует QR-код через PUBG, продавец не получает доступ к аккаунту и пополняет через Midasbuy.
- Число рядом с UC или способом — количество пополняемых UC: например, «8100 по входу» или «1800 по ID».
- «Пак» — пакет, то есть количество UC.
- Риск может обсуждаться при пополнении по входу или через дешёвое пополнение по ID («веб»).

Используй эти значения как естественные фоновые знания. Не перечисляй определения и не демонстрируй знание внутреннего словаря без необходимости. Если продавец употребил непонятный термин, коротко переспроси, что он означает.

Если во входящем сообщении есть платёжная ссылка или уведомление от @paygamesorg_bot с данными заявки PayGames, отвечай как покупатель, который пытается оплатить. Если платёж не проходит, сообщи об этом коротко и естественно в стиле текущей личности либо попроси другой способ. Не добавляй шаблонные вопросы «это нормально?» или «так и должно быть?». Не утверждай, что банк назвал получателя мошенниками, если такого предупреждения нет во входящем сообщении или истории. Если предупреждение о мошенничестве действительно показано, можно кратко пересказать его своими словами."""


class PersonaService:
    PROFILE_VERSION = 4
    ACCURACY_BY_LITERACY = {1: 60, 2: 68, 3: 93, 4: 95, 5: 98}
    LITERACY_WEIGHTS = [5, 5, 38.5, 41.5, 10]
    DEFAULT_PROFILE = {
        "profile_version": PROFILE_VERSION,
        "identity_prompt": "",
        "word_accuracy_percent": 98,
        "punctuation_accuracy_percent": 98,
        "literacy_level": 5,
        "aggression_level": 1,
        "friendliness_level": 3,
        "verbosity_level": 3,
        "humor_level": 2,
        "emoji_level": 1,
        "initiative_level": 3,
        "address_style": "ты",
        "terminal_period_percent": 1,
        "lowercase_start_percent": 10,
    }

    def __init__(self, requests: Any, corpus_path: Path | None = None) -> None:
        self.requests = requests
        self.corpus_path = corpus_path
        self._corpus_style: CorpusStyle | None = None
        self.knowledge = TermKnowledgeService(requests)

    def get(self, account_key: str) -> Dict[str, Any]:
        saved = self.requests.get_sender_persona(account_key) or {}
        if saved and int(saved.get("profile_version", 1)) < self.PROFILE_VERSION:
            literacy = int(saved.get("literacy_level", 5))
            saved = self.save(
                account_key,
                {
                    **saved,
                    "profile_version": self.PROFILE_VERSION,
                    "word_accuracy_percent": self.ACCURACY_BY_LITERACY[literacy],
                    "punctuation_accuracy_percent": self.ACCURACY_BY_LITERACY[
                        literacy
                    ],
                    "lowercase_start_percent": 10,
                },
            )
        return {**self.DEFAULT_PROFILE, **saved}

    def save(self, account_key: str, profile: Dict[str, Any]) -> Dict[str, Any]:
        merged = {**self.DEFAULT_PROFILE, **profile}
        if "literacy_level" in profile:
            literacy_accuracy = self.ACCURACY_BY_LITERACY[
                int(profile["literacy_level"])
            ]
            if "word_accuracy_percent" not in profile:
                merged["word_accuracy_percent"] = literacy_accuracy
            if "punctuation_accuracy_percent" not in profile:
                merged["punctuation_accuracy_percent"] = literacy_accuracy
        normalized = {
            "profile_version": int(merged["profile_version"]),
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
        if normalized["profile_version"] < 1:
            raise ValueError("profile_version must be positive")
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
        literacy = rng.choices(
            [1, 2, 3, 4, 5],
            weights=self.LITERACY_WEIGHTS,
        )[0]
        verbosity = rng.choices([1, 2, 3], weights=[45, 45, 10])[0]
        emoji_level = rng.choices([1, 2, 3], weights=[72, 24, 4])[0]
        terminal_period = max(
            0,
            min(6, style.terminal_period_percent + rng.randint(-1, 3)),
        )
        lowercase_start = 10
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
        accuracy = self.ACCURACY_BY_LITERACY[literacy]
        return self.save(
            account_key,
            {
                "identity_prompt": identity,
                "profile_version": self.PROFILE_VERSION,
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
            "aggression_level": "Резкость и напористость",
            "friendliness_level": "Дружелюбность",
            "verbosity_level": "Разговорчивость",
            "humor_level": "Юмор",
            "emoji_level": "Использование эмодзи",
            "initiative_level": "Инициативность",
        }
        lines = [PUBG_DOMAIN_CONTEXT]
        learned = self.knowledge.learned_prompt()
        if learned:
            lines.extend(["", learned])
        lines.extend(["", "Профиль текущего отправителя:"])
        effective_literacy = max(1, int(profile["literacy_level"]) - 1)
        lines.append(f"- Грамотность: {effective_literacy} из 5")
        lines.extend(f"- {label}: {profile[key]} из 5" for key, label in labels.items())
        lines.append(f"- Обращение к собеседнику: только на «{profile['address_style']}»")
        lines.append(
            "- Точка в конце сообщения: примерно "
            f"{profile['terminal_period_percent']}% коротких реплик"
        )
        if effective_literacy <= 2:
            lines.append(
                "- Иногда допускай естественные опечатки или простые ошибки, "
                "но не в каждом слове и без потери смысла"
            )
        elif effective_literacy <= 3:
            lines.append("- Допускай редкие разговорные ошибки и опечатки")
        if profile["identity_prompt"]:
            lines.append("- Дополнительное описание: " + profile["identity_prompt"])
        lines.append("Резкость не разрешает угрозы, травлю или оскорбления.")
        lines.extend(
            [
                "",
                "Обязательный контракт стиля (имеет приоритет над стилем "
                "общего сценария и словаря):",
                "- Общий prompt, предметный контекст и словарь определяют смысл, "
                "но не манеру письма",
                "- Манеру, длину, грамотность, обращение и пунктуацию всегда бери "
                "из профиля текущего отправителя",
                "- Не исправляй речь до литературной и не повышай грамотность из-за "
                "деловой темы, цены или платёжного сообщения",
                "- Не добавляй пустые встречные вопросы вроде «это нормально?» или "
                "«так и должно быть?»",
                "- Ты покупатель: не инструктируй продавца фразами «попробуй другой "
                "способ», «оплати иначе» или похожими. Описывай только свою проблему "
                "либо проси продавца дать другую ссылку или способ",
                "- Верни одну короткую естественную реплику без пояснений",
            ]
        )
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
        text = self.enforce_buyer_voice(text, seed=seed)
        rng = random.Random(seed)
        accuracy = profile["word_accuracy_percent"]
        punctuation_accuracy = profile["punctuation_accuracy_percent"]
        effective_literacy = max(1, int(profile["literacy_level"]) - 1)
        changed_words = 0

        def make_typo(word: str) -> str:
            index = rng.randrange(1, len(word) - 1)
            letters = list(word)
            error_kind = rng.choice(
                ["transpose", "delete", "replace"]
                if effective_literacy <= 2
                else ["transpose", "transpose", "replace"]
            )
            if error_kind == "delete" and len(letters) > 4:
                del letters[index]
            elif error_kind == "replace" and letters[index].casefold() in CYRILLIC_LETTERS:
                choices = CYRILLIC_LETTERS.replace(letters[index].casefold(), "")
                replacement = rng.choice(choices)
                letters[index] = (
                    replacement.upper() if letters[index].isupper() else replacement
                )
            else:
                letters[index], letters[index + 1] = letters[index + 1], letters[index]
            return "".join(letters)

        def vary_word(match: re.Match[str]) -> str:
            nonlocal changed_words
            word = match.group(0)
            if rng.randrange(100) < accuracy:
                return word
            changed = make_typo(word)
            changed_words += int(changed != word)
            return changed

        varied = WORD_PATTERN.sub(vary_word, text)
        ensure_error_percent = {
            1: 31,
            2: 31,
            3: 17,
            4: 7,
            5: 0,
        }[int(profile["literacy_level"])]
        if changed_words == 0 and rng.randrange(100) < ensure_error_percent:
            candidates = list(WORD_PATTERN.finditer(varied))
            if candidates:
                chosen = rng.choice(candidates)
                varied = (
                    varied[: chosen.start()]
                    + make_typo(chosen.group(0))
                    + varied[chosen.end() :]
                )

        def vary_punctuation(match: re.Match[str]) -> str:
            mark = match.group(0)
            if rng.randrange(100) < punctuation_accuracy:
                return mark
            if mark in ".!?":
                return rng.choice([".", "!", "?", ""])
            return rng.choice([",", ";", "", "."])

        varied = PUNCTUATION_PATTERN.sub(vary_punctuation, varied)
        punctuation_error_percent = 100 - int(punctuation_accuracy)
        if effective_literacy <= 2 and rng.randrange(100) < punctuation_error_percent:
            varied = re.sub(r"[,;:]", "", varied)
            if rng.randrange(100) < 65:
                varied = varied.replace(". ", " ")
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
        result = self.enforce_buyer_voice(result, seed=seed)
        without_canned_question = re.sub(
            r"(?:[.!?]\s*)?(?:это\s+норм(?:ально)?|так\s+и\s+должно\s+быть)\??$",
            "",
            result,
            flags=re.I,
        ).rstrip()
        if without_canned_question:
            result = without_canned_question
        rng = random.Random(seed + ":habits")
        period_percent = int(profile["terminal_period_percent"])
        effective_literacy = max(1, int(profile["literacy_level"]) - 1)
        punctuation_error_percent = 100 - int(
            profile["punctuation_accuracy_percent"]
        )
        if result.endswith(".") and not result.endswith("..."):
            if rng.randrange(100) >= period_percent:
                result = result[:-1].rstrip()
        elif result[-1] not in "!?…" and rng.randrange(100) < period_percent:
            result += "."
        if (
            effective_literacy <= 2
            and result.endswith(("?", "!"))
            and rng.randrange(100) < punctuation_error_percent // 3
        ):
            result = result[:-1].rstrip()
        if result[0].isalpha() and rng.randrange(100) < int(
            profile["lowercase_start_percent"]
        ):
            result = result[0].lower() + result[1:]
        return result

    def enforce_buyer_voice(
        self,
        text: str,
        *,
        seed: str,
        history: list[Dict[str, Any]] | None = None,
    ) -> str:
        needs_correction = re.search(
            r"\b(?:попробуй(?:те)?|оплати(?:те)?|проверь(?:те)?)\b",
            text,
            flags=re.I,
        ) and re.search(
            r"\b(?:плат[её]ж|оплат|способ)\w*\b",
            text,
            flags=re.I,
        )
        if not needs_correction:
            return text
        candidates = [
            "У меня платеж не проходит",
            "Банк у меня оплату не пропускает",
            "У меня банк платеж отклоняет",
            "Не могу оплатить",
            "СБП у меня не проходит",
            "Оплата у меня блокируется",
            "У меня с оплатой не выходит",
            "Банк не дает мне оплатить",
        ]

        def normalize(value: str) -> str:
            return re.sub(r"[^a-zа-яё0-9]+", " ", value.casefold()).strip()

        recent = [
            normalize(row["message_text"])
            for row in history or []
            if row.get("direction") == "outgoing" and row.get("message_text")
        ][-8:]
        fresh = [
            candidate
            for candidate in candidates
            if all(
                SequenceMatcher(None, normalize(candidate), previous).ratio() < 0.68
                for previous in recent
            )
        ]
        return random.Random(seed + ":buyer-voice").choice(fresh or candidates)

    @staticmethod
    def reply_mode_instruction(*, seed: str) -> str:
        direct_only = random.Random(seed + ":direct-mode").randrange(100) < 70
        if direct_only:
            return (
                "Режим именно этого ответа: напиши только по делу и не задавай "
                "встречных вопросов. Не добавляй просьбу подтвердить, что всё нормально."
            )
        return (
            "Режим именно этого ответа: один короткий вопрос допустим только если он "
            "реально нужен для продолжения сделки. Не используй «это нормально?» или "
            "«так и должно быть?»."
        )

    def clarification_question(
        self,
        account_key: str,
        term: str,
        *,
        seed: str,
    ) -> str:
        profile = self.get(account_key)
        if profile["address_style"] == "вы":
            variants = [
                f"а что значит «{term}»?",
                f"подскажите, что такое «{term}»?",
                f"не понял, что вы имеете в виду под «{term}»?",
            ]
        else:
            variants = [
                f"а что значит «{term}»?",
                f"подскажи, что такое «{term}»?",
                f"не понял, что ты имеешь в виду под «{term}»?",
            ]
        return random.Random(seed + ":term-question").choice(variants)

    def payment_notice_reply(
        self,
        account_key: str,
        incoming_text: str,
        *,
        history: list[Dict[str, Any]],
        seed: str,
    ) -> str | None:
        lowered = incoming_text.casefold()
        is_notice = (
            all(marker in lowered for marker in PAYMENT_NOTICE_MARKERS)
            and ("платеж" in lowered or "оплат" in lowered)
        ) or "paygamesorg_bot" in lowered or "paygames" in lowered or (
            "🧾" in incoming_text
            and ("сбп" in lowered or "оплат" in lowered or "платеж" in lowered)
        )
        if not is_notice:
            return None

        profile = self.get(account_key)
        polite = profile["address_style"] == "вы"
        direct_candidates = [
            "Банк оплату отклоняет",
            "У меня оплата не проходит",
            "Что-то банк не дает оплатить",
            "Почему-то платеж блокируется",
            "Не могу оплатить банк отклоняет",
            "Банк ругается на платеж",
            "СБП не проходит почему-то",
            "Мне банк не дает это оплатить",
            "Чет с оплатой не выходит банк блокает",
        ]
        question_candidates = [
            "У меня оплата не проходит, можно как-то иначе?",
            "Что-то банк не дает оплатить, что делать?",
            "Оплата не проходит, другую ссылку можно?",
            "Платеж отклоняется, можно другим способом?",
            "Не дает оплатить, ссылка точно рабочая?",
        ]
        if polite:
            question_candidates.extend(
                [
                    "Подскажите, почему банк отклоняет оплату?",
                    "Можете другую ссылку дать? Эта не проходит",
                ]
            )
        else:
            question_candidates.extend(
                [
                    "Подскажи че банк оплату не пропускает?",
                    "Можешь другую ссылку дать? Эта не проходит",
                ]
            )
        if profile["aggression_level"] >= 4:
            direct_candidates.append("Банк опять блокает оплату")
            question_candidates.append("Другой способ оплаты есть?")
        if any(marker in lowered for marker in ("мошенн", "подозр", "fraud")):
            direct_candidates.extend(
                ["Банк пишет что платеж подозрительный", "Банк предупреждает про мошенничество"]
            )
            question_candidates.append("Тут предупреждение про мошенников, что делать?")

        def normalize(value: str) -> str:
            return re.sub(r"[^a-zа-яё0-9]+", " ", value.casefold()).strip()

        recent = [
            normalize(row["message_text"])
            for row in history
            if row.get("direction") == "outgoing" and row.get("message_text")
        ][-8:]
        rng = random.Random(seed + ":payment-notice")
        candidates = (
            direct_candidates if rng.randrange(100) < 70 else question_candidates
        )
        fresh = [
            candidate
            for candidate in candidates
            if all(
                SequenceMatcher(None, normalize(candidate), previous).ratio() < 0.68
                for previous in recent
            )
        ]
        pool = fresh or candidates
        return rng.choice(pool)
