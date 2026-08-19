"""Регистр всех VK-юзеров, которых видел бот или панель.

Обновляется на каждое сообщение боту и на каждый успешный логин в панель.
Нужен, чтобы новый пользователь моментально появлялся в /admin/users —
даже если он ещё ничего не сохранил в notes/reminders/subscriptions.
"""

from __future__ import annotations


from ..config import now_msk

from ..db import connect


def _now() -> str:
    return now_msk().isoformat(timespec="seconds")


def touch(vk_id: int) -> None:
    """Регистрирует или обновляет last_seen для VK ID. Идемпотентно."""
    if not vk_id or vk_id <= 0:
        return
    now = _now()
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO seen_users (vk_id, first_seen, last_seen) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(vk_id) DO UPDATE SET last_seen=excluded.last_seen",
                (vk_id, now, now),
            )
    except Exception:
        # Молча: фоновый трекинг не должен ронять обработку сообщения.
        pass


def list_all() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT vk_id, first_seen, last_seen FROM seen_users"
        ).fetchall()
    return [
        {"vk_id": r[0], "first_seen": r[1], "last_seen": r[2]}
        for r in rows
    ]
