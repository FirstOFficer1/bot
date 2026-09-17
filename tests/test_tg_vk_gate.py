"""Telegram: доступ только после привязки к VK."""

from __future__ import annotations

import pytest

from vkbot import db
from vkbot.handlers import dispatch_pipeline, tg_vk_gate
from vkbot.ids import from_telegram
from vkbot.models import account_links, consents


@pytest.fixture(autouse=True)
def _schema():
    db.init()


class FakeMessage:
    def __init__(self, text: str = ""):
        self.text = text
        self.answers: list[str] = []
        self.keyboards: list = []

    async def answer(self, text: str, keyboard=None) -> None:
        self.answers.append(text)
        self.keyboards.append(keyboard)


async def _say(text: str, uid: int) -> FakeMessage:
    msg = FakeMessage(text)
    await dispatch_pipeline(None, msg, uid, text)
    return msg


@pytest.mark.asyncio
async def test_unlinked_tg_blocked_until_vk_question():
    tg = from_telegram(91001)
    consents.accept(tg, source="test")

    msg = await _say("📅 Расписание", tg)
    assert any("ВКонтакте" in a for a in msg.answers)
    assert any("уже есть диалог" in a for a in msg.answers)


@pytest.mark.asyncio
async def test_has_vk_leads_to_enter_code():
    tg = from_telegram(91002)
    consents.accept(tg, source="test")

    msg = await _say(tg_vk_gate.HAS_VK, tg)
    assert "Ввести код" in msg.answers[0]
    assert "VK" in msg.answers[0]


@pytest.mark.asyncio
async def test_no_vk_points_to_vk_bot():
    tg = from_telegram(91003)
    consents.accept(tg, source="test")

    msg = await _say(tg_vk_gate.NO_VK, tg)
    assert "ВКонтакте" in msg.answers[0]
    assert "Получить код" in msg.answers[0]


@pytest.mark.asyncio
async def test_linked_tg_reaches_menu():
    vk, tg = 51010, from_telegram(91010)
    consents.accept(vk, source="test")
    account_links.link(vk, tg)

    msg = await _say("🏠 Меню", tg)
    joined = "\n".join(msg.answers)
    assert "Главное меню" in joined or "Привет" in joined
    assert "уже есть диалог" not in joined


@pytest.mark.asyncio
async def test_vk_user_unaffected():
    uid = 51011
    consents.accept(uid, source="test")
    msg = await _say("🏠 Меню", uid)
    assert "уже есть диалог" not in "\n".join(msg.answers)


def test_gate_after_account_link_in_pipeline():
    from vkbot.handlers import _PIPELINE
    from vkbot.handlers import account_link as al
    from vkbot.handlers import privacy as pr

    assert _PIPELINE.index(al.try_handle) < _PIPELINE.index(tg_vk_gate.try_handle)
    assert _PIPELINE.index(pr.try_handle) < _PIPELINE.index(tg_vk_gate.try_handle)
