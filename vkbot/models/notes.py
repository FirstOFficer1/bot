"""CRUD для заметок."""

from __future__ import annotations

from ..config import now_msk
from ..db import connect


def add(uid: int, text: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO notes (user_id, note_text, timestamp) VALUES (?,?,?)",
            (uid, text, now_msk().isoformat(timespec="seconds")),
        )


def list_for(uid: int) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT id, note_text, timestamp FROM notes WHERE user_id=? ORDER BY id DESC",
            (uid,),
        ).fetchall()


def delete(ids: list[int]) -> None:
    if not ids:
        return
    with connect() as conn:
        conn.executemany("DELETE FROM notes WHERE id=?", ((i,) for i in ids))
