"""Идентификаторы платформ и конвертация клавиатур для Telegram-адаптера."""

from __future__ import annotations

import json

import pytest

from vkbot.ids import from_telegram, is_telegram, is_vk, to_telegram
from vkbot.keyboards import MAIN_KB, build

aiogram = pytest.importorskip("aiogram")


def test_telegram_ids_are_negative_and_roundtrip():
    assert from_telegram(42) == -42
    assert to_telegram(-42) == 42
    assert is_telegram(-42)
    assert is_vk(42)
    assert not is_telegram(42)
    assert not is_vk(-42)


def test_telegram_id_rejects_non_positive():
    with pytest.raises(ValueError):
        from_telegram(0)
    with pytest.raises(ValueError):
        from_telegram(-1)
    with pytest.raises(ValueError):
        to_telegram(5)


def test_vk_keyboard_json_converts_to_tg_rows():
    from tgbot.keyboards import to_reply_markup

    markup = to_reply_markup(MAIN_KB)
    assert markup is not None
    labels = [btn.text for row in markup.keyboard for btn in row]
    assert "📅 Расписание" in labels
    assert "🔔 Подписки на пары" in labels
    assert markup.resize_keyboard is True


def test_one_time_keyboard_flag():
    from tgbot.keyboards import to_reply_markup

    kb = build(["❌ Отмена"], one_time=True)
    markup = to_reply_markup(kb)
    assert markup.one_time_keyboard is True
    assert json.loads(kb)["one_time"] is True


def test_empty_keyboard_becomes_none():
    from tgbot.keyboards import to_reply_markup

    assert to_reply_markup(None) is None
