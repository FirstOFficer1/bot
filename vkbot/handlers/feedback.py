"""Хендлер раздела «Обратная связь»."""

from __future__ import annotations

import logging
import time

from .. import sender
from ..config import ADMIN_ID, MAX_INPUT_LEN
from ..keyboards import CANCEL_KB, MAIN_KB
from ..state import store

_FEEDBACK_COOLDOWN_SEC = 60
_last_feedback_at: dict[int, float] = {}


async def try_handle(bot, message, state, text, uid) -> bool:
    if text == "💬 Обратная связь":
        store[uid] = "feedback"
        await message.answer(
            "💬 Напиши своё предложение, вопрос или сообщение об ошибке.\n"
            "Мы постараемся рассмотреть его как можно скорее.",
            keyboard=CANCEL_KB,
        )
        return True

    if state != "feedback":
        return False

    if text in ("❌ Отмена", "Отмена"):
        store.pop(uid, None)
        await message.answer("Отменено.", keyboard=MAIN_KB)
        return True
    if not text:
        await message.answer(
            "Сообщение не может быть пустым. Попробуй ещё раз:", keyboard=CANCEL_KB
        )
        return True
    if len(text) > MAX_INPUT_LEN:
        await message.answer(
            f"Слишком длинное сообщение (максимум {MAX_INPUT_LEN} символов).",
            keyboard=CANCEL_KB,
        )
        return True

    # Rate-limit: одно сообщение от пользователя в минуту
    now_ts = time.time()
    last = _last_feedback_at.get(uid, 0.0)
    if now_ts - last < _FEEDBACK_COOLDOWN_SEC:
        wait = int(_FEEDBACK_COOLDOWN_SEC - (now_ts - last))
        await message.answer(
            f"⏳ Слишком часто. Подожди ещё {wait} сек.", keyboard=CANCEL_KB
        )
        return True
    _last_feedback_at[uid] = now_ts

    store.pop(uid, None)
    await message.answer("✅ Спасибо! Твоё сообщение получено.", keyboard=MAIN_KB)
    if ADMIN_ID:
        try:
            await sender.send(
                bot, ADMIN_ID, f"💬 Новый фидбэк от [id{uid}|id{uid}]:\n\n{text}"
            )
        except Exception:
            logging.exception("Не удалось переслать фидбэк админу")
    return True
