"""Привязка VK ↔ Telegram."""

from __future__ import annotations

import pytest

from vkbot import db
from vkbot.ids import from_telegram
from vkbot.models import account_links, consents, notes, panel_codes, subscriptions, user_data
from vkbot.models import user_prefs


@pytest.fixture(autouse=True)
def _schema():
    db.init()


def test_link_pair_merges_notes_and_subs():
    vk, tg = 50001, from_telegram(90001)
    notes.add(vk, "с vk")
    notes.add(tg, "с tg")
    subscriptions.add(tg, 3, "ИТ")
    consents.accept(tg, source="bot")

    code, _ttl = panel_codes.issue(vk, purpose=panel_codes.PURPOSE_LINK)
    issuer = panel_codes.verify(code, purpose=panel_codes.PURPOSE_LINK)
    assert issuer == vk

    account_links.link_pair(issuer, tg)

    assert account_links.canonical_uid(tg) == vk
    assert account_links.get_telegram_for_vk(vk) == tg
    texts = [r[1] for r in notes.list_for(vk)]
    assert "с vk" in texts and "с tg" in texts
    assert notes.list_for(tg) == []
    assert subscriptions.exists(vk, 3, "ИТ")
    assert consents.accepted(vk)


def test_cannot_link_same_platform():
    vk_a, vk_b = 50002, 50003
    code, _ = panel_codes.issue(vk_a, purpose=panel_codes.PURPOSE_LINK)
    issuer = panel_codes.verify(code, purpose=panel_codes.PURPOSE_LINK)
    with pytest.raises(ValueError, match="другой платформы"):
        account_links.link_pair(issuer, vk_b)


def test_prefs_vk_wins_on_merge():
    vk, tg = 50004, from_telegram(90004)
    user_prefs.set(vk, 4, "VK-направление")
    user_prefs.set(tg, 1, "TG-направление")
    account_links.link_pair(vk, tg)
    assert user_prefs.get(vk) == (4, "VK-направление")
    assert user_prefs.get(tg) is None


def test_delivery_targets_both_channels():
    vk, tg = 50005, from_telegram(90005)
    account_links.link(vk, tg)
    assert account_links.delivery_targets(vk) == [vk, tg]
    assert account_links.delivery_targets(tg) == [vk, tg]


def test_unlink():
    vk, tg = 50006, from_telegram(90006)
    account_links.link(vk, tg)
    assert account_links.unlink(vk)
    assert account_links.canonical_uid(tg) == tg


def test_purge_removes_link():
    vk, tg = 50007, from_telegram(90007)
    account_links.link(vk, tg)
    consents.accept(vk, source="test")
    user_data.wipe_account(vk)
    assert account_links.get_telegram_for_vk(vk) is None


@pytest.mark.asyncio
async def test_handler_issues_and_redeems():
    from vkbot.handlers import account_link as h
    from vkbot.state import store

    vk, tg = 50008, from_telegram(90008)

    class Msg:
        def __init__(self):
            self.answers = []

        async def answer(self, text, keyboard=None):
            self.answers.append(text)

    assert await h.try_handle(None, Msg(), None, h.LINK_BTN, vk)
    assert await h.try_handle(None, Msg(), store.get(vk), h.GET_CODE, vk)

    with db.connect() as conn:
        row = conn.execute(
            "SELECT code FROM panel_login_codes WHERE user_id=? AND purpose=? AND used=0 "
            "ORDER BY created_at DESC LIMIT 1",
            (vk, panel_codes.PURPOSE_LINK),
        ).fetchone()
    assert row
    code = row[0]

    assert await h.try_handle(None, Msg(), None, h.LINK_BTN, tg)
    assert await h.try_handle(None, Msg(), store.get(tg), h.ENTER_CODE, tg)
    done = Msg()
    assert await h.try_handle(None, done, store.get(tg), code, tg)
    assert "Готово" in done.answers[0]
    assert account_links.canonical_uid(tg) == vk
