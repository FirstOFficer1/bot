"""Воркер дедлайнов: уведомления за 1 день и 1 час, очистка старых."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time

from .. import config, sender
from ..models import audit
from ..models import deadlines as model
from ..models import heartbeats, panel_codes


def _housekeeping() -> None:
    """Чистка служебных таблиц: коды входа и журнал аудита.

    Функции чистки были написаны, но их никто не вызывал: таблица одноразовых
    кодов росла по строке на каждый запрос входа, аудит — навсегда. Живёт здесь,
    а не в отдельном воркере, чтобы не плодить сущности: тик дедлайнов и так
    самый редкий из «уборочных».
    """
    # Каждая чистка в своём try: панель может держать write-lock, и раньше
    # OperationalError отсюда обрывал весь тик — уведомления о дедлайнах
    # пропускались, heartbeat не ставился, и /healthz начинал мигать.
    try:
        removed_codes = panel_codes.cleanup_old(config.PANEL_CODES_CLEANUP_DAYS)
    except Exception:
        logging.exception("Не удалось почистить коды входа")
        removed_codes = 0
    removed_audit = audit.cleanup_older_than(config.AUDIT_KEEP_DAYS)
    if removed_codes or removed_audit:
        logging.info(
            "Housekeeping: коды входа -%s, аудит -%s", removed_codes, removed_audit
        )


async def run(bot) -> None:
    # None, а не 0.0: `time.monotonic()` отсчитывается от старта системы, и на
    # свежезагруженной машине разница с нулём меньше часа — уборка не запускалась
    # бы весь первый час работы сервиса.
    last_housekeeping: float | None = None

    while True:
        await asyncio.sleep(config.DEADLINE_POLL_SEC)
        try:
            now = config.now_msk()

            cutoff = (now - datetime.timedelta(days=config.DEADLINE_CLEANUP_DAYS)).strftime(
                "%Y-%m-%d %H:%M"
            )
            model.cleanup_older_than(cutoff)

            monotonic = time.monotonic()
            if (
                last_housekeeping is None
                or monotonic - last_housekeeping >= config.HOUSEKEEPING_EVERY_SEC
            ):
                last_housekeeping = monotonic
                _housekeeping()

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

                # Логика «порог пересечён», а не «попали в узкое окно»: уведомление
                # уходит, как только до дедлайна осталось не больше порога — и если
                # бот в этот момент лежал, оно «догонит» на ближайшем тике, а не
                # потеряется навсегда (флаг notified выставляется только по факту).
                if not n_1day and datetime.timedelta(hours=1) < delta <= datetime.timedelta(hours=24):
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

                if not n_1hour and datetime.timedelta(0) < delta <= datetime.timedelta(hours=1):
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
            heartbeats.mark(heartbeats.DEADLINES)
        except Exception:
            # Воркер не должен умирать: одна ошибка не отменяет следующий тик.
            logging.exception("Deadline worker tick failed")
