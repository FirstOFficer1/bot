"""Отметки живости фоновых воркеров.

Зачем: systemd видит только смерть процесса. Отказ, который реально случался, —
воркер молча перестал работать, а бот продолжает отвечать на сообщения; снаружи
всё «зелено», а напоминания и пуши о парах не приходят. Каждый воркер отмечается
здесь в конце тика, а панель отдаёт эти отметки в /healthz.

Время пишем в МСК (`now_msk`), как и всю предметную логику, — чтобы значение
можно было сравнивать с расписанием и не зависеть от таймзоны сервера.
"""

from __future__ import annotations

import datetime

from ..config import now_msk
from ..db import connect

# Имена воркеров — единый словарь для бота и панели.
REMINDERS = "reminders"
DEADLINES = "deadlines"
CLASSES = "classes"
SCHEDULE_RELOADER = "schedule_reloader"

ALL = (REMINDERS, DEADLINES, CLASSES, SCHEDULE_RELOADER)


def mark(name: str) -> None:
    """Фиксирует успешный тик воркера. Падать не должна — это телеметрия."""
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO worker_heartbeats (name, last_tick, ticks) "
                "VALUES (?, ?, 1) "
                "ON CONFLICT(name) DO UPDATE SET "
                "last_tick=excluded.last_tick, ticks=worker_heartbeats.ticks+1",
                (name, now_msk().isoformat(timespec="seconds")),
            )
    except Exception:
        pass


def all_ticks() -> dict[str, str]:
    """{имя воркера: время последнего тика}. Отсутствующие ключи = ни разу не тикал."""
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT name, last_tick FROM worker_heartbeats"
            ).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def age_seconds(last_tick: str | None) -> float | None:
    """Сколько секунд прошло с отметки. None, если отметки нет или она битая."""
    if not last_tick:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(last_tick)
    except ValueError:
        return None
    return (now_msk() - parsed).total_seconds()
