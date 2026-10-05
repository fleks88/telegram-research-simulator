from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


WORD_PATTERN = re.compile(r"[A-Za-zА-Яа-яЁё]+")
TY_PATTERN = re.compile(r"\b(?:ты|тебя|тебе|тобой|твой|твоя|твои|твоё)\b", re.I)
VY_PATTERN = re.compile(r"\b(?:вы|вас|вам|вами|ваш|ваша|ваши|ваше)\b", re.I)
EMOJI_PATTERN = re.compile("[\U0001F300-\U0001FAFF]")


@dataclass(frozen=True)
class CorpusStyle:
    message_count: int = 0
    median_characters: int = 15
    median_words: int = 3
    terminal_period_percent: int = 1
    emoji_percent: int = 2
    lowercase_start_percent: int = 5
    informal_address_percent: int = 72


def _plain_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in value
            if isinstance(item, (str, dict))
        )
    return ""


def _messages(document: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(document, dict):
        return ()
    chats = document.get("chats", {})
    if isinstance(chats, dict):
        chats = chats.get("list", [])
    if isinstance(chats, list):
        return (
            message
            for chat in chats
            if isinstance(chat, dict)
            for message in chat.get("messages", [])
            if isinstance(message, dict)
        )
    messages = document.get("messages", [])
    return (message for message in messages if isinstance(message, dict))


def analyze_telegram_export(path: Path, *, limit: int = 10_000) -> CorpusStyle:
    """Read only aggregate style statistics; message texts are never retained."""
    if not path.is_file():
        return CorpusStyle()
    with path.open("r", encoding="utf-8") as source:
        document = json.load(source)

    lengths: list[int] = []
    word_counts: list[int] = []
    periods = emojis = lowercase = ty_hits = vy_hits = 0
    for message in _messages(document):
        if message.get("type", "message") != "message":
            continue
        text = _plain_text(message.get("text", "")).strip()
        if not text:
            continue
        words = WORD_PATTERN.findall(text)
        lengths.append(len(text))
        word_counts.append(len(words))
        periods += int(text.endswith(".") and not text.endswith("..."))
        emojis += int(bool(EMOJI_PATTERN.search(text)))
        lowercase += int(bool(text[0].isalpha() and text[0].islower()))
        ty_hits += len(TY_PATTERN.findall(text))
        vy_hits += len(VY_PATTERN.findall(text))
        if len(lengths) >= limit:
            break

    count = len(lengths)
    if not count:
        return CorpusStyle()
    addressed = ty_hits + vy_hits
    return CorpusStyle(
        message_count=count,
        median_characters=round(statistics.median(lengths)),
        median_words=round(statistics.median(word_counts)),
        terminal_period_percent=round(periods * 100 / count),
        emoji_percent=round(emojis * 100 / count),
        lowercase_start_percent=round(lowercase * 100 / count),
        informal_address_percent=(
            round(ty_hits * 100 / addressed) if addressed else 72
        ),
    )
