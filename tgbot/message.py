"""Сообщение с тем же контрактом, что ждут хендлеры vkbot: ``answer(text, keyboard=)``."""

from __future__ import annotations

from aiogram.types import Message as TgMessage

from .keyboards import to_reply_markup


class AdapterMessage:
    """Обёртка aiogram Message под ``message.answer(..., keyboard=vk_json)``."""

    __slots__ = ("_msg", "text", "from_id")

    def __init__(self, message: TgMessage, uid: int) -> None:
        self._msg = message
        self.text = (message.text or "").strip()
        self.from_id = uid

    async def answer(self, text: str, keyboard: str | None = None) -> None:
        await self._msg.answer(text, reply_markup=to_reply_markup(keyboard))
