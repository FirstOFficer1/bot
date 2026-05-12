"""
VK-бот для учёбы: расписание, заметки, напоминания, уведомления о парах.

Переменные окружения (.env):
    VK_TOKEN — токен сообщества ВКонтакте (настройки сообщества → API → ключ доступа)

Запуск: python vk_bot.py
"""

from __future__ import annotations

import asyncio
import calendar as _cal
import datetime
import json
import logging
import random
import re
import sqlite3
import os
from contextlib import contextmanager

from dotenv import load_dotenv
from vkbottle import Bot, Keyboard, Text
from vkbottle.bot import Message

load_dotenv()

VK_TOKEN = os.getenv("VK_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
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
    """Возвращает 'чёт' или 'нечет' по ISO-номеру недели."""
    return "нечет" if now_msk().isocalendar()[1] % 2 == 0 else "чёт"


# ── DB-хелпер ────────────────────────────────────────────────────────────────
@contextmanager
def _db(path: str = "notes.db"):
    conn = sqlite3.connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Состояния пользователей (персистентные) ──────────────────────────────────
class _StateStore:
    def __init__(self):
        self._data: dict = {}

    def load_all(self) -> None:
        with _db() as conn:
            rows = conn.execute(
                "SELECT user_id, state_json FROM user_states"
            ).fetchall()
        for uid, json_str in rows:
            try:
                self._data[uid] = json.loads(json_str)
            except Exception:
                pass

    def get(self, uid: int, default=None):
        return self._data.get(uid, default)

    def __getitem__(self, uid: int):
        return self._data[uid]

    def __setitem__(self, uid: int, state) -> None:
        self._data[uid] = state
        with _db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_states (user_id, state_json) VALUES (?,?)",
                (uid, json.dumps(state, ensure_ascii=False)),
            )

    def pop(self, uid: int, *args):
        result = self._data.pop(uid, *args)
        with _db() as conn:
            conn.execute("DELETE FROM user_states WHERE user_id=?", (uid,))
        return result

    def patch(self, uid: int, **kwargs) -> dict:
        """Обновляет поля состояния и сохраняет в БД."""
        state = dict(self._data.get(uid) or {})
        state.update(kwargs)
        self[uid] = state
        return state


user_states = _StateStore()


# ── Вспомогательная функция для клавиатур ────────────────────────────────────
def _kb(*rows: list[str], one_time: bool = False) -> str:
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
    ["⏰ Напоминание", "📌 Дедлайны"],
    ["🔔 Подписки на пары"],
    ["💬 Обратная связь", "❓ Помощь"],
)

DAY_KB = _kb(
    ["Понедельник", "Вторник"],
    ["Среда", "Четверг"],
    ["Пятница", "Суббота"],
    ["◀ Назад", "🏠 Меню"],
)

CANCEL_KB = _kb(["❌ Отмена"], one_time=True)
BACK_KB = _kb(["◀ Назад"])

NOTES_LIMIT = 50

_DAYS = {"Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"}

_MONTH_NAMES = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def calendar_kb(year: int, month: int, min_day: int = 1) -> str:
    kb = Keyboard(one_time=False)
    kb.add(Text("◀"))
    kb.add(Text(f"{_MONTH_NAMES[month - 1]} {year}"))
    kb.add(Text("▶"))
    _, days_in_month = _cal.monthrange(year, month)
    shown = 0
    for i in range(1, days_in_month + 1):
        if i < min_day:
            continue
        if shown % 5 == 0:
            kb.row()
        kb.add(Text(str(i)))
        shown += 1
    kb.row()
    kb.add(Text("❌ Отмена"))
    return kb.get_json()


def _calendar_nav(uid: int, state: dict, text: str, today: datetime.date) -> tuple[int, int, int]:
    """Обрабатывает кнопки ◀/▶ календаря. Возвращает (year, month, min_day)."""
    year, month = state["cal_year"], state["cal_month"]
    if text == "◀":
        new_month, new_year = month - 1, year
        if new_month < 1:
            new_month, new_year = 12, year - 1
        if (new_year, new_month) >= (today.year, today.month):
            year, month = new_year, new_month
            user_states.patch(uid, cal_year=year, cal_month=month)
    elif text == "▶":
        month += 1
        if month > 12:
            month, year = 1, year + 1
        user_states.patch(uid, cal_year=year, cal_month=month)
    min_day = today.day if (year == today.year and month == today.month) else 1
    return year, month, min_day


# ── Сокращения для длинных названий направлений ──────────────────────────────
_PHRASE_ABBREVS: list[tuple[str, str]] = sorted([
    ("информационные системы и технологии", "ИСТ"),
    ("прикладная математика и информатика", "ПМИ"),
    ("математика и компьютерные науки", "МКН"),
    ("государственное и муниципальное управление", "ГМУ"),
    ("управление в технических системах", "УТС"),
    ("экономическая безопасность", "Экон. безоп."),
    ("информационная безопасность", "ИБ"),
    ("информационные технологии", "ИТ"),
    ("прикладная математика", "Прикл. матем."),
    ("прикладная информатика", "Прикл. инф."),
    ("программная инженерия", "Прогр. инж."),
    ("автоматизация и управление", "Автом. и упр."),
    ("техническая эксплуатация", "Тех. экспл."),
    ("мехатроника и робототехника", "Мехатроника"),
    ("экономика и управление", "Экон. и упр."),
    ("бизнес-информатика", "Бизнес-инф."),
    ("финансы и кредит", "Финансы и кр."),
    ("физическая культура", "Физ. культура"),
    ("педагогическое образование", "Пед. образ."),
    ("бухгалтерский учёт", "Бух. учёт"),
    ("бухгалтерский учет", "Бух. учёт"),
], key=lambda x: -len(x[0]))

_WORD_ABBREVS: dict[str, str] = {
    "информационный": "инф.", "информационная": "инф.",
    "информационных": "инф.", "информационные": "инф.",
    "технологический": "техн.", "технологическая": "техн.",
    "технологии": "техн.", "технология": "техн.",
    "управление": "упр.", "управления": "упр.",
    "экономический": "экон.", "экономическая": "экон.", "экономика": "экон.",
    "математический": "матем.", "математика": "матем.",
    "инженерный": "инж.", "инженерия": "инж.",
    "программный": "прогр.", "программирование": "прогр.",
    "системы": "сист.", "система": "сист.", "систем": "сист.",
    "электроника": "электр.", "электронный": "электр.",
    "безопасность": "безоп.", "безопасности": "безоп.",
    "строительство": "строит.", "строительства": "строит.",
    "производство": "произв.", "производства": "произв.",
}


def shorten_direction(name: str, max_len: int = 40) -> str:
    if len(name) <= max_len:
        return name
    result = name
    for phrase, abbr in _PHRASE_ABBREVS:
        result = re.sub(re.escape(phrase), abbr, result, flags=re.IGNORECASE)
        if len(result) <= max_len:
            return result
    words = result.split()
    result = " ".join(_WORD_ABBREVS.get(w.lower(), w) for w in words)
    if len(result) <= max_len:
        return result
    return result[:max_len - 1] + "…"


# ── База данных расписания (sсhedule.db) ──────────────────────────────────────
_dir_label_to_full: dict[int, dict[str, str]] = {}


def load_directions() -> dict[int, list[str]]:
    with _db("sсhedule.db") as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course INTEGER, direction TEXT, day TEXT,
                time TEXT, subject TEXT, teacher TEXT, room TEXT,
                week TEXT DEFAULT '',
                class_type TEXT DEFAULT '',
                date_range TEXT DEFAULT ''
            )
        """)
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(schedule)").fetchall()}
        if "week" not in existing_cols:
            conn.execute("ALTER TABLE schedule ADD COLUMN week TEXT DEFAULT ''")
        if "class_type" not in existing_cols:
            conn.execute("ALTER TABLE schedule ADD COLUMN class_type TEXT DEFAULT ''")
        if "date_range" not in existing_cols:
            conn.execute("ALTER TABLE schedule ADD COLUMN date_range TEXT DEFAULT ''")
        rows = conn.execute(
            "SELECT DISTINCT course, direction FROM schedule ORDER BY course"
        ).fetchall()

    result: dict[int, list[str]] = {}
    for course, direction in rows:
        cleaned = re.sub(r",?\s*\d+\s*курс.*", "", direction).strip()
        result.setdefault(course, [])
        if cleaned not in result[course]:
            result[course].append(cleaned)

    global _dir_label_to_full
    _dir_label_to_full = {}
    for course, dirs in result.items():
        mapping: dict[str, str] = {}
        used_labels: set[str] = set()
        for d in dirs:
            label = shorten_direction(d)
            if label in used_labels:
                label = d[:40]
            used_labels.add(label)
            mapping[label] = d
        _dir_label_to_full[course] = mapping

    return result


directions_by_course = load_directions()


def course_kb() -> str:
    rows = [[str(c)] for c in sorted(directions_by_course)]
    rows.append(["◀ Назад", "🏠 Меню"])
    return _kb(*rows)


def direction_kb(course: int) -> str:
    rows = [[label] for label in _dir_label_to_full.get(course, {})]
    rows.append(["◀ Назад", "🏠 Меню"])
    return _kb(*rows)


def resolve_direction(text: str, course: int) -> str | None:
    return _dir_label_to_full.get(course, {}).get(text)


def get_schedule(course: int, direction: str, day: str, week_type: str) -> str:
    with _db("sсhedule.db") as conn:
        rows = conn.execute(
            """
            SELECT time, subject, teacher, room, class_type, date_range FROM schedule
            WHERE course = ? AND LOWER(direction) = LOWER(?) AND LOWER(day) = LOWER(?)
              AND (week = '' OR week = ?)
            ORDER BY CAST(SUBSTR(time, 1, INSTR(time, ' ') - 1) AS INTEGER), time
            """,
            (course, direction, day, week_type),
        ).fetchall()

    if not rows:
        return "На этот день пар нет."

    lines = []
    for time_s, subject, teacher, room, class_type, date_range in rows:
        tags = [p for p in (class_type, date_range) if p]
        subj_str = f"{subject} [{', '.join(tags)}]" if tags else subject
        info = ", ".join(filter(None, [teacher, room]))
        lines.append(f"{time_s} — {subj_str}" + (f" ({info})" if info else ""))
    return "\n".join(lines)


# ── База данных заметок, напоминаний, подписок (notes.db) ────────────────────
def create_tables() -> None:
    with _db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_states (
                user_id INTEGER PRIMARY KEY,
                state_json TEXT NOT NULL
            );
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
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, text TEXT, timestamp TEXT
            );
            CREATE TABLE IF NOT EXISTS deadlines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                subject TEXT,
                description TEXT,
                deadline_at TEXT,
                notified_1day INTEGER DEFAULT 0,
                notified_1hour INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id INTEGER PRIMARY KEY,
                course INTEGER,
                direction TEXT
            );
        """)


# Заметки
def add_note(uid: int, text: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO notes (user_id, note_text, timestamp) VALUES (?,?,?)",
            (uid, text, now_msk().isoformat(timespec="seconds")),
        )


def get_notes(uid: int) -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT id, note_text, timestamp FROM notes WHERE user_id=? ORDER BY id DESC",
            (uid,),
        ).fetchall()


def delete_notes(ids: list[int]) -> None:
    if not ids:
        return
    with _db() as conn:
        conn.executemany("DELETE FROM notes WHERE id=?", ((i,) for i in ids))


# Напоминания
def add_reminder(uid: int, text: str, remind_at: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO reminders (user_id, reminder_text, remind_at, notified) VALUES (?,?,?,0)",
            (uid, text, remind_at),
        )


def get_pending_reminders() -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT id, user_id, reminder_text, remind_at FROM reminders WHERE notified=0"
        ).fetchall()


def mark_reminder_sent(rid: int) -> None:
    with _db() as conn:
        conn.execute("UPDATE reminders SET notified=1 WHERE id=?", (rid,))


# Подписки
def add_subscription(uid: int, course: int, direction: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO subscriptions (user_id, course, direction) VALUES (?,?,?)",
            (uid, course, direction),
        )


def get_user_subs(uid: int) -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT id, course, direction FROM subscriptions WHERE user_id=? ORDER BY id",
            (uid,),
        ).fetchall()


def get_all_subs() -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT id, user_id, course, direction FROM subscriptions"
        ).fetchall()


def delete_sub(sid: int) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM subscriptions WHERE id=?", (sid,))


# Фидбэк
def add_feedback(uid: int, text: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO feedback (user_id, text, timestamp) VALUES (?,?,?)",
            (uid, text, now_msk().isoformat(timespec="seconds")),
        )


# Дедлайны
def add_deadline(uid: int, subject: str, description: str, deadline_at: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO deadlines (user_id, subject, description, deadline_at) VALUES (?,?,?,?)",
            (uid, subject, description, deadline_at),
        )


def get_deadlines(uid: int) -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT id, subject, description, deadline_at FROM deadlines "
            "WHERE user_id=? ORDER BY deadline_at ASC",
            (uid,),
        ).fetchall()


def delete_deadline(did: int) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM deadlines WHERE id=?", (did,))


def get_all_pending_deadlines() -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT id, user_id, subject, description, deadline_at, notified_1day, notified_1hour "
            "FROM deadlines WHERE notified_1hour=0 OR notified_1day=0"
        ).fetchall()


def mark_deadline_1day(did: int) -> None:
    with _db() as conn:
        conn.execute("UPDATE deadlines SET notified_1day=1 WHERE id=?", (did,))


def mark_deadline_1hour(did: int) -> None:
    with _db() as conn:
        conn.execute("UPDATE deadlines SET notified_1hour=1 WHERE id=?", (did,))


# Предпочтения пользователя (курс/направление для быстрого расписания)
def get_user_pref(uid: int) -> tuple | None:
    with _db() as conn:
        return conn.execute(
            "SELECT course, direction FROM user_prefs WHERE user_id=?", (uid,)
        ).fetchone()


def set_user_pref(uid: int, course: int, direction: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_prefs (user_id, course, direction) VALUES (?,?,?)",
            (uid, course, direction),
        )


# Напоминания пользователя
def get_user_reminders(uid: int) -> list:
    with _db() as conn:
        return conn.execute(
            "SELECT id, reminder_text, remind_at FROM reminders "
            "WHERE user_id=? AND notified=0 ORDER BY remind_at",
            (uid,),
        ).fetchall()


def delete_reminder(rid: int) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM reminders WHERE id=?", (rid,))


# Дубли подписок
def sub_exists(uid: int, course: int, direction: str) -> bool:
    with _db() as conn:
        return conn.execute(
            "SELECT 1 FROM subscriptions WHERE user_id=? AND course=? AND LOWER(direction)=LOWER(?)",
            (uid, course, direction),
        ).fetchone() is not None


def _notif_sent(uid: int, row_id: int, date: str, time: str) -> bool:
    with _db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sent_class_notifications "
            "WHERE user_id=? AND schedule_row_id=? AND class_date=? AND class_time=?",
            (uid, row_id, date, time),
        ).fetchone()
    return exists is not None


def _mark_notif(uid: int, row_id: int, date: str, time: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO sent_class_notifications "
            "(user_id, schedule_row_id, class_date, class_time, sent_at) VALUES (?,?,?,?,?)",
            (uid, row_id, date, time, now_msk().isoformat(timespec="seconds")),
        )


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


async def deadline_checker() -> None:
    """Каждую минуту проверяет дедлайны; удаляет просроченные старше 7 дней."""
    while True:
        await asyncio.sleep(60)
        now = now_msk()

        cutoff = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
        with _db() as conn:
            conn.execute("DELETE FROM deadlines WHERE deadline_at < ?", (cutoff,))

        for did, uid, subject, description, deadline_at_str, n_1day, n_1hour in get_all_pending_deadlines():
            try:
                deadline_at = datetime.datetime.strptime(deadline_at_str, "%Y-%m-%d %H:%M")
            except ValueError:
                continue

            delta = deadline_at - now
            total_seconds = delta.total_seconds()

            if total_seconds < -3600:
                continue

            desc_line = f"\n📝 {description}" if description else ""

            if not n_1day and datetime.timedelta(hours=23) <= delta <= datetime.timedelta(hours=25):
                try:
                    deadline_fmt = deadline_at.strftime("%d.%m.%Y %H:%M")
                    await _send(
                        uid,
                        f"⚠️ Дедлайн завтра!\n"
                        f"📌 {subject}{desc_line}\n"
                        f"🕐 {deadline_fmt}",
                    )
                    mark_deadline_1day(did)
                except Exception:
                    logging.exception(f"Не удалось отправить уведомление о дедлайне id={did} (1 день)")

            if not n_1hour and datetime.timedelta(minutes=50) <= delta <= datetime.timedelta(minutes=70):
                try:
                    deadline_fmt = deadline_at.strftime("%d.%m.%Y %H:%M")
                    await _send(
                        uid,
                        f"🔴 Дедлайн через час!\n"
                        f"📌 {subject}{desc_line}\n"
                        f"🕐 {deadline_fmt}",
                    )
                    mark_deadline_1hour(did)
                except Exception:
                    logging.exception(f"Не удалось отправить уведомление о дедлайне id={did} (1 час)")


async def class_notification_checker() -> None:
    """Каждые 30 секунд рассылает уведомления за 10 мин до пары; чистит записи старше 2 дней."""
    ru_days = {
        "Monday": "Понедельник", "Tuesday": "Вторник", "Wednesday": "Среда",
        "Thursday": "Четверг", "Friday": "Пятница",
        "Saturday": "Суббота", "Sunday": "Воскресенье",
    }
    notify_before = datetime.timedelta(minutes=10)

    while True:
        await asyncio.sleep(30)
        now = now_msk()

        cutoff = (now - datetime.timedelta(days=2)).date().isoformat()
        with _db() as conn:
            conn.execute(
                "DELETE FROM sent_class_notifications WHERE class_date < ?", (cutoff,)
            )

        db_day = ru_days.get(now.strftime("%A"), now.strftime("%A"))
        week = current_week_type()
        exclude_prefix = "[нечет]%" if week == "чёт" else "[чёт]%"

        for _sid, uid, course, direction in get_all_subs():
            with _db("sсhedule.db") as conn:
                rows = conn.execute(
                    "SELECT rowid, time, subject, teacher, room FROM schedule "
                    "WHERE course=? AND LOWER(direction)=LOWER(?) "
                    "AND LOWER(day)=LOWER(?) AND subject NOT LIKE ?",
                    (course, direction, db_day, exclude_prefix),
                ).fetchall()

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

    if text == "🏠 Меню":
        user_states.pop(uid, None)
        await message.answer("Главное меню:", keyboard=MAIN_KB)
        return

    # ── РАСПИСАНИЕ ────────────────────────────────────────────────────────────
    if text == "📅 Расписание":
        if not directions_by_course:
            await message.answer(
                "База расписания пуста. Обратитесь к администратору.",
                keyboard=MAIN_KB,
            )
            return
        wt = current_week_type()
        hint = "чётная" if wt == "чёт" else "нечётная"
        pref = get_user_pref(uid)
        if pref:
            pref_course, pref_dir = pref
            user_states[uid] = {"step": "quick_day", "course": pref_course, "direction": pref_dir, "week_type": wt}
            await message.answer(
                f"Сейчас идёт {hint} неделя.\n"
                f"Последний выбор: {pref_course} курс — {pref_dir}\n\nВыбери день:",
                keyboard=_kb(
                    ["Понедельник", "Вторник"],
                    ["Среда", "Четверг"],
                    ["Пятница", "Суббота"],
                    ["🔄 Сменить курс/направление"],
                    ["◀ Назад", "🏠 Меню"],
                ),
            )
        else:
            user_states[uid] = {"step": "course", "week_type": wt}
            await message.answer(
                f"Сейчас идёт {hint} неделя.\nВыбери курс:",
                keyboard=course_kb(),
            )
        return

    if isinstance(state, dict) and state.get("step") == "course":
        if text == "◀ Назад":
            user_states.pop(uid, None)
            await message.answer("Главное меню:", keyboard=MAIN_KB)
            return
        if text.isdigit() and int(text) in directions_by_course:
            user_states.patch(uid, step="direction", course=int(text))
            await message.answer("Выбери направление:", keyboard=direction_kb(int(text)))
            return
        await message.answer("Выбери курс из кнопок ниже:", keyboard=course_kb())
        return

    if isinstance(state, dict) and state.get("step") == "direction":
        course = state["course"]
        if text == "◀ Назад":
            user_states.patch(uid, step="course")
            await message.answer("Выбери курс:", keyboard=course_kb())
            return
        direction = resolve_direction(text, course)
        if direction:
            user_states.patch(uid, step="day", direction=direction)
            await message.answer("Выбери день недели:", keyboard=DAY_KB)
            return
        await message.answer("Выбери направление из кнопок:", keyboard=direction_kb(course))
        return

    if isinstance(state, dict) and state.get("step") == "day":
        course = state["course"]
        direction = state["direction"]
        week_type = state["week_type"]
        if text == "◀ Назад":
            user_states.patch(uid, step="direction")
            await message.answer("Выбери направление:", keyboard=direction_kb(course))
            return
        if text in _DAYS:
            sched = get_schedule(course, direction, text, week_type)
            wlabel = "чётная" if week_type == "чёт" else "нечётная"
            set_user_pref(uid, course, direction)
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

    if isinstance(state, dict) and state.get("step") == "quick_day":
        course = state["course"]
        direction = state["direction"]
        week_type = state["week_type"]
        _quick_day_kb = _kb(
            ["Понедельник", "Вторник"],
            ["Среда", "Четверг"],
            ["Пятница", "Суббота"],
            ["🔄 Сменить курс/направление"],
            ["◀ Назад", "🏠 Меню"],
        )
        if text == "◀ Назад":
            user_states.pop(uid, None)
            await message.answer("Главное меню:", keyboard=MAIN_KB)
            return
        if text == "🔄 Сменить курс/направление":
            user_states.patch(uid, step="course")
            await message.answer("Выбери курс:", keyboard=course_kb())
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
        await message.answer("Выбери день из кнопок:", keyboard=_quick_day_kb)
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
            if len(get_notes(uid)) >= NOTES_LIMIT:
                user_states.pop(uid, None)
                await message.answer(
                    f"❌ Достигнут лимит ({NOTES_LIMIT} заметок). Удали старые, чтобы добавить новые.",
                    keyboard=MAIN_KB,
                )
            else:
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
        note_map = {}
        for i, (nid, note_text, ts) in enumerate(notes, 1):
            note_map[str(i)] = nid
            ans += f"[{i}] {note_text}\n📅 {ts}\n\n"
        ans += "Введи номер заметки для удаления (можно несколько через запятую).\nИли нажми «◀ Назад»."
        user_states[uid] = {"state": "view_notes", "note_map": note_map}
        await message.answer(ans, keyboard=BACK_KB)
        return

    if isinstance(state, dict) and state.get("state") == "view_notes":
        if text == "◀ Назад":
            user_states.pop(uid, None)
            await message.answer("Главное меню:", keyboard=MAIN_KB)
            return
        parts = re.split(r"[\s,]+", text)
        try:
            nums = [int(p) for p in parts if p.strip()]
        except ValueError:
            await message.answer(
                "Введи номер(а) заметки цифрами или нажми «◀ Назад».", keyboard=BACK_KB
            )
            return
        if not nums:
            await message.answer(
                "Введи номер(а) заметки цифрами или нажми «◀ Назад».", keyboard=BACK_KB
            )
            return
        note_map = state["note_map"]
        to_del_ids = [note_map[str(n)] for n in nums if str(n) in note_map]
        not_found = [n for n in nums if str(n) not in note_map]
        if to_del_ids:
            delete_notes(to_del_ids)
        resp_parts = []
        if to_del_ids:
            resp_parts.append(f"✅ Удалены заметки: {', '.join(str(n) for n in nums if str(n) in note_map)}")
        if not_found:
            resp_parts.append(f"❌ Не найдены: {', '.join(map(str, not_found))}")
        remaining = get_notes(uid)
        if remaining:
            rem = "\nОставшиеся заметки:\n\n"
            for i, (nid, nt, ts) in enumerate(remaining, 1):
                rem += f"[{i}] {nt}\n📅 {ts}\n\n"
            resp_parts.append(rem)
        else:
            resp_parts.append("Заметок больше нет.")
        user_states.pop(uid, None)
        await message.answer("\n".join(resp_parts), keyboard=MAIN_KB)
        return

    # ── НАПОМИНАНИЯ ───────────────────────────────────────────────────────────
    if text == "⏰ Напоминание":
        rems = get_user_reminders(uid)
        rem_map = {}
        if rems:
            ans = "⏰ Твои активные напоминания:\n\n"
            for i, (rid, rtext, rat) in enumerate(rems, 1):
                rem_map[str(i)] = rid
                try:
                    rat_fmt = datetime.datetime.strptime(rat, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
                except ValueError:
                    rat_fmt = rat
                ans += f"[{i}] {rtext}\n   📅 {rat_fmt}\n\n"
            ans += "Введи номер для отмены или добавь новое."
        else:
            ans = "У тебя нет активных напоминаний.\n\nДобавь первое!"
        user_states[uid] = {"state": "reminders", "rem_map": rem_map}
        await message.answer(ans, keyboard=_kb(["➕ Добавить напоминание"], ["◀ Назад"]))
        return

    if isinstance(state, dict) and state.get("state") == "reminders":
        if text == "◀ Назад":
            user_states.pop(uid, None)
            await message.answer("Главное меню:", keyboard=MAIN_KB)
            return
        if text == "➕ Добавить напоминание":
            user_states[uid] = {"step": "rem_text"}
            await message.answer("✏️ Введи текст напоминания:", keyboard=CANCEL_KB)
            return
        if text.isdigit():
            rem_map = state.get("rem_map", {})
            num = int(text)
            if str(num) in rem_map:
                delete_reminder(rem_map[str(num)])
                user_states.pop(uid, None)
                await message.answer(f"✅ Напоминание [{num}] отменено.", keyboard=MAIN_KB)
            else:
                await message.answer(
                    "❌ Напоминание с таким номером не найдено.",
                    keyboard=_kb(["➕ Добавить напоминание"], ["◀ Назад"]),
                )
            return
        await message.answer(
            "Введи номер напоминания для отмены или нажми кнопку.",
            keyboard=_kb(["➕ Добавить напоминание"], ["◀ Назад"]),
        )
        return

    if isinstance(state, dict) and state.get("step") == "rem_text":
        if text in ("❌ Отмена", "Отмена"):
            user_states.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return
        now = now_msk()
        user_states.patch(uid, step="rem_date", reminder_text=text, cal_year=now.year, cal_month=now.month)
        await message.answer("📅 Выбери дату:", keyboard=calendar_kb(now.year, now.month, now.day))
        return

    if isinstance(state, dict) and state.get("step") == "rem_date":
        if text in ("❌ Отмена", "Отмена"):
            user_states.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return
        today = now_msk().date()
        state = user_states.get(uid)
        if text in ("◀", "▶"):
            year, month, min_day = _calendar_nav(uid, state, text, today)
            await message.answer("📅 Выбери дату:", keyboard=calendar_kb(year, month, min_day))
            return
        year, month = state["cal_year"], state["cal_month"]
        min_day = today.day if (year == today.year and month == today.month) else 1
        if text == f"{_MONTH_NAMES[month - 1]} {year}":
            await message.answer("📅 Выбери дату:", keyboard=calendar_kb(year, month, min_day))
            return
        if text.isdigit():
            day = int(text)
            _, days_in_month = _cal.monthrange(year, month)
            selected = datetime.date(year, month, day)
            if 1 <= day <= days_in_month and selected >= today:
                date_str = f"{year:04d}-{month:02d}-{day:02d}"
                user_states.patch(uid, step="rem_clock", date_str=date_str)
                await message.answer(
                    f"✅ Дата: {day} {_MONTH_NAMES[month - 1]} {year}\n\n"
                    "⏰ Теперь введи время в формате ЧЧ:ММ\nПример: 09:00",
                    keyboard=CANCEL_KB,
                )
                return
        await message.answer("📅 Выбери дату из кнопок:", keyboard=calendar_kb(year, month, min_day))
        return

    if isinstance(state, dict) and state.get("step") == "rem_clock":
        if text in ("❌ Отмена", "Отмена"):
            user_states.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return
        try:
            datetime.datetime.strptime(text, "%H:%M")
        except ValueError:
            await message.answer(
                "Неверный формат. Введи время как ЧЧ:ММ\nПример: 09:00",
                keyboard=CANCEL_KB,
            )
            return
        remind_at = f"{state['date_str']} {text}"
        remind_dt = datetime.datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
        if remind_dt <= now_msk():
            await message.answer(
                "⚠️ Это время уже прошло. Введи время в будущем:",
                keyboard=CANCEL_KB,
            )
            return
        rem_text = state["reminder_text"]
        add_reminder(uid, rem_text, remind_at)
        user_states.pop(uid, None)
        await message.answer(
            f"✅ Напоминание создано!\n📝 {rem_text}\n📅 {remind_at}",
            keyboard=MAIN_KB,
        )
        return

    # ── ДЕДЛАЙНЫ ─────────────────────────────────────────────────────────────
    if text == "📌 Дедлайны":
        deadlines = get_deadlines(uid)
        now = now_msk()
        dl_map = {}
        if deadlines:
            lines = ["📌 Твои дедлайны:\n"]
            for i, (did, subj, desc, dl_at) in enumerate(deadlines, 1):
                dl_map[str(i)] = did
                try:
                    dl_dt = datetime.datetime.strptime(dl_at, "%Y-%m-%d %H:%M")
                    dl_fmt = dl_dt.strftime("%d.%m.%Y %H:%M")
                    delta = dl_dt - now
                    if delta.total_seconds() < 0:
                        status = "🔴 просрочен"
                    elif delta.total_seconds() < 3600:
                        status = "🟠 < 1 часа"
                    elif delta.total_seconds() < 86400:
                        status = "🟡 < 1 дня"
                    else:
                        status = f"🟢 {delta.days} дн."
                except ValueError:
                    dl_fmt = dl_at
                    status = ""
                desc_line = f"\n   {desc}" if desc else ""
                lines.append(f"[{i}] {subj}{desc_line}\n   📅 {dl_fmt}  {status}")
            ans = "\n\n".join(lines)
            ans += "\n\nВведи номер для удаления или добавь новый."
        else:
            ans = "У тебя пока нет дедлайнов.\n\nДобавь первый — и я напомню за день и за час до срока."
        user_states[uid] = {"state": "deadlines", "dl_map": dl_map}
        await message.answer(ans, keyboard=_kb(["➕ Добавить дедлайн"], ["◀ Назад"]))
        return

    if isinstance(state, dict) and state.get("state") == "deadlines":
        if text == "◀ Назад":
            user_states.pop(uid, None)
            await message.answer("Главное меню:", keyboard=MAIN_KB)
            return
        if text == "➕ Добавить дедлайн":
            user_states[uid] = {"step": "dl_subject"}
            await message.answer(
                "📌 Добавление дедлайна\n\nШаг 1/4 — Введи название предмета или задачи:\nНапример: «Математика» или «Курсовая по физике»",
                keyboard=CANCEL_KB,
            )
            return
        if text.isdigit():
            dl_map = state.get("dl_map", {})
            num = int(text)
            if str(num) in dl_map:
                delete_deadline(dl_map[str(num)])
                user_states.pop(uid, None)
                await message.answer(f"✅ Дедлайн [{num}] удалён.", keyboard=MAIN_KB)
            else:
                await message.answer(
                    "❌ Дедлайн с таким номером не найден.",
                    keyboard=_kb(["➕ Добавить дедлайн"], ["◀ Назад"]),
                )
            return
        await message.answer(
            "Введи номер дедлайна для удаления или нажми кнопку.",
            keyboard=_kb(["➕ Добавить дедлайн"], ["◀ Назад"]),
        )
        return

    if isinstance(state, dict) and state.get("step") == "dl_subject":
        if text in ("❌ Отмена", "Отмена"):
            user_states.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return
        if not text:
            await message.answer("Название не может быть пустым. Попробуй ещё раз:", keyboard=CANCEL_KB)
            return
        user_states.patch(uid, step="dl_desc", dl_subject=text)
        await message.answer(
            f"📌 Предмет: {text}\n\nШаг 2/4 — Добавь описание (необязательно):\nНапример, «Решить задачи 1-5» или нажми «Пропустить»",
            keyboard=_kb(["⏩ Пропустить"], ["❌ Отмена"]),
        )
        return

    if isinstance(state, dict) and state.get("step") == "dl_desc":
        if text in ("❌ Отмена", "Отмена"):
            user_states.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return
        desc = "" if text == "⏩ Пропустить" else text
        now = now_msk()
        user_states.patch(uid, step="dl_date", dl_desc=desc, cal_year=now.year, cal_month=now.month)
        await message.answer(
            "Шаг 3/4 — Выбери дату дедлайна:",
            keyboard=calendar_kb(now.year, now.month, now.day),
        )
        return

    if isinstance(state, dict) and state.get("step") == "dl_date":
        if text in ("❌ Отмена", "Отмена"):
            user_states.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return
        today = now_msk().date()
        state = user_states.get(uid)
        if text in ("◀", "▶"):
            year, month, min_day = _calendar_nav(uid, state, text, today)
            await message.answer("📅 Выбери дату:", keyboard=calendar_kb(year, month, min_day))
            return
        year, month = state["cal_year"], state["cal_month"]
        min_day = today.day if (year == today.year and month == today.month) else 1
        if text == f"{_MONTH_NAMES[month - 1]} {year}":
            await message.answer("📅 Выбери дату:", keyboard=calendar_kb(year, month, min_day))
            return
        if text.isdigit():
            day = int(text)
            _, days_in_month = _cal.monthrange(year, month)
            selected = datetime.date(year, month, day)
            if 1 <= day <= days_in_month and selected >= today:
                date_str = f"{year:04d}-{month:02d}-{day:02d}"
                user_states.patch(uid, step="dl_time", dl_date=date_str)
                await message.answer(
                    f"✅ Дата: {day} {_MONTH_NAMES[month - 1]} {year}\n\n"
                    "Шаг 4/4 — Введи время дедлайна в формате ЧЧ:ММ\n"
                    "Или нажми «Пропустить» (будет установлено 23:59)",
                    keyboard=_kb(["⏩ Пропустить"], ["❌ Отмена"]),
                )
                return
        await message.answer("📅 Выбери дату из кнопок:", keyboard=calendar_kb(year, month, min_day))
        return

    if isinstance(state, dict) and state.get("step") == "dl_time":
        if text in ("❌ Отмена", "Отмена"):
            user_states.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return
        if text == "⏩ Пропустить":
            time_str = "23:59"
        else:
            try:
                datetime.datetime.strptime(text, "%H:%M")
                time_str = text
            except ValueError:
                await message.answer(
                    "Неверный формат. Введи время как ЧЧ:ММ\nПример: 18:00\nИли нажми «Пропустить»",
                    keyboard=_kb(["⏩ Пропустить"], ["❌ Отмена"]),
                )
                return
        deadline_at = f"{state['dl_date']} {time_str}"
        deadline_dt = datetime.datetime.strptime(deadline_at, "%Y-%m-%d %H:%M")
        if deadline_dt <= now_msk():
            await message.answer(
                "⚠️ Это время уже прошло. Введи время в будущем, или нажми «Пропустить» (будет 23:59):",
                keyboard=_kb(["⏩ Пропустить"], ["❌ Отмена"]),
            )
            return
        add_deadline(uid, state["dl_subject"], state.get("dl_desc", ""), deadline_at)
        user_states.pop(uid, None)
        desc_line = f"\n📝 {state['dl_desc']}" if state.get("dl_desc") else ""
        await message.answer(
            f"✅ Дедлайн добавлен!\n\n"
            f"📌 {state['dl_subject']}{desc_line}\n"
            f"🕐 {deadline_at}\n\n"
            f"Напомню за 1 день и за 1 час до срока.",
            keyboard=MAIN_KB,
        )
        return

    # ── ПОДПИСКИ НА ПАРЫ ──────────────────────────────────────────────────────
    if text == "🔔 Подписки на пары":
        subs = get_user_subs(uid)
        sub_map = {}
        if subs:
            ans = "🔔 Твои подписки на уведомления о парах:\n\n"
            for i, (sid, course, direction) in enumerate(subs, 1):
                sub_map[str(i)] = sid
                ans += f"[{i}] {course} курс — {direction}\n"
            ans += "\nВведи номер для удаления подписки, или добавь новую."
        else:
            ans = (
                "У тебя пока нет подписок.\n\n"
                "Добавь подписку — и я буду напоминать о каждой паре за 10 минут."
            )
        user_states[uid] = {"state": "subs", "sub_map": sub_map}
        await message.answer(ans, keyboard=_kb(["➕ Добавить подписку"], ["◀ Назад"]))
        return

    if isinstance(state, dict) and state.get("state") == "subs":
        if text == "◀ Назад":
            user_states.pop(uid, None)
            await message.answer("Главное меню:", keyboard=MAIN_KB)
            return
        if text == "➕ Добавить подписку":
            user_states[uid] = {"step": "sub_course"}
            await message.answer("Выбери курс:", keyboard=course_kb())
            return
        if text.isdigit():
            sub_map = state.get("sub_map", {})
            num = int(text)
            if str(num) in sub_map:
                delete_sub(sub_map[str(num)])
                user_states.pop(uid, None)
                await message.answer(f"✅ Подписка [{num}] удалена.", keyboard=MAIN_KB)
            else:
                await message.answer(
                    "❌ Подписка с таким номером не найдена.",
                    keyboard=_kb(["➕ Добавить подписку"], ["◀ Назад"]),
                )
            return
        await message.answer(
            "Введи номер подписки для удаления или нажми кнопку.",
            keyboard=_kb(["➕ Добавить подписку"], ["◀ Назад"]),
        )
        return

    if isinstance(state, dict) and state.get("step") == "sub_course":
        if text == "◀ Назад":
            subs = get_user_subs(uid)
            sub_map = {str(i): sid for i, (sid, _, _) in enumerate(subs, 1)}
            user_states[uid] = {"state": "subs", "sub_map": sub_map}
            await message.answer(
                "Управление подписками:",
                keyboard=_kb(["➕ Добавить подписку"], ["◀ Назад"]),
            )
            return
        if text.isdigit() and int(text) in directions_by_course:
            user_states.patch(uid, step="sub_direction", course=int(text))
            await message.answer("Выбери направление:", keyboard=direction_kb(int(text)))
            return
        await message.answer("Выбери курс из кнопок:", keyboard=course_kb())
        return

    if isinstance(state, dict) and state.get("step") == "sub_direction":
        course = state["course"]
        if text == "◀ Назад":
            user_states.patch(uid, step="sub_course")
            await message.answer("Выбери курс:", keyboard=course_kb())
            return
        direction = resolve_direction(text, course)
        if direction:
            if sub_exists(uid, course, direction):
                user_states.pop(uid, None)
                await message.answer(
                    f"ℹ️ Ты уже подписан на {course} курс — {direction}.",
                    keyboard=MAIN_KB,
                )
            else:
                add_subscription(uid, course, direction)
                user_states.pop(uid, None)
                await message.answer(
                    f"✅ Подписка добавлена!\n{course} курс — {direction}\n\n"
                    "Буду напоминать о каждой паре за 10 минут до начала.",
                    keyboard=MAIN_KB,
                )
            return
        await message.answer("Выбери направление из кнопок:", keyboard=direction_kb(course))
        return

    # ── ОБРАТНАЯ СВЯЗЬ ───────────────────────────────────────────────────────
    if text == "💬 Обратная связь":
        user_states[uid] = "feedback"
        await message.answer(
            "💬 Напиши своё предложение, вопрос или сообщение об ошибке.\n"
            "Мы постараемся рассмотреть его как можно скорее.",
            keyboard=CANCEL_KB,
        )
        return

    if state == "feedback":
        if text in ("❌ Отмена", "Отмена"):
            user_states.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return
        if text:
            add_feedback(uid, text)
            user_states.pop(uid, None)
            await message.answer("✅ Спасибо! Твоё сообщение получено.", keyboard=MAIN_KB)
            if ADMIN_ID:
                try:
                    await _send(ADMIN_ID, f"💬 Новый фидбэк от [id{uid}|id{uid}]:\n\n{text}")
                except Exception:
                    logging.exception("Не удалось переслать фидбэк админу")
        else:
            await message.answer("Сообщение не может быть пустым. Попробуй ещё раз:", keyboard=CANCEL_KB)
        return

    # ── АДМИН ─────────────────────────────────────────────────────────────────
    if text.lower() == "/feedback" and uid == ADMIN_ID:
        with _db() as conn:
            rows = conn.execute(
                "SELECT id, user_id, text, timestamp FROM feedback ORDER BY id DESC LIMIT 20"
            ).fetchall()
        if not rows:
            await message.answer("Заявок пока нет.")
        else:
            lines = [f"📋 Последние заявки ({len(rows)}):\n"]
            for fid, fuid, ftext, fts in rows:
                lines.append(f"[{fid}] id{fuid} · {fts}\n{ftext}\n")
            await message.answer("\n".join(lines))
        return

    # ── ПОМОЩЬ ────────────────────────────────────────────────────────────────
    if text == "❓ Помощь":
        await message.answer(
            "📖 Краткое руководство:\n\n"
            "📅 Расписание\n"
            "Нажми «Расписание» — бот определит чётность недели.\n"
            "Бот запоминает последний выбор курса и направления — в следующий раз можно сразу выбрать день.\n"
            "На любом шаге «🏠 Меню» возвращает в главное меню.\n\n"
            "📝 Заметки\n"
            "«Добавить заметку» — введи текст, бот сохранит (лимит 50 заметок).\n"
            "«Мои заметки» — список с номерами; введи номер (или несколько через запятую) для удаления.\n\n"
            "⏰ Напоминания\n"
            "«Напоминание» — показывает список активных напоминаний, можно отменить по номеру.\n"
            "«Добавить напоминание» → введи текст → выбери дату → введи время (ЧЧ:ММ).\n"
            "В указанное время придёт сообщение.\n\n"
            "🔔 Подписки на пары\n"
            "«Подписки на пары» → добавь курс и направление.\n"
            "Бот будет напоминать за 10 минут до начала каждой пары.\n\n"
            "📌 Дедлайны\n"
            "«Дедлайны» → добавь предмет, описание задачи и дату сдачи.\n"
            "Бот напомнит за 1 день и за 1 час до срока.\n"
            "Рядом с каждым дедлайном видно, сколько времени осталось.\n\n"
            "💬 Обратная связь\n"
            "Напиши предложение, вопрос или сообщи об ошибке — администратор получит уведомление.\n\n"
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
    user_states.load_all()
    bot.loop_wrapper.add_task(reminder_checker())
    bot.loop_wrapper.add_task(class_notification_checker())
    bot.loop_wrapper.add_task(deadline_checker())
    bot.run_forever()


if __name__ == "__main__":
    main()
