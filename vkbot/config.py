"""Конфигурация: env, пути БД, тайминги, MSK."""

from __future__ import annotations

import datetime
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── VK API ────────────────────────────────────────────────────────────────────
VK_TOKEN: str = os.getenv("VK_TOKEN", "")
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))

# ── Пути БД ───────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
NOTES_DB: str = str(ROOT / "notes.db")
# ВНИМАНИЕ: в имени файла буква 'с' — кириллическая (U+0441), не латинская 'c'.
# Сохранено для обратной совместимости с существующей БД на проде.
SCHEDULE_DB: str = str(ROOT / "sсhedule.db")

# Директория для версий загруженных Excel-файлов
SCHEDULE_VERSIONS_DIR: Path = ROOT / "schedule_versions"
SCHEDULE_VERSIONS_DIR.mkdir(exist_ok=True)

# Файл-маркер для hot-reload расписания (mtime отслеживается воркером)
SCHEDULE_RELOAD_MARKER: Path = ROOT / ".schedule_reload"

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

# ── Часовой пояс ──────────────────────────────────────────────────────────────
MSK = datetime.timezone(datetime.timedelta(hours=3))


def now_msk() -> datetime.datetime:
    """Текущее время МСК как naive datetime (для сравнения со строками в БД)."""
    return datetime.datetime.now(MSK).replace(tzinfo=None)
