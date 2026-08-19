"""Журнал отправленных уведомлений о парах (для дедупликации).

Ключ дедупликации — `class_key`, стабильный отпечаток пары, а не `rowid` строки
расписания. Переимпорт расписания перезаписывает таблицу целиком и раздаёт новые
rowid, поэтому загрузка нового файла в середине учебного дня рассылала
уведомления о тех же парах по второму разу.
"""

from __future__ import annotations

from ..config import now_msk
from ..db import connect


def class_key(course: int, direction: str, subject: str, room: str | None = None) -> str:
    """Отпечаток пары, переживающий переимпорт расписания.

    День и время в ключ не входят: они и так часть уникальности вместе с
    `class_date` / `class_time`. Регистр и пробелы нормализуем — импортёр может
    отдать «ИСиТ » и «исит» как одно и то же направление.
    """
    parts = [str(course), direction or "", subject or "", room or ""]
    return "|".join(p.strip().lower() for p in parts)


def was_sent(uid: int, key: str, date: str, time: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM sent_class_notifications "
            "WHERE user_id=? AND class_key=? AND class_date=? AND class_time=?",
            (uid, key, date, time),
        ).fetchone() is not None


def mark(uid: int, key: str, date: str, time: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO sent_class_notifications "
            "(user_id, class_key, class_date, class_time, sent_at) "
            "VALUES (?,?,?,?,?)",
            (uid, key, date, time, now_msk().isoformat(timespec="seconds")),
        )


def cleanup_older_than(cutoff_date_iso: str) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM sent_class_notifications WHERE class_date < ?", (cutoff_date_iso,)
        )
