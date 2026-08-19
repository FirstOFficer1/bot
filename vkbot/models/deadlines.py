"""CRUD для дедлайнов."""

from __future__ import annotations

from ..db import connect


def add(uid: int, subject: str, description: str, deadline_at: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO deadlines (user_id, subject, description, deadline_at) "
            "VALUES (?,?,?,?)",
            (uid, subject, description, deadline_at),
        )


def list_for(uid: int) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT id, subject, description, deadline_at FROM deadlines "
            "WHERE user_id=? ORDER BY deadline_at ASC",
            (uid,),
        ).fetchall()


def delete(did: int, uid: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM deadlines WHERE id=? AND user_id=?", (did, uid))


def list_pending() -> list[tuple]:
    """Дедлайны, по которым ещё не отправлено хотя бы одно уведомление."""
    with connect() as conn:
        return conn.execute(
            "SELECT id, user_id, subject, description, deadline_at, "
            "notified_1day, notified_1hour FROM deadlines "
            "WHERE notified_1hour=0 OR notified_1day=0"
        ).fetchall()


def mark_1day(did: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE deadlines SET notified_1day=1 WHERE id=?", (did,))


def mark_1hour(did: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE deadlines SET notified_1hour=1 WHERE id=?", (did,))


def cleanup_older_than(cutoff_str: str) -> None:
    """Удаляет дедлайны с deadline_at < cutoff_str (формат YYYY-MM-DD HH:MM)."""
    with connect() as conn:
        conn.execute("DELETE FROM deadlines WHERE deadline_at < ?", (cutoff_str,))
