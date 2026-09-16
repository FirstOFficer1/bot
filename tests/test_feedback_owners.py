"""Фидбэк уходит специалистам support; без них — владельцам."""

from __future__ import annotations

import pytest

from vkbot.handlers import feedback
from vkbot.models import panel_users


@pytest.mark.asyncio
async def test_feedback_fallback_owners_when_no_support(monkeypatch):
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

    assert await feedback.try_handle(None, Msg(), "feedback", "привет", 5555)
    assert set(sent) == {1001, 1002, 2002}
