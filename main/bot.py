"""Асинхронный Telegram booking-бот на aiogram 3 и SQLite."""

from __future__ import annotations

import asyncio
import calendar
import html
import logging
import os
import random
import sqlite3
import socket
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    MenuButtonCommands,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = Path(os.getenv("DB_PATH", "data/bookings.sqlite3"))
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
WORKING_HOURS = ("10:00", "11:00", "12:00", "14:00", "15:00", "16:00", "17:00", "18:00")
SLOT_COLUMNS = 2
PENDING_ADMIN_DESCRIPTION: dict[int, tuple[str, str, int, int]] = {}
PENDING_BOOKING_DESCRIPTION: dict[int, tuple[str, Optional[str], int, int, int]] = {}
MAX_BOOKING_DESCRIPTION_LENGTH = 500
CALENDAR_SCREEN_IDS: dict[int, tuple[int, int]] = {}
ADMIN_SCREEN_IDS: dict[int, tuple[int, int]] = {}
KEYBOARD_SET_USER_IDS: set[int] = set()


async def delete_message_quietly(bot: Bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramAPIError:
        pass


async def configure_bot_menu(bot: Bot) -> None:
    """Показывает команды в системной кнопке Menu; список зависит от роли."""
    public_commands = [
        BotCommand(command="start", description="Открыть календарь"),
        BotCommand(command="calendar", description="Показать календарь"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="cancel", description="Отменить текущий ввод"),
    ]
    await bot.set_my_commands(public_commands, scope=BotCommandScopeDefault())
    admin_commands = public_commands + [
        BotCommand(command="admin", description="Управление календарём"),
        BotCommand(command="commands", description="Команды управления"),
        BotCommand(command="today", description="Настроить сегодняшний день"),
    ]
    for admin_id in ADMIN_IDS:
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("booking_bot")
router = Router()
calendar.setfirstweekday(calendar.MONDAY)


class DeleteIncomingMessagesMiddleware(BaseMiddleware):
    """Delete private-chat user messages, but keep their update for the handler."""

    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.chat.type == "private" and event.from_user:
            bot = data.get("bot") or event.bot
            await delete_message_quietly(bot, event.chat.id, event.message_id)
        return await handler(event, data)


router.message.outer_middleware(DeleteIncomingMessagesMiddleware())

WEEKDAYS = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
MONTH_TITLES = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)


def connect_db() -> sqlite3.Connection:
    """Открывает соединение. Контекстный менеджер вызывающая функция закрывает явно."""
    connection = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def init_db() -> None:
    """Создаёт таблицы и заполняет пустую БД демонстрационным расписанием."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = connect_db()
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_date TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                booked_by INTEGER,
                description TEXT,
                importance TEXT NOT NULL DEFAULT 'обычная'
                    CHECK (importance IN ('обычная', 'важная', 'срочная')),
                booked_name TEXT,
                booked_username TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(slot_date, slot_time)
            );
            CREATE INDEX IF NOT EXISTS idx_slots_date ON slots(slot_date);
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_id INTEGER NOT NULL UNIQUE REFERENCES slots(id),
                user_id INTEGER NOT NULL,
                booked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        # Миграция существующей БД: добавляем описание и метку важности.
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(slots)")}
        if "description" not in columns:
            connection.execute("ALTER TABLE slots ADD COLUMN description TEXT")
        if "importance" not in columns:
            connection.execute("ALTER TABLE slots ADD COLUMN importance TEXT NOT NULL DEFAULT 'обычная'")
        if "booked_name" not in columns:
            connection.execute("ALTER TABLE slots ADD COLUMN booked_name TEXT")
        if "booked_username" not in columns:
            connection.execute("ALTER TABLE slots ADD COLUMN booked_username TEXT")

        # Однократная очистка старого демо-календаря. Маркер не даёт
        # случайно удалить реальные записи при следующих перезапусках.
        today = date.today()
        end_day = today + timedelta(days=59)
        cleaned = connection.execute(
            "SELECT 1 FROM app_meta WHERE key = 'calendar_clean_v1'"
        ).fetchone()
        if not cleaned:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM bookings WHERE slot_id IN (SELECT id FROM slots WHERE slot_date BETWEEN ? AND ?)",
                    (today.isoformat(), end_day.isoformat()),
                )
                connection.execute(
                    "DELETE FROM slots WHERE slot_date BETWEEN ? AND ?",
                    (today.isoformat(), end_day.isoformat()),
                )
                rows = [
                    ((today + timedelta(days=offset)).isoformat(), value)
                    for offset in range(60)
                    for value in WORKING_HOURS
                ]
                connection.executemany(
                    "INSERT INTO slots(slot_date, slot_time) VALUES (?, ?)", rows
                )
                connection.execute(
                    "INSERT INTO app_meta(key, value) VALUES ('calendar_clean_v1', ?)",
                    (datetime.now().isoformat(timespec="seconds"),),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        else:
            # Добавляем только отсутствующие строки расписания, не трогая
            # занятые слоты и описания.
            rows = [
                ((today + timedelta(days=offset)).isoformat(), value)
                for offset in range(60)
                for value in WORKING_HOURS
            ]
            connection.executemany(
                "INSERT OR IGNORE INTO slots(slot_date, slot_time) VALUES (?, ?)", rows
            )
    finally:
        connection.close()


def clear_legacy_demo_bookings() -> int:
    """Разово снимает старые случайные брони, оставленные ранней версией бота."""
    connection = connect_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        demo_ids = [
            row["id"] for row in connection.execute(
                "SELECT id FROM slots WHERE booked_by < 0 AND description IS NULL"
            )
        ]
        if demo_ids:
            connection.executemany("DELETE FROM bookings WHERE slot_id = ?", ((item,) for item in demo_ids))
            connection.executemany(
                "UPDATE slots SET booked_by = NULL, importance = 'обычная', booked_name = NULL, booked_username = NULL WHERE id = ?",
                ((item,) for item in demo_ids),
            )
        connection.commit()
        return len(demo_ids)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_month_counts(year: int, month: int) -> dict[date, tuple[int, int]]:
    last_day = calendar.monthrange(year, month)[1]
    connection = connect_db()
    try:
        rows = connection.execute(
            """SELECT slot_date, COUNT(*) AS total,
                      SUM(CASE WHEN booked_by IS NULL AND
                          (slot_date > ? OR (slot_date = ? AND slot_time > ?))
                          THEN 1 ELSE 0 END) AS free
               FROM slots WHERE slot_date BETWEEN ? AND ? GROUP BY slot_date""",
            (
                date.today().isoformat(),
                date.today().isoformat(),
                datetime.now().strftime("%H:%M"),
                date(year, month, 1).isoformat(),
                date(year, month, last_day).isoformat(),
            ),
        ).fetchall()
        return {
            date.fromisoformat(row["slot_date"]): (int(row["free"] or 0), int(row["total"] or 0))
            for row in rows
        }
    finally:
        connection.close()


def get_day_slots(day: date) -> list[sqlite3.Row]:
    connection = connect_db()
    try:
        return connection.execute(
            "SELECT slot_time, booked_by, description, importance, booked_name, booked_username FROM slots WHERE slot_date = ? ORDER BY slot_time",
            (day.isoformat(),),
        ).fetchall()
    finally:
        connection.close()


def book_slot(
    day: date,
    slot_time: str,
    user_id: int,
    first_name: str = "",
    last_name: Optional[str] = None,
    username: Optional[str] = None,
    description: str = "",
) -> bool:
    """Условное обновление в транзакции сериализует конкурирующие бронирования."""
    connection = connect_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        if not is_booking_time_open(day, slot_time):
            connection.rollback()
            return False
        slot = connection.execute(
            "SELECT id FROM slots WHERE slot_date = ? AND slot_time = ? AND booked_by IS NULL",
            (day.isoformat(), slot_time),
        ).fetchone()
        if slot is None:
            connection.rollback()
            return False
        changed = connection.execute(
            """UPDATE slots
               SET booked_by = ?, booked_name = ?, booked_username = ?, description = ?
               WHERE id = ? AND booked_by IS NULL""",
            (
                user_id,
                " ".join(part for part in (first_name, last_name) if part).strip() or None,
                username,
                description,
                slot["id"],
            ),
        ).rowcount
        if changed != 1:
            connection.rollback()
            return False
        connection.execute(
            "INSERT INTO bookings(slot_id, user_id) VALUES (?, ?)", (slot["id"], user_id)
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def book_entire_day(
    day: date,
    user_id: int,
    first_name: str,
    last_name: Optional[str],
    username: Optional[str],
    description: str,
) -> int:
    """Бронирует весь день только если каждый его слот ещё свободен."""
    connection = connect_db()
    client_name = " ".join(part for part in (first_name, last_name) if part).strip() or None
    try:
        connection.execute("BEGIN IMMEDIATE")
        slots = connection.execute(
            "SELECT id, slot_time, booked_by FROM slots WHERE slot_date = ? ORDER BY slot_time",
            (day.isoformat(),),
        ).fetchall()
        if not slots or any(
            slot["booked_by"] is not None or not is_booking_time_open(day, slot["slot_time"])
            for slot in slots
        ):
            connection.rollback()
            return 0

        connection.execute(
            """UPDATE slots
               SET booked_by = ?, booked_name = ?, booked_username = ?, description = ?
               WHERE slot_date = ? AND booked_by IS NULL""",
            (user_id, client_name, username, description, day.isoformat()),
        )
        connection.executemany(
            "INSERT INTO bookings(slot_id, user_id) VALUES (?, ?)",
            ((slot["id"], user_id) for slot in slots),
        )
        connection.commit()
        return len(slots)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def admin_book_slot(day: date, slot_time: str, description: str, importance: str) -> bool:
    """Администратор вручную помечает слот занятым/недоступным."""
    connection = connect_db()
    try:
        changed = connection.execute(
            """UPDATE slots SET booked_by = -1, description = ?, importance = ?,
                      booked_name = 'Администратор', booked_username = NULL
               WHERE slot_date = ? AND slot_time = ? AND booked_by IS NULL""",
            (description or None, importance, day.isoformat(), slot_time),
        ).rowcount
        return changed == 1
    finally:
        connection.close()


def update_slot_note(day: date, slot_time: str, description: str, importance: str) -> bool:
    connection = connect_db()
    try:
        changed = connection.execute(
            """UPDATE slots SET description = ?, importance = ?
               WHERE slot_date = ? AND slot_time = ? AND booked_by IS NOT NULL""",
            (description or None, importance, day.isoformat(), slot_time),
        ).rowcount
        return changed == 1
    finally:
        connection.close()


def release_slot(day: date, slot_time: str) -> bool:
    connection = connect_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        slot = connection.execute(
            "SELECT id FROM slots WHERE slot_date = ? AND slot_time = ? AND booked_by IS NOT NULL",
            (day.isoformat(), slot_time),
        ).fetchone()
        if slot is None:
            connection.rollback()
            return False
        connection.execute("DELETE FROM bookings WHERE slot_id = ?", (slot["id"],))
        connection.execute(
            "UPDATE slots SET booked_by = NULL, description = NULL, importance = 'обычная', booked_name = NULL, booked_username = NULL WHERE id = ?",
            (slot["id"],),
        )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def release_day(day: date) -> int:
    connection = connect_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        ids = [row["id"] for row in connection.execute(
            "SELECT id FROM slots WHERE slot_date = ? AND booked_by IS NOT NULL", (day.isoformat(),)
        )]
        if ids:
            connection.executemany("DELETE FROM bookings WHERE slot_id = ?", ((item,) for item in ids))
            connection.execute(
                "UPDATE slots SET booked_by = NULL, description = NULL, importance = 'обычная', booked_name = NULL, booked_username = NULL WHERE slot_date = ?",
                (day.isoformat(),),
            )
        connection.commit()
        return len(ids)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + month - 1 + delta
    return index // 12, index % 12 + 1


def parse_day(value: str) -> Optional[date]:
    try:
        parsed = date.fromisoformat(value)
        return parsed if parsed.isoformat() == value else None
    except ValueError:
        return None


def is_booking_time_open(day: date, slot_time: str) -> bool:
    if day < date.today():
        return False
    if day > date.today():
        return True
    try:
        return datetime.strptime(slot_time, "%H:%M").time() > datetime.now().time()
    except ValueError:
        return False


def month_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    counts = get_month_counts(year, month)
    today = date.today()
    rows = [[InlineKeyboardButton(text=name, callback_data="ignore") for name in WEEKDAYS]]
    for week in calendar.monthcalendar(year, month):
        row = []
        for number in week:
            if number == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
                continue
            current = date(year, month, number)
            free, total = counts.get(current, (0, 0))
            if current < today:
                label, callback = f"{number}·", "ignore"
            elif total == 0 or free == 0:
                label, callback = f"{number}❌", "ignore"
            elif free <= 2:
                label, callback = f"{number}🟡", f"day:{current.isoformat()}"
            else:
                label, callback = f"{number}🟢", f"day:{current.isoformat()}"
            row.append(InlineKeyboardButton(text=label, callback_data=callback))
        # В каждой неделе строго семь кнопок.
        rows.append(row)
    previous = shift_month(year, month, -1)
    following = shift_month(year, month, 1)
    rows.append([
        InlineKeyboardButton(text="⬅️ Пред.", callback_data=f"month:{previous[0]:04d}-{previous[1]:02d}"),
        InlineKeyboardButton(text="След. ➡️", callback_data=f"month:{following[0]:04d}-{following[1]:02d}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def start_keyboard(year: int, month: int, is_admin: bool) -> InlineKeyboardMarkup:
    rows = month_keyboard(year, month).inline_keyboard
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_reply_keyboard(is_admin: bool) -> ReplyKeyboardMarkup:
    button_text = "🗓 Управление календарём" if is_admin else "📅 Календарь"
    buttons = [[KeyboardButton(text=button_text)], [KeyboardButton(text="📖 Помощь")]]
    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие",
    )


def slots_keyboard(day: date) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []
    day_slots = get_day_slots(day)
    for slot in day_slots:
        slot_time = slot["slot_time"]
        slot_free = slot["booked_by"] is None
        slot_open = slot_free and is_booking_time_open(day, slot_time)
        if not slot_free:
            button_text = f"🔒 {slot_time}"
        elif slot_open:
            button_text = f"🕒 {slot_time}"
        else:
            button_text = f"⌛ {slot_time}"
        button = InlineKeyboardButton(
            text=button_text,
            callback_data=f"book:{day.isoformat()}:{slot_time}" if slot_open else "ignore",
        )
        current_row.append(button)
        if len(current_row) == SLOT_COLUMNS:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)
    if day_slots and all(
        slot["booked_by"] is None and is_booking_time_open(day, slot["slot_time"])
        for slot in day_slots
    ):
        rows.append([InlineKeyboardButton(
            text="📅 Забронировать весь день",
            callback_data=f"bookday:{day.isoformat()}",
        )])
    elif day_slots:
        rows.append([InlineKeyboardButton(text="🔒 Весь день уже недоступен", callback_data="ignore")])
    rows.append([InlineKeyboardButton(text="📅 К календарю", callback_data=f"back:{day.isoformat()}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def month_text(year: int, month: int) -> str:
    return (
        f"📅 {MONTH_TITLES[month - 1]} {year}\n"
        "🟢 свободно · 🟡 мало мест · ❌ занято · · прошло"
    )


def day_text(day: date) -> str:
    return f"🕒 Свободное время на {day.day} {MONTHS[day.month - 1]}"


def admin_only(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def importance_mark(value: str) -> str:
    return {"обычная": "▫️", "важная": "❗", "срочная": "🚨"}.get(value, "▫️")


def admin_day_report(day: date) -> str:
    slots = get_day_slots(day)
    busy = [slot for slot in slots if slot["booked_by"] is not None]
    lines = [f"📋 Занятость за {day.isoformat()}"]
    if not busy:
        lines.append("Занятых слотов нет.")
    for slot in busy:
        desc = slot["description"] or "описание не задано"
        if slot["booked_by"] == -1:
            owner = "администраторская блокировка"
        else:
            owner = slot["booked_name"] or "Клиент"
            if slot["booked_username"]:
                owner += f" (@{slot['booked_username']})"
        lines.append(
            f"{importance_mark(slot['importance'])} {slot['slot_time']} — {slot['importance']}\n"
            f"   {desc}\n   {owner}"
        )
    lines.append("\nКоманды: /busy ДАТА ВРЕМЯ ВАЖНОСТЬ описание; /importance ДАТА ВРЕМЯ обычная|важная|срочная; /release ДАТА ВРЕМЯ; /release_day ДАТА")
    return "\n".join(lines)


def parse_importance(value: str) -> Optional[str]:
    aliases = {"обычная": "обычная", "важная": "важная", "срочная": "срочная"}
    return aliases.get(value.casefold())


def update_slot_description(day: date, slot_time: str, description: str) -> bool:
    connection = connect_db()
    try:
        changed = connection.execute(
            "UPDATE slots SET description = ? WHERE slot_date = ? AND slot_time = ? AND booked_by IS NOT NULL",
            (description or None, day.isoformat(), slot_time),
        ).rowcount
        return changed == 1
    finally:
        connection.close()


def update_slot_importance(day: date, slot_time: str, importance: str) -> bool:
    connection = connect_db()
    try:
        changed = connection.execute(
            "UPDATE slots SET importance = ? WHERE slot_date = ? AND slot_time = ? AND booked_by IS NOT NULL",
            (importance, day.isoformat(), slot_time),
        ).rowcount
        return changed == 1
    finally:
        connection.close()


def admin_calendar_keyboard(year: int, month: int) -> InlineKeyboardMarkup:
    counts = get_month_counts(year, month)
    today = date.today()
    rows = [[InlineKeyboardButton(text=name, callback_data="ignore") for name in WEEKDAYS]]
    for week in calendar.monthcalendar(year, month):
        row = []
        for number in week:
            if number == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
                continue
            day = date(year, month, number)
            free, total = counts.get(day, (0, 0))
            busy = total - free
            status = "❌" if total == 0 or free == 0 else "🟡" if free <= 2 else "🟢"
            row.append(InlineKeyboardButton(
                text=f"{number}{status}",
                callback_data=f"admin:day:{day.isoformat()}",
            ))
        rows.append(row)
    prev_year, prev_month = shift_month(year, month, -1)
    next_year, next_month = shift_month(year, month, 1)
    rows.append([
        InlineKeyboardButton(text="⬅️ Пред.", callback_data=f"admin:month:{prev_year:04d}-{prev_month:02d}"),
        InlineKeyboardButton(text="Сегодня", callback_data=f"admin:day:{today.isoformat()}"),
        InlineKeyboardButton(text="След. ➡️", callback_data=f"admin:month:{next_year:04d}-{next_month:02d}"),
    ])
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="admin:panel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_day_keyboard(day: date) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for slot in get_day_slots(day):
        busy = slot["booked_by"] is not None
        icon = importance_mark(slot["importance"]) if busy else "➕"
        label = f"{icon} {slot['slot_time']}" + (" · занято" if busy else " · свободно")
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"admin:slot:{day.isoformat()}:{slot['slot_time']}",
        )])
    rows.append([InlineKeyboardButton(text="🔓 Освободить весь день", callback_data=f"admin:confirmday:{day.isoformat()}")])
    rows.append([InlineKeyboardButton(text="🔙 К выбору даты", callback_data=f"admin:month:{day:%Y-%m}")])
    rows.append([InlineKeyboardButton(text="⚙️ Команды", callback_data="admin:commands")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_day_text(day: date) -> str:
    slots = get_day_slots(day)
    busy = [slot for slot in slots if slot["booked_by"] is not None]
    lines = [f"📋 {day.isoformat()}", f"Занято: {len(busy)} из {len(slots)} слотов", ""]
    if busy:
        for slot in busy:
            lines.append(f"{importance_mark(slot['importance'])} {slot['slot_time']} — {slot['importance']}")
            lines.append(f"   {slot['description'] or 'Описание не задано'}")
    else:
        lines.append("Занятых слотов нет.")
    lines.extend(["", "Нажмите время для блокировки, просмотра или изменения записи."])
    return "\n".join(lines)


def get_slot(day: date, slot_time: str) -> Optional[sqlite3.Row]:
    connection = connect_db()
    try:
        return connection.execute(
            "SELECT slot_time, booked_by, description, importance, booked_name, booked_username FROM slots WHERE slot_date = ? AND slot_time = ?",
            (day.isoformat(), slot_time),
        ).fetchone()
    finally:
        connection.close()


def save_slot_profile(day: date, slot_time: str, name: str, username: Optional[str]) -> None:
    connection = connect_db()
    try:
        connection.execute(
            "UPDATE slots SET booked_name = ?, booked_username = ? WHERE slot_date = ? AND slot_time = ? AND booked_by > 0",
            (name or None, username, day.isoformat(), slot_time),
        )
    finally:
        connection.close()


def client_profile_html(slot: sqlite3.Row) -> str:
    user_id = int(slot["booked_by"])
    if user_id < 0:
        return "👤 Администраторская блокировка"
    full_name = (slot["booked_name"] or "Клиент").strip()
    username = slot["booked_username"]
    profile_url = f"https://t.me/{username}" if username else f"tg://user?id={user_id}"
    result = f'👤 <a href="{profile_url}">{html.escape(full_name)}</a>'
    if username:
        safe_username = html.escape(username, quote=True)
        result += f'\n🔗 <a href="https://t.me/{safe_username}">@{safe_username}</a>'
    else:
        result += "\n🔗 @username не указан · <a href=\"" + profile_url + "\">Открыть профиль</a>"
    return result


def admin_slot_text(day: date, slot_time: str, slot: sqlite3.Row) -> str:
    if slot["booked_by"] is None:
        return f"🟢 {day.isoformat()} в {slot_time}\nСлот свободен. Его можно закрыть для записи."
    return (
        f"🔒 {day.isoformat()} в {slot_time}\n"
        f"Важность: {slot['importance']}\n"
        f"Описание: {html.escape(slot['description'] or 'не задано')}\n"
        f"{client_profile_html(slot)}\n\nВыберите действие:"
    )


def admin_slot_keyboard(day: date, slot_time: str, busy: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if busy:
        rows.append([InlineKeyboardButton(text="📝 Изменить описание", callback_data=f"admin:desc:{day.isoformat()}:{slot_time}")])
        rows.extend([
            [InlineKeyboardButton(text=f"{importance_mark(value)} {value.title()}", callback_data=f"admin:importance:{day.isoformat()}:{slot_time}:{value}")]
            for value in ("обычная", "важная", "срочная")
        ])
        rows.append([InlineKeyboardButton(text="🧹 Освободить слот", callback_data=f"admin:confirmrelease:{day.isoformat()}:{slot_time}")])
    else:
        rows.append([InlineKeyboardButton(text="⛔ Отметить занятым", callback_data=f"admin:block:{day.isoformat()}:{slot_time}")])
    rows.append([InlineKeyboardButton(text="🔙 К списку времени", callback_data=f"admin:day:{day.isoformat()}")])
    rows.append([InlineKeyboardButton(text="⚙️ Команды", callback_data="admin:commands")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def edit_calendar(callback: CallbackQuery, year: int, month: int) -> None:
    await callback.message.edit_text(month_text(year, month), reply_markup=month_keyboard(year, month))


async def edit_slots(callback: CallbackQuery, day: date) -> None:
    await callback.message.edit_text(day_text(day), reply_markup=slots_keyboard(day))


async def ignore_handler(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    today = date.today()
    user_id = message.from_user.id
    is_admin = admin_only(user_id)
    if message.from_user:
        logger.info("Получен /start от Telegram user_id=%s", user_id)
    old_screen = CALENDAR_SCREEN_IDS.get(user_id)
    screen_message = None
    if old_screen:
        try:
            await message.bot.edit_message_text(
                chat_id=old_screen[0],
                message_id=old_screen[1],
                text=month_text(today.year, today.month),
                reply_markup=start_keyboard(today.year, today.month, is_admin),
            )
            screen_message = old_screen
        except TelegramBadRequest:
            pass
    if screen_message is None:
        sent = await message.answer(
            month_text(today.year, today.month),
            reply_markup=start_keyboard(today.year, today.month, is_admin),
        )
        screen_message = (sent.chat.id, sent.message_id)
    CALENDAR_SCREEN_IDS[user_id] = screen_message

    if user_id not in KEYBOARD_SET_USER_IDS:
        await message.answer(
            "\u2063",
            reply_markup=main_reply_keyboard(is_admin),
        )
        KEYBOARD_SET_USER_IDS.add(user_id)


@router.message(F.text == "📅 Календарь")
@router.message(F.text == "/calendar")
async def calendar_menu_button(message: Message) -> None:
    today = date.today()
    old_screen = CALENDAR_SCREEN_IDS.get(message.from_user.id)
    if old_screen:
        try:
            await message.bot.edit_message_text(
                chat_id=old_screen[0], message_id=old_screen[1],
                text=month_text(today.year, today.month),
                reply_markup=month_keyboard(today.year, today.month),
            )
            return
        except TelegramBadRequest:
            pass
    sent = await message.answer(month_text(today.year, today.month), reply_markup=month_keyboard(today.year, today.month))
    CALENDAR_SCREEN_IDS[message.from_user.id] = (sent.chat.id, sent.message_id)


@router.message(F.text == "🗓 Управление календарём")
@router.message(F.text == "🛠 Админ-панель")  # совместимость со старой клавиатурой до следующего /start
async def admin_menu_button(message: Message) -> None:
    if not admin_only(message.from_user.id):
        await message.answer("У вас нет доступа к админ-панели.")
        return
    await show_admin_home(message)


@router.message(F.text == "⚙️ Команды")
@router.message(F.text == "/commands")
async def admin_commands_button(message: Message) -> None:
    if not admin_only(message.from_user.id):
        return
    await show_admin_commands(message)


@router.message(F.text == "/today")
async def admin_today_command(message: Message) -> None:
    if not admin_only(message.from_user.id):
        await message.answer("Команда доступна только администраторам.")
        return
    today = date.today()
    old_screen = ADMIN_SCREEN_IDS.get(message.from_user.id)
    if old_screen:
        try:
            await message.bot.edit_message_text(
                chat_id=old_screen[0],
                message_id=old_screen[1],
                text=admin_day_text(today),
                reply_markup=admin_day_keyboard(today),
            )
            return
        except TelegramBadRequest:
            pass
    sent = await message.answer(admin_day_text(today), reply_markup=admin_day_keyboard(today))
    ADMIN_SCREEN_IDS[message.from_user.id] = (sent.chat.id, sent.message_id)


async def show_admin_home(target: Message | CallbackQuery) -> None:
    text = "🗓 Управление календарём\n\nВыберите действие:"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📆 Расписание и занятость", callback_data=f"admin:month:{date.today():%Y-%m}")],
        [InlineKeyboardButton(text="📋 Занятость сегодня", callback_data=f"admin:day:{date.today().isoformat()}")],
        [InlineKeyboardButton(text="⚙️ Команды", callback_data="admin:commands")],
    ])
    if isinstance(target, CallbackQuery):
        ADMIN_SCREEN_IDS[target.from_user.id] = (target.message.chat.id, target.message.message_id)
        await target.message.edit_text(text, reply_markup=keyboard)
    else:
        old_screen = ADMIN_SCREEN_IDS.get(target.from_user.id)
        if old_screen:
            try:
                await target.bot.edit_message_text(
                    chat_id=old_screen[0], message_id=old_screen[1], text=text, reply_markup=keyboard
                )
                return
            except TelegramBadRequest:
                pass
        sent = await target.answer(text, reply_markup=keyboard)
        ADMIN_SCREEN_IDS[target.from_user.id] = (sent.chat.id, sent.message_id)


async def show_admin_commands(target: Message | CallbackQuery) -> None:
    text = "⚙️ Команды управления\nВыберите действие, затем дату и время кнопками:"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Настроить сегодня", callback_data="admin:cmd:today")],
        [InlineKeyboardButton(text="📆 Выбрать дату", callback_data="admin:cmd:calendar")],
        [InlineKeyboardButton(text="⛔ Закрыть свободный слот", callback_data="admin:cmd:block")],
        [InlineKeyboardButton(text="✏️ Описание и важность", callback_data="admin:cmd:edit")],
        [InlineKeyboardButton(text="🔓 Освободить слот или день", callback_data="admin:cmd:release")],
        [InlineKeyboardButton(text="🏠 Управление календарём", callback_data="admin:panel")],
    ])
    if isinstance(target, CallbackQuery):
        ADMIN_SCREEN_IDS[target.from_user.id] = (target.message.chat.id, target.message.message_id)
        await target.message.edit_text(text, reply_markup=keyboard)
        return
    old_screen = ADMIN_SCREEN_IDS.get(target.from_user.id)
    if old_screen:
        try:
            await target.bot.edit_message_text(
                chat_id=old_screen[0], message_id=old_screen[1], text=text, reply_markup=keyboard
            )
            return
        except TelegramBadRequest:
            pass
    sent = await target.answer(text, reply_markup=keyboard)
    ADMIN_SCREEN_IDS[target.from_user.id] = (sent.chat.id, sent.message_id)


@router.callback_query(F.data == "admin:panel")
async def admin_panel_callback(callback: CallbackQuery) -> None:
    if not admin_only(callback.from_user.id):
        await callback.answer(f"Ваш Telegram ID: {callback.from_user.id}", show_alert=True)
        await callback.message.answer(
            f"Ваш Telegram ID: `{callback.from_user.id}`\n"
            "Передайте его владельцу бота для добавления в ADMIN_IDS, затем нажмите /start.",
            parse_mode="Markdown",
        )
        return
    await callback.answer()
    await show_admin_home(callback)


async def require_admin(callback: CallbackQuery) -> bool:
    if not admin_only(callback.from_user.id):
        await callback.answer("Доступ только администраторам.", show_alert=True)
        return False
    return True


@router.callback_query(F.data == "admin:commands")
async def admin_commands_callback(callback: CallbackQuery) -> None:
    if not await require_admin(callback):
        return
    await callback.answer()
    await show_admin_commands(callback)


@router.callback_query(F.data.startswith("admin:cmd:"))
async def admin_command_action(callback: CallbackQuery) -> None:
    if not await require_admin(callback):
        return
    action = callback.data.removeprefix("admin:cmd:")
    if action == "today":
        selected = date.today()
        await callback.answer()
        await callback.message.edit_text(
            admin_day_text(selected), reply_markup=admin_day_keyboard(selected)
        )
        return
    titles = {
        "calendar": "Выберите дату для просмотра и настройки слотов.",
        "block": "Закрытие времени: выберите дату, затем свободный слот.",
        "edit": "Изменение записи: выберите дату, затем занятый слот.",
        "release": "Освобождение времени: выберите дату и занятый слот или весь день.",
    }
    if action not in titles:
        await callback.answer("Неизвестная команда.", show_alert=True)
        return
    await callback.answer()
    today = date.today()
    await callback.message.edit_text(
        titles[action],
        reply_markup=admin_calendar_keyboard(today.year, today.month),
    )


@router.callback_query(F.data.startswith("admin:month:"))
async def admin_month_callback(callback: CallbackQuery) -> None:
    if not await require_admin(callback):
        return
    value = callback.data.removeprefix("admin:month:")
    try:
        parsed = datetime.strptime(value, "%Y-%m")
        if parsed.strftime("%Y-%m") != value:
            raise ValueError
    except ValueError:
        await callback.answer("Некорректный месяц.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "📆 Выберите дату. 🟢 свободно · 🟡 мало мест · ❌ занято:",
        reply_markup=admin_calendar_keyboard(parsed.year, parsed.month),
    )


@router.callback_query(F.data.startswith("admin:day:"))
async def admin_day_callback(callback: CallbackQuery) -> None:
    if not await require_admin(callback):
        return
    selected = parse_day(callback.data.removeprefix("admin:day:"))
    if selected is None:
        await callback.answer("Некорректная дата.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(admin_day_text(selected), reply_markup=admin_day_keyboard(selected))


@router.callback_query(F.data.startswith("admin:slot:"))
async def admin_slot_callback(callback: CallbackQuery) -> None:
    if not await require_admin(callback):
        return
    PENDING_ADMIN_DESCRIPTION.pop(callback.from_user.id, None)
    parts = callback.data.removeprefix("admin:slot:").split(":", 1)
    if len(parts) != 2:
        await callback.answer("Некорректный слот.", show_alert=True)
        return
    selected = parse_day(parts[0])
    slot_time = parts[1]
    slot = await asyncio.to_thread(get_slot, selected, slot_time) if selected else None
    if slot is None:
        await callback.answer("Слот не найден.", show_alert=True)
        await callback.message.edit_text("Слот не найден.")
        return
    await callback.answer()
    busy = slot["booked_by"] is not None
    if busy:
        if slot["booked_by"] > 0 and (not slot["booked_name"] or not slot["booked_username"]):
            try:
                profile = await callback.bot.get_chat(slot["booked_by"])
                profile_name = " ".join(part for part in (profile.first_name, profile.last_name) if part).strip()
                profile_username = profile.username
                if profile_name or profile_username:
                    await asyncio.to_thread(save_slot_profile, selected, slot_time, profile_name, profile_username)
                    slot = await asyncio.to_thread(get_slot, selected, slot_time)
            except (TelegramBadRequest, TelegramNetworkError):
                logger.info("Профиль клиента %s недоступен через Telegram API", slot["booked_by"])
            slot = await asyncio.to_thread(get_slot, selected, slot_time)
    text = admin_slot_text(selected, slot_time, slot)
    await callback.message.edit_text(
        text,
        reply_markup=admin_slot_keyboard(selected, slot_time, busy),
        parse_mode="HTML" if busy else None,
        link_preview_options=LinkPreviewOptions(is_disabled=True) if busy else None,
    )


@router.callback_query(F.data.startswith("admin:block:"))
async def admin_block_callback(callback: CallbackQuery) -> None:
    if not await require_admin(callback):
        return
    parts = callback.data.removeprefix("admin:block:").split(":", 1)
    selected = parse_day(parts[0]) if len(parts) == 2 else None
    if selected is None:
        await callback.answer("Некорректный слот.", show_alert=True)
        return
    ok = await asyncio.to_thread(admin_book_slot, selected, parts[1], "Заблокировано администратором", "обычная")
    if not ok:
        await callback.answer("Слот уже занят или не найден.", show_alert=True)
        return
    await callback.answer("Слот заблокирован")
    await callback.message.edit_text(
        f"🔒 {selected.isoformat()} в {parts[1]}\nСлот закрыт для клиентов. Добавьте описание или измените важность.",
        reply_markup=admin_slot_keyboard(selected, parts[1], True),
    )


@router.callback_query(F.data.startswith("admin:importance:"))
async def admin_importance_callback(callback: CallbackQuery) -> None:
    if not await require_admin(callback):
        return
    parts = callback.data.removeprefix("admin:importance:").split(":")
    if len(parts) != 4:
        await callback.answer("Некорректный запрос.", show_alert=True)
        return
    selected = parse_day(parts[0])
    slot_time = parts[1] + ":" + parts[2]
    importance = parse_importance(parts[3])
    if not selected or not importance:
        await callback.answer("Некорректная дата или важность.", show_alert=True)
        return
    await callback.answer()
    await asyncio.to_thread(update_slot_importance, selected, slot_time, importance)
    slot = await asyncio.to_thread(get_slot, selected, slot_time)
    if slot is None or slot["booked_by"] is None:
        await callback.message.edit_text("Слот уже освободили.")
        return
    client_info = client_profile_html(slot)
    await callback.message.edit_text(
        f"🔒 {selected.isoformat()} в {slot_time}\nВажность: {slot['importance']}\n"
        f"Описание: {html.escape(slot['description'] or 'не задано')}\n{client_info}\n\nВыберите действие:",
        reply_markup=admin_slot_keyboard(selected, slot_time, True),
        parse_mode="HTML",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.callback_query(F.data.startswith("admin:confirmrelease:"))
async def admin_confirm_release_callback(callback: CallbackQuery) -> None:
    if not await require_admin(callback):
        return
    parts = callback.data.removeprefix("admin:confirmrelease:").split(":", 1)
    selected = parse_day(parts[0]) if len(parts) == 2 else None
    if not selected:
        await callback.answer("Некорректный слот.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        f"Снять занятость {selected.isoformat()} в {parts[1]}? Слот снова станет доступен клиентам.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Да, освободить", callback_data=f"admin:release:{selected.isoformat()}:{parts[1]}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"admin:slot:{selected.isoformat()}:{parts[1]}")],
        ]),
    )


@router.callback_query(F.data.startswith("admin:release:"))
async def admin_release_callback(callback: CallbackQuery) -> None:
    if not await require_admin(callback):
        return
    parts = callback.data.removeprefix("admin:release:").split(":", 1)
    selected = parse_day(parts[0]) if len(parts) == 2 else None
    if not selected:
        await callback.answer("Некорректный слот.", show_alert=True)
        return
    await callback.answer()
    await asyncio.to_thread(release_slot, selected, parts[1])
    await callback.message.edit_text(
        f"✅ Слот {parts[1]} освобождён. Он снова доступен для бронирования.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 К расписанию дня", callback_data=f"admin:day:{selected.isoformat()}")
        ]]),
    )


@router.callback_query(F.data.startswith("admin:confirmday:"))
async def admin_confirm_release_day(callback: CallbackQuery) -> None:
    if not await require_admin(callback):
        return
    value = callback.data.removeprefix("admin:confirmday:")
    selected = parse_day(value)
    if not selected:
        await callback.answer("Некорректная дата.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        f"Освободить все занятые слоты за {selected.isoformat()}?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Да, освободить день", callback_data=f"admin:releaseall:{value}")],
            [InlineKeyboardButton(text="Отмена", callback_data=f"admin:day:{value}")],
        ]),
    )


@router.callback_query(F.data.startswith("admin:releaseall:"))
async def admin_release_day_callback(callback: CallbackQuery) -> None:
    if not await require_admin(callback):
        return
    selected = parse_day(callback.data.removeprefix("admin:releaseall:"))
    if not selected:
        await callback.answer("Некорректная дата.", show_alert=True)
        return
    await callback.answer()
    count = await asyncio.to_thread(release_day, selected)
    await callback.message.edit_text(
        f"✅ Освобождено слотов: {count}.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 К расписанию дня", callback_data=f"admin:day:{selected.isoformat()}")
        ]]),
    )


@router.callback_query(F.data.startswith("admin:desc:"))
async def admin_description_prompt(callback: CallbackQuery) -> None:
    if not await require_admin(callback):
        return
    parts = callback.data.removeprefix("admin:desc:").split(":", 1)
    selected = parse_day(parts[0]) if len(parts) == 2 else None
    if not selected:
        await callback.answer("Некорректный слот.", show_alert=True)
        return
    await callback.answer()
    PENDING_ADMIN_DESCRIPTION[callback.from_user.id] = (
        selected.isoformat(), parts[1], callback.message.chat.id, callback.message.message_id
    )
    await callback.message.edit_text(
        f"📝 Отправьте новое описание для {selected.isoformat()} в {parts[1]} отдельным сообщением.\n"
        "Для отмены нажмите кнопку ниже.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Назад к записи", callback_data=f"admin:slot:{selected.isoformat()}:{parts[1]}")
        ]]),
    )


@router.message(F.text == "/cancel")
async def cancel_admin_description(message: Message) -> None:
    booking = PENDING_BOOKING_DESCRIPTION.pop(message.from_user.id, None)
    admin_edit = PENDING_ADMIN_DESCRIPTION.pop(message.from_user.id, None)
    if booking:
        day_value, _, chat_id, screen_id, prompt_id = booking
        selected = parse_day(day_value)
        await delete_message_quietly(message.bot, chat_id, prompt_id)
        if selected:
            try:
                await message.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=screen_id,
                    text=month_text(selected.year, selected.month),
                    reply_markup=month_keyboard(selected.year, selected.month),
                )
            except TelegramBadRequest:
                pass
    elif admin_edit:
        day_value, slot_time, chat_id, screen_id = admin_edit
        selected = parse_day(day_value)
        slot = await asyncio.to_thread(get_slot, selected, slot_time) if selected else None
        if selected and slot:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=screen_id,
                text=admin_slot_text(selected, slot_time, slot),
                reply_markup=admin_slot_keyboard(selected, slot_time, slot["booked_by"] is not None),
                parse_mode="HTML" if slot["booked_by"] is not None else None,
                link_preview_options=LinkPreviewOptions(is_disabled=True) if slot["booked_by"] is not None else None,
            )
    await delete_message_quietly(message.bot, message.chat.id, message.message_id)


@router.message(F.text & ~F.text.startswith("/") & (F.text != "📖 Помощь") & (F.text != "📚 Помощь") & (F.text != "Помощь"))
async def save_admin_description(message: Message) -> None:
    booking = PENDING_BOOKING_DESCRIPTION.get(message.from_user.id)
    if booking:
        day_value, slot_time, chat_id, message_id, prompt_id = booking
        description = (message.text or "").strip()
        if not description:
            await message.answer("Описание обязательно. Отправьте его текстом или /cancel для отмены.")
            await delete_message_quietly(message.bot, message.chat.id, message.message_id)
            return
        if len(description) > MAX_BOOKING_DESCRIPTION_LENGTH:
            await message.answer(
                f"Описание слишком длинное. Максимум {MAX_BOOKING_DESCRIPTION_LENGTH} символов."
            )
            await delete_message_quietly(message.bot, message.chat.id, message.message_id)
            return
        selected = parse_day(day_value)
        if selected is None or selected < date.today():
            PENDING_BOOKING_DESCRIPTION.pop(message.from_user.id, None)
            await message.answer("Дата уже недоступна. Начните запись заново через /start.")
            await delete_message_quietly(message.bot, message.chat.id, message.message_id)
            await delete_message_quietly(message.bot, chat_id, prompt_id)
            return
        try:
            if slot_time is None:
                count = await asyncio.to_thread(
                    book_entire_day,
                    selected,
                    message.from_user.id,
                    message.from_user.first_name,
                    message.from_user.last_name,
                    message.from_user.username,
                    description,
                )
                success = count > 0
            else:
                success = await asyncio.to_thread(
                    book_slot,
                    selected,
                    slot_time,
                    message.from_user.id,
                    message.from_user.first_name,
                    message.from_user.last_name,
                    message.from_user.username,
                    description,
                )
                count = 1 if success else 0
        except sqlite3.Error:
            logger.exception("Не удалось сохранить бронирование с описанием")
            await message.answer("Не удалось сохранить запись. Попробуйте ещё раз или отправьте /cancel.")
            await delete_message_quietly(message.bot, message.chat.id, message.message_id)
            return

        PENDING_BOOKING_DESCRIPTION.pop(message.from_user.id, None)
        if success:
            if slot_time is None:
                confirmation = (
                    f"✅ Забронирован весь день: {selected.isoformat()}\n"
                    f"Временных слотов: {count}\nОписание: {description}\n\n"
                    f"{month_text(selected.year, selected.month)}"
                )
            else:
                confirmation = (
                    f"✅ Вы записаны на {selected.day} {MONTHS[selected.month - 1]} в {slot_time}.\n"
                    f"Описание: {description}\n\n{month_text(selected.year, selected.month)}"
                )
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=confirmation,
                reply_markup=month_keyboard(selected.year, selected.month),
            )
        else:
            try:
                await message.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=(
                        "⚠️ Слот или часть дня уже заняли. Обновил календарь.\n\n"
                        f"{month_text(selected.year, selected.month)}"
                    ),
                    reply_markup=month_keyboard(selected.year, selected.month),
                )
            except TelegramBadRequest:
                logger.info("Не удалось обновить экран слотов после конфликта")
        await delete_message_quietly(message.bot, chat_id, prompt_id)
        await delete_message_quietly(message.bot, message.chat.id, message.message_id)
        return

    pending = PENDING_ADMIN_DESCRIPTION.get(message.from_user.id)
    if not pending or not admin_only(message.from_user.id):
        return
    day_value, slot_time, chat_id, screen_id = pending
    selected = parse_day(day_value)
    if selected:
        await asyncio.to_thread(update_slot_description, selected, slot_time, message.text.strip())
        slot = await asyncio.to_thread(get_slot, selected, slot_time)
        if slot:
            await message.bot.edit_message_text(
                chat_id=chat_id,
                message_id=screen_id,
                text=admin_slot_text(selected, slot_time, slot),
                reply_markup=admin_slot_keyboard(selected, slot_time, slot["booked_by"] is not None),
                parse_mode="HTML" if slot["booked_by"] is not None else None,
                link_preview_options=LinkPreviewOptions(is_disabled=True) if slot["booked_by"] is not None else None,
            )
    PENDING_ADMIN_DESCRIPTION.pop(message.from_user.id, None)
    await delete_message_quietly(message.bot, message.chat.id, message.message_id)


@router.message(F.text == "/myid")
async def my_id_handler(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@router.message(F.text == "/help")
@router.message(F.text == "📖 Помощь")
@router.message(F.text == "📚 Помощь")
@router.message(F.text == "Помощь")
async def user_help(message: Message) -> None:
    await message.answer(
        "📖 <b>Руководство пользователя</b>\nВыберите нужный раздел:",
        reply_markup=guide_keyboard(message.from_user.id),
        parse_mode="HTML",
    )


def guide_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="📗 Инструкция клиента", callback_data="guide:client")]]
    if admin_only(user_id):
        rows.append([InlineKeyboardButton(text="🛠 Инструкция администратора", callback_data="guide:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def guide_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ К разделам руководства", callback_data="guide:menu")
    ]])


CLIENT_GUIDE = (
    "📗 <b>Инструкция для клиента</b>\n"
    "1. Отправьте /start и выберите свободный день.\n"
    "2. Нажмите доступное время 🕒 или «Забронировать весь день», если все слоты свободны.\n"
    "3. Отправьте обязательное описание бронирования (до 500 символов).\n"
    "4. Дождитесь подтверждения. Для отмены ввода отправьте /cancel.\n\n"
    "В календаре: 🟢 — свободно, 🟡 — осталось мало мест, ❌ — мест нет, · — прошедший день. Полностью занятый день выбрать нельзя.\n"
    "Если слот одновременно выбрал другой человек, бот сообщит об этом и обновит расписание."
)

ADMIN_GUIDE = (
    "🛠 <b>Инструкция администратора</b>\n"
    "Нажмите нижнюю кнопку «Управление календарём». Выберите расписание или занятость сегодня, затем дату и слот. Красные и полностью занятые даты тоже открываются для настроек. Кнопка «Сегодня» сразу открывает настройки текущей даты.\n"
    "Карточка показывает клиента, описание и важность. Здесь можно изменить описание или важность, закрыть свободное время, освободить слот или день с подтверждением.\n\n"
    "Клиенты могут бронировать отдельное время или весь день, только если все слоты на этот день свободны; описание обязательно.\n\n"
    "Административные действия находятся в нижней кнопке «Команды». Выберите операцию и следуйте кнопкам даты и времени. Для выхода из ввода описания используйте «Отмена»."
)


@router.callback_query(F.data == "guide:menu")
async def guide_menu_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(
        "📖 <b>Руководство пользователя</b>\nВыберите нужный раздел:",
        reply_markup=guide_keyboard(callback.from_user.id),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "guide:client")
async def guide_client_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.edit_text(CLIENT_GUIDE, reply_markup=guide_back_keyboard(), parse_mode="HTML")


@router.callback_query(F.data == "guide:admin")
async def guide_admin_callback(callback: CallbackQuery) -> None:
    if not admin_only(callback.from_user.id):
        await callback.answer("Раздел доступен только администраторам.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(ADMIN_GUIDE, reply_markup=guide_back_keyboard(), parse_mode="HTML")


@router.message(F.text.startswith("/admin"))
async def admin_help(message: Message) -> None:
    if not admin_only(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return
    await show_admin_home(message)


@router.message(F.text.startswith("/day"))
async def admin_day(message: Message) -> None:
    if not admin_only(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return
    parts = (message.text or "").split()
    selected = parse_day(parts[1]) if len(parts) == 2 else None
    if selected is None:
        await message.answer("Формат: /day YYYY-MM-DD")
        return
    await message.answer(await asyncio.to_thread(admin_day_report, selected))


@router.message(F.text.startswith("/busy"))
async def admin_busy(message: Message) -> None:
    if not admin_only(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return
    parts = (message.text or "").split(maxsplit=4)
    if len(parts) < 4:
        await message.answer("Формат: /busy YYYY-MM-DD HH:MM обычная|важная|срочная описание")
        return
    selected = parse_day(parts[1])
    importance = parse_importance(parts[3])
    try:
        valid_time = datetime.strptime(parts[2], "%H:%M").strftime("%H:%M") == parts[2]
    except ValueError:
        valid_time = False
    if selected is None or not valid_time or importance is None:
        await message.answer("Проверьте дату, время и важность. Важность: обычная, важная или срочная.")
        return
    ok = await asyncio.to_thread(admin_book_slot, selected, parts[2], parts[4] if len(parts) > 4 else "", importance)
    await message.answer("Слот отмечен занятым.\n" + await asyncio.to_thread(admin_day_report, selected) if ok else "Слот не найден или уже занят.")


@router.message(F.text.startswith("/importance"))
async def admin_importance(message: Message) -> None:
    if not admin_only(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return
    parts = (message.text or "").split(maxsplit=4)
    if len(parts) < 4:
        await message.answer("Формат: /importance YYYY-MM-DD HH:MM обычная|важная|срочная описание")
        return
    selected = parse_day(parts[1])
    importance = parse_importance(parts[3])
    try:
        valid_time = datetime.strptime(parts[2], "%H:%M").strftime("%H:%M") == parts[2]
    except ValueError:
        valid_time = False
    if selected is None or not valid_time or importance is None:
        await message.answer("Проверьте дату, время и важность.")
        return
    ok = await asyncio.to_thread(update_slot_note, selected, parts[2], parts[4] if len(parts) > 4 else "", importance)
    await message.answer("Пометка обновлена.\n" + await asyncio.to_thread(admin_day_report, selected) if ok else "Занятый слот не найден.")


@router.message(F.text.startswith("/release_day"))
async def admin_release_day(message: Message) -> None:
    if not admin_only(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return
    parts = (message.text or "").split()
    selected = parse_day(parts[1]) if len(parts) == 2 else None
    if selected is None:
        await message.answer("Формат: /release_day YYYY-MM-DD")
        return
    count = await asyncio.to_thread(release_day, selected)
    await message.answer(f"Освобождено слотов: {count}.\n" + await asyncio.to_thread(admin_day_report, selected))


@router.message(F.text.startswith("/release"))
async def admin_release(message: Message) -> None:
    if not admin_only(message.from_user.id):
        await message.answer("Команда доступна только администратору.")
        return
    parts = (message.text or "").split()
    selected = parse_day(parts[1]) if len(parts) == 3 else None
    try:
        valid_time = len(parts) == 3 and datetime.strptime(parts[2], "%H:%M").strftime("%H:%M") == parts[2]
    except ValueError:
        valid_time = False
    if selected is None or not valid_time:
        await message.answer("Формат: /release YYYY-MM-DD HH:MM")
        return
    ok = await asyncio.to_thread(release_slot, selected, parts[2])
    await message.answer("Слот освобождён.\n" + await asyncio.to_thread(admin_day_report, selected) if ok else "Занятый слот не найден.")


@router.callback_query(F.data == "ignore")
async def ignore_callback(callback: CallbackQuery) -> None:
    await ignore_handler(callback)


@router.callback_query(F.data.startswith("month:"))
async def month_handler(callback: CallbackQuery) -> None:
    value = callback.data.removeprefix("month:")
    try:
        parsed = datetime.strptime(value, "%Y-%m")
        if parsed.strftime("%Y-%m") != value:
            raise ValueError
    except ValueError:
        await callback.answer("Некорректный месяц.", show_alert=True)
        return
    await callback.answer()
    try:
        await edit_calendar(callback, parsed.year, parsed.month)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


@router.callback_query(F.data.startswith("day:"))
async def day_handler(callback: CallbackQuery) -> None:
    selected = parse_day(callback.data.removeprefix("day:"))
    if selected is None or selected < date.today():
        await callback.answer("Эту дату выбрать нельзя.", show_alert=True)
        return
    slots = await asyncio.to_thread(get_day_slots, selected)
    if not any(
        slot["booked_by"] is None and is_booking_time_open(selected, slot["slot_time"])
        for slot in slots
    ):
        await callback.answer("На этот день свободных мест нет.", show_alert=True)
        return
    await callback.answer()
    await edit_slots(callback, selected)


@router.callback_query(F.data.startswith("back:"))
async def back_handler(callback: CallbackQuery) -> None:
    selected = parse_day(callback.data.removeprefix("back:"))
    if selected is None:
        await callback.answer("Некорректная дата.", show_alert=True)
        return
    await callback.answer()
    try:
        await edit_calendar(callback, selected.year, selected.month)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            raise


async def prompt_booking_description(
    callback: CallbackQuery,
    selected: date,
    slot_time: Optional[str],
) -> None:
    await callback.answer()
    target = f"слот {slot_time}" if slot_time else "весь день"
    prompt = await callback.message.answer(
        f"Чтобы забронировать {target} на {selected.isoformat()}, укажите описание.\n"
        f"Описание обязательно, максимум {MAX_BOOKING_DESCRIPTION_LENGTH} символов.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔙 Назад к времени" if slot_time else "🔙 Назад к календарю",
                callback_data="booking:back",
            )],
            [InlineKeyboardButton(text="✖ Отменить запись", callback_data="booking:cancel")],
        ]),
    )
    PENDING_BOOKING_DESCRIPTION[callback.from_user.id] = (
        selected.isoformat(),
        slot_time,
        callback.message.chat.id,
        callback.message.message_id,
        prompt.message_id,
    )


@router.callback_query(F.data.in_({"booking:back", "booking:cancel"}))
async def booking_prompt_navigation(callback: CallbackQuery) -> None:
    """Возвращает пользователя на предыдущий экран и удаляет вопрос об описании."""
    pending = PENDING_BOOKING_DESCRIPTION.pop(callback.from_user.id, None)
    if pending is None:
        await callback.answer("Этот шаг уже завершён.", show_alert=True)
        return
    day_value, slot_time, chat_id, screen_id, prompt_id = pending
    selected = parse_day(day_value)
    await callback.answer()
    await delete_message_quietly(callback.bot, chat_id, prompt_id)
    if selected is None:
        return
    if callback.data == "booking:back" and slot_time is not None:
        text = day_text(selected)
        markup = slots_keyboard(selected)
    else:
        text = month_text(selected.year, selected.month)
        markup = month_keyboard(selected.year, selected.month)
    try:
        await callback.bot.edit_message_text(
            chat_id=chat_id, message_id=screen_id, text=text, reply_markup=markup
        )
    except TelegramBadRequest:
        # Если исходный календарь уже недоступен, показываем экран заново.
        sent = await callback.bot.send_message(chat_id, text, reply_markup=markup)
        CALENDAR_SCREEN_IDS[callback.from_user.id] = (sent.chat.id, sent.message_id)
@router.callback_query(F.data.startswith("bookday:"))
async def book_day_handler(callback: CallbackQuery) -> None:
    selected = parse_day(callback.data.removeprefix("bookday:"))
    if selected is None or selected < date.today():
        await callback.answer("Эту дату уже нельзя забронировать.", show_alert=True)
        return
    slots = await asyncio.to_thread(get_day_slots, selected)
    if not slots or any(
        slot["booked_by"] is not None or not is_booking_time_open(selected, slot["slot_time"])
        for slot in slots
    ):
        await callback.answer(
            "Весь день можно забронировать, только пока все слоты свободны.",
            show_alert=True,
        )
        try:
            await edit_slots(callback, selected)
        except TelegramBadRequest:
            pass
        return
    await prompt_booking_description(callback, selected, None)


@router.callback_query(F.data.startswith("book:"))
async def book_handler(callback: CallbackQuery) -> None:
    # Дата и время разделены двоеточиями; ограничиваем split на два поля.
    parts = callback.data.removeprefix("book:").split(":", 2)
    if len(parts) != 3:
        await callback.answer("Некорректный запрос.", show_alert=True)
        return
    selected = parse_day(parts[0])
    slot_time = parts[1] + ":" + parts[2]
    try:
        parsed_time = datetime.strptime(slot_time, "%H:%M")
        if parsed_time.strftime("%H:%M") != slot_time:
            raise ValueError
    except ValueError:
        await callback.answer("Некорректное время.", show_alert=True)
        return
    if selected is None or not is_booking_time_open(selected, slot_time):
        await callback.answer("Эту дату уже нельзя забронировать.", show_alert=True)
        return
    slot = await asyncio.to_thread(get_slot, selected, slot_time)
    if slot is None or slot["booked_by"] is not None:
        await callback.answer("Этот слот уже занят. Выберите другое время.", show_alert=True)
        try:
            await edit_slots(callback, selected)
        except TelegramBadRequest:
            pass
        return
    await prompt_booking_description(callback, selected, slot_time)


async def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "put_your_bot_token_here":
        raise RuntimeError("Укажите настоящий BOT_TOKEN в файле .env")
    await asyncio.to_thread(init_db)
    cleared = await asyncio.to_thread(clear_legacy_demo_bookings)
    if cleared:
        logger.info("Снято старых тестовых броней: %d", cleared)
    bot = Bot(token=BOT_TOKEN)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    logger.info("Бот запускается; выполняю подключение к Telegram")
    try:
        delay = 2
        while True:
            try:
                await bot.get_me()
                await configure_bot_menu(bot)
                logger.info("Подключение к Telegram установлено")
                delay = 2
                await dispatcher.start_polling(bot, handle_signals=False)
                # start_polling обычно возвращает управление после корректного
                # stop; не запускаем busy loop при неожиданном возврате.
                logger.warning("Polling завершился; повторный запуск через %s сек.", delay)
                await asyncio.sleep(delay)
            except (TelegramNetworkError, OSError, socket.gaierror) as error:
                logger.warning("Telegram недоступен (%s); повтор через %s сек.", error, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
