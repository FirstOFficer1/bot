"""Отправка сообщений пользователю с обработкой ошибок платформы."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Any

from .ids import is_telegram, to_telegram

_counter = itertools.count()
_vk_bot: Any = None
_tg_bot: Any = None


def _random_id(uid: int) -> int:
    """Генерирует достаточно уникальный random_id для дедупликации VK.

    Комбинируем timestamp (мс) ⊕ uid ⊕ счётчик — коллизия в течение часа
    практически невозможна.
    """
    return ((int(time.time() * 1000) ^ uid) + next(_counter)) & 0x7FFFFFFF


# Коды ошибок VK, при которых пользователь больше не получает сообщения
_DISABLED_CODES = {901, 902, 917}


def set_vk_bot(bot: Any) -> None:
    """Регистрирует vkbottle-бота для пушей VK-пользователям (uid > 0)."""
    global _vk_bot
    _vk_bot = bot


def set_telegram_bot(bot: Any) -> None:
    """Регистрирует aiogram-бота для пушей Telegram-пользователям (uid < 0)."""
    global _tg_bot
    _tg_bot = bot


def _vk_api(bot: Any) -> Any | None:
    api = getattr(bot, "api", None)
    messages = getattr(api, "messages", None)
    return messages


async def send(bot: Any, uid: int, text: str) -> bool:
    """Отправляет сообщение. При «пользователь недоступен» — отключает подписки.

    Возвращает True при успехе хотя бы на одном канале.
    Воркер может крутиться в VK- или TG-процессе: выбираем API по знаку uid,
    а не по типу переданного ``bot``. Для связанного аккаунта пуш идёт в оба
    мессенджера.
    """
    from .models import account_links

    targets = account_links.delivery_targets(uid)
    ok_any = False
    for i, target in enumerate(targets):
        # Подписки висят на каноне (первый в списке). Блокировка второго канала
        # не должна их гасить.
        disable_subs = i == 0
        if is_telegram(target):
            ok = await _send_telegram(target, text, disable_subs=disable_subs)
        else:
            ok = await _send_vk(bot, target, text, disable_subs=disable_subs)
        ok_any = ok_any or ok
    return ok_any


async def _send_vk(
    bot: Any, uid: int, text: str, *, disable_subs: bool = True
) -> bool:
    vk = _vk_bot or bot
    messages = _vk_api(vk)
    if messages is None:
        logging.error("VK bot не зарегистрирован — пуш uid=%s пропущен", uid)
        return False
    try:
        await messages.send(
            user_id=uid,
            message=text,
            random_id=_random_id(uid),
        )
        return True
    except Exception as e:
        code = getattr(e, "code", None) or getattr(getattr(e, "error", None), "code", None)
        if code in _DISABLED_CODES:
            if disable_subs:
                from .models import subscriptions
                # В поток: при массовой рассылке таких отказов бывает много подряд,
                # и каждый synchronous UPDATE останавливал бы весь event loop.
                await asyncio.to_thread(subscriptions.disable_all_for_user, uid)
                logging.info(
                    "User %s disabled bot (VK code %s) — subscriptions deactivated",
                    uid, code,
                )
            else:
                logging.info(
                    "VK fan-out uid=%s недоступен (code %s) — подписки не трогаем",
                    uid, code,
                )
        else:
            logging.exception("Не удалось отправить сообщение пользователю %s", uid)
        return False


async def _send_telegram(
    uid: int, text: str, *, disable_subs: bool = True
) -> bool:
    tg = _tg_bot
    if tg is None:
        logging.error("Telegram bot не зарегистрирован — пуш uid=%s пропущен", uid)
        return False
    chat_id = to_telegram(uid)
    try:
        await tg.send_message(chat_id, text)
        return True
    except Exception as e:
        # Forbidden / bot blocked / chat not found — как VK 901/902/917.
        name = type(e).__name__
        blocked = name in {
            "TelegramForbiddenError",
            "TelegramBadRequest",
            "ForbiddenError",
            "ChatNotFound",
        } or "blocked" in str(e).lower() or "deactivated" in str(e).lower()
        if blocked:
            if disable_subs:
                from .models import subscriptions

                await asyncio.to_thread(subscriptions.disable_all_for_user, uid)
                logging.info(
                    "User %s disabled Telegram bot (%s) — subscriptions deactivated",
                    uid, name,
                )
            else:
                logging.info(
                    "TG fan-out uid=%s недоступен (%s) — подписки не трогаем",
                    uid, name,
                )
        else:
            logging.exception("Не удалось отправить Telegram-сообщение uid=%s", uid)
        return False
