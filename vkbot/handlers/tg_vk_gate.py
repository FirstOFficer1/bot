"""Telegram: без привязки к VK полный доступ закрыт.

Иначе у одного человека легко появляются два профиля (заметки/группа в TG
и отдельно в VK). Согласие уже есть — дальше спрашиваем про VK и ведём
только к вводу кода из VK-бота.
"""

from __future__ import annotations

import asyncio
import os

from ..ids import is_telegram
from ..keyboards import build
from ..models import account_links
from . import account_link

HAS_VK = "✅ Да, есть VK"
NO_VK = "❌ Пока нет"
BACK_ASK = "◀ К вопросу"

ASK_KB = build([HAS_VK], [NO_VK], one_time=True)
HAS_VK_KB = build(
    [account_link.ENTER_CODE],
    [account_link.LINK_BTN],
    [BACK_ASK],
    one_time=True,
)
NO_VK_KB = build([HAS_VK], [BACK_ASK], one_time=True)


def _vk_bot_url() -> str:
    """Ссылка «написать сообществу». Пустая — без строки в тексте."""
    explicit = (os.getenv("SUPPORT_URL") or "").strip()
    if explicit:
        return explicit
    raw = (os.getenv("VK_GROUP_ID") or "").strip()
    if raw.isdigit() and int(raw) > 0:
        return f"https://vk.me/club{raw}"
    return ""


def needs_vk_link(uid: int) -> bool:
    """True, если это Telegram и аккаунт ещё не связан с VK."""
    if not is_telegram(uid):
        return False
    return account_links.get_vk_for_telegram(uid) is None


def ask_text() -> str:
    return (
        "Чтобы пользоваться ботом в Telegram, нужен аккаунт ВКонтакте — "
        "так не будет двух разных профилей с разными заметками и группой.\n\n"
        "У тебя уже есть диалог с нашим ботом ВКонтакте?"
    )


def _has_vk_text() -> str:
    return (
        "Отлично. Связь делается кодом из VK:\n\n"
        "1. Открой VK-бот → «⚙️ Прочее» → «🔗 Связать аккаунты» "
        "→ «🔑 Получить код».\n"
        "2. Вернись сюда и нажми «✏️ Ввести код».\n\n"
        "Пока аккаунты не связаны, расписание и остальное здесь недоступны."
    )


def _no_vk_text() -> str:
    url = _vk_bot_url()
    link = f"\n\nНаписать VK-боту: {url}" if url else ""
    return (
        "Тогда сначала зайди в наш бот ВКонтакте, прими согласие и открой меню.\n"
        "Потом там: «⚙️ Прочее» → «🔗 Связать аккаунты» → «🔑 Получить код» — "
        "и вернись сюда ввести этот код."
        f"{link}\n\n"
        "Когда будет аккаунт VK — нажми «✅ Да, есть VK»."
    )


async def offer(message) -> None:
    """Показать стартовый вопрос (после согласия или по «Меню»)."""
    await message.answer(ask_text(), keyboard=ASK_KB)


async def try_handle(_bot, message, _state, text, uid) -> bool:
    """Для несвязанного TG перехватывает всё, кроме уже обработанной привязки."""
    if not await asyncio.to_thread(needs_vk_link, uid):
        return False

    text = (text or "").strip()

    if text == HAS_VK:
        await message.answer(_has_vk_text(), keyboard=HAS_VK_KB)
        return True

    if text == NO_VK:
        await message.answer(_no_vk_text(), keyboard=NO_VK_KB)
        return True

    if text == BACK_ASK:
        await offer(message)
        return True

    # Любое другое сообщение (включая «Меню» / приветствие) — снова вопрос.
    await offer(message)
    return True
