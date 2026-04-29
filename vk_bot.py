"""
VK-бот для учёбы: расписание, заметки, напоминания, уведомления о парах.

Переменные окружения (.env):
    VK_TOKEN — токен сообщества ВКонтакте (настройки сообщества → API → ключ доступа)

Запуск: python vk_bot.py
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import random
import re
import sqlite3
import os

from dotenv import load_dotenv
from vkbottle import Bot, Keyboard, Text
from vkbottle.bot import Message

load_dotenv()

VK_TOKEN = os.getenv("VK_TOKEN", "")
bot = Bot(token=VK_TOKEN)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

# ── Московское время ──────────────────────────────────────────────────────────
_MSK = datetime.timezone(datetime.timedelta(hours=3))


def now_msk() -> datetime.datetime:
    return datetime.datetime.now(_MSK).replace(tzinfo=None)


def current_week_type() -> str:
    """Возвращает 'чёт' для чётной ISO-недели, 'нечет' для нечётной."""
    return "чёт" if now_msk().isocalendar()[1] % 2 == 0 else "нечет"


# ── Состояния пользователей ───────────────────────────────────────────────────
user_states: dict = {}


# ── Вспомогательная функция для клавиатур ────────────────────────────────────
def _kb(*rows: list[str], one_time: bool = False) -> str:
    """Строит JSON-клавиатуру VK из списка строк с кнопками."""
    kb = Keyboard(one_time=one_time)
    for i, row in enumerate(rows):
        if i > 0:
            kb.row()
        for label in row:
            kb.add(Text(label[:40]))
    return kb.get_json()


# ── Стандартные клавиатуры ────────────────────────────────────────────────────
MAIN_KB = _kb(
    ["📅 Расписание"],
    ["📝 Добавить заметку", "📋 Мои заметки"],
    ["⏰ Напоминание", "🔔 Подписки на пары"],
    ["❓ Помощь"],
)

WEEK_KB = _kb(
    ["📅 Чётная неделя", "📅 Нечётная неделя"],
    ["◀ Назад"],
)

DAY_KB = _kb(
    ["Понедельник", "Вторник"],
    ["Среда", "Четверг"],
    ["Пятница", "Суббота"],
    ["◀ Назад"],
)

CANCEL_KB = _kb(["❌ Отмена"], one_time=True)
BACK_KB = _kb(["◀ Назад"])

_DAYS = {"Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"}


# ── База данных расписания (s.db) ─────────────────────────────────────────────
def load_directions() -> dict[int, list[str]]:
    conn = sqlite3.connect("s.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course INTEGER, direction TEXT, day TEXT,
            time TEXT, subject TEXT, teacher TEXT, room TEXT
        )
    """)
    conn.commit()
    cur.execute("SELECT DISTINCT course, direction FROM schedule ORDER BY course")
    result: dict[int, list[str]] = {}
    for course, direction in cur.fetchall():
        cleaned = re.sub(r",?\s*\d+\s*курс.*", "", direction).strip()
        result.setdefault(course, [])
        if cleaned not in result[course]:
            result[course].append(cleaned)
    conn.close()
    return result


directions_by_course = load_directions()


def course_kb() -> str:
    rows = [[str(c)] for c in sorted(directions_by_course)]
    rows.append(["◀ Назад"])
    return _kb(*rows)


def direction_kb(course: int) -> str:
    rows = [[d[:40]] for d in directions_by_course[course]]
    rows.append(["◀ Назад"])
    return _kb(*rows)


def get_schedule(course: int, direction: str, day: str, week_type: str) -> str:
    """
    Возвращает расписание, отфильтрованное по типу недели.
    week_type='чёт'   → показывает общие пары и пары чётной недели ([чёт]).
    week_type='нечет' → показывает общие пары и пары нечётной недели ([нечет]).
    """
    conn = sqlite3.connect("s.db")
    cur = conn.cursor()
    # Исключаем пары противоположной недели
    exclude_prefix = "[нечет]%" if week_type == "чёт" else "[чёт]%"
    cur.execute(
        """
        SELECT time, subject, teacher, room FROM schedule
        WHERE course = ? AND LOWER(direction) = LOWER(?) AND LOWER(day) = LOWER(?)
          AND subject NOT LIKE ?
        ORDER BY CAST(SUBSTR(time, 1, INSTR(time, ' ') - 1) AS INTEGER), time
        """,
        (course, direction, day, exclude_prefix),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return "На этот день пар нет."

    lines = []
    for time_s, subject, teacher, room in rows:
        subject = re.sub(r"^\[(чёт|нечет)\]\s*", "", subject)
        info = ", ".join(filter(None, [teacher, room]))
        lines.append(f"{time_s} — {subject}" + (f" ({info})" if info else ""))
    return "\n".join(lines)


# ── База данных заметок, напоминаний, подписок (notes.db) ────────────────────
def create_tables() -> None:
    conn = sqlite3.connect("notes.db")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, note_text TEXT, timestamp TEXT
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, reminder_text TEXT,
            remind_at TEXT, notified INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, course INTEGER, direction TEXT
        );
        CREATE TABLE IF NOT EXISTS sent_class_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, schedule_row_id INTEGER,
            class_date TEXT, class_time TEXT, sent_at TEXT
        );
    """)
    conn.commit()
    conn.close()


# Заметки
def add_note(uid: int, text: str) -> None:
    conn = sqlite3.connect("notes.db")
    conn.execute(
        "INSERT INTO notes (user_id, note_text, timestamp) VALUES (?,?,?)",
        (uid, text, datetime.datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


def get_notes(uid: int) -> list:
    conn = sqlite3.connect("notes.db")
    rows = conn.execute(
        "SELECT id, note_text, timestamp FROM notes WHERE user_id=? ORDER BY id DESC",
        (uid,),
    ).fetchall()
    conn.close()
    return rows


def _renumber_notes() -> None:
    conn = sqlite3.connect("notes.db")
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT user_id, note_text, timestamp FROM notes ORDER BY id"
    ).fetchall()
    cur.execute("DELETE FROM notes")
    cur.executemany(
        "INSERT INTO notes (user_id, note_text, timestamp) VALUES (?,?,?)", rows
    )
    conn.commit()
    conn.close()


def delete_notes(ids: list[int]) -> None:
    if not ids:
        return
    conn = sqlite3.connect("notes.db")
    conn.executemany("DELETE FROM notes WHERE id=?", ((i,) for i in ids))
    conn.commit()
    conn.close()
    _renumber_notes()


# Напоминания
def add_reminder(uid: int, text: str, remind_at: str) -> None:
    conn = sqlite3.connect("notes.db")
    conn.execute(
        "INSERT INTO reminders (user_id, reminder_text, remind_at, notified) VALUES (?,?,?,0)",
        (uid, text, remind_at),
    )
    conn.commit()
    conn.close()


def get_pending_reminders() -> list:
    conn = sqlite3.connect("notes.db")
    rows = conn.execute(
        "SELECT id, user_id, reminder_text, remind_at FROM reminders WHERE notified=0"
    ).fetchall()
    conn.close()
    return rows


def mark_reminder_sent(rid: int) -> None:
    conn = sqlite3.connect("notes.db")
    conn.execute("UPDATE reminders SET notified=1 WHERE id=?", (rid,))
    conn.commit()
    conn.close()


# Подписки
def add_subscription(uid: int, course: int, direction: str) -> None:
    conn = sqlite3.connect("notes.db")
    conn.execute(
        "INSERT INTO subscriptions (user_id, course, direction) VALUES (?,?,?)",
        (uid, course, direction),
    )
    conn.commit()
    conn.close()


def get_user_subs(uid: int) -> list:
    conn = sqlite3.connect("notes.db")
    rows = conn.execute(
        "SELECT id, course, direction FROM subscriptions WHERE user_id=? ORDER BY id",
        (uid,),
    ).fetchall()
    conn.close()
    return rows


def get_all_subs() -> list:
    conn = sqlite3.connect("notes.db")
    rows = conn.execute(
        "SELECT id, user_id, course, direction FROM subscriptions"
    ).fetchall()
    conn.close()
    return rows


def delete_sub(sid: int) -> None:
    conn = sqlite3.connect("notes.db")
    conn.execute("DELETE FROM subscriptions WHERE id=?", (sid,))
    conn.commit()
    conn.close()


def _notif_sent(uid: int, row_id: int, date: str, time: str) -> bool:
    conn = sqlite3.connect("notes.db")
    exists = conn.execute(
        "SELECT 1 FROM sent_class_notifications "
        "WHERE user_id=? AND schedule_row_id=? AND class_date=? AND class_time=?",
        (uid, row_id, date, time),
    ).fetchone()
    conn.close()
    return exists is not None


def _mark_notif(uid: int, row_id: int, date: str, time: str) -> None:
    conn = sqlite3.connect("notes.db")
    conn.execute(
        "INSERT INTO sent_class_notifications "
        "(user_id, schedule_row_id, class_date, class_time, sent_at) VALUES (?,?,?,?,?)",
        (uid, row_id, date, time, datetime.datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


# ── Отправка сообщения пользователю ──────────────────────────────────────────
async def _send(uid: int, text: str) -> None:
    await bot.api.messages.send(
        user_id=uid,
        message=text,
        random_id=random.randint(1, 2**31),
    )


# ── Фоновые задачи ────────────────────────────────────────────────────────────
async def reminder_checker() -> None:
    """Каждые 30 секунд проверяет и отправляет просроченные напоминания."""
    while True:
        await asyncio.sleep(30)
        now = now_msk()
        for rid, uid, text, remind_at_str in get_pending_reminders():
            try:
                remind_at = datetime.datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            if remind_at <= now:
                try:
                    await _send(uid, f"⏰ Напоминание: {text}")
                    mark_reminder_sent(rid)
                except Exception:
                    logging.exception(f"Не удалось отправить напоминание id={rid}")


async def class_notification_checker() -> None:
    """Каждые 30 секунд проверяет подписки и рассылает уведомления за 10 мин до пары."""
    ru_days = {
        "Monday": "Понедельник", "Tuesday": "Вторник", "Wednesday": "Среда",
        "Thursday": "Четверг", "Friday": "Пятница",
        "Saturday": "Суббота", "Sunday": "Воскресенье",
    }
    notify_before = datetime.timedelta(minutes=10)

    while True:
        await asyncio.sleep(30)
        now = now_msk()
        db_day = ru_days.get(now.strftime("%A"), now.strftime("%A"))
        week = current_week_type()
        exclude_prefix = "[нечет]%" if week == "чёт" else "[чёт]%"

        for _sid, uid, course, direction in get_all_subs():
            conn = sqlite3.connect("s.db")
            rows = conn.execute(
                "SELECT rowid, time, subject, teacher, room FROM schedule "
                "WHERE course=? AND LOWER(direction)=LOWER(?) "
                "AND LOWER(day)=LOWER(?) AND subject NOT LIKE ?",
                (course, direction, db_day, exclude_prefix),
            ).fetchall()
            conn.close()

            for row_id, time_field, subject, teacher, room in rows:
                m = re.search(r"(\d{1,2}[:.]\d{2})", time_field or "")
                if not m:
                    continue
                try:
                    hhmm = datetime.datetime.strptime(
                        m.group(1).replace(".", ":"), "%H:%M"
                    ).time()
                except ValueError:
                    continue

                class_start = datetime.datetime.combine(now.date(), hhmm)
                class_end = class_start + datetime.timedelta(minutes=90)

                if class_end < now - datetime.timedelta(minutes=1):
                    continue

                start_str = class_start.strftime("%H:%M")
                date_str = class_start.date().isoformat()
                subj_clean = re.sub(r"^\[(чёт|нечет)\]\s*", "", subject)

                if class_start - notify_before <= now < class_start:
                    if not _notif_sent(uid, row_id, date_str, start_str):
                        try:
                            await _send(
                                uid,
                                f"📚 Через 10 минут начнётся пара!\n"
                                f"• {subj_clean}\n"
                                f"• Преподаватель: {teacher}\n"
                                f"• Аудитория: {room}\n"
                                f"• Начало: {start_str}",
                            )
                            _mark_notif(uid, row_id, date_str, start_str)
                        except Exception:
                            logging.exception("Не удалось отправить уведомление о паре")


# ── Обработчик сообщений ──────────────────────────────────────────────────────
@bot.on.message()
async def handle(message: Message) -> None:
    uid = message.from_id
    text = (message.text or "").strip()
    state = user_states.get(uid)

    # ── Приветствие / главное меню ────────────────────────────────────────────
    if not text or text.lower() in ("начать", "старт", "/start", "start", "меню", "главное меню"):
        user_states.pop(uid, None)
        await message.answer(
            "👋 Привет! Я бот-помощник для учёбы.\n\n"
            "Я помогу тебе:\n"
            "📅 Посмотреть расписание пар\n"
            "📝 Сохранить заметки\n"
            "⏰ Создать напоминание\n"
            "🔔 Получать уведомления о начале пар\n\n"
            "Выбери нужный раздел:",
            keyboard=MAIN_KB,
        )
        return

    # ── РАСПИСАНИЕ ────────────────────────────────────────────────────────────
    if text == "📅 Расписание":
        if not directions_by_course:
            await message.answer(
                "База расписания пуста. Обратитесь к администратору.",
                keyboard=MAIN_KB,
            )
            return
        user_states[uid] = {"step": "week"}
        wt = current_week_type()
        hint = "чётная" if wt == "чёт" else "нечётная"
        await message.answer(
            f"Сейчас идёт {hint} неделя.\nВыбери тип недели:",
            keyboard=WEEK_KB,
        )
        return

    if isinstance(state, dict) and state.get("step") == "week":
        if text == "◀ Назад":
            user_states.pop(uid, None)
            await message.answer("Главное меню:", keyboard=MAIN_KB)
            return
        if "Чётная" in text or "Нечётная" in text:
            week_type = "чёт" if "Чётная" in text else "нечет"
            state.update(step="course", week_type=week_type)
            await message.answer("Выбери курс:", keyboard=course_kb())
            return
        return

    if isinstance(state, dict) and state.get("step") == "course":
        if text == "◀ Назад":
            state["step"] = "week"
            wt = current_week_type()
            hint = "чётная" if wt == "чёт" else "нечётная"
            await message.answer(
                f"Сейчас идёт {hint} неделя.\nВыбери тип недели:",
                keyboard=WEEK_KB,
            )
            return
        if text.isdigit() and int(text) in directions_by_course:
            state.update(step="direction", course=int(text))
            await message.answer("Выбери направление:", keyboard=direction_kb(int(text)))
            return
        await message.answer("Выбери курс из кнопок ниже:", keyboard=course_kb())
        return

    if isinstance(state, dict) and state.get("step") == "direction":
        course = state["course"]
        if text == "◀ Назад":
            state["step"] = "course"
            await message.answer("Выбери курс:", keyboard=course_kb())
            return
        if text in directions_by_course.get(course, []):
            state.update(step="day", direction=text)
            await message.answer("Выбери день недели:", keyboard=DAY_KB)
            return
        await message.answer("Выбери направление из кнопок:", keyboard=direction_kb(course))
        return

    if isinstance(state, dict) and state.get("step") == "day":
        course = state["course"]
        direction = state["direction"]
        week_type = state["week_type"]
        if text == "◀ Назад":
            state.update(step="direction")
            await message.answer("Выбери направление:", keyboard=direction_kb(course))
            return
        if text in _DAYS:
            sched = get_schedule(course, direction, text, week_type)
            wlabel = "чётная" if week_type == "чёт" else "нечётная"
            user_states.pop(uid, None)
            await message.answer(
                f"📅 {text} ({wlabel} неделя)\n"
                f"{course} курс · {direction}\n\n"
                f"{sched}",
                keyboard=MAIN_KB,
            )
            return
        await message.answer("Выбери день из кнопок:", keyboard=DAY_KB)
        return

    # ── ЗАМЕТКИ ───────────────────────────────────────────────────────────────
    if text == "📝 Добавить заметку":
        user_states[uid] = "add_note"
        await message.answer("✏️ Введи текст заметки:", keyboard=CANCEL_KB)
        return

    if state == "add_note":
        if text in ("❌ Отмена", "Отмена"):
            user_states.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return
        if text:
            add_note(uid, text)
            user_states.pop(uid, None)
            await message.answer("✅ Заметка сохранена!", keyboard=MAIN_KB)
        else:
            await message.answer("Текст не может быть пустым. Попробуй ещё раз:", keyboard=CANCEL_KB)
        return

    if text == "📋 Мои заметки":
        notes = get_notes(uid)
        if not notes:
            await message.answer("У тебя пока нет заметок.", keyboard=MAIN_KB)
            return
        ans = "📋 Твои заметки:\n\n"
        for nid, note_text, ts in notes:
            ans += f"[{nid}] {note_text}\n📅 {ts}\n\n"
        ans += "Введи ID заметки для удаления (можно несколько через запятую).\nИли нажми «◀ Назад»."
        user_states[uid] = "view_notes"
        await message.answer(ans, keyboard=BACK_KB)
        return

    if state == "view_notes":
        if text == "◀ Назад":
            user_states.pop(uid, None)
            await message.answer("Главное меню:", keyboard=MAIN_KB)
            return
        parts = re.split(r"[\s,]+", text)
        try:
            ids = [int(p) for p in parts if p.strip()]
        except ValueError:
            await message.answer(
                "Введи номер(а) заметки цифрами или нажми «◀ Назад».", keyboard=BACK_KB
            )
            return
        if not ids:
            await message.answer(
                "Введи номер(а) заметки цифрами или нажми «◀ Назад».", keyboard=BACK_KB
            )
            return
        notes = get_notes(uid)
        available = {n[0] for n in notes}
        to_del = [i for i in ids if i in available]
        not_found = [i for i in ids if i not in available]
        if to_del:
            delete_notes(to_del)
        resp_parts = []
        if to_del:
            resp_parts.append(f"✅ Удалены заметки: {', '.join(map(str, to_del))}")
        if not_found:
            resp_parts.append(f"❌ Не найдены: {', '.join(map(str, not_found))}")
        remaining = get_notes(uid)
        if remaining:
            rem = "\nОставшиеся заметки:\n\n"
            for nid, nt, ts in remaining:
                rem += f"[{nid}] {nt}\n📅 {ts}\n\n"
            resp_parts.append(rem)
        else:
            resp_parts.append("Заметок больше нет.")
        user_states.pop(uid, None)
        await message.answer("\n".join(resp_parts), keyboard=MAIN_KB)
        return

    # ── НАПОМИНАНИЯ ───────────────────────────────────────────────────────────
    if text == "⏰ Напоминание":
        user_states[uid] = {"step": "rem_text"}
        await message.answer("✏️ Введи текст напоминания:", keyboard=CANCEL_KB)
        return

    if isinstance(state, dict) and state.get("step") == "rem_text":
        if text in ("❌ Отмена", "Отмена"):
            user_states.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return
        state.update(step="rem_time", reminder_text=text)
        await message.answer(
            "📅 Теперь введи дату и время:\n"
            "Формат: ГГГГ-ММ-ДД ЧЧ:ММ\n"
            "Пример: 2025-09-01 09:00",
            keyboard=CANCEL_KB,
        )
        return

    if isinstance(state, dict) and state.get("step") == "rem_time":
        if text in ("❌ Отмена", "Отмена"):
            user_states.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return
        try:
            datetime.datetime.strptime(text, "%Y-%m-%d %H:%M")
        except ValueError:
            await message.answer(
                "Неверный формат. Попробуй ещё раз.\nПример: 2025-09-01 09:00",
                keyboard=CANCEL_KB,
            )
            return
        rem_text = state["reminder_text"]
        add_reminder(uid, rem_text, text)
        user_states.pop(uid, None)
        await message.answer(
            f"✅ Напоминание создано!\n📝 {rem_text}\n📅 {text}",
            keyboard=MAIN_KB,
        )
        return

    # ── ПОДПИСКИ НА ПАРЫ ──────────────────────────────────────────────────────
    if text == "🔔 Подписки на пары":
        subs = get_user_subs(uid)
        if subs:
            ans = "🔔 Твои подписки на уведомления о парах:\n\n"
            for sid, course, direction in subs:
                ans += f"[{sid}] {course} курс — {direction}\n"
            ans += "\nВведи ID для удаления подписки, или добавь новую."
        else:
            ans = (
                "У тебя пока нет подписок.\n\n"
                "Добавь подписку — и я буду напоминать о каждой паре за 10 минут."
            )
        user_states[uid] = "subs"
        await message.answer(ans, keyboard=_kb(["➕ Добавить подписку"], ["◀ Назад"]))
        return

    if state == "subs":
        if text == "◀ Назад":
            user_states.pop(uid, None)
            await message.answer("Главное меню:", keyboard=MAIN_KB)
            return
        if text == "➕ Добавить подписку":
            user_states[uid] = {"step": "sub_course"}
            await message.answer("Выбери курс:", keyboard=course_kb())
            return
        if text.isdigit():
            delete_sub(int(text))
            user_states.pop(uid, None)
            await message.answer(f"✅ Подписка {text} удалена.", keyboard=MAIN_KB)
            return
        await message.answer(
            "Введи ID подписки для удаления или нажми кнопку.",
            keyboard=_kb(["➕ Добавить подписку"], ["◀ Назад"]),
        )
        return

    if isinstance(state, dict) and state.get("step") == "sub_course":
        if text == "◀ Назад":
            user_states[uid] = "subs"
            await message.answer(
                "Управление подписками:",
                keyboard=_kb(["➕ Добавить подписку"], ["◀ Назад"]),
            )
            return
        if text.isdigit() and int(text) in directions_by_course:
            state.update(step="sub_direction", course=int(text))
            await message.answer("Выбери направление:", keyboard=direction_kb(int(text)))
            return
        await message.answer("Выбери курс из кнопок:", keyboard=course_kb())
        return

    if isinstance(state, dict) and state.get("step") == "sub_direction":
        course = state["course"]
        if text == "◀ Назад":
            state["step"] = "sub_course"
            await message.answer("Выбери курс:", keyboard=course_kb())
            return
        if text in directions_by_course.get(course, []):
            add_subscription(uid, course, text)
            user_states.pop(uid, None)
            await message.answer(
                f"✅ Подписка добавлена!\n{course} курс — {text}\n\n"
                "Буду напоминать о каждой паре за 10 минут до начала.",
                keyboard=MAIN_KB,
            )
            return
        await message.answer("Выбери направление из кнопок:", keyboard=direction_kb(course))
        return

    # ── ПОМОЩЬ ────────────────────────────────────────────────────────────────
    if text == "❓ Помощь":
        await message.answer(
            "📖 Краткое руководство:\n\n"
            "📅 Расписание\n"
            "Нажми «Расписание» → выбери тип недели (чётная / нечётная) "
            "→ курс → направление → день.\n\n"
            "📝 Заметки\n"
            "«Добавить заметку» — введи текст, бот сохранит.\n"
            "«Мои заметки» — список с номерами; введи номер (или несколько через запятую) для удаления.\n\n"
            "⏰ Напоминания\n"
            "«Напоминание» → введи текст → введи дату и время (ГГГГ-ММ-ДД ЧЧ:ММ).\n"
            "В указанное время придёт сообщение.\n\n"
            "🔔 Подписки на пары\n"
            "«Подписки на пары» → добавь курс и направление.\n"
            "Бот будет напоминать за 10 минут до начала каждой пары.\n\n"
            "Если что-то пошло не так — напиши «Меню» для возврата в главное меню.",
            keyboard=MAIN_KB,
        )
        return

    # ── Неизвестный ввод ──────────────────────────────────────────────────────
    await message.answer(
        "Не понимаю эту команду. Воспользуйся кнопками меню:",
        keyboard=MAIN_KB,
    )


# ── Запуск ────────────────────────────────────────────────────────────────────
def main() -> None:
    create_tables()
    bot.loop_wrapper.add_task(reminder_checker())
    bot.loop_wrapper.add_task(class_notification_checker())
    bot.run_forever()


if __name__ == "__main__":
    main()
