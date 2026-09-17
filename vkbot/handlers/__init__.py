"""Хендлеры сообщений, разнесённые по доменам.

Каждый модуль экспортирует `async def try_handle(message, state, text, uid) -> bool`,
возвращающую True, если сообщение обработано (дальше диспатчер не идёт).

Порядок регистрации важен: первой идёт consent, затем common, затем доменные.
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
    privacy,
    reminders,
    schedule,
    subscriptions,
    support,
)
from ..state import store

# Порядок имеет значение. Первым идёт согласие: пока его нет, обрабатывать
# данные не на чем, поэтому оно перехватывает вообще всё, включая приветствие.
# Хендлер, поставленный выше него, окажется доступен без согласия.
# Дальше common перехватывает "Меню" / приветствие.
# support — сразу после panel_login: специалисты отвечают командами, не проходя
# пользовательские разделы.
_PIPELINE = (
    consent.try_handle,
    common.try_intro,
    common.try_category,
    panel_login.try_handle,
    support.try_handle,
    schedule.try_handle,
    notes.try_handle,
    reminders.try_handle,
    deadlines.try_handle,
    subscriptions.try_handle,
    feedback.try_handle,
    privacy.try_handle,
    common.try_help,
    # Перед fallback: перехватывает только код входа, и только у того,
    # кто недавно его запрашивал.
    panel_login.try_code_hint,
    common.fallback,
)


async def dispatch_pipeline(bot, message, uid: int, text: str) -> None:
    """Общий пайплайн для VK и Telegram: первый ``True`` останавливает цепочку."""
    state = store.get(uid)
    for handler in _PIPELINE:
        if await handler(bot, message, state, text, uid):
            return


def register(bot) -> None:
    @bot.on.message()
    async def dispatch(message: Message) -> None:
        uid = message.from_id
        # Беседы: ответ идёт в peer_id. OTP, заметки и дедлайны нельзя светить
        # всем участникам — персональный pipeline только в личке.
        peer = getattr(message, "peer_id", None)
        if peer is not None and peer != uid:
            await message.answer(
                "Персональные команды работают только в личных сообщениях с ботом."
            )
            return
        # Отметка о посещении — в consent.try_handle: до согласия писать
        # vk_id в seen_users нельзя.
        text = (message.text or "").strip()
        await dispatch_pipeline(bot, message, uid, text)
