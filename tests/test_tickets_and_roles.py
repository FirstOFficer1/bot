"""Мультироли и тикеты техподдержки."""

from __future__ import annotations

import pytest

from vkbot.handlers import feedback, support
from vkbot.models import panel_users, tickets


def test_multi_roles_independent():
    panel_users.grant(501, granted_by=1001, name="A", role=panel_users.ROLE_ADMIN)
    panel_users.grant_role(501, panel_users.ROLE_SUPPORT, granted_by=1001)
    assert panel_users.roles_of(501) == {"admin", "support"}
    assert panel_users.is_admin(501)
    assert panel_users.is_support(501)
    assert not panel_users.is_owner(501)


def test_support_alone_is_not_admin():
    panel_users.grant_role(502, panel_users.ROLE_SUPPORT, granted_by=1001, name="S")
    assert panel_users.is_support(502)
    assert not panel_users.is_admin(502)
    assert panel_users.role_of(502) == "support"


def test_owner_revoke_keeps_admin():
    panel_users.grant(503, granted_by=1001, name="O", role=panel_users.ROLE_OWNER)
    panel_users.set_role(503, panel_users.ROLE_ADMIN)
    assert panel_users.is_admin(503)
    assert not panel_users.is_owner(503)


def test_revoke_admin_keeps_support():
    panel_users.grant(504, granted_by=1001, name="B", role=panel_users.ROLE_ADMIN)
    panel_users.grant_role(504, panel_users.ROLE_SUPPORT, granted_by=1001)
    panel_users.revoke(504)
    assert not panel_users.is_admin(504)
    assert panel_users.is_support(504)


@pytest.mark.asyncio
async def test_ticket_goes_to_support_not_only_owners(monkeypatch):
    sent: list[int] = []
    feedback._last_feedback_at.clear()

    async def fake_send(_bot, uid, _text):
        sent.append(uid)
        return True

    class Msg:
        async def answer(self, *_a, **_kw):
            return None

    monkeypatch.setattr(feedback.sender, "send", fake_send)
    monkeypatch.setattr(feedback, "env_owner_ids", lambda: {1001})
    panel_users.grant_role(3001, panel_users.ROLE_SUPPORT, granted_by=1001, name="Sup")

    assert await feedback.try_handle(None, Msg(), "feedback", "сломалось", 7777)
    assert sent == [3001]
    open_ones = tickets.list_open_for_staff()
    assert len(open_ones) == 1
    assert open_ones[0]["user_id"] == 7777


@pytest.mark.asyncio
async def test_ticket_fallback_to_owners_without_support(monkeypatch):
    sent: list[int] = []
    feedback._last_feedback_at.clear()

    async def fake_send(_bot, uid, _text):
        sent.append(uid)
        return True

    class Msg:
        async def answer(self, *_a, **_kw):
            return None

    monkeypatch.setattr(feedback.sender, "send", fake_send)
    monkeypatch.setattr(feedback, "env_owner_ids", lambda: {1001, 1002})
    panel_users.grant(2002, granted_by=1001, name="Co", role=panel_users.ROLE_OWNER)

    assert await feedback.try_handle(None, Msg(), "feedback", "привет", 8888)
    assert set(sent) == {1001, 1002, 2002}


@pytest.mark.asyncio
async def test_staff_reply_reaches_telegram_user(monkeypatch):
    sent: list[tuple[int, str]] = []

    async def fake_send(_bot, uid, text):
        sent.append((uid, text))
        return True

    class Msg:
        async def answer(self, *_a, **_kw):
            return None

    monkeypatch.setattr(support.sender, "send", fake_send)
    monkeypatch.setattr(support, "env_owner_ids", lambda: {1001})
    panel_users.grant_role(1001, panel_users.ROLE_SUPPORT, granted_by=1001, name="Me")

    tg_uid = -424242
    t = tickets.open_or_get(tg_uid)
    tickets.add_message(t["id"], tg_uid, tickets.DIR_USER, "help")

    assert await support.try_handle(
        None, Msg(), None, f"Ответить {t['id']}", 1001
    )
    assert await support.try_handle(
        None, Msg(), {"mode": "support_reply", "ticket_id": t["id"]}, "держи фикс", 1001
    )
    assert any(uid == tg_uid and "держи фикс" in text for uid, text in sent)
