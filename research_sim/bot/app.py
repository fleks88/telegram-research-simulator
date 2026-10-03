from __future__ import annotations

import asyncio
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


LOGGER = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")
ADD_ACCOUNT_KEY, ADD_ACCOUNT_LABEL, SEND_ACCOUNT, SEND_TEXT, SEND_ATTACHMENT = range(5)
CAMPAIGN_START_DATE, CAMPAIGN_PHRASES, CAMPAIGN_SLOTS, CAMPAIGN_PROMPT = range(5, 9)
ACCOUNT_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")


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


def is_admin(user_id: Optional[int], admin_ids: Set[int]) -> bool:
    return user_id is not None and user_id in admin_ids


def parse_day_slots(value: str) -> Dict[str, list[str]]:
    groups = value.split("|")
    if len(groups) != 3:
        raise ValueError("Укажите слоты для трёх дней через |, например 10:00 | 10:00,15:00 | 10:00")
    result: Dict[str, list[str]] = {}
    for day, group in enumerate(groups, start=1):
        slots = [slot.strip() for slot in group.split(",") if slot.strip()]
        if not slots:
            continue
        if len(slots) > 3 or any(not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", slot) for slot in slots):
            raise ValueError("В каждом дне нужно от 1 до 3 слотов формата HH:MM")
        if any(int(slot[:2]) > 23 for slot in slots):
            raise ValueError("Часы должны быть в диапазоне 00:00–23:59")
        minutes = [int(slot[:2]) * 60 + int(slot[3:]) for slot in slots]
        if minutes != sorted(set(minutes)):
            raise ValueError("Слоты должны быть уникальны и идти по времени")
        if any(later - earlier < 30 for earlier, later in zip(minutes, minutes[1:])):
            raise ValueError("Между слотами одного дня нужно не менее 30 минут")
        result[str(day)] = slots
    if not result:
        raise ValueError("Укажите хотя бы один слот за три дня")
    return result


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
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Аккаунты", callback_data="menu:accounts")],
            [InlineKeyboardButton("История", callback_data="menu:history")],
            [InlineKeyboardButton("Планировщик", callback_data="menu:campaign")],
            [InlineKeyboardButton("Отправить сообщение", callback_data="menu:send")],
        ]
    )
    await update.effective_message.reply_text(
        "Панель управления Telegram API. Все даты и время показываются по Москве.",
        reply_markup=keyboard,
    )


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
        await _reply(update, "Аккаунтов пока нет. Добавление: /add_account")
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
    )
    await _reply(update, "\n".join(lines) + "\n\nДобавить: /add_account", reply_markup=markup)


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
        reply_markup=InlineKeyboardMarkup([[button] for button in buttons]),
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
        entries = await _api(context).account_history(account_key, limit=10)
    except Exception as exc:
        await _reply(update, f"Не удалось получить историю: {exc}")
        return
    if not entries:
        await _reply(update, f"Для аккаунта {account_key} отправок пока нет.")
        return
    lines = [f"Последние отправки: {account_key} (МСК)"]
    for entry in entries:
        status = "отправлено" if entry["status"] == "sent" else entry["status"]
        content = entry.get("message_text", "").replace("\n", " ")[:180]
        attachment = " 📷" if entry.get("photo_attached") else ""
        lines.append(
            f"#{entry['id']} · {format_moscow_time(entry['created_at'])} · {status}{attachment}\n"
            f"→ @{entry['recipient']}: {content}"
        )
    await _reply(update, "\n\n".join(lines))


async def show_campaign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    try:
        config = await _api(context).get_campaign()
    except Exception as exc:
        await _reply(update, f"Не удалось получить расписание: {exc}")
        return
    if not config:
        await _reply(update, "Расписание не настроено. Начать настройку: /campaign_setup")
        return
    state = "включено" if config["enabled"] else "выключено"
    slots = "; ".join(
        f"день {day}: {', '.join(values)} МСК"
        for day, values in sorted(config["day_slots"].items(), key=lambda item: int(item[0]))
    )
    text = (
        f"Кампания: {config['campaign_id']}\nСтатус: {state}\n"
        f"Старт: {config['start_date']}\nПолучатель: @{config['recipient']}\n"
        f"Слоты: {slots}\nФраз в пуле: {len(config['phrases'])}\n"
        f"Автоответы: {'включены' if config.get('auto_reply_enabled') else 'выключены'}\n"
        f"Шаблоны:\n- " + "\n- ".join(config["phrases"][:10])
    )
    if len(config["phrases"]) > 10:
        text += f"\n… и ещё {len(config['phrases']) - 10}"
    prompt = config.get("reply_prompt")
    if prompt:
        text += "\n\nPrompt:\n" + (prompt[:2500] + ("…" if len(prompt) > 2500 else ""))
    text += (
        "\n\nНастройка: /campaign_setup · /campaign_enable · /campaign_disable"
        "\nАвтоответы: /auto_reply_enable · /auto_reply_disable"
    )
    await _reply(update, text[:3900])


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    query = update.callback_query
    value = query.data or ""
    if value == "menu:accounts":
        await show_accounts(update, context)
    elif value == "menu:history":
        await show_history(update, context)
    elif value == "menu:campaign":
        await show_campaign(update, context)
    elif value == "menu:send":
        await query.answer()
        await query.message.reply_text("Чтобы выбрать аккаунт и отправить сообщение, используйте /send")
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


async def add_account_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "Введите ключ сессии (например business). На сервере должен существовать "
        "уже авторизованный файл TELEGRAM_SESSION_DIR/business.session в формате Telethon.\n/cancel для отмены."
    )
    return ADD_ACCOUNT_KEY


async def add_account_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    key = update.effective_message.text.strip().lower()
    if not ACCOUNT_KEY_PATTERN.fullmatch(key):
        await update.effective_message.reply_text("Допустимы латинские буквы, цифры, _ и -. Повторите:")
        return ADD_ACCOUNT_KEY
    session_path = Path(context.application.bot_data["session_dir"]) / (key + ".session")
    if not session_path.is_file():
        await update.effective_message.reply_text(
            f"Файл сессии не найден на сервере: {session_path}. Я не принимаю файлы сессий через Telegram."
        )
        return ADD_ACCOUNT_KEY
    context.user_data["new_account_key"] = key
    await update.effective_message.reply_text("Введите название аккаунта для списка:")
    return ADD_ACCOUNT_LABEL


async def add_account_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    label = update.effective_message.text.strip()
    if not label or len(label) > 80:
        await update.effective_message.reply_text("Название должно быть от 1 до 80 символов. Повторите:")
        return ADD_ACCOUNT_LABEL
    try:
        result = await _api(context).add_account(
            context.user_data.pop("new_account_key"),
            label,
        )
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось добавить аккаунт: {exc}")
        return ConversationHandler.END
    await update.effective_message.reply_text(
        f"Добавлен аккаунт #{result['account_index']}: {result['label']} ({result['account_key']})."
    )
    return ConversationHandler.END


async def send_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    try:
        accounts = [account for account in await _api(context).accounts() if account["enabled"]]
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось получить аккаунты: {exc}")
        return ConversationHandler.END
    if not accounts:
        await update.effective_message.reply_text("Нет включённых аккаунтов. Добавьте сессию через /add_account")
        return ConversationHandler.END
    buttons = [
        InlineKeyboardButton(
            f"{account['account_index']}. {account['label']}",
            callback_data=f"sendacct:{account['account_index']}",
        )
        for account in accounts
    ]
    await update.effective_message.reply_text(
        "Выберите аккаунт отправителя:",
        reply_markup=InlineKeyboardMarkup([[button] for button in buttons]),
    )
    return SEND_ACCOUNT


async def send_account_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    context.user_data["send_account_index"] = int(query.data.split(":", 1)[1])
    await query.edit_message_text("Введите текст сообщения. Для отмены: /cancel")
    return SEND_TEXT


async def send_text_entered(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    text = update.effective_message.text.strip()
    if not text or len(text) > 4096:
        await update.effective_message.reply_text("Введите текст до 4096 символов:")
        return SEND_TEXT
    context.user_data["send_text"] = text
    await update.effective_message.reply_text(
        "Пришлите JPEG-фото или отправьте /skip, чтобы послать только текст."
    )
    return SEND_ATTACHMENT


async def _deliver_from_bot(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    photo: Optional[bytes] = None,
    filename: str = "photo.jpg",
) -> int:
    config = context.application.bot_data["config"]
    try:
        if photo is None:
            result = await _api(context).send_message(
                recipient=config["recipient"],
                text=context.user_data["send_text"],
                account_index=context.user_data["send_account_index"],
            )
        else:
            result = await _api(context).send_photo(
                recipient=config["recipient"],
                text=context.user_data["send_text"],
                account_index=context.user_data["send_account_index"],
                photo=photo,
                filename=filename,
            )
    except Exception as exc:
        await update.effective_message.reply_text(f"Отправка не выполнена: {exc}")
        return ConversationHandler.END
    await update.effective_message.reply_text(
        f"Сообщение отправлено через аккаунт #{result['account_index']} "
        f"({result['sender_account']}) центральному получателю."
    )
    return ConversationHandler.END


async def send_text_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    return await _deliver_from_bot(update, context)


async def send_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    telegram_photo = update.effective_message.photo[-1]
    if telegram_photo.file_size and telegram_photo.file_size > 10 * 1024 * 1024:
        await update.effective_message.reply_text("Фото должно быть не больше 10 МБ.")
        return SEND_ATTACHMENT
    if len(context.user_data.get("send_text", "")) > 1024:
        await update.effective_message.reply_text(
            "Подпись к фото ограничена 1024 символами. Отмените отправку и сократите текст."
        )
        return SEND_ATTACHMENT
    file = await telegram_photo.get_file()
    photo_bytes = bytes(await file.download_as_bytearray())
    return await _deliver_from_bot(
        update,
        context,
        photo=photo_bytes,
        filename="telegram-upload.jpg",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text("Действие отменено.")
    return ConversationHandler.END


async def campaign_setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "Введите дату начала в формате YYYY-MM-DD. Все слоты будут по Москве."
    )
    return CAMPAIGN_START_DATE


async def campaign_start_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    try:
        datetime.strptime(update.effective_message.text.strip(), "%Y-%m-%d")
    except ValueError:
        await update.effective_message.reply_text("Нужна дата YYYY-MM-DD. Повторите:")
        return CAMPAIGN_START_DATE
    context.user_data["campaign_start_date"] = update.effective_message.text.strip()
    await update.effective_message.reply_text(
        "Введите пул шаблонов, по одному на строку (до 100). Можно использовать "
        "{date}, {day}, {slot}; например: Тестовый этап {day}, слот {slot}."
    )
    return CAMPAIGN_PHRASES


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
        "Введите системный prompt для автоответов на входящие сообщения центрального "
        "тестового аккаунта. До 4000 символов. Для пропуска отправьте /skip_prompt."
    )
    return CAMPAIGN_PROMPT


async def campaign_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    prompt = update.effective_message.text.strip()
    if not prompt or len(prompt) > 4000:
        await update.effective_message.reply_text("Prompt должен быть от 1 до 4000 символов:")
        return CAMPAIGN_PROMPT
    context.user_data["campaign_reply_prompt"] = prompt
    await update.effective_message.reply_text(
        "Теперь задайте слоты для дней 1, 2 и 3 через |. Пример: "
        "10:00 | 10:00,15:00 | 10:00\nДень можно оставить пустым."
    )
    return CAMPAIGN_SLOTS


async def campaign_prompt_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    context.user_data["campaign_reply_prompt"] = None
    await update.effective_message.reply_text(
        "Автоответы будут недоступны без prompt. Теперь задайте слоты дней 1–3, "
        "например: 10:00 | 10:00,15:00 | 10:00"
    )
    return CAMPAIGN_SLOTS


async def campaign_slots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_authorized(update, context):
        await _deny(update)
        return ConversationHandler.END
    try:
        slots = parse_day_slots(update.effective_message.text)
    except ValueError as exc:
        await update.effective_message.reply_text(f"{exc}\nПовторите ввод слотов:")
        return CAMPAIGN_SLOTS
    config = context.application.bot_data["config"]
    campaign = {
        "campaign_id": "bot-campaign-" + context.user_data["campaign_start_date"],
        "enabled": False,
        "auto_reply_enabled": False,
        "reply_prompt": context.user_data.get("campaign_reply_prompt"),
        "start_date": context.user_data["campaign_start_date"],
        "recipient": config["recipient"],
        "phrases": context.user_data["campaign_phrases"],
        "day_slots": slots,
    }
    try:
        await _api(context).save_campaign(campaign)
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось сохранить расписание: {exc}")
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Пул, prompt и расписание сохранены выключенными. Проверьте /campaign; "
        "плановые сообщения: /campaign_enable, автоответы: /auto_reply_enable."
    )
    return ConversationHandler.END


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
            await update.effective_message.reply_text("Сначала настройте кампанию: /campaign_setup")
            return
        if enabled and not (campaign.get("reply_prompt") or "").strip():
            await update.effective_message.reply_text(
                "В кампании нет prompt. Запустите /campaign_setup и задайте prompt."
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


async def campaign_enable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    await set_campaign_enabled(update, context, True)


async def campaign_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, context):
        await _deny(update)
        return
    await set_campaign_enabled(update, context, False)


async def set_campaign_enabled(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    enabled: bool,
) -> None:
    try:
        campaign = await _api(context).get_campaign()
        if not campaign:
            await update.effective_message.reply_text("Сначала настройте кампанию: /campaign_setup")
            return
        campaign["enabled"] = enabled
        await _api(context).save_campaign(campaign)
    except Exception as exc:
        await update.effective_message.reply_text(f"Не удалось изменить кампанию: {exc}")
        return
    await update.effective_message.reply_text(
        "Кампания включена." if enabled else "Кампания выключена."
    )


async def scheduler_loop(application: Application) -> None:
    api: ApiClient = application.bot_data["api"]
    while True:
        try:
            await api.tick_campaign()
        except RuntimeError as exc:
            if "404" not in str(exc) and "409" not in str(exc):
                LOGGER.warning("Campaign tick failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Unexpected campaign scheduler error")
        now = datetime.now(MOSCOW)
        seconds_until_next_minute = 60 - now.second - now.microsecond / 1_000_000
        await asyncio.sleep(max(1, seconds_until_next_minute))


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Открыть панель управления"),
            BotCommand("accounts", "Список аккаунтов отправителя"),
            BotCommand("add_account", "Добавить серверную Telethon-сессию"),
            BotCommand("send", "Отправить текст или фото"),
            BotCommand("history", "Посмотреть историю отправок"),
            BotCommand("campaign", "Показать расписание"),
            BotCommand("campaign_setup", "Настроить расписание"),
            BotCommand("campaign_enable", "Включить расписание"),
            BotCommand("campaign_disable", "Выключить расписание"),
            BotCommand("auto_reply_enable", "Включить автоответы тестовому аккаунту"),
            BotCommand("auto_reply_disable", "Выключить автоответы"),
            BotCommand("whoami", "Показать свой Telegram user ID"),
        ]
    )
    application.bot_data["scheduler_task"] = asyncio.create_task(scheduler_loop(application))


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.get("scheduler_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await application.bot_data["api"].close()


def build_application() -> Application:
    load_dotenv()
    bot_token = os.environ.get("CONTROL_BOT_TOKEN", "").strip()
    api_token = os.environ.get("API_TOKEN", "").strip()
    recipient = os.environ.get("TELEGRAM_ALLOWED_RECIPIENTS", "").strip().removeprefix("@").casefold()
    api_url = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000").strip()
    session_dir = os.environ.get(
        "TELEGRAM_SESSION_DIR",
        "~/.telegram-research-simulator/sessions",
    )
    if not bot_token:
        raise ValueError("CONTROL_BOT_TOKEN is required")
    if not api_token:
        raise ValueError("API_TOKEN is required")
    if not recipient:
        raise ValueError("TELEGRAM_ALLOWED_RECIPIENTS must contain the central recipient")
    admin_ids = parse_admin_ids(os.environ.get("CONTROL_ADMIN_IDS", ""))
    config = {"recipient": recipient}
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
    application.bot_data["config"] = config
    application.bot_data["session_dir"] = str(Path(session_dir).expanduser())

    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("add_account", add_account_start),
            CommandHandler("send", send_start),
            CommandHandler("campaign_setup", campaign_setup_start),
        ],
        states={
            ADD_ACCOUNT_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_key)],
            ADD_ACCOUNT_LABEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_account_label)],
            SEND_ACCOUNT: [CallbackQueryHandler(send_account_chosen, pattern=r"^sendacct:\d+$")],
            SEND_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, send_text_entered)],
            SEND_ATTACHMENT: [
                MessageHandler(filters.PHOTO, send_photo),
                CommandHandler("skip", send_text_only),
            ],
            CAMPAIGN_START_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, campaign_start_date)],
            CAMPAIGN_PHRASES: [MessageHandler(filters.TEXT & ~filters.COMMAND, campaign_phrases)],
            CAMPAIGN_PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, campaign_prompt),
                CommandHandler("skip_prompt", campaign_prompt_skip),
            ],
            CAMPAIGN_SLOTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, campaign_slots)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
    application.add_handler(conversation)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(CommandHandler("accounts", show_accounts))
    application.add_handler(CommandHandler("history", show_history))
    application.add_handler(CommandHandler("campaign", show_campaign))
    application.add_handler(CommandHandler("campaign_enable", campaign_enable))
    application.add_handler(CommandHandler("campaign_disable", campaign_disable))
    application.add_handler(CommandHandler("auto_reply_enable", auto_reply_enable))
    application.add_handler(CommandHandler("auto_reply_disable", auto_reply_disable))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(menu:|history:|account:)"))
    return application


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()