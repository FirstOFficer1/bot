"""CRUD для подписок на уведомления о парах."""

from __future__ import annotations

from ..db import connect


def add(uid: int, course: int, direction: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO subscriptions (user_id, course, direction) VALUES (?,?,?)",
            (uid, course, direction),
        )


def list_for(uid: int) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT id, course, direction FROM subscriptions "
            "WHERE user_id=? AND COALESCE(disabled,0)=0 ORDER BY id",
            (uid,),
        ).fetchall()


def list_all_active() -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT id, user_id, course, direction FROM subscriptions "
            "WHERE COALESCE(disabled,0)=0"
        ).fetchall()


def delete(sid: int, uid: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM subscriptions WHERE id=? AND user_id=?", (sid, uid))


def exists(uid: int, course: int, direction: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM subscriptions "
            "WHERE user_id=? AND course=? AND LOWER(direction)=LOWER(?) "
            "AND COALESCE(disabled,0)=0",
            (uid, course, direction),
        ).fetchone() is not None


def disable_all_for_user(uid: int) -> None:
    """Помечает все подписки пользователя как неактивные.

    Используется, когда VK сообщает «пользователь запретил сообщения от сообщества».
    """
    with connect() as conn:
        conn.execute("UPDATE subscriptions SET disabled=1 WHERE user_id=?", (uid,))
