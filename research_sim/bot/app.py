from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .client import ApiClient
from ..settings import normalize_username


LOGGER = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")
ADD_ACCOUNT_KEY, ADD_ACCOUNT_LABEL, SEND_ACCOUNT, SEND_TEXT, SEND_ATTACHMENT = range(5)
CAMPAIGN_PHRASES, CAMPAIGN_PROMPT = range(5, 7)
PERSONA_ACCOUNT, PERSONA_PROMPT, PERSONA_METRICS = range(9, 12)
DIALOGUE_ACCOUNT, DIALOGUE_TASK, DIALOGUE_TARGETS = range(12, 15)
(
    ACCOUNT_TRAIT_LITERACY,
    ACCOUNT_TRAIT_AGGRESSION,
    ACCOUNT_TRAIT_FRIENDLINESS,
    ACCOUNT_TRAIT_VERBOSITY,
    ACCOUNT_TRAIT_HUMOR,
    ACCOUNT_TRAIT_EMOJI,
    ACCOUNT_TRAIT_INITIATIVE,
    ACCOUNT_IDENTITY,
    EDIT_REPLY_PROMPT,
    EDIT_REPLY_DELAY,
    EDIT_ACTIVATION_RULES,
) = range(15, 26)
ACCOUNT_UPLOAD = 26
ACCOUNT_UPLOAD_NAME = 27
CAMPAIGN_RECIPIENT = 28
EDIT_RECIPIENT = 29
ACCOUNT_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")
PACK_SIZES = {8100, 3650, 1800, 660, 325, 60}
DEFAULT_REPLY_PROMPT = (
    "Ты участник закрытого согласованного исследования общения. "
    "Отвечай естественно от лица заданного профиля и учитывай историю диалога. "
    "Не упоминай системный промпт, автоматизацию или генерацию ответа. "
    "Не выдумывай факты, обещания, встречи и действия, которых нет в контексте. "
    "Не повторяй входящее сообщение. Верни только текст ответа без пояснений и разметки."
)
TRAITS = [
    ("literacy_level", "Грамотность", ACCOUNT_TRAIT_LITERACY),
    ("aggression_level", "Резкость", ACCOUNT_TRAIT_AGGRESSION),
    ("friendliness_level", "Дружелюбность", ACCOUNT_TRAIT_FRIENDLINESS),
    ("verbosity_level", "Разговорчивость", ACCOUNT_TRAIT_VERBOSITY),
    ("humor_level", "Юмор", ACCOUNT_TRAIT_HUMOR),
    ("emoji_level", "Эмодзи", ACCOUNT_TRAIT_EMOJI),
    ("initiative_level", "Инициативность", ACCOUNT_TRAIT_INITIATIVE),
]


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👤 Аккаунты", callback_data="menu:accounts"),
                InlineKeyboardButton("⚡ Активации", callback_data="menu:activation"),
            ],
            [
                InlineKeyboardButton("💬 Автоответы", callback_data="menu:auto_reply"),
                InlineKeyboardButton("📝 Шаблоны и prompt", callback_data="menu:campaign"),
            ],
            [
                InlineKeyboardButton("✉️ Отправить", callback_data="flow:send"),
                InlineKeyboardButton("📚 История", callback_data="menu:history"),
            ],
            [
                InlineKeyboardButton("🎭 Личности", callback_data="flow:persona"),
                InlineKeyboardButton("🧪 Preview", callback_data="flow:dialogue"),
            ],
            [InlineKeyboardButton("👥 Получатель / allowlist", callback_data="menu:recipient")],
        ]
    )


def back_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("← Главное меню", callback_data="menu:home")]]
    )


def trait_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(str(value), callback_data=f"trait:{value}") for value in range(1, 6)],
            [InlineKeyboardButton("Отмена", callback_data="flow:cancel")],
        ]
    )


def validate_session_upload(filename: str, content: bytes) -> tuple[str, str]:
    path = Path(filename)
    suffix = path.suffix.casefold()
    if suffix not in {".session", ".json"}:
        raise ValueError("нужен файл .session или .json")
    raw_stem = path.stem.strip().casefold()
    account_key = re.sub(r"[^a-z0-9_-]+", "_", raw_stem).strip("_-")
    if not account_key:
        digest = hashlib.sha256(raw_stem.encode("utf-8")).hexdigest()[:12]
        account_key = "account_" + digest
    elif len(account_key) > 48:
        digest = hashlib.sha256(raw_stem.encode("utf-8")).hexdigest()[:12]
        account_key = account_key[:35].rstrip("_-") + "_" + digest
    if suffix == ".session":
        if not content.startswith(b"SQLite format 3\x00"):
            raise ValueError(".session не похож на SQLite-сессию Telethon")
    else:
        try:
            metadata = json.loads(content.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(".json должен содержать корректный JSON") from exc
        if not isinstance(metadata, dict):
            raise ValueError("верхний уровень .json должен быть объектом")
    return account_key, suffix


def save_session_bundle(
    session_dir: Path,
    account_key: str,
    files: Dict[str, bytes],
) -> None:
    session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    targets = {
        suffix: session_dir / f"{account_key}{suffix}"
        for suffix in (".session", ".json")
    }
    existing = [path for path in targets.values() if path.exists()]
    if existing:
        raise FileExistsError(f"файл уже существует: {existing[0].name}")
    temporary: list[Path] = []
    try:
        for suffix, target in targets.items():
            temp_path = target.with_name(target.name + f".upload-{os.getpid()}")
            with temp_path.open("xb") as stream:
                stream.write(files[suffix])
            os.chmod(temp_path, 0o600)
            temporary.append(temp_path)
        for suffix, target in targets.items():
            os.replace(target.with_name(target.name + f".upload-{os.getpid()}"), target)
    finally:
        for path in temporary:
            if path.exists():
                path.unlink()


def suggest_next_account_key(existing_keys: Set[str]) -> str:
    number = 1
    while f"acc{number}" in existing_keys:
        number += 1
    return f"acc{number}"


def parse_admin_ids(value: str) -> Set[int]:
    try:
        admin_ids = {int(item.strip()) for item in value.split(",") if item.strip()}
    except ValueError as exc:
        raise ValueError("CONTROL_ADMIN_IDS must be comma-separated Telegram user IDs") from exc
    if not admin_ids or any(item <= 0 for item in admin_ids):
        raise ValueError("CONTROL_ADMIN_IDS must contain at least one positive user ID")
    return admin_ids


def format_moscow_time(timestamp: float) -> str:
    value = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(MOSCOW)
    return value.strftime("%d.%m.%Y %H:%M МСК")


def format_time_until(timestamp: float, *, now: Optional[float] = None) -> str:
    current = datetime.now(timezone.utc).timestamp() if now is None else now
    seconds = max(0, int(timestamp - current))
    minutes = (seconds + 59) // 60
    if minutes < 60:
        return f"через {minutes} мин"
    hours, remaining = divmod(minutes, 60)
    return f"через {hours} ч {remaining} мин"


def is_admin(user_id: Optional[int], admin_ids: Set[int]) -> bool:
    return user_id is not None and user_id in admin_ids


def parse_activation_rules(value: str) -> Dict[str, int]:
    rules: Dict[str, int] = {}
    seen: set[int] = set()
    for raw_item in value.split(","):
        item = raw_item.strip().lower().replace("х", "x")
        if not item:
            continue
        match = re.fullmatch(r"(\d+)x(\d+)", item)
        if match is None:
            raise ValueError("Формат правила: pack x multiplier, например 8100x10")
        pack_size, multiplier = map(int, match.groups())
        if pack_size not in PACK_SIZES:
            raise ValueError(f"Неизвестный пакет {pack_size}; доступны: 8100, 3650, 1800, 660, 325, 60")
        if pack_size in seen:
            raise ValueError(f"Пакет {pack_size} указан больше одного раза")
        seen.add(pack_size)
        if not 0 <= multiplier <= 1_000_000:
            raise ValueError("Множитель должен быть от 0 до 1000000")
        if multiplier > 0:
            rules[str(pack_size)] = multiplier
    if not rules:
        raise ValueError("Укажите хотя бы одно правило с множителем больше нуля")
    return rules


def _api(context: ContextTypes.DEFAULT_TYPE) -> ApiClient:
    return context.application.bot_data["api"]


def _is_authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    return is_admin(
        user.id if user else None,
        context.application.bot_data["admin_ids"],
    )


async def _deny(update: Update) -> None:
    if update.callback_query:
        await update.callback_query.answer("Доступ запрещён", show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text("Доступ запрещён.")


async def _reply(
    update: Update,
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(text, reply_markup=reply_markup)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=reply_markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    # /start is also the reliable escape hatch from any abandoned wizard.
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Панель управления исследовательским стендом. Время показывается по Москве.",
        reply_markup=home_keyboard(),
    )


async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    await _reply(update, "Главное меню:", reply_markup=home_keyboard())


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is not None and update.effective_message is not None:
        await update.effective_message.reply_text(
            f"Ваш Telegram user ID: {user.id}\n"
            "Добавьте это число в CONTROL_ADMIN_IDS на сервере бота."
        )


async def show_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    try:
        accounts = await _api(context).accounts()
    except Exception as exc:
        await _reply(update, f"Не удалось получить аккаунты: {exc}")
        return
    if not accounts:
        await _reply(
            update,
            "Аккаунтов пока нет. Сначала положите готовый Telethon .session в каталог сессий.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Добавить .session", callback_data="flow:add_account")],
                [InlineKeyboardButton("← Главное меню", callback_data="menu:home")],
            ]),
        )
        return
    lines = ["Аккаунты отправителя:"]
    buttons = []
    for account in accounts:
        state = "включён" if account["enabled"] else "выключен"
        last_used = (
            format_moscow_time(account["last_used_at"])
            if account.get("last_used_at")
            else "ещё не использовался"
        )
        lines.append(
            f"{account['account_index']}. {account['label']} ({account['account_key']}) · "
            f"{state} · отправок: {account['send_count']} · {last_used}"
        )
        action = "disable" if account["enabled"] else "enable"
        buttons.append(
            InlineKeyboardButton(
                f"{account['account_index']}: {state}",
                callback_data=f"account:{action}:{account['account_key']}",
            )
        )
    markup = InlineKeyboardMarkup(
        [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
        + [
            [InlineKeyboardButton("➕ Добавить .session", callback_data="flow:add_account")],
            [InlineKeyboardButton("← Главное меню", callback_data="menu:home")],
        ]
    )
    await _reply(update, "\n".join(lines), reply_markup=markup)


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    try:
        accounts = await _api(context).accounts()
    except Exception as exc:
        await _reply(update, f"Не удалось получить аккаунты: {exc}")
        return
    buttons = [
        InlineKeyboardButton(
            f"{account['account_index']}. {account['label']}",
            callback_data=f"history:{account['account_key']}",
        )
        for account in accounts
    ]
    if not buttons:
        await _reply(update, "Список аккаунтов пуст.")
        return
    await _reply(
        update,
        "Выберите аккаунт. История будет показана по московскому времени:",
        reply_markup=InlineKeyboardMarkup(
            [[button] for button in buttons]
            + [[InlineKeyboardButton("← Главное меню", callback_data="menu:home")]]
        ),
    )


async def show_account_history(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    account_key: str,
) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    try:
        entries = await _api(context).account_timeline(account_key, limit=30)
    except Exception as exc:
        await _reply(update, f"Не удалось получить историю: {exc}")
        return
    if not entries:
        await _reply(update, f"Для аккаунта {account_key} отправок пока нет.")
        return
    lines = [f"Диалог и очередь: {account_key} (МСК)"]
    for entry in entries:
        content = entry.get("message_text", "").replace("\n", " ")[:180]
        if entry["kind"] == "incoming":
            lines.append(
                f"👤 Человек · {format_moscow_time(entry['created_at'])}\n{content}"
            )
        elif entry["kind"] == "planned":
            due_at = float(entry["due_at"])
            lines.append(
                f"⏳ {account_key} · {format_time_until(due_at)} "
                f"({format_moscow_time(due_at)})\n{content or 'Ответ будет подготовлен'}"
            )
        else:
            status = "отправлено" if entry["status"] == "sent" else entry["status"]
            lines.append(
                f"✅ {account_key} · {format_moscow_time(entry['created_at'])} · {status}\n{content}"
            )
    await _reply(
        update,
        "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("← К истории", callback_data="menu:history")],
            [InlineKeyboardButton("← Главное меню", callback_data="menu:home")],
        ]),
    )


async def show_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    try:
        config = await _api(context).get_campaign()
    except Exception as exc:
        await _reply(update, f"Не удалось получить настройки: {exc}")
        return
    if not config:
        await _reply(
            update,
            "Шаблоны и общий prompt ещё не настроены.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Настроить", callback_data="flow:campaign")],
                [InlineKeyboardButton("← Главное меню", callback_data="menu:home")],
            ]),
        )
        return
    text = (
        f"Шаблоны сообщений по активациям: {len(config['phrases'])}\n"
        f"Получатель: @{config['recipient']}\n"
        f"Автоответы: {'включены' if config.get('auto_reply_enabled') else 'выключены'}\n"
        f"Триггер по активациям: {'включён' if config.get('activation_enabled') else 'выключен'}\n"
        f"Шаблоны:\n- " + "\n- ".join(config["phrases"][:10])
    )
    if len(config["phrases"]) > 10:
        text += f"\n… и ещё {len(config['phrases']) - 10}"
    prompt = config.get("reply_prompt")
    if prompt:
        text += "\n\nPrompt:\n" + (prompt[:2500] + ("…" if len(prompt) > 2500 else ""))
    await _reply(
        update,
        text[:3900],
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ Изменить шаблоны и prompt", callback_data="flow:campaign")],
            [InlineKeyboardButton("👥 Изменить получателя", callback_data="flow:recipient")],
            [InlineKeyboardButton("← Главное меню", callback_data="menu:home")],
        ]),
    )


async def show_recipient_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    try:
        campaign = await _api(context).get_campaign()
    except Exception as exc:
        await _reply(update, f"Не удалось загрузить allowlist: {exc}")
        return
    current = f"@{campaign['recipient']}" if campaign else "не настроен"
    action = (
        InlineKeyboardButton("✏️ Изменить username", callback_data="flow:recipient")
        if campaign
        else InlineKeyboardButton("⚙️ Настроить", callback_data="flow:campaign")
    )
    await _reply(
        update,
        "👥 Получатель / allowlist\n"
        f"Сейчас: {current}\n\n"
        "Система отправляет сообщения и отвечает только этому username.",
        reply_markup=InlineKeyboardMarkup([
            [action],
            [InlineKeyboardButton("← Главное меню", callback_data="menu:home")],
        ]),
    )


async def render_dialogue_proposals(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    account_key: str,
) -> None:
    try:
        proposals = await _api(context).account_dialogues(account_key, limit=5)
    except Exception as exc:
        await _reply(update, f"Не удалось получить preview: {exc}")
        return
    if not proposals:
        await _reply(update, f"Для {account_key} сохранённых preview пока нет.")
        return
    chunks: list[str] = []
    for proposal in proposals:
        lines = [f"Preview #{proposal['id']} для {account_key}:"]
        for index, turn in enumerate(proposal["dialogue"], start=1):
            lines.append(f"{index}. Личность: {turn['sender']}\n   Target: {turn['target']}")
        chunks.append("\n".join(lines))
    await _reply(update, "\n\n".join(chunks)[:3900])


async def show_dialogue_proposals(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    if context.args:
        await render_dialogue_proposals(update, context, context.args[0])
        return
    try:
        accounts = await _api(context).accounts()
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось загрузить аккаунты: {exc}")
        return
    buttons = [
        InlineKeyboardButton(
            f"{account['account_index']}. {account['label']}",
            callback_data=f"proposals:{account['account_key']}",
        )
        for account in accounts
    ]
    if not buttons:
        await update.effective_message.reply_text("Список аккаунтов пуст.")
        return
    await update.effective_message.reply_text(
        "Выберите sender-аккаунт для просмотра сохранённых preview:",
        reply_markup=InlineKeyboardMarkup([[button] for button in buttons]),
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    query = update.callback_query
    value = query.data or ""
    if value == "menu:home":
        await show_home(update, context)
    elif value == "menu:accounts":
        await show_accounts(update, context)
    elif value == "menu:history":
        await show_history(update, context)
    elif value == "menu:campaign":
        await show_campaign(update, context)
    elif value == "menu:activation":
        await show_activation_menu(update, context)
    elif value == "menu:auto_reply":
        await show_auto_reply_menu(update, context)
    elif value == "menu:recipient":
        await show_recipient_menu(update, context)
    elif value.startswith("proposals:"):
        await render_dialogue_proposals(update, context, value.split(":", 1)[1])
    elif value.startswith("history:"):
        await show_account_history(update, context, value.split(":", 1)[1])
    elif value.startswith("account:"):
        _, action, account_key = value.split(":", 2)
        try:
            account = await _api(context).set_account_enabled(
                account_key,
                enabled=action == "enable",
            )
            await _reply(
                update,
                f"{account['label']}: {'включён' if account['enabled'] else 'выключен'}.",
            )
        except Exception as exc:
            await _reply(update, f"Ошибка обновления аккаунта: {exc}")


async def show_auto_reply_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        campaign = await _api(context).get_campaign()
    except Exception as exc:
        await _reply(update, f"Не удалось загрузить настройки: {exc}")
        return
    if not campaign:
        await _reply(
            update,
            "Сначала создайте кампанию и общий пул шаблонов.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Настроить кампанию", callback_data="flow:campaign")],
                [InlineKeyboardButton("← Главное меню", callback_data="menu:home")],
            ]),
        )
        return
    pending = 0
    try:
        pending = (await _api(context).auto_reply_status()).get("pending", 0)
    except Exception:
        pass
    enabled = bool(campaign.get("auto_reply_enabled"))
    prompt = (campaign.get("reply_prompt") or "").strip()
    text = (
        f"💬 Автоответы: {'включены' if enabled else 'выключены'}\n"
        f"Задержка: {campaign.get('reply_delay_min_minutes', 2)}–"
        f"{campaign.get('reply_delay_max_minutes', 180)} минут\n"
        f"В очереди: {pending}\n\n"
        f"Полный системный промпт:\n{prompt[:2500] or 'не задан'}"
    )
    toggle = "auto:disable" if enabled else "auto:enable"
    toggle_label = "⏸ Выключить" if enabled else "▶️ Включить"
    await _reply(
        update,
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(toggle_label, callback_data=toggle)],
            [InlineKeyboardButton("✏️ Изменить полный промпт", callback_data="flow:reply_prompt")],
            [InlineKeyboardButton("⏱ Изменить задержку", callback_data="flow:reply_delay")],
            [InlineKeyboardButton("👥 Изменить получателя", callback_data="flow:recipient")],
            [InlineKeyboardButton("← Главное меню", callback_data="menu:home")],
        ]),
    )


async def show_activation_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = await _api(context).activation_status()
    except Exception as exc:
        await _reply(update, f"Не удалось получить статус: {exc}")
        return
    rules = data.get("rules") or {}
    rules_text = ", ".join(f"{pack}×{value}" for pack, value in rules.items()) or "не заданы"
    enabled = bool(data.get("enabled"))
    await _reply(
        update,
        "⚡ Отправка по активациям\n"
        f"Статус: {'включена' if enabled else 'выключена'}\n"
        f"Endpoint: {'настроен' if data.get('endpoint_configured') else 'не настроен'}\n"
        f"Правила: {rules_text}\n"
        f"Очередь: {data.get('pending', 0)}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏸ Выключить" if enabled else "▶️ Включить", callback_data="activation:disable" if enabled else "activation:enable")],
            [InlineKeyboardButton("🔢 Изменить пороги", callback_data="flow:activation_rules")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="menu:activation")],
            [InlineKeyboardButton("← Главное меню", callback_data="menu:home")],
        ]),
    )


async def add_account_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    session_dir = Path(context.application.bot_data["session_dir"])
    try:
        registered = {item["account_key"] for item in await _api(context).accounts()}
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось получить аккаунты: {exc}")
        return ConversationHandler.END
    available = [
        path.stem
        for path in sorted(session_dir.glob("*.session"))
        if ACCOUNT_KEY_PATTERN.fullmatch(path.stem) and path.stem not in registered
    ]
    if available:
        buttons = [
            [InlineKeyboardButton(f"📄 {key}.session", callback_data=f"session:{key}")]
            for key in available[:30]
        ]
        buttons.extend([
            [InlineKeyboardButton("⬆️ Загрузить .session + .json", callback_data="session:upload")],
            [InlineKeyboardButton("Ввести ключ вручную", callback_data="session:manual")],
            [InlineKeyboardButton("Отмена", callback_data="flow:cancel")],
        ])
        await update.effective_message.reply_text(
            "Выберите готовую авторизованную Telethon-сессию:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:
        await update.effective_message.reply_text(
            "Новых .session файлов не найдено. Положите готовый авторизованный "
            f"Telethon-файл в {session_dir} или введите его ключ вручную.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬆️ Загрузить .session + .json", callback_data="session:upload")],
                [InlineKeyboardButton("Ввести ключ вручную", callback_data="session:manual")],
                [InlineKeyboardButton("Отмена", callback_data="flow:cancel")],
            ]),
        )
    return ADD_ACCOUNT_KEY


async def add_account_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        selected = query.data.split(":", 1)[1]
        if selected == "upload":
            context.user_data["session_upload"] = {}
            context.user_data.pop("session_upload_key", None)
            await query.edit_message_text(
                "Отправьте два документа с одинаковым именем: account.session и "
                "account.json. Порядок не важен. Максимум: session 20 МБ, JSON 1 МБ.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Отмена", callback_data="flow:cancel")]
                ]),
            )
            return ACCOUNT_UPLOAD
        if selected == "manual":
            await query.edit_message_text(
                "Введите ключ готового файла без суффикса .session, например business:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Отмена", callback_data="flow:cancel")]
                ]),
            )
            return ADD_ACCOUNT_KEY
        account_key = selected
    else:
        account_key = update.effective_message.text.strip().lower()
    if not ACCOUNT_KEY_PATTERN.fullmatch(account_key):
        await update.effective_message.reply_text(
            "Допустимы латинские буквы, цифры, _ и -. Повторите ввод:"
        )
        return ADD_ACCOUNT_KEY
    session_path = Path(context.application.bot_data["session_dir"]) / (account_key + ".session")
    if not session_path.is_file():
        await update.effective_message.reply_text(
            f"Не найдена Telethon-сессия {session_path}. Скопируйте её на сервер и повторите."
        )
        return ADD_ACCOUNT_KEY
    context.user_data["new_account_key"] = account_key
    await update.effective_message.reply_text(
        "Введите подпись аккаунта для списка:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Отмена", callback_data="flow:cancel")]
        ]),
    )
    return ADD_ACCOUNT_LABEL


async def account_upload_received(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    document = update.effective_message.document
    filename = Path(document.file_name or "").name
    suffix = Path(filename).suffix.casefold()
    size_limit = 20 * 1024 * 1024 if suffix == ".session" else 1024 * 1024
    if document.file_size is not None and document.file_size > size_limit:
        await update.effective_message.reply_text(
            ".session должен быть не больше 20 МБ, .json — не больше 1 МБ."
        )
        return ACCOUNT_UPLOAD
    telegram_file = await document.get_file()
    content = bytes(await telegram_file.download_as_bytearray())
    if len(content) > size_limit:
        await update.effective_message.reply_text("Файл слишком большой.")
        return ACCOUNT_UPLOAD
    try:
        account_key, suffix = validate_session_upload(filename, content)
    except ValueError as exc:
        await update.effective_message.reply_text(f"Файл не принят: {exc}")
        return ACCOUNT_UPLOAD
    expected_key = context.user_data.get("session_upload_key")
    if expected_key is not None and account_key != expected_key:
        await update.effective_message.reply_text(
            f"Имена должны совпадать. Ожидаются файлы для {expected_key}."
        )
        return ACCOUNT_UPLOAD
    context.user_data["session_upload_key"] = account_key
    uploaded = context.user_data.setdefault("session_upload", {})
    uploaded[suffix] = content
    missing = [item for item in (".session", ".json") if item not in uploaded]
    if missing:
        await update.effective_message.reply_text(
            f"Принят {filename}. Теперь отправьте {account_key}{missing[0]}."
        )
        return ACCOUNT_UPLOAD
    try:
        accounts = await _api(context).accounts()
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось получить аккаунты: {exc}")
        return ConversationHandler.END
    session_dir = Path(context.application.bot_data["session_dir"])
    existing_keys = {item["account_key"] for item in accounts}
    existing_keys.update(path.stem for path in session_dir.glob("*.session"))
    suggested = suggest_next_account_key(existing_keys)
    context.user_data["suggested_account_key"] = suggested
    await update.effective_message.reply_text(
        f"Пара {account_key}.session + {account_key}.json принята. "
        f"Предлагаю имя {suggested}:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Использовать {suggested}", callback_data=f"uploadname:{suggested}")],
            [InlineKeyboardButton("Ввести вручную", callback_data="uploadname:manual")],
            [InlineKeyboardButton("Отмена", callback_data="flow:cancel")],
        ]),
    )
    return ACCOUNT_UPLOAD_NAME


async def account_upload_name_chosen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        selected = query.data.split(":", 1)[1]
        if selected == "manual":
            await query.edit_message_text(
                "Введите имя аккаунта: латинские буквы, цифры, _ или -.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Отмена", callback_data="flow:cancel")]
                ]),
            )
            return ACCOUNT_UPLOAD_NAME
        account_key = selected
    else:
        account_key = update.effective_message.text.strip().casefold()
    if not ACCOUNT_KEY_PATTERN.fullmatch(account_key):
        await update.effective_message.reply_text(
            "Имя должно содержать 1–48 латинских букв, цифр, _ или -."
        )
        return ACCOUNT_UPLOAD_NAME
    session_dir = Path(context.application.bot_data["session_dir"])
    try:
        accounts = await _api(context).accounts()
        if account_key in {item["account_key"] for item in accounts}:
            raise ValueError("аккаунт с таким именем уже зарегистрирован")
        save_session_bundle(
            session_dir,
            account_key,
            context.user_data["session_upload"],
        )
    except (OSError, ValueError) as exc:
        await update.effective_message.reply_text(f"Не удалось сохранить аккаунт: {exc}")
        return ACCOUNT_UPLOAD_NAME
    context.user_data.pop("session_upload", None)
    context.user_data.pop("session_upload_key", None)
    context.user_data.pop("suggested_account_key", None)
    context.user_data["new_account_key"] = account_key
    return await _register_new_account(update, context, account_key)


async def add_account_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    label = update.effective_message.text.strip()
    if not label or len(label) > 80:
        await update.effective_message.reply_text("Подпись должна быть длиной 1–80 символов:")
        return ADD_ACCOUNT_LABEL
    return await _register_new_account(update, context, label)


async def _register_new_account(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    label: str,
) -> int:
    try:
        account = await _api(context).add_account(
            context.user_data.pop("new_account_key"),
            label,
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось добавить аккаунт: {exc}")
        return ConversationHandler.END
    try:
        profile = await _api(context).account_persona(account["account_key"])
    except Exception as exc:
        await update.effective_message.reply_text(
            f"Аккаунт добавлен, но личность не создалась: {exc}",
            reply_markup=home_keyboard(),
        )
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text(
        f"Добавлен аккаунт #{account['account_index']}: {account['label']} "
        f"({account['account_key']}).\n\n"
        "Личность создана автоматически и сохранена:\n"
        f"• обращение: на «{profile['address_style']}»\n"
        f"• грамотность: {max(1, profile['literacy_level'] - 1)}/5\n"
        f"• резкость: {profile['aggression_level']}/5\n"
        f"• разговорчивость: {profile['verbosity_level']}/5\n"
        f"• точка в конце: {profile['terminal_period_percent']}%\n\n"
        "Изменить профиль можно в разделе «Личности».",
        reply_markup=home_keyboard(),
    )
    return ConversationHandler.END


async def account_trait_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    value = int(query.data.split(":", 1)[1])
    position = int(context.user_data.get("trait_position", 0))
    field, _, _ = TRAITS[position]
    context.user_data["profile_traits"][field] = value
    position += 1
    context.user_data["trait_position"] = position
    if position < len(TRAITS):
        _, title, state = TRAITS[position]
        descriptions = {
            "Резкость": "1 — мягкий, 5 — очень прямой и напористый",
            "Дружелюбность": "1 — холодный, 5 — очень тёплый",
            "Разговорчивость": "1 — отвечает коротко, 5 — отвечает подробно",
            "Юмор": "1 — серьёзный, 5 — часто шутит",
            "Эмодзи": "1 — почти без эмодзи, 5 — использует часто",
            "Инициативность": "1 — только отвечает, 5 — развивает разговор",
        }
        await query.edit_message_text(
            f"{title}: {descriptions[title]}", reply_markup=trait_keyboard()
        )
        return state
    await query.edit_message_text(
        "Добавьте свободное описание личности и манеры общения или нажмите «Пропустить».",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Пропустить", callback_data="identity:skip")],
            [InlineKeyboardButton("Отмена", callback_data="flow:cancel")],
        ]),
    )
    return ACCOUNT_IDENTITY


async def account_identity_entered(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    identity = context.user_data.get("profile_existing", {}).get("identity_prompt", "")
    if update.callback_query:
        await update.callback_query.answer()
    else:
        identity = update.effective_message.text.strip()
        if len(identity) > 2000:
            await update.effective_message.reply_text("Описание должно быть до 2000 символов:")
            return ACCOUNT_IDENTITY
    traits = context.user_data.get("profile_traits", {})
    literacy = int(traits.get("literacy_level", 5))
    accuracy_by_level = {1: 60, 2: 68, 3: 93, 4: 95, 5: 98}
    profile = {
        **context.user_data.get("profile_existing", {}),
        **traits,
        "identity_prompt": identity,
        "word_accuracy_percent": accuracy_by_level[literacy],
        "punctuation_accuracy_percent": accuracy_by_level[literacy],
    }
    account_key = context.user_data["profile_account_key"]
    try:
        await _api(context).save_account_persona(account_key, profile)
    except Exception as exc:
        await _reply(update, f"Аккаунт добавлен, но профиль не сохранился: {exc}")
        return ConversationHandler.END
    context.user_data.clear()
    await _reply(
        update,
        f"Готово: аккаунт {account_key} и его индивидуальный профиль сохранены.",
        reply_markup=home_keyboard(),
    )
    return ConversationHandler.END


async def send_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    try:
        accounts = [account for account in await _api(context).accounts() if account["enabled"]]
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось получить аккаунты: {exc}")
        return ConversationHandler.END
    if not accounts:
        await update.effective_message.reply_text("Нет активных аккаунтов. Добавьте их в разделе «Аккаунты».")
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton(
            f"{account['account_index']}. {account['label']}",
            callback_data=f"sendacct:{account['account_index']}",
        )]
        for account in accounts
    ]
    await update.effective_message.reply_text(
        "Выберите аккаунт отправителя:",
        reply_markup=InlineKeyboardMarkup(
            keyboard + [[InlineKeyboardButton("Отмена", callback_data="flow:cancel")]]
        ),
    )
    return SEND_ACCOUNT


async def send_account_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    context.user_data["send_account_index"] = int(query.data.split(":", 1)[1])
    await query.edit_message_text(
        "Введите текст сообщения:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Отмена", callback_data="flow:cancel")]
        ]),
    )
    return SEND_TEXT


async def send_text_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    text = update.effective_message.text.strip()
    if not text or len(text) > 4096:
        await update.effective_message.reply_text("Текст должен быть от 1 до 4096 символов:")
        return SEND_TEXT
    context.user_data["send_text"] = text
    await update.effective_message.reply_text(
        "Пришлите JPEG или отправьте только текст:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Отправить без фото", callback_data="send:skip")],
            [InlineKeyboardButton("Отмена", callback_data="flow:cancel")],
        ]),
    )
    return SEND_ATTACHMENT


async def _deliver_from_bot(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    photo: Optional[bytes] = None,
) -> int:
    try:
        campaign = await _api(context).get_campaign()
        if not campaign or not campaign.get("recipient"):
            raise RuntimeError("сначала настройте получателя в allowlist")
        recipient = campaign["recipient"]
        if photo is None:
            result = await _api(context).send_message(
                recipient=recipient,
                text=context.user_data["send_text"],
                account_index=context.user_data["send_account_index"],
            )
        else:
            result = await _api(context).send_photo(
                recipient=recipient,
                text=context.user_data["send_text"],
                account_index=context.user_data["send_account_index"],
                photo=photo,
            )
    except Exception as exc:
        await update.effective_message.reply_text(f"Отправка не прошла: {exc}")
        return ConversationHandler.END
    await update.effective_message.reply_text(
        f"Отправлено через аккаунт #{result['account_index']} ({result['sender_account']}).",
        reply_markup=home_keyboard(),
    )
    context.user_data.pop("send_text", None)
    context.user_data.pop("send_account_index", None)
    return ConversationHandler.END


async def send_text_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    return await _deliver_from_bot(update, context)


async def send_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    photo = update.effective_message.photo[-1]
    if photo.file_size and photo.file_size > 10 * 1024 * 1024:
        await update.effective_message.reply_text("Фото должно быть не больше 10 МБ.")
        return SEND_ATTACHMENT
    if len(context.user_data.get("send_text", "")) > 1024:
        await update.effective_message.reply_text("Подпись к фото не может быть длиннее 1024 символов.")
        return SEND_ATTACHMENT
    telegram_file = await photo.get_file()
    image = bytes(await telegram_file.download_as_bytearray())
    return await _deliver_from_bot(update, context, photo=image)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "Действие отменено.", reply_markup=home_keyboard()
        )
    else:
        await update.effective_message.reply_text(
            "Действие отменено.", reply_markup=home_keyboard()
        )
    return ConversationHandler.END


async def edit_reply_prompt_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Отправьте новый полный системный промпт (до 4000 символов). "
        "Он будет использоваться целиком; профиль выбранного аккаунта добавится ниже него."
    )
    return EDIT_REPLY_PROMPT


async def edit_reply_prompt_entered(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    prompt = update.effective_message.text.strip()
    if not prompt or len(prompt) > 4000:
        await update.effective_message.reply_text("Нужен непустой промпт до 4000 символов:")
        return EDIT_REPLY_PROMPT
    try:
        campaign = await _api(context).get_campaign()
        if not campaign:
            raise RuntimeError("сначала настройте кампанию")
        campaign["reply_prompt"] = prompt
        await _api(context).save_campaign(campaign)
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось сохранить промпт: {exc}")
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "Полный системный промпт сохранён.", reply_markup=home_keyboard()
    )
    return ConversationHandler.END


async def edit_reply_delay_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Введите минимальную и максимальную задержку в минутах через пробел. "
        "Например: 2 180"
    )
    return EDIT_REPLY_DELAY


async def edit_reply_delay_entered(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    parts = update.effective_message.text.replace(",", " ").split()
    try:
        minimum, maximum = map(int, parts)
    except (ValueError, TypeError):
        await update.effective_message.reply_text("Введите два целых числа, например: 2 180")
        return EDIT_REPLY_DELAY
    if minimum < 0 or maximum > 1440 or minimum > maximum:
        await update.effective_message.reply_text(
            "Диапазон должен быть от 0 до 1440 минут, минимум не больше максимума:"
        )
        return EDIT_REPLY_DELAY
    try:
        campaign = await _api(context).get_campaign()
        if not campaign:
            raise RuntimeError("сначала настройте кампанию")
        campaign["reply_delay_min_minutes"] = minimum
        campaign["reply_delay_max_minutes"] = maximum
        await _api(context).save_campaign(campaign)
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось сохранить задержку: {exc}")
        return ConversationHandler.END
    await update.effective_message.reply_text(
        f"Задержка автоответов: {minimum}–{maximum} минут.",
        reply_markup=home_keyboard(),
    )
    return ConversationHandler.END


async def edit_activation_rules_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Введите правила, например:\n8100x10,3650x4,1800x0,660x0,325x0,60x0\n"
        "Ноль отключает пакет."
    )
    return EDIT_ACTIVATION_RULES


async def edit_activation_rules_entered(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    try:
        rules = parse_activation_rules(update.effective_message.text)
        campaign = await _api(context).get_campaign()
        if not campaign:
            raise RuntimeError("сначала настройте кампанию")
        campaign["activation_rules"] = rules
        await _api(context).save_campaign(campaign)
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось сохранить правила: {exc}")
        return EDIT_ACTIVATION_RULES
    await update.effective_message.reply_text(
        "Пороги активаций сохранены.", reply_markup=home_keyboard()
    )
    return ConversationHandler.END


async def inline_toggle_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    query = update.callback_query
    await query.answer()
    action = query.data or ""
    if action.startswith("auto:"):
        await set_auto_reply_enabled(update, context, action.endswith("enable"))
    elif action.startswith("activation:"):
        await set_activation_enabled(update, context, action.endswith("enable"))


async def campaign_setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    context.user_data["campaign_start_date"] = datetime.now(MOSCOW).date().isoformat()
    current = await _api(context).get_campaign()
    if current and current.get("recipient"):
        context.user_data["campaign_recipient"] = current["recipient"]
        return await _ask_campaign_phrases(update, context)
    legacy_recipient = context.application.bot_data.get("legacy_recipient")
    if legacy_recipient:
        context.user_data["campaign_recipient"] = legacy_recipient
        return await _ask_campaign_phrases(update, context)
    await update.effective_message.reply_text(
        "Введите Telegram username получателя. Система будет писать и отвечать "
        "только ему, например @partner_user:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Отмена", callback_data="flow:cancel")]
        ]),
    )
    return CAMPAIGN_RECIPIENT


async def campaign_recipient_entered(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    try:
        context.user_data["campaign_recipient"] = normalize_username(
            update.effective_message.text
        )
    except ValueError:
        await update.effective_message.reply_text(
            "Нужен корректный Telegram username, например @partner_user:"
        )
        return CAMPAIGN_RECIPIENT
    return await _ask_campaign_phrases(update, context)


async def _ask_campaign_phrases(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    await update.effective_message.reply_text(
        "Введите шаблоны сообщений по активациям, по одному на строку (до 100). "
        "Можно использовать {date}, {day}, {slot}, {pack}, {activations}.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Отмена", callback_data="flow:cancel")]
        ]),
    )
    return CAMPAIGN_PHRASES


async def edit_recipient_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    await update.effective_message.reply_text(
        "Введите новый Telegram username. После сохранения система будет писать и "
        "отвечать только ему:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Отмена", callback_data="flow:cancel")]
        ]),
    )
    return EDIT_RECIPIENT


async def edit_recipient_entered(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    try:
        recipient = normalize_username(update.effective_message.text)
        campaign = await _api(context).get_campaign()
        if not campaign:
            raise RuntimeError("сначала выполните первоначальную настройку шаблонов")
        campaign["recipient"] = recipient
        await _api(context).save_campaign(campaign)
    except Exception as exc:
        await update.effective_message.reply_text(
            f"Не удалось сохранить получателя: {exc}"
        )
        return EDIT_RECIPIENT
    await update.effective_message.reply_text(
        f"Получатель изменён на @{recipient}. Старые ответы другому username отправлены не будут.",
        reply_markup=home_keyboard(),
    )
    return ConversationHandler.END


async def campaign_phrases(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    phrases = [line.strip() for line in update.effective_message.text.splitlines() if line.strip()]
    if not phrases or len(phrases) > 100 or any(len(phrase) > 1024 for phrase in phrases):
        await update.effective_message.reply_text("Нужно от 1 до 100 непустых фраз, каждая до 1024 символов.")
        return CAMPAIGN_PHRASES
    context.user_data["campaign_phrases"] = phrases
    await update.effective_message.reply_text(
        "Введите полный системный промпт для автоответов (до 4000 символов) "
        "или используйте безопасный базовый вариант.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Использовать базовый промпт", callback_data="prompt:default")],
            [InlineKeyboardButton("Отмена", callback_data="flow:cancel")],
        ]),
    )
    return CAMPAIGN_PROMPT


async def campaign_prompt_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    context.user_data["campaign_reply_prompt"] = (
        DEFAULT_REPLY_PROMPT
    )
    return await _save_message_settings(update, context)


async def campaign_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    prompt = update.effective_message.text.strip()
    if not prompt or len(prompt) > 4000:
        await update.effective_message.reply_text("Prompt должен быть от 1 до 4000 символов:")
        return CAMPAIGN_PROMPT
    context.user_data["campaign_reply_prompt"] = prompt
    return await _save_message_settings(update, context)


async def _save_message_settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    current = await _api(context).get_campaign() or {}
    campaign = {
        "campaign_id": current.get("campaign_id", "activation-messages"),
        "enabled": False,
        "auto_reply_enabled": bool(current.get("auto_reply_enabled", False)),
        "reply_prompt": context.user_data["campaign_reply_prompt"],
        "reply_delay_min_minutes": int(current.get("reply_delay_min_minutes", 2)),
        "reply_delay_max_minutes": int(current.get("reply_delay_max_minutes", 180)),
        "start_date": current.get("start_date", context.user_data["campaign_start_date"]),
        "recipient": context.user_data.get("campaign_recipient")
        or current.get("recipient"),
        "phrases": context.user_data["campaign_phrases"],
        "day_slots": {},
        "activation_enabled": bool(current.get("activation_enabled", False)),
        "activation_rules": current.get("activation_rules", {}),
    }
    try:
        await _api(context).save_campaign(campaign)
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось сохранить настройки: {exc}")
        return ConversationHandler.END
    context.user_data.clear()
    text = "Шаблоны активаций и общий prompt сохранены. Плановых отправок по времени нет."
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=home_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=home_keyboard())
    return ConversationHandler.END


async def campaign_prompt_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    context.user_data["campaign_reply_prompt"] = DEFAULT_REPLY_PROMPT
    return await _save_message_settings(update, context)


async def set_auto_reply_enabled(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    enabled: bool,
) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    try:
        campaign = await _api(context).get_campaign()
        if not campaign:
            await update.effective_message.reply_text("Сначала настройте кампанию в главном меню.")
            return
        if enabled and not (campaign.get("reply_prompt") or "").strip():
            await update.effective_message.reply_text(
                "В кампании нет prompt. Откройте «Автоответы» и задайте его."
            )
            return
        campaign["auto_reply_enabled"] = enabled
        await _api(context).save_campaign(campaign)
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось изменить автоответы: {exc}")
        return
    state = "включены" if enabled else "выключены"
    await update.effective_message.reply_text(
        f"Автоответы {state}. Слушается только центральный аккаунт из allowlist."
    )


async def auto_reply_enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_auto_reply_enabled(update, context, True)


async def auto_reply_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_auto_reply_enabled(update, context, False)


async def persona_setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    accounts = await _api(context).accounts()
    buttons = [
        InlineKeyboardButton(
            f"{account['account_index']}. {account['label']}",
            callback_data=f"personaacct:{account['account_key']}",
        )
        for account in accounts
    ]
    if not buttons:
        await update.effective_message.reply_text("Сначала добавьте аккаунт в разделе «Аккаунты».")
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "Выберите аккаунт для настройки личности:",
        reply_markup=InlineKeyboardMarkup([[button] for button in buttons]),
    )
    return PERSONA_ACCOUNT


async def persona_account_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    account_key = query.data.split(":", 1)[1]
    try:
        profile = await _api(context).account_persona(account_key)
    except Exception as exc:
        await query.edit_message_text(f"Не удалось загрузить профиль: {exc}")
        return ConversationHandler.END
    context.user_data["profile_account_key"] = account_key
    context.user_data["profile_existing"] = profile
    context.user_data["profile_traits"] = {}
    context.user_data["trait_position"] = 0
    await query.edit_message_text(
        "Грамотность сейчас: "
        f"{max(1, profile.get('literacy_level', 5) - 1)} из 5. "
        "Выберите новое значение:",
        reply_markup=trait_keyboard(),
    )
    return ACCOUNT_TRAIT_LITERACY


async def persona_prompt_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    identity_prompt = update.effective_message.text.strip()
    if identity_prompt == ".":
        identity_prompt = ""
    if len(identity_prompt) > 2000:
        await update.effective_message.reply_text("Описание должно быть до 2000 символов:")
        return PERSONA_PROMPT
    context.user_data["persona_identity_prompt"] = identity_prompt
    existing = context.user_data.get("persona_existing", {})
    await update.effective_message.reply_text(
        "Укажите проценты точности слов и пунктуации через запятую (0–100). "
        f"Текущие: {existing.get('word_accuracy_percent', 100)}, "
        f"{existing.get('punctuation_accuracy_percent', 100)}. Пример: 96,85"
    )
    return PERSONA_METRICS


async def persona_metrics_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    parts = [part.strip() for part in update.effective_message.text.split(",")]
    if len(parts) != 2:
        await update.effective_message.reply_text("Введите два целых числа через запятую, например 96,85:")
        return PERSONA_METRICS
    try:
        word_accuracy, punctuation_accuracy = map(int, parts)
    except ValueError:
        await update.effective_message.reply_text("Оба значения должны быть целыми числами:")
        return PERSONA_METRICS
    if not 0 <= word_accuracy <= 100 or not 0 <= punctuation_accuracy <= 100:
        await update.effective_message.reply_text("Проценты должны быть от 0 до 100:")
        return PERSONA_METRICS
    account_key = context.user_data["persona_account_key"]
    existing = context.user_data.get("persona_existing", {})
    try:
        await _api(context).save_account_persona(
            account_key,
            {
                **existing,
                "identity_prompt": context.user_data["persona_identity_prompt"],
                "word_accuracy_percent": word_accuracy,
                "punctuation_accuracy_percent": punctuation_accuracy,
            },
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось сохранить профиль: {exc}")
        return ConversationHandler.END
    context.user_data.pop("persona_account_key", None)
    context.user_data.pop("persona_identity_prompt", None)
    context.user_data.pop("persona_existing", None)
    await update.effective_message.reply_text(
        f"Профиль {account_key} сохранён: точность слов {word_accuracy}%, "
        f"пунктуации {punctuation_accuracy}%."
    )
    return ConversationHandler.END


async def dialogue_preview_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    if update.callback_query:
        await update.callback_query.answer()
    accounts = await _api(context).accounts()
    buttons = [
        InlineKeyboardButton(
            f"{account['account_index']}. {account['label']}",
            callback_data=f"dialogueacct:{account['account_key']}",
        )
        for account in accounts
    ]
    if not buttons:
        await update.effective_message.reply_text("Сначала добавьте аккаунт в разделе «Аккаунты».")
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "Выберите аккаунт для проверки ответа:",
        reply_markup=InlineKeyboardMarkup([[button] for button in buttons]),
    )
    return DIALOGUE_ACCOUNT


async def dialogue_account_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    account_key = query.data.split(":", 1)[1]
    context.user_data["dialogue_account_key"] = account_key
    try:
        campaign = await _api(context).get_campaign()
    except Exception as exc:
        await query.edit_message_text(f"Не удалось загрузить prompt: {exc}")
        return ConversationHandler.END
    prompt = ((campaign or {}).get("reply_prompt") or "").strip()
    if not prompt:
        await query.edit_message_text(
            "Сначала задайте общий prompt в разделе «Автоответы».",
            reply_markup=home_keyboard(),
        )
        return ConversationHandler.END
    await query.edit_message_text(
        f"Общий prompt:\n{prompt[:2600]}\n\n"
        f"Теперь напишите тестовое сообщение человека для {account_key}. "
        "Бот покажет ответ аккаунта, но ничего не отправит."
    )
    return DIALOGUE_TASK


async def dialogue_task_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    incoming_text = update.effective_message.text.strip()
    if not incoming_text or len(incoming_text) > 4096:
        await update.effective_message.reply_text("Введите сообщение до 4096 символов:")
        return DIALOGUE_TASK
    account_key = context.user_data["dialogue_account_key"]
    try:
        preview = await _api(context).preview_reply(
            account_key=account_key,
            incoming_text=incoming_text,
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось создать preview: {exc}")
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Preview — в Telegram ничего не отправлено:\n\n"
        f"👤 Человек:\n{preview['incoming_text']}\n\n"
        f"🤖 {account_key}:\n{preview['reply_text']}",
        reply_markup=home_keyboard(),
    )
    return ConversationHandler.END


async def dialogue_targets_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    target_replies = [line.strip() for line in update.effective_message.text.splitlines()]
    if len(target_replies) != 5 or any(not reply for reply in target_replies):
        await update.effective_message.reply_text("Нужно ровно пять непустых строк с ответами:")
        return DIALOGUE_TARGETS
    try:
        proposal = await _api(context).propose_dialogue(
            account_key=context.user_data["dialogue_account_key"],
            task_prompt=context.user_data["dialogue_task_prompt"],
            target_replies=target_replies,
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось построить preview: {exc}")
        return ConversationHandler.END
    lines = [f"Preview #{proposal['proposal_id']} — только просмотр, не отправлено:"]
    for index, turn in enumerate(proposal["dialogue"], start=1):
        lines.append(
            f"{index}. Личность: {turn['sender']}\n"
            f"   Центральный тестовый аккаунт: {turn['target']}"
        )
    context.user_data.pop("dialogue_account_key", None)
    context.user_data.pop("dialogue_task_prompt", None)
    await update.effective_message.reply_text("\n\n".join(lines)[:3900])
    return ConversationHandler.END


async def activation_rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    raw_rules = " ".join(context.args).strip()
    if not raw_rules:
        await update.effective_message.reply_text(
            "Задайте множители для пакетов: "
            "8100x10,3650x0,1800x5,660x0,325x0,60x0\n"
            "Ноль означает выключенный пакет. Порог сообщения = размер пакета × множитель."
        )
        return
    try:
        rules = parse_activation_rules(raw_rules)
        campaign = await _api(context).get_campaign()
        if not campaign:
            await update.effective_message.reply_text("Сначала настройте общий шаблон в разделе «Кампания».")
            return
        campaign["activation_rules"] = rules
        await _api(context).save_campaign(campaign)
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось сохранить правила: {exc}")
        return
    lines = ["Правила сохранены; отправка пока не включалась:"]
    lines.extend(
        f"Пакет {pack_size} × {multiplier} = {int(pack_size) * multiplier} активаций/сообщение"
        for pack_size, multiplier in rules.items()
    )
    await update.effective_message.reply_text("\n".join(lines))


async def set_activation_enabled(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    enabled: bool,
) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    try:
        campaign = await _api(context).get_campaign()
        if not campaign:
            await update.effective_message.reply_text("Сначала настройте кампанию в главном меню.")
            return
        if enabled and not campaign.get("activation_rules"):
            await update.effective_message.reply_text("Сначала задайте правила кнопкой «Изменить пороги».")
            return
        if enabled:
            activation_status = await _api(context).activation_status()
            if not activation_status.get("endpoint_configured"):
                await update.effective_message.reply_text(
                    "На сервере API не задан PACK_ACTIVATION_ENDPOINT в .env."
                )
                return
        campaign["activation_enabled"] = enabled
        await _api(context).save_campaign(campaign)
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось изменить автоотправку: {exc}")
        return
    await update.effective_message.reply_text(
        "Автоотправка по активациям включена." if enabled
        else "Автоотправка по активациям выключена."
    )


async def activation_enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_activation_enabled(update, context, True)


async def activation_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_activation_enabled(update, context, False)


async def activation_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    try:
        status_data = await _api(context).activation_status()
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось получить статус: {exc}")
        return
    first = status_data.get("first_response")
    latest = status_data.get("last_response")
    lines = [
        f"Endpoint: {'настроен' if status_data['endpoint_configured'] else 'не настроен в API'}",
        f"Отправка: {'включена' if status_data['enabled'] else 'выключена'}",
        f"Ожидают отправки: {status_data['pending']}",
    ]
    if first:
        lines.append(f"Первая выборка: {first['as_of']}")
    if latest:
        lines.append(f"Последняя выборка: {latest['as_of']}")
        lines.extend(
            f"Пакет {pack['pack_size']}: {pack['activations_24h']} за 24ч, "
            f"+{pack['activations_since_previous_sync']} с прошлого sync"
            for pack in latest["packs"]
        )
    if status_data.get("rules"):
        lines.append("Правила: " + ", ".join(
            f"{pack}x{multiplier} (порог {int(pack) * multiplier})"
            for pack, multiplier in status_data["rules"].items()
        ))
    await update.effective_message.reply_text("\n".join(lines))


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Открыть панель управления"),
        ]
    )


async def post_shutdown(application: Application) -> None:
    await application.bot_data["api"].close()


def build_application() -> Application:
    load_dotenv()
    bot_token = os.environ.get("CONTROL_BOT_TOKEN", "").strip()
    api_token = os.environ.get("API_TOKEN", "").strip()
    legacy_recipient = os.environ.get("TELEGRAM_ALLOWED_RECIPIENTS", "").split(",")[0]
    legacy_recipient = legacy_recipient.strip().removeprefix("@").casefold()
    api_url = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000").strip()
    session_dir = os.environ.get(
        "TELEGRAM_SESSION_DIR",
        "~/.telegram-research-simulator/sessions",
    )
    if not bot_token:
        raise ValueError("CONTROL_BOT_TOKEN is required")
    if not api_token:
        raise ValueError("API_TOKEN is required")
    admin_ids = parse_admin_ids(os.environ.get("CONTROL_ADMIN_IDS", ""))
    api_client = ApiClient(api_url, api_token)
    application = (
        Application.builder()
        .token(bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["api"] = api_client
    application.bot_data["admin_ids"] = admin_ids
    application.bot_data["legacy_recipient"] = legacy_recipient
    application.bot_data["session_dir"] = str(Path(session_dir).expanduser())

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("add_account", add_account_start),
            CommandHandler("send", send_start),
            CommandHandler("campaign_setup", campaign_setup_start),
            CommandHandler("persona_setup", persona_setup_start),
            CommandHandler("dialogue_preview", dialogue_preview_start),
            CallbackQueryHandler(add_account_start, pattern=r"^flow:add_account$"),
            CallbackQueryHandler(send_start, pattern=r"^flow:send$"),
            CallbackQueryHandler(campaign_setup_start, pattern=r"^flow:campaign$"),
            CallbackQueryHandler(persona_setup_start, pattern=r"^flow:persona$"),
            CallbackQueryHandler(dialogue_preview_start, pattern=r"^flow:dialogue$"),
            CallbackQueryHandler(edit_reply_prompt_start, pattern=r"^flow:reply_prompt$"),
            CallbackQueryHandler(edit_reply_delay_start, pattern=r"^flow:reply_delay$"),
            CallbackQueryHandler(edit_activation_rules_start, pattern=r"^flow:activation_rules$"),
            CallbackQueryHandler(edit_recipient_start, pattern=r"^flow:recipient$"),
        ],
        states={
            ADD_ACCOUNT_KEY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_key),
                CallbackQueryHandler(add_account_key, pattern=r"^session:[a-zA-Z0-9_-]+$"),
            ],
            ADD_ACCOUNT_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_label)],
            ACCOUNT_UPLOAD: [
                MessageHandler(filters.Document.ALL, account_upload_received)
            ],
            ACCOUNT_UPLOAD_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, account_upload_name_chosen),
                CallbackQueryHandler(
                    account_upload_name_chosen,
                    pattern=r"^uploadname:[a-zA-Z0-9_-]+$",
                ),
            ],
            SEND_ACCOUNT: [CallbackQueryHandler(send_account_chosen, pattern=r"^sendacct:\d+$")],
            SEND_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_text_entered)],
            SEND_ATTACHMENT: [
                MessageHandler(filters.PHOTO, send_photo),
                CommandHandler("skip", send_text_only),
                CallbackQueryHandler(send_text_only, pattern=r"^send:skip$"),
            ],
            CAMPAIGN_PHRASES: [MessageHandler(filters.TEXT & ~filters.COMMAND, campaign_phrases)],
            CAMPAIGN_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, campaign_prompt),
                CommandHandler("skip_prompt", campaign_prompt_skip),
                CallbackQueryHandler(campaign_prompt_choice, pattern=r"^prompt:(default|skip)$"),
            ],
            CAMPAIGN_RECIPIENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, campaign_recipient_entered)
            ],
            PERSONA_ACCOUNT: [
                CallbackQueryHandler(
                    persona_account_chosen,
                    pattern=r"^personaacct:[a-zA-Z0-9_-]+$",
                )
            ],
            PERSONA_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, persona_prompt_entered)],
            PERSONA_METRICS: [MessageHandler(filters.TEXT & ~filters.COMMAND, persona_metrics_entered)],
            DIALOGUE_ACCOUNT: [
                CallbackQueryHandler(
                    dialogue_account_chosen,
                    pattern=r"^dialogueacct:[a-zA-Z0-9_-]+$",
                )
            ],
            DIALOGUE_TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, dialogue_task_entered)],
            DIALOGUE_TARGETS: [MessageHandler(filters.TEXT & ~filters.COMMAND, dialogue_targets_entered)],
            ACCOUNT_TRAIT_LITERACY: [CallbackQueryHandler(account_trait_chosen, pattern=r"^trait:[1-5]$")],
            ACCOUNT_TRAIT_AGGRESSION: [CallbackQueryHandler(account_trait_chosen, pattern=r"^trait:[1-5]$")],
            ACCOUNT_TRAIT_FRIENDLINESS: [CallbackQueryHandler(account_trait_chosen, pattern=r"^trait:[1-5]$")],
            ACCOUNT_TRAIT_VERBOSITY: [CallbackQueryHandler(account_trait_chosen, pattern=r"^trait:[1-5]$")],
            ACCOUNT_TRAIT_HUMOR: [CallbackQueryHandler(account_trait_chosen, pattern=r"^trait:[1-5]$")],
            ACCOUNT_TRAIT_EMOJI: [CallbackQueryHandler(account_trait_chosen, pattern=r"^trait:[1-5]$")],
            ACCOUNT_TRAIT_INITIATIVE: [CallbackQueryHandler(account_trait_chosen, pattern=r"^trait:[1-5]$")],
            ACCOUNT_IDENTITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, account_identity_entered),
                CallbackQueryHandler(account_identity_entered, pattern=r"^identity:skip$"),
            ],
            EDIT_REPLY_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_reply_prompt_entered)],
            EDIT_REPLY_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_reply_delay_entered)],
            EDIT_ACTIVATION_RULES: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_activation_rules_entered)],
            EDIT_RECIPIENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_recipient_entered)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern=r"^flow:cancel$"),
        ],
        allow_reentry=True,
        per_message=False,
    )
    application.add_handler(conversation)
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(CommandHandler("accounts", show_accounts))
    application.add_handler(CommandHandler("history", show_history))
    application.add_handler(CommandHandler("campaign", show_campaign))
    application.add_handler(CommandHandler("activation_rules", activation_rules_command))
    application.add_handler(CommandHandler("activation_enable", activation_enable))
    application.add_handler(CommandHandler("activation_disable", activation_disable))
    application.add_handler(CommandHandler("activation_status", activation_status_command))
    application.add_handler(CommandHandler("auto_reply_enable", auto_reply_enable))
    application.add_handler(CommandHandler("auto_reply_disable", auto_reply_disable))
    application.add_handler(CommandHandler("dialogues", show_dialogue_proposals))
    application.add_handler(
        CallbackQueryHandler(
            menu_callback,
            pattern=r"^(menu:|history:|account:|proposals:)",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            inline_toggle_callback,
            pattern=r"^(auto:|activation:)(enable|disable)$",
        )
    )
    return application


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
