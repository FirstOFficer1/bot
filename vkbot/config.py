"""Конфигурация: env, пути БД, тайминги, MSK."""

from __future__ import annotations

import datetime
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── VK API ────────────────────────────────────────────────────────────────────
VK_TOKEN: str = os.getenv("VK_TOKEN", "")


def int_env(name: str, default: int = 0) -> int:
    """Целое из env, устойчивое к пустому и мусорному значению.

    В .env.example переменные лежат пустыми (`ADMIN_ID=`), и голый int("")
    ронял импорт и бота, и панели — с ValueError вместо внятного сообщения.
    """
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


ADMIN_ID: int = int_env("ADMIN_ID")

# Публичный адрес панели. Боту он нужен, чтобы дать ссылку на полный текст
# согласия и политику: в чате их не разместишь, а показать до сбора данных
# обязаны. Тот же адрес читает web_panel — держите значения одинаковыми.
PANEL_BASE_URL: str = (os.getenv("PANEL_BASE_URL") or "").rstrip("/")

# ── Пути БД ───────────────────────────────────────────────────────────────────
# Пути абсолютные (от корня проекта), а не относительные cwd: иначе сервис,
# запущенный из другой директории, открывал бы вторую пустую БД.
# Переопределяются через env — удобно для тестов и для выноса данных на volume.
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR: Path = Path(os.getenv("DATA_DIR") or ROOT)

NOTES_DB: str = os.getenv("NOTES_DB") or str(DATA_DIR / "notes.db")
# ВНИМАНИЕ: в имени файла буква 'с' — кириллическая (U+0441), не латинская 'c'.
# Сохранено для обратной совместимости с существующей БД на проде.
SCHEDULE_DB: str = os.getenv("SCHEDULE_DB") or str(DATA_DIR / "sсhedule.db")

# Директория для версий загруженных Excel-файлов
SCHEDULE_VERSIONS_DIR: Path = Path(
    os.getenv("SCHEDULE_VERSIONS_DIR") or (DATA_DIR / "schedule_versions")
)
SCHEDULE_VERSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Файл-маркер для hot-reload расписания (mtime отслеживается воркером)
SCHEDULE_RELOAD_MARKER: Path = Path(
    os.getenv("SCHEDULE_RELOAD_MARKER") or (DATA_DIR / ".schedule_reload")
)

# ── Лимиты и тайминги ─────────────────────────────────────────────────────────
NOTES_LIMIT: int = 50
MAX_INPUT_LEN: int = 2000              # максимум символов в одном пользовательском сообщении

REMINDER_POLL_SEC: int = 30
DEADLINE_POLL_SEC: int = 60
CLASS_NOTIFY_POLL_SEC: int = 30
SCHEDULE_RELOAD_POLL_SEC: int = 60

CLASS_NOTIFY_BEFORE_MIN: int = 10      # за сколько минут предупреждать о паре
CLASS_DURATION_MIN: int = 90
DEADLINE_CLEANUP_DAYS: int = 7         # удаление просроченных дедлайнов
SENT_NOTIFS_CLEANUP_DAYS: int = 2
PANEL_CODES_CLEANUP_DAYS: int = 1      # использованные и просроченные коды входа
AUDIT_KEEP_DAYS: int = 365             # журнал действий: год, потом удаляется
HOUSEKEEPING_EVERY_SEC: int = 3600     # как часто воркер дедлайнов чистит таблицы

# ── Часовой пояс ──────────────────────────────────────────────────────────────
MSK = datetime.timezone(datetime.timedelta(hours=3))


def now_msk() -> datetime.datetime:
    """Текущее время МСК как naive datetime (для сравнения со строками в БД)."""
    return datetime.datetime.now(MSK).replace(tzinfo=None)
