"""Хендлер раздела «Обратная связь»."""

from __future__ import annotations

import logging
import time

from .. import sender
from ..config import MAX_INPUT_LEN, env_owner_ids
from ..keyboards import CANCEL_KB, MAIN_KB
from ..models import panel_users
from ..state import store

_FEEDBACK_COOLDOWN_SEC = 60
_last_feedback_at: dict[int, float] = {}


def _feedback_recipients() -> list[int]:
    """Все владельцы: env-якорь плюс co-owners из панели."""
    ids = set(env_owner_ids())
    try:
        ids |= panel_users.owner_ids()
    except Exception:
        logging.exception("Не удалось прочитать владельцев для фидбэка")
    return sorted(ids)


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
    body = f"💬 Новый фидбэк от [id{uid}|id{uid}]:\n\n{text}"
    for owner_id in _feedback_recipients():
        try:
            await sender.send(bot, owner_id, body)
        except Exception:
            logging.exception("Не удалось переслать фидбэк владельцу %s", owner_id)
    return True
