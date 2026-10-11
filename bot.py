"""
Telegram-бот записи к мастеру.
Клиент выбирает услугу, день и время, оставляет имя и телефон.
Мастер (администратор) получает уведомление о каждой новой записи и отмене.
"""

import asyncio
import logging
import os
import socket
import sqlite3
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = "bookings.db"

# Услуги: id -> (название, цена)
SERVICES = {
    "1": ("Стрижка мужская", 1500),
    "2": ("Стрижка женская", 2500),
    "3": ("Окрашивание", 4500),
    "4": ("Укладка", 1800),
}

WORK_START = 10          # начало рабочего дня (часы)
WORK_END = 19            # конец рабочего дня (последняя запись в 18:00)
DAYS_AHEAD = 7           # на сколько дней вперёд можно записаться
DAYS_OFF = {6}           # выходные: 0 = пн ... 6 = вс
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

MENU_BOOK = "📅 Записаться"
MENU_MY = "📋 Мои записи"
MENU_CONTACTS = "📍 Контакты"

# ---------------------------------------------------------------------------
# База данных (SQLite — отдельный сервер не нужен)
# ---------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                name      TEXT NOT NULL,
                phone     TEXT NOT NULL,
                service   TEXT NOT NULL,
                day       TEXT NOT NULL,
                time      TEXT NOT NULL,
                created   TEXT NOT NULL,
                UNIQUE(day, time)
            )
            """
        )


def busy_times(day: str) -> set[str]:
    with db() as conn:
        rows = conn.execute("SELECT time FROM bookings WHERE day = ?", (day,)).fetchall()
    return {r["time"] for r in rows}


def add_booking(user_id, name, phone, service, day, time) -> int | None:
    """Возвращает id записи или None, если время уже занято."""
    try:
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO bookings (user_id, name, phone, service, day, time, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, name, phone, service, day, time, datetime.now().isoformat()),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def user_bookings(user_id: int):
    today = date.today().isoformat()
    with db() as conn:
        return conn.execute(
            "SELECT * FROM bookings WHERE user_id = ? AND day >= ? ORDER BY day, time",
            (user_id, today),
        ).fetchall()


def upcoming_bookings():
    today = date.today().isoformat()
    with db() as conn:
        return conn.execute(
            "SELECT * FROM bookings WHERE day >= ? ORDER BY day, time", (today,)
        ).fetchall()


def get_booking(booking_id: int):
    with db() as conn:
        return conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()


def delete_booking(booking_id: int):
    with db() as conn:
        conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def fmt_day(day_iso: str) -> str:
    d = date.fromisoformat(day_iso)
    return f"{WEEKDAYS[d.weekday()]} {d.strftime('%d.%m')}"


def available_days() -> list[str]:
    days = []
    for i in range(DAYS_AHEAD + 1):
        d = date.today() + timedelta(days=i)
        if d.weekday() not in DAYS_OFF and free_slots(d.isoformat()):
            days.append(d.isoformat())
    return days


def free_slots(day_iso: str) -> list[str]:
    taken = busy_times(day_iso)
    now = datetime.now()
    slots = []
    for hour in range(WORK_START, WORK_END):
        t = f"{hour:02d}:00"
        slot_dt = datetime.fromisoformat(f"{day_iso}T{t}")
        if slot_dt > now and t not in taken:
            slots.append(t)
    return slots


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MENU_BOOK)],
            [KeyboardButton(text=MENU_MY), KeyboardButton(text=MENU_CONTACTS)],
        ],
        resize_keyboard=True,
    )


def booking_text(b) -> str:
    return (
        f"💇 {b['service']}\n"
        f"🗓 {fmt_day(b['day'])} в {b['time']}\n"
        f"👤 {b['name']}, {b['phone']}"
    )


# ---------------------------------------------------------------------------
# Сценарий записи
# ---------------------------------------------------------------------------
class Booking(StatesGroup):
    service = State()
    day = State()
    time = State()
    name = State()
    phone = State()
    confirm = State()


router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        f"Здравствуйте, {message.from_user.first_name}! 👋\n\n"
        "Я помогу записаться к мастеру. Выберите действие в меню ниже.",
        reply_markup=main_menu(),
    )


@router.message(F.text == MENU_CONTACTS)
async def contacts(message: Message):
    await message.answer(
        "📍 Адрес: ул. Примерная, 1\n"
        "🕙 Работаем: пн–сб, 10:00–19:00\n"
        "📞 +7 (900) 000-00-00"
    )


@router.message(F.text == MENU_BOOK)
async def choose_service(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardBuilder()
    for sid, (name, price) in SERVICES.items():
        kb.button(text=f"{name} — {price} ₽", callback_data=f"svc:{sid}")
    kb.adjust(1)
    await message.answer("Выберите услугу:", reply_markup=kb.as_markup())
    await state.set_state(Booking.service)


@router.callback_query(Booking.service, F.data.startswith("svc:"))
async def choose_day(call: CallbackQuery, state: FSMContext):
    sid = call.data.split(":", 1)[1]
    await state.update_data(service=SERVICES[sid][0])

    days = available_days()
    if not days:
        await call.message.edit_text("К сожалению, свободных дней на ближайшую неделю нет.")
        await state.clear()
        return

    kb = InlineKeyboardBuilder()
    for d in days:
        kb.button(text=fmt_day(d), callback_data=f"day:{d}")
    kb.adjust(3)
    await call.message.edit_text(
        f"Услуга: {SERVICES[sid][0]}\n\nВыберите день:", reply_markup=kb.as_markup()
    )
    await state.set_state(Booking.day)
    await call.answer()


@router.callback_query(Booking.day, F.data.startswith("day:"))
async def choose_time(call: CallbackQuery, state: FSMContext):
    day = call.data.split(":", 1)[1]
    slots = free_slots(day)
    if not slots:
        await call.answer("На этот день всё занято, выберите другой", show_alert=True)
        return
    await state.update_data(day=day)

    kb = InlineKeyboardBuilder()
    for t in slots:
        kb.button(text=t, callback_data=f"time:{t}")
    kb.adjust(4)
    await call.message.edit_text(
        f"День: {fmt_day(day)}\n\nВыберите время:", reply_markup=kb.as_markup()
    )
    await state.set_state(Booking.time)
    await call.answer()


@router.callback_query(Booking.time, F.data.startswith("time:"))
async def ask_name(call: CallbackQuery, state: FSMContext):
    t = call.data.split(":", 1)[1]
    await state.update_data(time=t)
    await call.message.edit_text(f"Время: {t}")
    await call.message.answer("Как вас зовут?")
    await state.set_state(Booking.name)
    await call.answer()


@router.message(Booking.name, F.text)
async def ask_phone(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:50])
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(
        "Оставьте номер телефона: нажмите кнопку ниже или напишите его вручную.",
        reply_markup=kb,
    )
    await state.set_state(Booking.phone)


@router.message(Booking.phone, F.contact | F.text)
async def confirm(message: Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text.strip()
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 10:
        await message.answer("Похоже, номер неполный. Попробуйте ещё раз.")
        return
    await state.update_data(phone=phone)
    data = await state.get_data()

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data="confirm")
    kb.button(text="❌ Отменить", callback_data="abort")
    await message.answer("Проверьте данные:", reply_markup=main_menu())
    await message.answer(booking_text(data), reply_markup=kb.as_markup())
    await state.set_state(Booking.confirm)


@router.callback_query(Booking.confirm, F.data == "confirm")
async def save(call: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    booking_id = add_booking(
        call.from_user.id, data["name"], data["phone"], data["service"], data["day"], data["time"]
    )
    await state.clear()

    if booking_id is None:
        await call.message.edit_text(
            "Увы, это время только что заняли. Нажмите «📅 Записаться», чтобы выбрать другое."
        )
        await call.answer()
        return

    await call.message.edit_text("✅ Вы записаны!\n\n" + booking_text(data) + "\n\nЖдём вас!")
    await call.answer()

    if ADMIN_ID:
        username = f"@{call.from_user.username}" if call.from_user.username else "без username"
        await bot.send_message(
            ADMIN_ID, f"🔔 Новая запись #{booking_id} ({username})\n\n" + booking_text(data)
        )


@router.callback_query(Booking.confirm, F.data == "abort")
async def abort(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Запись отменена.")
    await call.answer()


# ---------------------------------------------------------------------------
# Мои записи и отмена
# ---------------------------------------------------------------------------
@router.message(F.text == MENU_MY)
async def my_bookings(message: Message, state: FSMContext):
    await state.clear()
    rows = user_bookings(message.from_user.id)
    if not rows:
        await message.answer("У вас пока нет записей.")
        return
    for b in rows:
        kb = InlineKeyboardBuilder()
        kb.button(text="Отменить запись", callback_data=f"del:{b['id']}")
        await message.answer(booking_text(b), reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("del:"))
async def cancel_booking(call: CallbackQuery, bot: Bot):
    booking_id = int(call.data.split(":", 1)[1])
    b = get_booking(booking_id)
    if not b or b["user_id"] != call.from_user.id:
        await call.answer("Запись не найдена", show_alert=True)
        return
    delete_booking(booking_id)
    await call.message.edit_text("🗑 Запись отменена:\n\n" + booking_text(b))
    await call.answer()
    if ADMIN_ID:
        await bot.send_message(ADMIN_ID, f"⚠️ Клиент отменил запись #{booking_id}\n\n" + booking_text(b))


# ---------------------------------------------------------------------------
# Команда для мастера
# ---------------------------------------------------------------------------
@router.message(Command("admin"))
async def admin_list(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    rows = upcoming_bookings()
    if not rows:
        await message.answer("Предстоящих записей нет.")
        return
    lines = [f"#{b['id']} · {fmt_day(b['day'])} {b['time']} — {b['service']}, {b['name']}, {b['phone']}"
             for b in rows]
    await message.answer("📋 Предстоящие записи:\n\n" + "\n".join(lines))


# Если пользователь пишет что-то вне сценария
@router.message()
async def fallback(message: Message):
    await message.answer("Выберите действие в меню 👇", reply_markup=main_menu())


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
def find_proxy() -> str | None:
    """Прокси из .env (PROXY=...) или системный прокси Windows/Mac (его ставят многие VPN)."""
    proxy = os.getenv("PROXY")
    if not proxy:
        import urllib.request
        sys_proxies = urllib.request.getproxies()
        proxy = sys_proxies.get("https") or sys_proxies.get("http")
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = "http://" + proxy
    if proxy.startswith("https://"):
        proxy = "http://" + proxy[len("https://"):]
    if proxy.startswith("socks://"):
        proxy = "socks5://" + proxy[len("socks://"):]
    return proxy


def make_session() -> AiohttpSession:
    proxy = find_proxy()
    if proxy:
        print(f"Подключение через прокси: {proxy}")
        return AiohttpSession(proxy=proxy)
    session = AiohttpSession()
    # Подключаемся только по IPv4: на части сетей и VPN IPv6 «висит» до таймаута
    session._connector_init["family"] = socket.AF_INET
    print("Подключение напрямую (IPv4)")
    return session


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не найден BOT_TOKEN. Создайте файл .env по образцу .env.example")
    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(BOT_TOKEN, session=make_session())
    dp = Dispatcher()
    dp.include_router(router)
    print("Бот запущен. Остановить: Ctrl+C")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
