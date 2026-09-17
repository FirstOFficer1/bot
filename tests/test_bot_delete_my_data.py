"""Удаление своих данных из бота после согласия (VK и TG — один пайплайн)."""

from __future__ import annotations

import pytest

from vkbot.handlers import privacy
from vkbot.keyboards import MISC_KB
from vkbot.models import audit, consents, notes, user_data
from vkbot.state import store

UID = 91001


class _Msg:
    def __init__(self):
        self.answers: list[str] = []

    async def answer(self, text, keyboard=None):
        self.answers.append(text)


@pytest.mark.asyncio
async def test_misc_keyboard_has_delete_button():
    import json
    labels = [
        btn["action"]["label"]
        for row in json.loads(MISC_KB)["buttons"]
        for btn in row
    ]
    assert privacy.PURGE_BTN in labels


@pytest.mark.asyncio
async def test_delete_requires_confirmation_then_wipes():
    consents.accept(UID, source="test")
    notes.add(UID, "конспект")
    assert user_data.count_all(UID)

    ask = _Msg()
    assert await privacy.try_handle(None, ask, None, privacy.PURGE_BTN, UID)
    assert "Точно удалить" in ask.answers[0] or "удалить всё" in ask.answers[0].lower()
    assert user_data.count_all(UID), "без подтверждения ничего не стираем"
    assert store.get(UID) and store.get(UID).get("mode") == "delete_my_data"

    done = _Msg()
    state = store.get(UID)
    assert await privacy.try_handle(None, done, state, privacy.CONFIRM_BTN, UID)
    assert "забыл" in done.answers[0]
    assert user_data.count_all(UID) == {}
    assert consents.get(UID) is None
    actions = [e["action"] for e in audit.list_recent(limit=20)]
    assert "me.delete_all" in actions


@pytest.mark.asyncio
async def test_delete_can_be_cancelled():
    consents.accept(UID, source="test")
    notes.add(UID, "оставить")

    await privacy.try_handle(None, _Msg(), None, privacy.PURGE_BTN, UID)
    cancel = _Msg()
    assert await privacy.try_handle(
        None, cancel, store.get(UID), "❌ Отмена", UID
    )
    assert "Отменено" in cancel.answers[0]
    assert notes.list_for(UID)
    assert store.get(UID) is None
