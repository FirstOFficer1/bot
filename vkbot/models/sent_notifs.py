"""Журнал отправленных уведомлений о парах (для дедупликации)."""

from __future__ import annotations

from ..config import now_msk
from ..db import connect


def was_sent(uid: int, row_id: int, date: str, time: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM sent_class_notifications "
            "WHERE user_id=? AND schedule_row_id=? AND class_date=? AND class_time=?",
            (uid, row_id, date, time),
        ).fetchone() is not None


def mark(uid: int, row_id: int, date: str, time: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO sent_class_notifications "
            "(user_id, schedule_row_id, class_date, class_time, sent_at) "
            "VALUES (?,?,?,?,?)",
            (uid, row_id, date, time, now_msk().isoformat(timespec="seconds")),
        )


def cleanup_older_than(cutoff_date_iso: str) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM sent_class_notifications WHERE class_date < ?", (cutoff_date_iso,)
        )
