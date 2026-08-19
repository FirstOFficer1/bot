"""Аудит-лог админских действий: кто, когда, что сделал.

Пишется в таблицу audit_log (создаётся в db.init).
Используется панелью для подотчётности: выдача/снятие админа, загрузка
расписания, рассылки, отзыв сессий и т.д.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from ..config import now_msk

from ..db import connect


def _now() -> str:
    return now_msk().isoformat(timespec="seconds")


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


def cleanup_older_than(days: int) -> int:
    """Удаляет события старше N дней. Возвращает, сколько удалил.

    Журнал безопасности рос неограниченно: каждый вход, каждая загрузка
    расписания и каждая рассылка — строка навсегда. Срок хранения задаётся
    `config.AUDIT_KEEP_DAYS`.
    """
    cutoff = (now_msk() - timedelta(days=days)).isoformat(timespec="seconds")
    try:
        with connect() as conn:
            cur = conn.execute("DELETE FROM audit_log WHERE created_at < ?", (cutoff,))
            return cur.rowcount or 0
    except Exception:
        logging.exception("Не удалось почистить audit_log")
        return 0


def list_recent(
    limit: int = 200,
    *,
    action_prefix: str = "",
    actor_vk_id: int | None = None,
    since: str = "",
) -> list[dict]:
    """Список последних событий с опциональными фильтрами.

    action_prefix: фильтр по группе ('auth.', 'admin.', 'schedule.', 'broadcast.').
    actor_vk_id: только события от конкретного юзера.
    since: ISO-дата 'YYYY-MM-DD' — события не раньше этой даты.
    """
    sql = ["SELECT id, actor_vk_id, action, target, details, created_at FROM audit_log"]
    conds: list[str] = []
    params: list = []
    if action_prefix:
        conds.append("action LIKE ?")
        params.append(action_prefix + "%")
    if actor_vk_id and actor_vk_id > 0:
        conds.append("actor_vk_id = ?")
        params.append(actor_vk_id)
    if since:
        conds.append("created_at >= ?")
        params.append(since)
    if conds:
        sql.append("WHERE " + " AND ".join(conds))
    sql.append("ORDER BY id DESC LIMIT ?")
    params.append(int(limit))
    try:
        with connect() as conn:
            rows = conn.execute(" ".join(sql), params).fetchall()
        return [
            {
                "id": r[0], "actor_vk_id": r[1], "action": r[2],
                "target": r[3], "details": r[4], "created_at": r[5],
            }
            for r in rows
        ]
    except Exception:
        return []


def distinct_action_prefixes() -> list[str]:
    """Возвращает уникальные префиксы action ('auth', 'admin', 'schedule'…)."""
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT substr(action, 1, instr(action, '.') - 1) AS prefix "
                "FROM audit_log WHERE action LIKE '%.%' "
                "ORDER BY prefix"
            ).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def distinct_actors() -> list[int]:
    """Список VK ID, у кого есть хоть одно событие в логе."""
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT actor_vk_id FROM audit_log "
                "WHERE actor_vk_id > 0 ORDER BY actor_vk_id"
            ).fetchall()
        return [int(r[0]) for r in rows]
    except Exception:
        return []
