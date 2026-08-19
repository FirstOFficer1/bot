"""Общая настройка тестов.

Главное: перенаправить БД в временную директорию ДО импорта пакета `vkbot` —
пути к базам вычисляются на уровне модуля `vkbot.config`, поэтому env нужно
выставить раньше первого импорта. conftest выполняется до сбора тестов, так что
достаточно сделать это здесь на уровне модуля.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="vkbot-tests-"))

os.environ["DATA_DIR"] = str(_TMP)
os.environ["NOTES_DB"] = str(_TMP / "notes.db")
os.environ["SCHEDULE_DB"] = str(_TMP / "schedule.db")
os.environ["SCHEDULE_VERSIONS_DIR"] = str(_TMP / "versions")
os.environ["SCHEDULE_RELOAD_MARKER"] = str(_TMP / ".schedule_reload")

# Пустой токен: vk_names/notifier тогда не ходят в сеть.
os.environ["VK_TOKEN"] = ""
os.environ["ADMIN_ID"] = "1001"
os.environ["PANEL_SECRET"] = "test-secret-not-for-production"
os.environ["PANEL_BASE_URL"] = "http://127.0.0.1:5000"
os.environ["PANEL_TRUSTED_PROXIES"] = "0"
os.environ["UPLOAD_API_TOKEN"] = "test-api-token"

import pytest  # noqa: E402

from vkbot import db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Схема создаётся один раз на сессию тестов."""
    db.init()


@pytest.fixture(autouse=True)
def _clean_tables():
    """Между тестами чистим пользовательские таблицы."""
    yield
    with db.connect() as conn:
        for table in (
            "notes", "reminders", "deadlines", "subscriptions", "user_states",
            "user_prefs", "sent_class_notifications", "panel_login_codes",
            "panel_users", "panel_remember_tokens", "seen_users", "audit_log",
            "worker_heartbeats",
        ):
            conn.execute(f"DELETE FROM {table}")


@pytest.fixture
def admin_id() -> int:
    return int(os.environ["ADMIN_ID"])
