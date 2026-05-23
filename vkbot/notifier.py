"""Синхронная рассылка сообщений через VK API (для web_panel и cron-задач).

Не путать с `sender.py` — тот для async-бота на vkbottle.
Здесь — обычный HTTP-вызов `messages.send` через requests.
"""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass
from typing import Iterable

import requests

from . import config
from .models import subscriptions

log = logging.getLogger(__name__)

_VK_API = "https://api.vk.com/method"
_API_VERSION = "5.131"
# VK ограничивает messages.send до ~20 RPS на токен сообщества
_RATE_DELAY_SEC = 0.06

# Коды, означающие, что юзер заблокировал/удалил диалог — отключаем подписки
_USER_DEAD_CODES = {901, 902, 917}


@dataclass
class BroadcastResult:
    sent: int
    failed: int
    disabled_uids: list[int]

    def as_dict(self) -> dict:
        return {
            "sent": self.sent,
            "failed": self.failed,
            "disabled": len(self.disabled_uids),
        }


def _next_random_id(uid: int, counter: itertools.count) -> int:
    return (int(time.time() * 1000) ^ uid) + next(counter)


def send_one(uid: int, text: str, counter: itertools.count) -> tuple[bool, int | None]:
    """Возвращает (ok, vk_error_code_if_any)."""
    try:
        resp = requests.post(
            f"{_VK_API}/messages.send",
            data={
                "access_token": config.VK_TOKEN,
                "v": _API_VERSION,
                "user_id": uid,
                "message": text,
                "random_id": _next_random_id(uid, counter),
                "dont_parse_links": 1,
            },
            timeout=10,
        )
        data = resp.json()
        if "error" in data:
            code = data["error"].get("error_code")
            log.warning("VK error sending to %s: %s", uid, data["error"])
            return False, code
        return True, None
    except Exception as e:
        log.warning("HTTP error sending to %s: %s", uid, e)
        return False, None


def broadcast(text: str, uids: Iterable[int]) -> BroadcastResult:
    """Шлёт сообщение всем uids. Юзеров с кодами 901/902/917 — отключает."""
    if not config.VK_TOKEN:
        raise RuntimeError("VK_TOKEN не задан")
    counter = itertools.count()
    sent = failed = 0
    disabled: list[int] = []
    seen: set[int] = set()
    for uid in uids:
        if uid in seen:
            continue
        seen.add(uid)
        ok, code = send_one(uid, text, counter)
        if ok:
            sent += 1
        else:
            failed += 1
            if code in _USER_DEAD_CODES:
                subscriptions.disable_all_for_user(uid)
                disabled.append(uid)
        time.sleep(_RATE_DELAY_SEC)
    return BroadcastResult(sent=sent, failed=failed, disabled_uids=disabled)


def subscribed_uids() -> list[int]:
    """Все user_id, у которых есть активные подписки."""
    seen: set[int] = set()
    for row in subscriptions.list_all_active():
        # list_all_active возвращает (id, user_id, course, direction)
        seen.add(row[1])
    return sorted(seen)
