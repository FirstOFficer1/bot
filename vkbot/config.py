"""Конфигурация: env, пути БД, тайминги, MSK."""

from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_log = logging.getLogger(__name__)

# ── VK API ────────────────────────────────────────────────────────────────────
VK_TOKEN: str = os.getenv("VK_TOKEN", "")

# ── Telegram (опционально) ────────────────────────────────────────────────────
# Токен от @BotFather. Без него процесс `python tg_bot.py` не стартует.
# VK-процесс его не требует; пуши TG-пользователям (uid < 0) идут из tg-процесса.
TELEGRAM_BOT_TOKEN: str = (
    os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or ""
).strip()


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


def env_owner_ids() -> set[int]:
    """Владельцы из env: ADMIN_ID / ADMIN_VK_ID / ADMIN_VK_IDS.

    То же множество, что панель считает несменяемым якорем — бот использует
    его, когда нужно достучаться до всех владельцев (например, фидбэк).
    """
    ids: set[int] = set()
    for key in ("ADMIN_VK_ID", "ADMIN_ID"):
        raw = (os.getenv(key) or "").strip()
        if raw.isdigit():
            ids.add(int(raw))
    for part in (os.getenv("ADMIN_VK_IDS") or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


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

# Раньше файл звался sсhedule.db с кириллической «с» (U+0441). Канон теперь
# латинский schedule.db; при резолве пути старый файл одноразово
# переименовывается (вместе с -wal/-shm), если нового ещё нет.
SCHEDULE_DB_NAME = "schedule.db"
_LEGACY_CYRILLIC_SCHEDULE_NAME = "s\u0441hedule.db"


def migrate_legacy_schedule_db(data_dir: Path | None = None) -> Path | None:
    """sсhedule.db (кириллица) → schedule.db. None, если переносить было нечего."""
    root = Path(data_dir) if data_dir is not None else DATA_DIR
    latin = root / SCHEDULE_DB_NAME
    legacy = root / _LEGACY_CYRILLIC_SCHEDULE_NAME
    if latin.exists() or not legacy.exists():
        return None
    for suffix in ("", "-wal", "-shm"):
        src = root / f"{_LEGACY_CYRILLIC_SCHEDULE_NAME}{suffix}"
        dst = root / f"{SCHEDULE_DB_NAME}{suffix}"
        if not src.exists() or dst.exists():
            continue
        try:
            src.rename(dst)
        except OSError as exc:
            # Параллельный старт бота и панели: второй процесс уже перенёс файл.
            _log.warning("не удалось переименовать %s → %s: %s", src.name, dst.name, exc)
            continue
        _log.warning("расписание: %s → %s", src.name, dst.name)
    return latin if latin.exists() else None


def _default_schedule_db() -> str:
    migrate_legacy_schedule_db(DATA_DIR)
    return str(DATA_DIR / SCHEDULE_DB_NAME)


SCHEDULE_DB: str = os.getenv("SCHEDULE_DB") or _default_schedule_db()

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
