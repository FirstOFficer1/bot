"""Воркер уведомлений о парах: за N минут до начала каждой пары."""

from __future__ import annotations

import asyncio
import datetime
import logging
import re

from .. import config, sender
from ..db import connect
from ..models import heartbeats, sent_notifs, subscriptions
from ..schedule.week import current_week_type

_RU_DAYS = {
    "Monday": "Понедельник", "Tuesday": "Вторник", "Wednesday": "Среда",
    "Thursday": "Четверг", "Friday": "Пятница",
    "Saturday": "Суббота", "Sunday": "Воскресенье",
}

# Легаси-префикс чётности в названии предмета (так писал старый Telegram-бот).
# Актуальный импортёр держит чётность в колонке `week`, но строки со старым
# форматом могли остаться в проде — вырезаем префикс при выводе и отсекаем
# чужую неделю в запросе.
_WEEK_PREFIX_RE = re.compile(r"^\[(чёт|нечет)\]\s*")


def _group_subscriptions() -> dict[tuple[int, str], list[int]]:
    """Группирует подписчиков по (курс, направление).

    Расписание у группы общее, поэтому достаточно одного запроса к БД на
    группу — раньше отдельный запрос уходил на каждую подписку.
    """
    groups: dict[tuple[int, str], list[int]] = {}
    for _sid, uid, course, direction in subscriptions.list_all_active():
        groups.setdefault((course, direction), []).append(uid)
    return groups


def _parse_start(time_field: str | None) -> datetime.time | None:
    """Достаёт время начала пары из строки вида '1 пара 08:00-09:30'."""
    m = re.search(r"(\d{1,2}[:.]\d{2})", time_field or "")
    if not m:
        return None
    try:
        return datetime.datetime.strptime(m.group(1).replace(".", ":"), "%H:%M").time()
    except ValueError:
        return None


def _day_rows(course: int, direction: str, day: str, week: str) -> list[tuple]:
    """Пары группы на сегодня с учётом чётности недели: (время, предмет, препод, ауд.).

    Чётность берётся из колонки `week` — так же, как в `repo.get_day()`.
    Пустое значение означает «пара идёт каждую неделю».
    """
    other = "нечет" if week == "чёт" else "чёт"
    with connect(config.SCHEDULE_DB) as conn:
        return conn.execute(
            "SELECT time, subject, teacher, room FROM schedule "
            "WHERE course=? AND LOWER(direction)=LOWER(?) "
            "AND LOWER(day)=LOWER(?) "
            "AND (week IS NULL OR week='' OR week=?) "
            "AND subject NOT LIKE ?",
            (course, direction, day, week, "[" + other + "]%"),
        ).fetchall()


async def run(bot) -> None:
    notify_before = datetime.timedelta(minutes=config.CLASS_NOTIFY_BEFORE_MIN)

    while True:
        await asyncio.sleep(config.CLASS_NOTIFY_POLL_SEC)
        try:
            now = config.now_msk()

            cutoff = (
                now - datetime.timedelta(days=config.SENT_NOTIFS_CLEANUP_DAYS)
            ).date().isoformat()
            sent_notifs.cleanup_older_than(cutoff)

            db_day = _RU_DAYS.get(now.strftime("%A"), now.strftime("%A"))
            week = current_week_type()

            for (course, direction), uids in _group_subscriptions().items():
                for time_field, subject, teacher, room in _day_rows(
                    course, direction, db_day, week
                ):
                    hhmm = _parse_start(time_field)
                    if hhmm is None:
                        continue

                    class_start = datetime.datetime.combine(now.date(), hhmm)
                    # Окно «пора предупредить, но пара ещё не началась».
                    if not (class_start - notify_before <= now < class_start):
                        continue

                    start_str = class_start.strftime("%H:%M")
                    date_str = class_start.date().isoformat()
                    subj_clean = _WEEK_PREFIX_RE.sub("", subject or "")
                    # Ключ дедупликации не должен зависеть от rowid: переимпорт
                    # расписания раздаёт новые rowid, и пуши уходили повторно.
                    key = sent_notifs.class_key(course, direction, subj_clean, room)
                    lines = [
                        f"📚 Через {config.CLASS_NOTIFY_BEFORE_MIN} минут начнётся пара!",
                        f"• {subj_clean}",
                    ]
                    if teacher:
                        lines.append(f"• Преподаватель: {teacher}")
                    if room:
                        lines.append(f"• Аудитория: {room}")
                    lines.append(f"• Начало: {start_str}")
                    text = "\n".join(lines)

                    for uid in uids:
                        if sent_notifs.was_sent(uid, key, date_str, start_str):
                            continue
                        try:
                            if await sender.send(bot, uid, text):
                                sent_notifs.mark(uid, key, date_str, start_str)
                        except Exception:
                            logging.exception("Class notify send failure uid=%s", uid)
            heartbeats.mark(heartbeats.CLASSES)
        except Exception:
            # Воркер не должен умирать: одна ошибка не отменяет следующий тик.
            logging.exception("Class notify worker tick failed")
