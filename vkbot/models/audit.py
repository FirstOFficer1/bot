"""Аудит-лог админских действий: кто, когда, что сделал.

Пишется в таблицу audit_log (создаётся в db.init).
Используется панелью для подотчётности: выдача/снятие админа, загрузка
расписания, рассылки, отзыв сессий и т.д.
"""

from __future__ import annotations

from datetime import datetime

from ..db import connect


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log(actor_vk_id: int | None, action: str, target: str = "", details: str = "") -> None:
    """Атомарная запись. Падать не должно никогда — это лог."""
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (actor_vk_id, action, target, details, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (actor_vk_id or 0, action, target or "", details or "", _now()),
            )
    except Exception:
        pass


def list_recent(limit: int = 200) -> list[dict]:
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, actor_vk_id, action, target, details, created_at "
                "FROM audit_log ORDER BY id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [
            {
                "id": r[0], "actor_vk_id": r[1], "action": r[2],
                "target": r[3], "details": r[4], "created_at": r[5],
            }
            for r in rows
        ]
    except Exception:
        return []
