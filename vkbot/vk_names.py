"""Резолвер VK user_id → отображаемое имя через VK API.

Кэширует на время жизни процесса. Безопасно вызывать с любым количеством ID —
делает запросы пачками по 1000.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable

import requests

from . import config

log = logging.getLogger(__name__)

_API = "https://api.vk.com/method/users.get"
_API_VERSION = "5.131"
_BATCH = 1000

_cache: dict[int, str] = {}
_lock = threading.Lock()


def _fetch_batch(uids: list[int]) -> dict[int, str]:
    if not uids or not config.VK_TOKEN:
        return {}
    try:
        resp = requests.post(
            _API,
            data={
                "access_token": config.VK_TOKEN,
                "v": _API_VERSION,
                "user_ids": ",".join(str(u) for u in uids),
                "fields": "screen_name",
            },
            timeout=10,
        )
        data = resp.json()
        if "error" in data:
            log.warning("VK users.get error: %s", data["error"])
            return {}
        out: dict[int, str] = {}
        for u in data.get("response", []):
            uid = int(u.get("id", 0))
            first = u.get("first_name", "")
            last = u.get("last_name", "")
            name = f"{first} {last}".strip() or u.get("screen_name") or f"id{uid}"
            if u.get("deactivated"):
                name = f"{name} (deactivated)"
            out[uid] = name
        return out
    except Exception as e:
        log.warning("VK users.get HTTP error: %s", e)
        return {}


def resolve(uids: Iterable[int]) -> dict[int, str]:
    """Возвращает {vk_id: name} для всех запрошенных. Кэширует."""
    uids_set = {int(u) for u in uids if int(u) > 0}
    if not uids_set:
        return {}
    with _lock:
        miss = [u for u in uids_set if u not in _cache]
    if miss:
        # Запрашиваем пачками
        fetched: dict[int, str] = {}
        for i in range(0, len(miss), _BATCH):
            batch = miss[i : i + _BATCH]
            fetched.update(_fetch_batch(batch))
        with _lock:
            for uid in miss:
                _cache[uid] = fetched.get(uid, f"id{uid}")
    with _lock:
        return {u: _cache[u] for u in uids_set if u in _cache}


def resolve_one(uid: int) -> str:
    return resolve([uid]).get(uid, f"id{uid}")


def invalidate(uid: int | None = None) -> None:
    """Сброс кэша. Без аргумента — весь, иначе один ID."""
    with _lock:
        if uid is None:
            _cache.clear()
        else:
            _cache.pop(uid, None)
