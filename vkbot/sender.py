"""Отправка сообщений пользователю с обработкой VK-ошибок."""

from __future__ import annotations

import itertools
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vkbottle import Bot

_counter = itertools.count()


def _random_id(uid: int) -> int:
    """Генерирует достаточно уникальный random_id для дедупликации VK.

    Комбинируем timestamp (мс) ⊕ uid ⊕ счётчик — коллизия в течение часа
    практически невозможна.
    """
    return ((int(time.time() * 1000) ^ uid) + next(_counter)) & 0x7FFFFFFF


# Коды ошибок VK, при которых пользователь больше не получает сообщения
_DISABLED_CODES = {901, 902, 917}


async def send(bot: "Bot", uid: int, text: str) -> bool:
    """Отправляет сообщение. При «пользователь недоступен» — отключает подписки.

    Возвращает True при успехе, False при невосстановимой ошибке.
    """
    try:
        await bot.api.messages.send(
            user_id=uid,
            message=text,
            random_id=_random_id(uid),
        )
        return True
    except Exception as e:
        code = getattr(e, "code", None) or getattr(getattr(e, "error", None), "code", None)
        if code in _DISABLED_CODES:
            from .models import subscriptions
            subscriptions.disable_all_for_user(uid)
            logging.info(
                "User %s disabled bot (VK code %s) — subscriptions deactivated", uid, code
            )
        else:
            logging.exception("Не удалось отправить сообщение пользователю %s", uid)
        return False
