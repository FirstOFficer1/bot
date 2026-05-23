"""Воркер hot-reload расписания: следит за файлом-маркером."""

from __future__ import annotations

import asyncio

from .. import config
from ..schedule import repo


async def run(_bot) -> None:
    while True:
        await asyncio.sleep(config.SCHEDULE_RELOAD_POLL_SEC)
        repo.check_reload_marker()
