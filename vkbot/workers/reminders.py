"""Воркер напоминаний: проверяет и отправляет просроченные раз в N секунд."""

from __future__ import annotations

import asyncio
import datetime
import logging

from .. import config, sender
from ..models import heartbeats
from ..models import reminders as model


async def run(bot) -> None:
    while True:
        await asyncio.sleep(config.REMINDER_POLL_SEC)
        try:
            now = config.now_msk()
            for rid, uid, text, remind_at_str in model.list_pending():
                try:
                    remind_at = datetime.datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M")
                except ValueError:
                    continue
                if remind_at <= now:
                    try:
                        ok = await sender.send(bot, uid, f"⏰ Напоминание: {text}")
                        if ok:
                            model.mark_sent(rid)
                    except Exception:
                        logging.exception("Reminder send failure id=%s", rid)
            heartbeats.mark(heartbeats.REMINDERS)
        except Exception:
            # Воркер не должен умирать: одна ошибка не отменяет следующий тик.
            logging.exception("Reminder worker tick failed")
