"""Воркер дедлайнов: уведомления за 1 день и 1 час, очистка старых."""

from __future__ import annotations

import asyncio
import datetime
import logging

from .. import config, sender
from ..models import deadlines as model


async def run(bot) -> None:
    while True:
        await asyncio.sleep(config.DEADLINE_POLL_SEC)
        now = config.now_msk()

        cutoff = (now - datetime.timedelta(days=config.DEADLINE_CLEANUP_DAYS)).strftime(
            "%Y-%m-%d %H:%M"
        )
        model.cleanup_older_than(cutoff)

        for did, uid, subject, description, deadline_at_str, n_1day, n_1hour in model.list_pending():
            try:
                deadline_at = datetime.datetime.strptime(deadline_at_str, "%Y-%m-%d %H:%M")
            except ValueError:
                continue

            delta = deadline_at - now
            if delta.total_seconds() < -3600:
                continue

            desc_line = f"\n📝 {description}" if description else ""
            deadline_fmt = deadline_at.strftime("%d.%m.%Y %H:%M")

            if not n_1day and datetime.timedelta(hours=23) <= delta <= datetime.timedelta(hours=25):
                try:
                    ok = await sender.send(
                        bot, uid,
                        f"⚠️ Дедлайн завтра!\n"
                        f"📌 {subject}{desc_line}\n"
                        f"🕐 {deadline_fmt}",
                    )
                    if ok:
                        model.mark_1day(did)
                except Exception:
                    logging.exception("Deadline 1day send failure id=%s", did)

            if not n_1hour and datetime.timedelta(minutes=50) <= delta <= datetime.timedelta(minutes=70):
                try:
                    ok = await sender.send(
                        bot, uid,
                        f"🔴 Дедлайн через час!\n"
                        f"📌 {subject}{desc_line}\n"
                        f"🕐 {deadline_fmt}",
                    )
                    if ok:
                        model.mark_1hour(did)
                except Exception:
                    logging.exception("Deadline 1hour send failure id=%s", did)
