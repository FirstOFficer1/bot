import os
import sys
from dotenv import load_dotenv
import datetime
import sqlite3
from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
import asyncio
import re
import logging

load_dotenv()

# ── Московское время ──────────────────────────────────────────────────
_MSK = datetime.timezone(datetime.timedelta(hours=3))
def now_msk():
    return datetime.datetime.now(_MSK).replace(tzinfo=None)

# ── SOCKS5 прокси с авто-ротацией ────────────────────────────────────
PROXY_LIST = [
    "socks5://185.218.137.242:1080",   # NL
    "socks5://167.172.161.22:1088",    # DE
    "socks5://65.109.218.115:1080",    # FI
    "socks5://91.217.81.131:1080",     # RU
]
_proxy_errors = 0
_PROXY_ERROR_LIMIT = 8

def _make_session(proxy_url=None):
    if proxy_url:
        return AiohttpSession(proxy=proxy_url, timeout=60)
    return AiohttpSession(timeout=60)

async def _build_bot():
    """Выбирает первый рабочий прокси из PROXY_LIST, иначе — без прокси."""
    for proxy_url in PROXY_LIST:
        try:
            test_bot = Bot(token=TOKEN, session=_make_session(proxy_url))
            me = await test_bot.get_me()
            logging.info(f"Proxy OK: {proxy_url} (@{me.username})")
            return test_bot
        except Exception as e:
            logging.warning(f"Proxy {proxy_url} failed: {e}")
    logging.warning("All proxies failed, connecting directly")
    return Bot(token=TOKEN, session=_make_session())

async def proxy_watchdog():
    """Перезапускает процесс если прокси умер (N ошибок подряд)."""
    global _proxy_errors
    while True:
        await asyncio.sleep(15)
        if _proxy_errors >= _PROXY_ERROR_LIMIT:
            logging.warning(f"Proxy died ({_proxy_errors} errors), restarting...")
            sys.exit(1)

# === НАСТРОЙКИ ===
TOKEN = os.getenv("TOKEN", "")
YANDEXGPT_API_KEY = os.getenv("YANDEXGPT_API_KEY", "")
FOLDER_ID = os.getenv("FOLDER_ID", "")
MODEL_URI = os.getenv("MODEL_URI", "")
# If you want to use an Open-Source model deployed in Yandex Cloud, set MODEL_URI
# to the model identifier / URI provided by Yandex Cloud (for example a model from
# the 'open' family). If left empty, the code uses the default YandexGPT modelUri.

bot = Bot(token=TOKEN, session=_make_session())
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Настройка логирования
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s', force=True)
logging.getLogger().setLevel(logging.DEBUG)  # Устанавливаем уровень DEBUG для всех логов

# === Состояния пользователей: хранение режима (помощник/GPT) и другие шаги ===
user_modes = {}
user_states = {}

# ----------------------------------------------------------------------------------
#                             БАЗА ДАННЫХ ДЛЯ РАСПИСАНИЯ
# ----------------------------------------------------------------------------------

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
    """Сокращает название направления до max_len символов с помощью аббревиатур."""
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


_dir_label_to_full: dict[int, dict[str, str]] = {}


def load_directions_from_db():
    """
    Загружает (course, direction) из s.db.
    Возвращает словарь {course_number: [direction1, direction2, ...], ...}.
    """
    conn = sqlite3.connect("s.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            course INTEGER, direction TEXT, day TEXT,
            time TEXT, subject TEXT, teacher TEXT, room TEXT,
            week TEXT DEFAULT '',
            class_type TEXT DEFAULT '',
            date_range TEXT DEFAULT ''
        )
    """)
    existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(schedule)").fetchall()}
    if "week" not in existing_cols:
        cursor.execute("ALTER TABLE schedule ADD COLUMN week TEXT DEFAULT ''")
    if "class_type" not in existing_cols:
        cursor.execute("ALTER TABLE schedule ADD COLUMN class_type TEXT DEFAULT ''")
    if "date_range" not in existing_cols:
        cursor.execute("ALTER TABLE schedule ADD COLUMN date_range TEXT DEFAULT ''")
    conn.commit()
    cursor.execute("SELECT DISTINCT course, direction FROM schedule ORDER BY course;")
    directions_from_db = cursor.fetchall()
    conn.close()

    directions_by_course = {}
    for course, direction in directions_from_db:
        cleaned_direction = re.sub(r",?\s*\d+\s*курс.*", "", direction).strip()
        if course not in directions_by_course:
            directions_by_course[course] = []
        if cleaned_direction not in directions_by_course[course]:
            directions_by_course[course].append(cleaned_direction)

    global _dir_label_to_full
    _dir_label_to_full = {}
    for course, dirs in directions_by_course.items():
        mapping: dict[str, str] = {}
        used_labels: set[str] = set()
        for d in dirs:
            label = shorten_direction(d)
            if label in used_labels:
                label = d[:40]
            used_labels.add(label)
            mapping[label] = d
        _dir_label_to_full[course] = mapping

    return directions_by_course


directions_by_course = load_directions_from_db()


def direction_keyboard_for(course: int) -> ReplyKeyboardMarkup:
    """Клавиатура с сокращёнными названиями направлений."""
    buttons = [[KeyboardButton(text=label)] for label in _dir_label_to_full.get(course, {})]
    buttons.append([KeyboardButton(text="Назад")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def resolve_direction_label(text: str, course: int) -> str | None:
    """Возвращает полное название направления по метке кнопки."""
    return _dir_label_to_full.get(course, {}).get(text)

def get_schedule_by_course(course: int, direction: str, day: str) -> str:
    week_type = "нечет" if now_msk().isocalendar()[1] % 2 == 0 else "чёт"
    conn = sqlite3.connect("s.db")
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT time, subject, teacher, room, class_type, date_range FROM schedule
        WHERE course = ? AND LOWER(direction) = LOWER(?) AND LOWER(day) = LOWER(?)
          AND (week = '' OR week = ?)
        ORDER BY
            CAST(SUBSTR(time, 1, INSTR(time, ' ') - 1) AS INTEGER),
            time
        """,
        (course, direction, day, week_type)
    )
    rows = cursor.fetchall()
    conn.close()

    if rows:
        lines = []
        for time, subject, teacher, room, class_type, date_range in rows:
            tags = [p for p in (class_type, date_range) if p]
            subj_str = f"{subject} [{', '.join(tags)}]" if tags else subject
            info = ", ".join(filter(None, [teacher, room]))
            lines.append(f"{time} — {subj_str}" + (f" ({info})" if info else ""))
        return "\n".join(lines)
    return "Расписание не найдено."


# ----------------------------------------------------------------------------------
#                     БАЗА ДАННЫХ ДЛЯ ЗАМЕТОК И НАПОМИНАНИЙ (notes.db)
# ----------------------------------------------------------------------------------

def create_notes_tables():
    """
    Создаёт (при необходимости) таблицы notes и reminders в notes.db.
    - notes: для сохранения заметок
    - reminders: для напоминаний
    """
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    # Таблица заметок
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            note_text TEXT,
            timestamp TEXT
        );
    """)
    # Таблица напоминаний
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            reminder_text TEXT,
            remind_at TEXT,        -- хранит время напоминания, строка в ISO8601
            notified INTEGER       -- флаг, отправлено ли уведомление (0/1)
        );
    """)
    conn.commit()
    conn.close()

    # Таблицы для подписок и учёта отправленных уведомлений о парах
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            course INTEGER,
            direction TEXT
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sent_class_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            schedule_row_id INTEGER,
            class_date TEXT,
            class_time TEXT,
            sent_at TEXT
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS gpt_settings (
            user_id INTEGER PRIMARY KEY,
            api_key TEXT,
            base_url TEXT,
            model_uri TEXT
        );
    """)
    conn.commit()
    conn.close()

def get_gpt_settings(user_id: int) -> dict | None:
    """Возвращает GPT-настройки пользователя или None если не заданы."""
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute("SELECT api_key, base_url, model_uri FROM gpt_settings WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0]:
        return {"api_key": row[0], "base_url": row[1], "model_uri": row[2]}
    return None

def save_gpt_settings(user_id: int, api_key: str, base_url: str, model_uri: str):
    """Сохраняет или обновляет GPT-настройки пользователя."""
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO gpt_settings (user_id, api_key, base_url, model_uri)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET api_key=excluded.api_key,
            base_url=excluded.base_url, model_uri=excluded.model_uri
    """, (user_id, api_key, base_url, model_uri))
    conn.commit()
    conn.close()

def add_note_to_db(user_id: int, note_text: str):
    """
    Сохраняет заметку в таблице notes, поле timestamp – в виде ISO8601-строки.
    """
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    now_str = datetime.datetime.now().isoformat(timespec="seconds")
    cursor.execute("INSERT INTO notes (user_id, note_text, timestamp) VALUES (?, ?, ?)",
                   (user_id, note_text, now_str))
    conn.commit()
    conn.close()

def get_notes_for_user(user_id: int):
    """
    Возвращает список заметок пользователя из таблицы notes.
    """
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, note_text, timestamp
        FROM notes
        WHERE user_id = ?
        ORDER BY id DESC
    """, (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows  # [(id, text, timestamp_str), ...]

# ------------------------- Напоминания -------------------------

def add_reminder(user_id: int, text: str, remind_at_str: str):
    """
    Добавляет напоминание для пользователя user_id:
    - text: текст напоминания
    - remind_at_str: строка вида "YYYY-MM-DD HH:MM"
    - по умолчанию notified=0 (ещё не отправляли)
    """
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO reminders (user_id, reminder_text, remind_at, notified)
        VALUES (?, ?, ?, 0)
    """, (user_id, text, remind_at_str))
    conn.commit()
    conn.close()

def get_not_notified_reminders():
    """
    Возвращает все напоминания, у которых notified=0.
    Результат: [(id, user_id, text, remind_at_str, notified), ...]
    """
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, user_id, reminder_text, remind_at, notified
        FROM reminders
        WHERE notified=0
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows

# ------------------------- Подписки и уведомления о парах -------------------------
def add_subscription(user_id: int, course: int, direction: str):
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO subscriptions (user_id, course, direction) VALUES (?, ?, ?)", (user_id, course, direction))
    conn.commit()
    conn.close()

def get_subscriptions_for_user(user_id: int):
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, course, direction FROM subscriptions WHERE user_id = ? ORDER BY id DESC", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def delete_subscription_by_id(sub_id: int):
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
    conn.commit()
    conn.close()

def get_all_subscriptions():
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, course, direction FROM subscriptions")
    rows = cursor.fetchall()
    conn.close()
    return rows

def notification_already_sent(user_id: int, schedule_row_id: int, class_date: str, class_time: str) -> bool:
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM sent_class_notifications WHERE user_id=? AND schedule_row_id=? AND class_date=? AND class_time=?",
        (user_id, schedule_row_id, class_date, class_time)
    )
    exists = cursor.fetchone() is not None
    conn.close()
    return exists

def mark_class_notification_sent(user_id: int, schedule_row_id: int, class_date: str, class_time: str):
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    now_str = datetime.datetime.now().isoformat(timespec="seconds")
    cursor.execute(
        "INSERT INTO sent_class_notifications (user_id, schedule_row_id, class_date, class_time, sent_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, schedule_row_id, class_date, class_time, now_str)
    )
    conn.commit()
    conn.close()

def compute_lesson_start_by_number(lesson_number: int) -> datetime.time:
    """Вычисляет время начала пары по её номеру (1-based), используя правила:
    - 1-я пара: 08:15 (90 мин)
    - пауза между 1 и 2: 10 мин
    - пауза между 2 и 3: 35 мин
    - паузы после 3-й и далее: 10 мин
    Возвращает объект time()."""
    base = datetime.datetime.combine(datetime.date.today(), datetime.time(hour=8, minute=15))
    if lesson_number == 1:
        return base.time()
    # iterate to compute start
    current = base
    for n in range(1, lesson_number):
        # add lesson duration
        current = current + datetime.timedelta(minutes=90)
        # add break
        if n == 1:
            current = current + datetime.timedelta(minutes=10)
        elif n == 2:
            current = current + datetime.timedelta(minutes=35)
        else:
            current = current + datetime.timedelta(minutes=10)
    return current.time()

def extract_lesson_number_from_time_field(time_field: str) -> int | None:
    """Если поле time содержит номер пары в начале (например '1 08:15'), пытаемся вернуть номер пары как int."""
    if not time_field:
        return None
    m = re.match(r"\s*(\d+)\b", time_field)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def mark_reminder_notified(reminder_id: int):
    """
    Ставит напоминанию (id=reminder_id) флаг notified=1 (помечаем, что уже отправили).
    """
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE reminders SET notified=1 WHERE id=?", (reminder_id,))
    conn.commit()
    conn.close()

# ----------------------------------------------------------------------------------
#                           Фоновая задача: отправка напоминаний
# ----------------------------------------------------------------------------------
async def reminder_checker():
    """
    Запускается фоном. Каждую минуту проверяет, какие напоминания (reminders)
    уже «просрочены» (время напоминания <= сейчас) и ещё не отправлены (notified=0).
    Отправляет их и помечает notified=1.
    """
    while True:
        await asyncio.sleep(10)  # например, раз в 10 секунд (или 60)
        now = now_msk()

        # получаем все неотправленные напоминания
        reminders = get_not_notified_reminders()
        for rid, uid, text, remind_at_str, notified in reminders:
            # Преобразуем remind_at_str в datetime
            try:
                remind_at_dt = datetime.datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M")
            except ValueError:
                # если формат у пользователя "YYYY-MM-DD HH:MM:SS" или другой —
                # тогда нужно подстраивать, либо брать fromisoformat.
                # Для упрощения даём пример с "%Y-%m-%d %H:%M".
                # Если парсинг не удался — пропускаем
                continue

            # если время напоминания уже настало/просрочено
            if remind_at_dt <= now:
                # отправляем сообщение
                try:
                    await bot.send_message(uid, f"Напоминание: {text}")
                    mark_reminder_notified(rid)
                except Exception as _re:
                    logging.exception(f"Не удалось отправить напоминание id={rid}")
                    if "proxy" in str(_re).lower() or "connect" in str(_re).lower():
                        global _proxy_errors
                        _proxy_errors += 1


async def class_notification_checker():
    """Фоновая задача, которая проверяет подписки и рассылает уведомления за 10 минут до начала пары."""
    notify_before = datetime.timedelta(minutes=10)
    while True:
        await asyncio.sleep(30)  # проверяем каждые 30 секунд
        now = now_msk()

        # Получаем все подписки
        subs = get_all_subscriptions()
        for sub_id, uid, course, direction in subs:
            # Для каждого подписанного пользователя берем расписание на сегодня для их направления
            today_weekday = now.strftime('%A')  # English weekday; but DB days may be in Russian
            # Попробуем поддержать русские названия
            ru_weekday_map = {
                'Monday': 'Понедельник', 'Tuesday': 'Вторник', 'Wednesday': 'Среда',
                'Thursday': 'Четверг', 'Friday': 'Пятница', 'Saturday': 'Суббота', 'Sunday': 'Воскресенье'
            }
            db_day = ru_weekday_map.get(now.strftime('%A'), now.strftime('%A'))

            conn = sqlite3.connect('s.db')
            cursor = conn.cursor()
            cursor.execute(
                "SELECT rowid, time, subject, teacher, room FROM schedule WHERE course=? AND LOWER(direction)=LOWER(?) AND LOWER(day)=LOWER(?)",
                (course, direction, db_day)
            )
            rows = cursor.fetchall()
            conn.close()

            for row in rows:
                schedule_row_id, time_field, subject, teacher, room = row

                # пытаемся найти точное время в поле time
                m = re.search(r"(\d{1,2}[:.]\d{2})", (time_field or ''))
                if m:
                    tstr = m.group(1).replace('.', ':')
                    try:
                        hhmm = datetime.datetime.strptime(tstr, "%H:%M").time()
                        class_start_dt = datetime.datetime.combine(now.date(), hhmm)
                    except ValueError:
                        class_start_dt = None
                else:
                    # если нет времени — пробуем извлечь номер пары и посчитать время
                    lesson_number = extract_lesson_number_from_time_field(time_field)
                    if lesson_number is not None:
                        t = compute_lesson_start_by_number(lesson_number)
                        class_start_dt = datetime.datetime.combine(now.date(), t)
                    else:
                        class_start_dt = None

                if class_start_dt is None:
                    continue

                # вычисляем конец пары (90 минут)
                class_end_dt = class_start_dt + datetime.timedelta(minutes=90)

                # если пара уже прошла — пропускаем
                if class_end_dt < now - datetime.timedelta(minutes=1):
                    continue

                # Формат времени с точкой: ЧЧ.ММ
                class_start_str = class_start_dt.time().strftime('%H.%M')
                class_end_str = class_end_dt.time().strftime('%H.%M')
                class_date_str = class_start_dt.date().isoformat()

                # уведомление за N минут до начала
                if class_start_dt - notify_before <= now < class_start_dt:
                    # не шлём дубликаты для старта
                    if not notification_already_sent(uid, schedule_row_id, class_date_str, class_start_str):
                        try:
                            await bot.send_message(uid, f"Скоро пара: {subject} ({teacher}, {room}) — начало в {class_start_str}")
                            mark_class_notification_sent(uid, schedule_row_id, class_date_str, class_start_str)
                        except Exception:
                            logging.exception("Не удалось отправить уведомление о паре")

                # уведомление об окончании пары: отправляем, если время окончания наступило (в небольшой интервал)
                end_window = datetime.timedelta(seconds=60)
                if class_end_dt <= now < class_end_dt + end_window:
                    # не шлём дубликаты для окончания
                    if not notification_already_sent(uid, schedule_row_id, class_date_str, class_end_str):
                        try:
                            await bot.send_message(uid, f"Пара окончена: {subject} ({teacher}, {room}) — окончание в {class_end_str}")
                            mark_class_notification_sent(uid, schedule_row_id, class_date_str, class_end_str)
                        except Exception:
                            logging.exception("Не удалось отправить уведомление об окончании пары")

# ----------------------------------------------------------------------------------
#                                   Клавиатуры
# ----------------------------------------------------------------------------------

course_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=str(i))] for i in directions_by_course.keys()] + [[KeyboardButton(text="Назад")]],
    resize_keyboard=True
)

day_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Понедельник"), KeyboardButton(text="Вторник")],
        [KeyboardButton(text="Среда"), KeyboardButton(text="Четверг")],
        [KeyboardButton(text="Пятница"), KeyboardButton(text="Суббота")],
        [KeyboardButton(text="Назад")]
    ],
    resize_keyboard=True
)

assistant_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Расписание")],
        [KeyboardButton(text="Добавить заметку"), KeyboardButton(text="Мои заметки")],
        [KeyboardButton(text="Добавить напоминание"), KeyboardButton(text="Подписаться на направление")],
        [KeyboardButton(text="Мои подписки")],
        [KeyboardButton(text="Настройки GPT"), KeyboardButton(text="Режим ответа на вопросы")],
        [KeyboardButton(text="Помощь")]
    ],
    resize_keyboard=True
)

gpt_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Режим помощника")],
        [KeyboardButton(text="Настройки GPT")]
    ],
    resize_keyboard=True
)

cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Отмена")]],
    resize_keyboard=True
)

notes_menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Назад")]
    ],
    resize_keyboard=True
)

# ----------------------------------------------------------------------------------
#                   Яндекс GPT: функция для ответа
# ----------------------------------------------------------------------------------
def ai_response(prompt: str, api_key: str, base_url: str, model_uri: str) -> str:
    """Получить ответ от модели через OpenAI-compatible API с настройками пользователя."""
    if not api_key or not model_uri:
        return "GPT не настроен. Нажмите «Настройки GPT» и укажите API-ключ и модель."

    try:
        import openai
    except Exception:
        return "Пакет 'openai' не установлен: pip install openai"

    # Нормализуем base_url: OpenAI SDK дописывает /chat/completions к base_url,
    # поэтому адрес должен заканчиваться на /v1 (или аналогичный суффикс провайдера).
    # Если пользователь ввёл URL без пути (только домен), пробуем добавить /v1.
    effective_url = base_url.rstrip("/") if base_url else None
    if effective_url and "/" not in effective_url.split("://", 1)[-1]:
        effective_url = effective_url + "/v1"

    logging.debug(f"[GPT] url={effective_url} model={model_uri}")

    def _call(url):
        client = openai.OpenAI(api_key=api_key, base_url=url)
        resp = client.chat.completions.create(
            model=model_uri,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.3,
        )
        return resp.choices[0].message.content or "Пустой ответ от модели."

    try:
        return _call(effective_url)
    except openai.NotFoundError:
        # 404 — возможно /v1 уже был в URL и не нужен, или путь другой
        logging.warning(f"[GPT] 404 on {effective_url}, retrying without /v1 suffix")
        try:
            alt_url = (base_url.rstrip("/") if base_url else None)
            return _call(alt_url)
        except Exception as e:
            logging.exception("[GPT] retry failed")
            return (
                f"Ошибка 404: сервер не нашёл эндпоинт /chat/completions.\n"
                f"Проверьте Base URL в настройках GPT — он должен включать /v1.\n"
                f"Пример: https://api.example.com/v1"
            )
    except openai.AuthenticationError:
        return "Ошибка аутентификации: неверный API-ключ. Проверьте настройки GPT."
    except openai.RateLimitError:
        return "Превышен лимит запросов к API. Попробуйте позже."
    except openai.BadRequestError as e:
        msg = str(e)
        is_yandex = bool(base_url and "yandex" in base_url.lower())
        if is_yandex or "parse model" in msg.lower() or "model uri" in msg.lower():
            return (
                "❌ Неверный формат модели.\n\n"
                "Для Yandex укажите модель в формате:\n"
                "<code>gpt://FOLDER_ID/yandexgpt/latest</code>\n\n"
                "Доступные модели:\n"
                "• <code>gpt://FOLDER_ID/yandexgpt/latest</code>\n"
                "• <code>gpt://FOLDER_ID/yandexgpt-lite/latest</code>\n\n"
                "FOLDER_ID — ID каталога в Yandex Cloud (консоль → Обзор каталога)."
            )
        return f"Ошибка запроса: {msg}"
    except Exception as e:
        logging.exception("[GPT] API error")
        return f"Ошибка API: {type(e).__name__}. Проверьте настройки GPT (ключ, URL, модель)."

# ----------------------------------------------------------------------------------
#       ФУНКЦИИ ДЛЯ УДАЛЕНИЯ/РЕДАКТИРОВАНИЯ ЗАМЕТОК И НАПОМИНАНИЙ
# ----------------------------------------------------------------------------------
def delete_note_from_db(note_id: int):
    """Удаляет заметку из базы данных по ID."""
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    conn.commit()
    conn.close()

    # После удаления перенумеруем заметки, чтобы убрать пробелы в id
    try:
        renumber_all_notes()
    except Exception:
        logging.exception("Не удалось перенумеровать заметки после удаления")


def delete_notes_from_db(note_ids: list):
    """Удаляет несколько заметок за одну операцию и перенумеровывает таблицу один раз."""
    if not note_ids:
        return
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    try:
        cursor.executemany("DELETE FROM notes WHERE id = ?", ((nid,) for nid in note_ids))
        conn.commit()
    finally:
        conn.close()

    # После удаления перенумеруем заметки один раз
    try:
        renumber_all_notes()
    except Exception:
        logging.exception("Не удалось перенумеровать заметки после массового удаления")


def renumber_all_notes():
    """Перенумеровывает все заметки в таблице `notes` подряд от 1 до N.
    Это пересоздаёт таблицу с сохранением user_id, note_text и timestamp,
    но присваивает новым записям новые id 1..N. Это полезно когда нужно
    чтобы id не имели пропусков после удаления.
    """
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, user_id, note_text, timestamp FROM notes ORDER BY id")
        rows = cursor.fetchall()
        logging.debug(f"[REN] current notes rows: {rows}")

        # Если нет строк — просто убедимся, что таблица существует и sqlite_sequence обнулён
        if len(rows) == 0:
            cursor.execute("DROP TABLE IF EXISTS notes")
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, note_text TEXT, timestamp TEXT)"
            )
            try:
                cursor.execute("DELETE FROM sqlite_sequence WHERE name='notes'")
            except Exception:
                pass
            conn.commit()
            return

        # Создаём временную таблицу с AUTOINCREMENT для аккуратного присвоения новых id
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN TRANSACTION")
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS notes_tmp (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, note_text TEXT, timestamp TEXT)"
        )

        # Вставляем строки без указания id, чтобы sqlite присвоил последовательные id начиная с 1
        for _old_id, user_id, note_text, timestamp in rows:
            cursor.execute(
                "INSERT INTO notes_tmp (user_id, note_text, timestamp) VALUES (?, ?, ?)",
                (user_id, note_text, timestamp),
            )

        # Удаляем старую таблицу и создаём новую с автонумерацией
        cursor.execute("DROP TABLE IF EXISTS notes")
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, note_text TEXT, timestamp TEXT)"
        )

        # Копируем данные обратно, сохраняя новые id
        cursor.execute(
            "INSERT INTO notes (id, user_id, note_text, timestamp) SELECT id, user_id, note_text, timestamp FROM notes_tmp ORDER BY id"
        )

        # Очистим временную таблицу
        cursor.execute("DROP TABLE IF EXISTS notes_tmp")

        # Обновим sqlite_sequence, чтобы AUTOINCREMENT знал максимальный id
        try:
            # Обновим sqlite_sequence, чтобы AUTOINCREMENT знал максимальный id
            new_max = len(rows)
            cursor.execute("DELETE FROM sqlite_sequence WHERE name='notes'")
            cursor.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('notes', ?)", (new_max,))
        except Exception:
            # sqlite_sequence может отсутствовать или поведение отличаться — игнорируем ошибку
            pass

        conn.commit()
        logging.debug(f"[REN] renumbered notes, new max id = {new_max}")
    except Exception:
        conn.rollback()
        logging.exception("Ошибка при перенумерации заметок")
    finally:
        cursor.execute("PRAGMA foreign_keys=ON")
        conn.close()

def update_note_in_db(note_id: int, new_text: str):
    """Обновляет текст заметки в базе данных по ID."""
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE notes SET note_text = ? WHERE id = ?", (new_text, note_id))
    conn.commit()
    conn.close()

def delete_reminder(reminder_id: int):
    """Удаляет напоминание из базы данных по ID."""
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    conn.commit()
    conn.close()

def update_reminder(reminder_id: int, new_text: str, new_time: str):
    """Обновляет текст и/или время напоминания в базе данных."""
    conn = sqlite3.connect("notes.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE reminders SET reminder_text = ?, remind_at = ? WHERE id = ?", 
                   (new_text, new_time, reminder_id))
    conn.commit()
    conn.close()

# ----------------------------------------------------------------------------------
#                                Хэндлеры
# ----------------------------------------------------------------------------------

@router.message(Command("start"))
async def start_command(message: Message):
    """
    Обработка /start. Сразу приветствуем и ставим режим "помощник".
    """
    user_modes[message.from_user.id] = "помощник"
    await message.answer(
        "Добро пожаловать! Я ваш помощник.\n"
        "Вы можете работать с расписанием, заметками, напоминаниями или GPT.\n\n"
        "Выберите действие:",
        reply_markup=assistant_keyboard
    )

@router.message()
async def handle_message(message: Message):
    if not message.text:
        return
    user_id = message.from_user.id
    user_mode = user_modes.get(user_id, "помощник")

    # 1. Переключение режимов
    if message.text in ("Режим ответа на вопросы", "Сменить режим"):
        if user_mode == "помощник":
            user_modes[user_id] = "gpt"
            await message.answer("Вы переключились в GPT. Спросите что-нибудь:", reply_markup=gpt_keyboard)
        else:
            user_modes[user_id] = "помощник"
            await message.answer("Вы вернулись в режим помощника. Выберите действие:", reply_markup=assistant_keyboard)
        return

    # кнопка в режиме GPT для возврата в помощник
    if message.text == "Режим помощника":
        user_modes[user_id] = "помощник"
        await message.answer("Вы вернулись в режим помощника. Выберите действие:", reply_markup=assistant_keyboard)
        return

    # Настройки GPT (доступны из любого режима)
    if message.text == "Настройки GPT":
        settings = get_gpt_settings(user_id)
        current = (
            f"Текущие настройки:\n"
            f"• API-ключ: {'✅ задан' if settings else '❌ не задан'}\n"
            f"• URL: {settings['base_url'] or '—' if settings else '—'}\n"
            f"• Модель: {settings['model_uri'] if settings else '—'}\n\n"
        ) if settings else "Настройки GPT не заданы.\n\n"
        user_states[user_id] = {"step": "gpt_api_key"}
        await message.answer(
            current +
            "Введите API-ключ (например, от OpenAI, Yandex Cloud или другого провайдера).\n"
            "Для OpenAI: https://platform.openai.com/api-keys\n"
            "Для Yandex Cloud: https://console.yandex.cloud → IAM → Сервисный аккаунт → Ключ API",
            reply_markup=cancel_keyboard
        )
        return

    if isinstance(user_states.get(user_id), dict) and user_states[user_id].get("step") == "gpt_api_key":
        if message.text.strip().lower() == "отмена":
            user_states.pop(user_id, None)
            await message.answer("Настройка отменена.", reply_markup=assistant_keyboard)
            return
        user_states[user_id]["api_key"] = message.text.strip()
        user_states[user_id]["step"] = "gpt_base_url"
        await message.answer(
            "Введите Base URL API (адрес сервера).\n\n"
            "⚠️ URL должен заканчиваться на /v1 (или аналог вашего провайдера).\n\n"
            "Примеры:\n"
            "• OpenAI: https://api.openai.com/v1\n"
            "• Yandex GPT: https://ai.api.cloud.yandex.net/v1\n"
            "• OpenRouter: https://openrouter.ai/api/v1\n"
            "• Другой прокси: https://api.example.com/v1\n\n"
            "Или введите «—» чтобы использовать OpenAI по умолчанию.",
            reply_markup=cancel_keyboard
        )
        return

    if isinstance(user_states.get(user_id), dict) and user_states[user_id].get("step") == "gpt_base_url":
        if message.text.strip().lower() == "отмена":
            user_states.pop(user_id, None)
            await message.answer("Настройка отменена.", reply_markup=assistant_keyboard)
            return
        raw_url = message.text.strip()
        user_states[user_id]["base_url"] = "" if raw_url == "—" else raw_url
        user_states[user_id]["step"] = "gpt_model"
        await message.answer(
            "Введите название модели.\n\n"
            "Примеры:\n"
            "• OpenAI: gpt-4o-mini  или  gpt-4o\n"
            "• OpenRouter: openai/gpt-4o-mini\n\n"
            "⚠️ Для Yandex GPT формат обязателен:\n"
            "gpt://FOLDER_ID/yandexgpt/latest\n"
            "gpt://FOLDER_ID/yandexgpt-lite/latest\n"
            "(FOLDER_ID — ID каталога из Yandex Cloud)",
            reply_markup=cancel_keyboard
        )
        return

    if isinstance(user_states.get(user_id), dict) and user_states[user_id].get("step") == "gpt_model":
        if message.text.strip().lower() == "отмена":
            user_states.pop(user_id, None)
            await message.answer("Настройка отменена.", reply_markup=assistant_keyboard)
            return
        model = message.text.strip()
        s = user_states.pop(user_id, {})
        save_gpt_settings(user_id, s["api_key"], s.get("base_url", ""), model)
        await message.answer(
            f"✅ GPT настроен!\n"
            f"• Модель: {model}\n"
            f"• URL: {s.get('base_url') or 'по умолчанию (OpenAI)'}\n\n"
            "Теперь нажмите «Режим ответа на вопросы» чтобы начать общение.",
            reply_markup=assistant_keyboard
        )
        return

    # 2. Режим GPT
    if user_mode == "gpt":
        settings = get_gpt_settings(user_id)
        if not settings:
            await message.answer(
                "GPT не настроен. Нажмите «Настройки GPT» и укажите API-ключ, URL и модель.",
                reply_markup=gpt_keyboard
            )
            return
        gpt_answer = await asyncio.to_thread(
            ai_response, message.text,
            settings["api_key"], settings["base_url"], settings["model_uri"]
        )
        await message.answer(gpt_answer, reply_markup=gpt_keyboard)
        return

    # === ДОБАВЛЕННЫЕ КОМАНДЫ УДАЛЕНИЯ/РЕДАКТИРОВАНИЯ ЗАМЕТОК И НАПОМИНАНИЙ ===

    # Редактирование заметки
    # Интерактивное редактирование заметки: если пользователь отправил точную команду
    if message.text == "Редактировать заметку":
        notes = get_notes_for_user(user_id)
        if not notes:
            await message.answer("У вас нет заметок.", reply_markup=assistant_keyboard)
            return
        text = "Выберите ID заметки для редактирования:\n\n"
        for nid, text_, ts in notes:
            text += f"[{nid}] {text_}\nСоздана: {ts}\n\n"
        user_states[user_id] = {"step": "editing_choose"}
        await message.answer(text + "Введите ID (например: 1) или 'Назад' для отмены.", reply_markup=notes_menu_keyboard)
        return

    # Поддержка однострочной команды редактирования: 'Редактировать заметку <ID> Новый текст'
    if message.text.startswith("Редактировать заметку "):
        parts = message.text.split(" ", 2)
        if len(parts) < 3:
            await message.answer("Формат: 'Редактировать заметку ID Новый текст'")
            return
        try:
            note_id = int(parts[1])
            new_text = parts[2]
            update_note_in_db(note_id, new_text)
            await message.answer(f"Заметка {note_id} обновлена.")
        except ValueError:
            await message.answer("Некорректный ID заметки.")
        return

    # Удаление напоминания
    if message.text.startswith("Удалить напоминание "):
        try:
            reminder_id = int(message.text.split()[-1])
            delete_reminder(reminder_id)
            await message.answer(f"Напоминание {reminder_id} удалено.")
        except ValueError:
            await message.answer("Некорректный ID напоминания.")
        return

    # Редактирование напоминания
    if message.text.startswith("Редактировать напоминание "):
        parts = message.text.split(" ", 3)
        if len(parts) < 4:
            await message.answer("Формат: 'Редактировать напоминание ID Новый текст ГГГГ-ММ-ДД ЧЧ:ММ'")
            return
        try:
            reminder_id = int(parts[1])
            new_text = parts[2]
            new_time = parts[3]
            update_reminder(reminder_id, new_text, new_time)
            await message.answer(f"Напоминание {reminder_id} обновлено.")
        except ValueError:
            await message.answer("Некорректный ID напоминания.")
        return

    # Удаление заметки через команду (поддерживает множественные ID через запятую или пробел)
    if message.text.startswith("Удалить заметку"):
        try:
            ids_text = message.text[len("Удалить заметку"):].strip()
            if not ids_text:
                await message.answer("Пожалуйста, укажите ID заметки. Пример: Удалить заметку 1")
                return

            parts = re.split(r"[\s,]+", ids_text)
            ids = [int(p) for p in parts if p.strip()]

            notes = get_notes_for_user(user_id)
            available_ids = {n[0] for n in notes}

            to_delete = [i for i in ids if i in available_ids]
            not_found = [i for i in ids if i not in available_ids]

            if to_delete:
                delete_notes_from_db(to_delete)

            # Собираем ответ пользователю: какие удалены, какие не найдены, и список оставшихся
            resp_parts = []
            if to_delete:
                resp_parts.append("Удалены заметки: " + ", ".join(map(str, to_delete)) + ".")
            if not_found:
                resp_parts.append("Не найдены заметки с ID: " + ", ".join(map(str, not_found)) + ".")

            remaining = get_notes_for_user(user_id)
            if remaining:
                rem_text = "Оставшиеся заметки:\n\n"
                for nid, text_, ts in remaining:
                    rem_text += f"[{nid}] {text_}\nСоздана: {ts}\n\n"
                resp_parts.append(rem_text)
            else:
                resp_parts.append("У вас нет заметок.")

            await message.answer("\n".join(resp_parts), reply_markup=notes_menu_keyboard)
        except ValueError:
            await message.answer("Некорректный формат команды. Укажите ID заметки числом(а). Пример: Удалить заметку 1 или Удалить заметку 1,2,3")
        return

    # 3. Режим помощника
    # 3.1 Добавление заметки
    if message.text == "Добавить заметку":
        user_states[user_id] = "adding_note"
        await message.answer("Введите текст заметки:", reply_markup=cancel_keyboard)
        return

    if user_states.get(user_id) == "adding_note":
        note_text = message.text.strip()
        if note_text.lower() == "отмена":
            await message.answer("Добавление заметки отменено.", reply_markup=assistant_keyboard)
            user_states.pop(user_id, None)
            return
        if note_text:
            add_note_to_db(user_id, note_text)
            await message.answer("Заметка сохранена!", reply_markup=assistant_keyboard)
        else:
            await message.answer("Текст заметки не может быть пустым. Попробуйте ещё раз.", reply_markup=cancel_keyboard)
        user_states.pop(user_id, None)
        return

    # 3.2 Просмотр заметок
    if message.text == "Мои заметки":
        notes = get_notes_for_user(user_id)
        if notes:
            answer_text = "Ваши заметки:\n\n"
            for nid, text_, ts in notes:
                answer_text += f"[{nid}] {text_}\nСоздана: {ts}\n\n"
        else:
            answer_text = "У вас нет заметок."
        user_states[user_id] = "deleting_note"
        await message.answer(answer_text + "\nВведите только цифру (ID заметки) для удаления или 'Назад' для возврата.", reply_markup=notes_menu_keyboard)
        return

    # Подменю заметок
    if user_states.get(user_id) == "deleting_note":
        if message.text == "Назад":
            user_states.pop(user_id, None)
            await message.answer("Вы вернулись в главное меню.", reply_markup=assistant_keyboard)
            return
        # Поддерживаем ввод одного ID или нескольких через запятую/пробел
        ids_text = message.text.strip()
        # Попробуем распарсить числа
        parts = re.split(r"[\s,]+", ids_text)
        try:
            ids = [int(p) for p in parts if p.strip()]
        except ValueError:
            await message.answer("Пожалуйста, введите только цифры (ID заметки) через запятую или 'Назад'.", reply_markup=notes_menu_keyboard)
            return

        notes = get_notes_for_user(user_id)
        available_ids = {n[0] for n in notes}
        to_delete = [i for i in ids if i in available_ids]
        not_found = [i for i in ids if i not in available_ids]

        if to_delete:
            delete_notes_from_db(to_delete)

        resp_parts = []
        if to_delete:
            resp_parts.append("Удалены заметки: " + ", ".join(map(str, to_delete)) + ".")
        if not_found:
            resp_parts.append("Не найдены заметки с ID: " + ", ".join(map(str, not_found)) + ".")

        remaining = get_notes_for_user(user_id)
        if remaining:
            rem_text = "Оставшиеся заметки:\n\n"
            for nid, text_, ts in remaining:
                rem_text += f"[{nid}] {text_}\nСоздана: {ts}\n\n"
            resp_parts.append(rem_text)
        else:
            resp_parts.append("У вас нет заметок.")

        await message.answer("\n".join(resp_parts), reply_markup=notes_menu_keyboard)
        return

    # Если пользователь в процессе редактирования (интерактивный режим)
    if isinstance(user_states.get(user_id), dict) and user_states[user_id].get("step") == "editing_choose":
        # Ожидаем ID заметки или 'Назад'
        if message.text == "Назад":
            user_states.pop(user_id, None)
            await message.answer("Редактирование отменено.", reply_markup=assistant_keyboard)
            return
        if not message.text.strip().isdigit():
            await message.answer("Введите ID числом или 'Назад'.", reply_markup=notes_menu_keyboard)
            return
        note_id = int(message.text.strip())
        notes = get_notes_for_user(user_id)
        if not any(n[0] == note_id for n in notes):
            await message.answer(f"Заметка с ID {note_id} не найдена.", reply_markup=notes_menu_keyboard)
            return
        # Запрашиваем новый текст
        user_states[user_id] = {"step": "editing_text", "note_id": note_id}
        await message.answer(f"Введите новый текст для заметки {note_id} или 'Отмена' to cancel:", reply_markup=cancel_keyboard)
        return

    if isinstance(user_states.get(user_id), dict) and user_states[user_id].get("step") == "editing_text":
        note_id = user_states[user_id].get("note_id")
        if message.text.lower() == "отмена":
            user_states.pop(user_id, None)
            await message.answer("Редактирование отменено.", reply_markup=assistant_keyboard)
            return
        new_text = message.text.strip()
        if not new_text:
            await message.answer("Текст не может быть пустым. Введите новый текст или 'Отмена'.", reply_markup=cancel_keyboard)
            return
        update_note_in_db(note_id, new_text)
        user_states.pop(user_id, None)
        # Ответ с подтверждением и списком оставшихся заметок
        remaining = get_notes_for_user(user_id)
        rem_text = f"Заметка {note_id} обновлена.\n\nВаши заметки:\n\n"
        for nid, text_, ts in remaining:
            rem_text += f"[{nid}] {text_}\nСоздана: {ts}\n\n"
        await message.answer(rem_text, reply_markup=assistant_keyboard)
        return

    # 3.3 Добавить напоминание
    if message.text == "Добавить напоминание":
        # Шаг 1: попросим ввести текст напоминания
        user_states[user_id] = {"step": "reminder_text"}
        await message.answer("Введите текст напоминания:", reply_markup=assistant_keyboard)
        return

    # Если пользователь в процессе добавления напоминания
    if isinstance(user_states.get(user_id), dict) and user_states[user_id].get("step") == "reminder_text":
        # Сохраняем текст, переходим к запросу времени
        user_states[user_id]["reminder_text"] = message.text
        user_states[user_id]["step"] = "reminder_time"
        await message.answer(
            "Теперь введите время напоминания в формате ГГГГ-ММ-ДД ЧЧ:ММ\n"
            "Например: 2025-03-08 13:45",
            reply_markup=assistant_keyboard
        )
        return

    if isinstance(user_states.get(user_id), dict) and user_states[user_id].get("step") == "reminder_time":
        reminder_text = user_states[user_id].get("reminder_text")
        remind_at_str = message.text.strip()
        # Тут можно проверить корректность формата, но для простоты попробуем сразу записать
        try:
            # Проверим, парсится ли
            datetime.datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M")
        except ValueError:
            await message.answer("Неверный формат даты/времени. Попробуйте ещё раз (ГГГГ-ММ-ДД ЧЧ:ММ).")
            return

        # Если всё ок, добавляем в БД
        add_reminder(user_id, reminder_text, remind_at_str)
        await message.answer(f"Напоминание '{reminder_text}' создано на {remind_at_str}", reply_markup=assistant_keyboard)
        user_states.pop(user_id, None)
        return

    # 3.4 Расписание
    if message.text == "Расписание":
        user_states[user_id] = "choosing_course"
        await message.answer("Выберите курс:", reply_markup=course_keyboard)
        return

    # Подписка на направление (логин по направлению)
    if message.text == "Подписаться на направление":
        user_states[user_id] = "sub_choosing_course"
        await message.answer("Выберите курс для подписки:", reply_markup=course_keyboard)
        return

    if user_states.get(user_id) == "sub_choosing_course":
        if message.text == "Назад":
            user_states.pop(user_id, None)
            await message.answer("Отменено.", reply_markup=assistant_keyboard)
            return
        if message.text.isdigit() and int(message.text) in directions_by_course:
            course = int(message.text)
            user_states[user_id] = {"course": course, "step": "sub_choosing_direction"}
            await message.answer("Выберите направление для подписки:", reply_markup=direction_keyboard_for(course))
        else:
            await message.answer("Пожалуйста, выберите корректный курс.")
        return

    if isinstance(user_states.get(user_id), dict) and user_states[user_id].get("step") == "sub_choosing_direction":
        if message.text == "Назад":
            user_states[user_id] = "sub_choosing_course"
            await message.answer("Выберите курс для подписки:", reply_markup=course_keyboard)
            return
        course = user_states[user_id]["course"]
        full_dir = resolve_direction_label(message.text, course)
        if full_dir is not None:
            add_subscription(user_id, course, full_dir)
            user_states.pop(user_id, None)
            await message.answer(f"Вы подписаны на {course} курс, направление {full_dir}.", reply_markup=assistant_keyboard)
        else:
            await message.answer("Пожалуйста, выберите направление из списка.")
        return

    # Просмотр/удаление подписок
    if message.text == "Мои подписки":
        try:
            subs = get_subscriptions_for_user(user_id)
        except Exception:
            logging.exception(f"Ошибка при получении подписок для пользователя {user_id}")
            await message.answer("Не удалось получить подписки. Попробуйте позже.", reply_markup=assistant_keyboard)
            return

        logging.debug(f"[SUBS] user={user_id} subs={subs}")

        if subs:
            text = "Ваши подписки:\n\n"
            for sid, course, direction in subs:
                text += f"[{sid}] {course} - {direction}\n"
            text += "\nЧтобы удалить подписку, введите просто её ID (только цифру), или 'Назад' для отмены."
            user_states[user_id] = "managing_subs"
            managing_keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text='Назад')]], resize_keyboard=True)
            await message.answer(text, reply_markup=managing_keyboard)
        else:
            await message.answer("У вас нет подписок.", reply_markup=assistant_keyboard)
        return

    if user_states.get(user_id) == "managing_subs":
        if message.text == "Назад":
            user_states.pop(user_id, None)
            await message.answer("Вы вернулись в главное меню.", reply_markup=assistant_keyboard)
            return
        if message.text.isdigit():
            sub_id = int(message.text)
            delete_subscription_by_id(sub_id)
            await message.answer(f"Подписка {sub_id} удалена.", reply_markup=assistant_keyboard)
            user_states.pop(user_id, None)
        else:
            await message.answer("Пожалуйста, введите только ID подписки (цифру) или 'Назад'.")
        return

    # выбор курса
    if user_states.get(user_id) == "choosing_course":
        # Обработка кнопки "Назад" при выборе курса
        if message.text == "Назад":
            user_states.pop(user_id, None)
            await message.answer("Вы вернулись в главное меню.", reply_markup=assistant_keyboard)
            return

        if message.text.isdigit() and int(message.text) in directions_by_course:
            course = int(message.text)
            user_states[user_id] = {"course": course, "step": "choosing_direction"}
            await message.answer("Выберите направление:", reply_markup=direction_keyboard_for(course))
        else:
            await message.answer("Пожалуйста, выберите корректный курс.")
        return

    # выбор направления
    if isinstance(user_states.get(user_id), dict) and user_states[user_id].get("step") == "choosing_direction":
        course = user_states[user_id]["course"]
        if message.text == "Назад":
            user_states[user_id] = "choosing_course"
            await message.answer("Выберите курс:", reply_markup=course_keyboard)
            return

        full_dir = resolve_direction_label(message.text, course)
        if full_dir is not None:
            user_states[user_id]["direction"] = full_dir
            user_states[user_id]["step"] = "choosing_day"
            await message.answer("Выберите день недели:", reply_markup=day_keyboard)
        else:
            await message.answer("Пожалуйста, выберите направление из списка.")
        return

    # выбор дня
    if isinstance(user_states.get(user_id), dict) and user_states[user_id].get("step") == "choosing_day":
        course = user_states[user_id]["course"]
        direction = user_states[user_id]["direction"]
        if message.text == "Назад":
            user_states[user_id]["step"] = "choosing_direction"
            await message.answer("Выберите направление:", reply_markup=direction_keyboard_for(course))
            return

        schedule_text = get_schedule_by_course(course, direction, message.text)
        user_states.pop(user_id, None)
        await message.answer(
            f"Расписание для {course}-го курса ({direction}) на {message.text}:\n\n{schedule_text}",
            reply_markup=assistant_keyboard
        )
        return

    # 3.5 Помощь или неизвестная команда
    if message.text == "Помощь":
        await message.answer(
            "Как пользоваться ботом — краткое руководство:\n\n"
            "1) Расписание\n"
            "- Нажмите 'Показать расписание', выберите курс и направление, потом день недели.\n\n"
            "2) Заметки\n"
            "- Добавить заметку: нажмите 'Добавить заметку' и введите текст.\n"
            "- Просмотреть: нажмите 'Мои заметки' — вы увидите список с ID.\n"
            "- Удалить: в подменю 'Мои заметки' введите ID заметки (например '1'),\n"
            "  (поддерживаются несколько ID: '1,2,3' или через пробел).\n"
            "- После удаления ID автоматически перенумеровываются подряд (начиная с 1).\n\n"
            "3) Напоминания\n"
            "- Создание: нажмите 'Добавить напоминание', сначала введите текст, затем время в формате 'ГГГГ-MM-ДД ЧЧ:ММ' (пример: 2025-03-08 13:45).\n"
            "4) Подписки и уведомления о парах\n"
            "- Подписаться: нажмите 'Подписаться на направление', выберите курс и направление.\n"
            "- Мои подписки: покажет ваши подписки и их ID; удалять подписку можно по ID.\n"
            "- Уведомления: бот пришлёт сообщение за ~10 минут до начала пары и сообщение о завершении пары для подписанных направлений.\n\n"
            "5) Режим GPT (ответ на вопросы)\n"
            "- Переключение: нажмите 'Режим ответа на вопросы' для GPT; введите вопрос — бот ответит.\n"
            "- Вернуться в помощник: нажмите 'Режим помощника' или используйте главное меню.\n\n"
            "Если что-то пойдёт не так — напишите 'Помощь' снова, чтобы увидеть это сообщение.\n\n"
            "Выберите действие:",
            reply_markup=assistant_keyboard
        )
    else:
        await message.answer("Неизвестная команда. Выберите действие:", reply_markup=assistant_keyboard)

# ----------------------------------------------------------------------------------
#                               Запуск бота
# ----------------------------------------------------------------------------------

async def main():
    global bot
    bot = await _build_bot()

    # 1. Создаём нужные таблицы
    create_notes_tables()

    # 2. Запускаем фоновую задачи
    asyncio.create_task(reminder_checker())
    asyncio.create_task(proxy_watchdog())
    # Фоновая задача для уведомлений о парах
    asyncio.create_task(class_notification_checker())

    # 3. Запускаем поллинг
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
