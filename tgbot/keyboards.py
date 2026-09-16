"""Конвертация VK-клавиатур (JSON от vkbottle) в ReplyKeyboard Telegram."""

from __future__ import annotations

import json

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove


def to_reply_markup(keyboard: str | None):
    """VK JSON → ReplyKeyboardMarkup. ``None`` — без клавиатуры."""
    if not keyboard:
        return None
    data = json.loads(keyboard)
    rows: list[list[KeyboardButton]] = []
    for row in data.get("buttons") or []:
        buttons: list[KeyboardButton] = []
        for btn in row:
            action = btn.get("action") or {}
            label = (action.get("label") or "").strip()
            if label:
                buttons.append(KeyboardButton(text=label))
        if buttons:
            rows.append(buttons)
    if not rows:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        one_time_keyboard=bool(data.get("one_time")),
    )
