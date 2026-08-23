"""Хендлеры сообщений, разнесённые по доменам.

Каждый модуль экспортирует `async def try_handle(message, state, text, uid) -> bool`,
возвращающую True, если сообщение обработано (дальше диспатчер не идёт).

Порядок регистрации важен: первой идёт common (меню/приветствие), затем доменные.
"""

from __future__ import annotations

from vkbottle.bot import Message

from . import (
    common,
    consent,
    deadlines,
    feedback,
    notes,
    panel_login,
    reminders,
    schedule,
    subscriptions,
)
from ..models import seen_users
from ..state import store

# Порядок имеет значение. Первым идёт согласие: пока его нет, обрабатывать
# данные не на чем, поэтому оно перехватывает вообще всё, включая приветствие.
# Хендлер, поставленный выше него, окажется доступен без согласия.
# Дальше common перехватывает "Меню" / приветствие.
_PIPELINE = (
    consent.try_handle,
    common.try_intro,
    common.try_category,
    panel_login.try_handle,
    schedule.try_handle,
    notes.try_handle,
    reminders.try_handle,
    deadlines.try_handle,
    subscriptions.try_handle,
    feedback.try_handle,
    common.try_help,
    # Перед fallback: перехватывает только шестизначный код, и только у того,
    # кто недавно его запрашивал.
    panel_login.try_code_hint,
    common.fallback,
)


def register(bot) -> None:
    @bot.on.message()
    async def dispatch(message: Message) -> None:
        uid = message.from_id
        # Регистрируем юзера моментально — чтобы /admin/users показал его сразу,
        # ещё до того, как он что-то сохранит.
        seen_users.touch(uid)
        text = (message.text or "").strip()
        state = store.get(uid)
        for handler in _PIPELINE:
            if await handler(bot, message, state, text, uid):
                return
