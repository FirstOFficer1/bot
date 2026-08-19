"""CRUD для напоминаний."""

from __future__ import annotations

from ..db import connect


def add(uid: int, text: str, remind_at: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO reminders (user_id, reminder_text, remind_at, notified) "
            "VALUES (?,?,?,0)",
            (uid, text, remind_at),
        )


def list_pending() -> list[tuple]:
    """Все напоминания, которые ещё не отправлены."""
    with connect() as conn:
        return conn.execute(
            "SELECT id, user_id, reminder_text, remind_at FROM reminders WHERE notified=0"
        ).fetchall()


def mark_sent(rid: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE reminders SET notified=1 WHERE id=?", (rid,))


def list_for(uid: int) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT id, reminder_text, remind_at FROM reminders "
            "WHERE user_id=? AND notified=0 ORDER BY remind_at",
            (uid,),
        ).fetchall()


def delete(rid: int, uid: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM reminders WHERE id=? AND user_id=?", (rid, uid))
