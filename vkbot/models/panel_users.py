"""CRUD для админов веб-панели.

Owner — это VK ID из env ADMIN_ID (или ADMIN_VK_IDS); он всегда админ
и не может быть удалён через панель. Остальные админы хранятся здесь.
"""

from __future__ import annotations

from datetime import datetime

from ..db import connect


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def grant(vk_id: int, granted_by: int, name: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO panel_users (vk_id, role, name, added_at, added_by) "
            "VALUES (?, 'admin', ?, ?, ?)",
            (vk_id, name, _now(), granted_by),
        )


def revoke(vk_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM panel_users WHERE vk_id=?", (vk_id,))


def is_admin(vk_id: int) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM panel_users WHERE vk_id=?", (vk_id,)
        ).fetchone() is not None


def list_all() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT vk_id, role, name, added_at, added_by "
            "FROM panel_users ORDER BY added_at DESC"
        ).fetchall()
    return [
        {
            "vk_id": r[0],
            "role": r[1],
            "name": r[2],
            "added_at": r[3],
            "added_by": r[4],
        }
        for r in rows
    ]


def update_name(vk_id: int, name: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE panel_users SET name=? WHERE vk_id=?", (name, vk_id))
