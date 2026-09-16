"""Обратная связь пользователя → тикет техподдержки."""

from __future__ import annotations

import asyncio
import logging
import time

from .. import sender
from ..config import MAX_INPUT_LEN, env_owner_ids
from ..ids import is_telegram, to_telegram
from ..keyboards import CANCEL_KB, MAIN_KB
from ..models import panel_users, tickets
from ..state import store

_FEEDBACK_COOLDOWN_SEC = 60
_last_feedback_at: dict[int, float] = {}


def _ticket_recipients() -> list[int]:
    """Специалисты support; если никого нет — владельцы (env + панель)."""
    try:
        ids = set(panel_users.support_ids())
    except Exception:
        logging.exception("Не удалось прочитать support")
        ids = set()
    if ids:
        return sorted(ids)
    ids = set(env_owner_ids())
    try:
        ids |= panel_users.owner_ids()
    except Exception:
        logging.exception("Не удалось прочитать владельцев для тикетов")
    return sorted(ids)


def _user_label(uid: int) -> str:
    if is_telegram(uid):
        return f"Telegram id{to_telegram(uid)}"
    return f"[id{uid}|id{uid}]"


async def try_handle(bot, message, state, text, uid) -> bool:
    if text == "💬 Обратная связь":
        store[uid] = "feedback"
        await message.answer(
            "💬 Напиши вопрос или опиши проблему — создадим обращение "
            "в техподдержку.\n"
            "Если уже есть открытый тикет, сообщение добавится к нему.",
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
    ticket = await asyncio.to_thread(tickets.open_or_get, uid)
    await asyncio.to_thread(
        tickets.add_message, ticket["id"], uid, tickets.DIR_USER, text
    )

    await message.answer(
        f"✅ Обращение №{ticket['id']} принято. Ответ придёт сюда же.",
        keyboard=MAIN_KB,
    )
    body = (
        f"🎫 Тикет #{ticket['id']} от {_user_label(uid)}:\n\n{text}\n\n"
        f"Ответить: напиши «Ответить {ticket['id']}»\n"
        f"Закрыть: «Закрыть {ticket['id']}»"
    )
    for staff_id in _ticket_recipients():
        try:
            await sender.send(bot, staff_id, body)
        except Exception:
            logging.exception(
                "Не удалось переслать тикет #%s → %s", ticket["id"], staff_id
            )
    return True
