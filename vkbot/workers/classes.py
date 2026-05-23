"""Воркер уведомлений о парах: за 10 минут до начала каждой пары."""

from __future__ import annotations

import asyncio
import datetime
import logging
import re

from .. import config, sender
from ..db import connect
from ..models import sent_notifs, subscriptions
from ..schedule.week import current_week_type

_RU_DAYS = {
    "Monday": "Понедельник", "Tuesday": "Вторник", "Wednesday": "Среда",
    "Thursday": "Четверг", "Friday": "Пятница",
    "Saturday": "Суббота", "Sunday": "Воскресенье",
}


async def run(bot) -> None:
    notify_before = datetime.timedelta(minutes=config.CLASS_NOTIFY_BEFORE_MIN)
    class_dur = datetime.timedelta(minutes=config.CLASS_DURATION_MIN)

    while True:
        await asyncio.sleep(config.CLASS_NOTIFY_POLL_SEC)
        now = config.now_msk()

        cutoff = (now - datetime.timedelta(days=config.SENT_NOTIFS_CLEANUP_DAYS)).date().isoformat()
        sent_notifs.cleanup_older_than(cutoff)

        db_day = _RU_DAYS.get(now.strftime("%A"), now.strftime("%A"))
        week = current_week_type()
        exclude_prefix = "[нечет]%" if week == "чёт" else "[чёт]%"

        for _sid, uid, course, direction in subscriptions.list_all_active():
            with connect(config.SCHEDULE_DB) as conn:
                rows = conn.execute(
                    "SELECT rowid, time, subject, teacher, room FROM schedule "
                    "WHERE course=? AND LOWER(direction)=LOWER(?) "
                    "AND LOWER(day)=LOWER(?) AND subject NOT LIKE ?",
                    (course, direction, db_day, exclude_prefix),
                ).fetchall()

            for row_id, time_field, subject, teacher, room in rows:
                m = re.search(r"(\d{1,2}[:.]\d{2})", time_field or "")
                if not m:
                    continue
                try:
                    hhmm = datetime.datetime.strptime(
                        m.group(1).replace(".", ":"), "%H:%M"
                    ).time()
                except ValueError:
                    continue

                class_start = datetime.datetime.combine(now.date(), hhmm)
                class_end = class_start + class_dur
                if class_end < now - datetime.timedelta(minutes=1):
                    continue

                start_str = class_start.strftime("%H:%M")
                date_str = class_start.date().isoformat()
                subj_clean = re.sub(r"^\[(чёт|нечет)\]\s*", "", subject)

                if class_start - notify_before <= now < class_start:
                    if not sent_notifs.was_sent(uid, row_id, date_str, start_str):
                        try:
                            ok = await sender.send(
                                bot, uid,
                                f"📚 Через {config.CLASS_NOTIFY_BEFORE_MIN} минут начнётся пара!\n"
                                f"• {subj_clean}\n"
                                f"• Преподаватель: {teacher}\n"
                                f"• Аудитория: {room}\n"
                                f"• Начало: {start_str}",
                            )
                            if ok:
                                sent_notifs.mark(uid, row_id, date_str, start_str)
                        except Exception:
                            logging.exception("Class notify send failure uid=%s", uid)
