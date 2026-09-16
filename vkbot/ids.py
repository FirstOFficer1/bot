"""Пространства идентификаторов для VK и Telegram в одной notes.db.

VK user_id всегда положительный. Telegram-пользователей храним как
``-(telegram_id)``: так нельзя перепутать платформы и повторить баг легаси
``bot.py``, где оба мира писали в одни и те же строки без разделения.
"""

from __future__ import annotations


def is_telegram(uid: int) -> bool:
    return uid < 0


def is_vk(uid: int) -> bool:
    return uid > 0


def from_telegram(telegram_id: int) -> int:
    """Внешний Telegram id → внутренний uid для БД и хендлеров."""
    if telegram_id <= 0:
        raise ValueError("telegram_id должен быть положительным")
    return -int(telegram_id)


def to_telegram(uid: int) -> int:
    """Внутренний uid → внешний Telegram id."""
    if uid >= 0:
        raise ValueError("это не Telegram-пользователь")
    return -uid
