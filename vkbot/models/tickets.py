"""Тикеты техподдержки."""

from __future__ import annotations

from ..config import now_msk
from ..db import connect

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"
DIR_USER = "user"
DIR_STAFF = "staff"


def _now() -> str:
    return now_msk().isoformat(timespec="seconds")


def open_or_get(user_id: int) -> dict:
    """Открытый тикет пользователя или новый."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id, user_id, status, created_at, updated_at "
            "FROM support_tickets WHERE user_id=? AND status=? "
            "ORDER BY id DESC LIMIT 1",
            (user_id, STATUS_OPEN),
        ).fetchone()
        if row:
            return {
                "id": row[0],
                "user_id": row[1],
                "status": row[2],
                "created_at": row[3],
                "updated_at": row[4],
            }
        stamp = _now()
        cur = conn.execute(
            "INSERT INTO support_tickets (user_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, STATUS_OPEN, stamp, stamp),
        )
        tid = int(cur.lastrowid)
    return {
        "id": tid,
        "user_id": user_id,
        "status": STATUS_OPEN,
        "created_at": stamp,
        "updated_at": stamp,
    }


def get(ticket_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id, user_id, status, created_at, updated_at "
            "FROM support_tickets WHERE id=?",
            (ticket_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "status": row[2],
        "created_at": row[3],
        "updated_at": row[4],
    }


def add_message(
    ticket_id: int, author_id: int, direction: str, body: str
) -> None:
    stamp = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO support_messages "
            "(ticket_id, author_id, direction, body, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (ticket_id, author_id, direction, body, stamp),
        )
        conn.execute(
            "UPDATE support_tickets SET updated_at=? WHERE id=?",
            (stamp, ticket_id),
        )


def close(ticket_id: int) -> bool:
    stamp = _now()
    with connect() as conn:
        cur = conn.execute(
            "UPDATE support_tickets SET status=?, updated_at=? "
            "WHERE id=? AND status=?",
            (STATUS_CLOSED, stamp, ticket_id, STATUS_OPEN),
        )
        return cur.rowcount > 0


def list_open_for_staff(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, user_id, status, created_at, updated_at "
            "FROM support_tickets WHERE status=? "
            "ORDER BY updated_at DESC LIMIT ?",
            (STATUS_OPEN, limit),
        ).fetchall()
    return [
        {
            "id": r[0],
            "user_id": r[1],
            "status": r[2],
            "created_at": r[3],
            "updated_at": r[4],
        }
        for r in rows
    ]
