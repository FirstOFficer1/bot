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


def sent_uids(key: str, date: str, time: str) -> set[int]:
    """Кому эта пара уже отправлена — одним запросом на пару, а не на подписчика.

    Воркер раньше звал `was_sent()` по каждому получателю: у группы в двести
    человек это двести открытий соединения на одну пару, и все они —
    синхронные, внутри event loop бота.
    """
    with connect() as conn:
        return {
            int(row[0])
            for row in conn.execute(
                "SELECT user_id FROM sent_class_notifications "
                "WHERE class_key=? AND class_date=? AND class_time=?",
                (key, date, time),
            )
        }


def mark(uid: int, key: str, date: str, time: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO sent_class_notifications "
            "(user_id, class_key, class_date, class_time, sent_at) "
            "VALUES (?,?,?,?,?)",
            (uid, key, date, time, now_msk().isoformat(timespec="seconds")),
        )


def mark_many(uids: list[int], key: str, date: str, time: str) -> None:
    """Отмечает сразу всех, кому пара ушла: одна транзакция вместо N."""
    if not uids:
        return
    stamp = now_msk().isoformat(timespec="seconds")
    with connect() as conn:
        conn.executemany(
            "INSERT INTO sent_class_notifications "
            "(user_id, class_key, class_date, class_time, sent_at) "
            "VALUES (?,?,?,?,?)",
            [(uid, key, date, time, stamp) for uid in uids],
        )


def cleanup_older_than(cutoff_date_iso: str) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM sent_class_notifications WHERE class_date < ?", (cutoff_date_iso,)
        )
