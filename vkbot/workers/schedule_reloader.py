"""Воркер hot-reload расписания: следит за файлом-маркером."""

from __future__ import annotations

import asyncio
import logging

from .. import config
from ..models import heartbeats
from ..schedule import repo


async def run(_bot) -> None:
    while True:
        await asyncio.sleep(config.SCHEDULE_RELOAD_POLL_SEC)
        try:
            repo.check_reload_marker()
            heartbeats.mark(heartbeats.SCHEDULE_RELOADER)
        except Exception:
            # Воркер не должен умирать: одна ошибка не отменяет следующий тик.
            logging.exception("Schedule reloader tick failed")
