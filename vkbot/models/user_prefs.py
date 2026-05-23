"""Предпочтения пользователя: последний выбор курса/направления."""

from __future__ import annotations

from ..db import connect


def get(uid: int) -> tuple | None:
    with connect() as conn:
        return conn.execute(
            "SELECT course, direction FROM user_prefs WHERE user_id=?", (uid,)
        ).fetchone()


def set(uid: int, course: int, direction: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_prefs (user_id, course, direction) VALUES (?,?,?)",
            (uid, course, direction),
        )
