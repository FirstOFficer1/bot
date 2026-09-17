"""Сканер красных флагов: эвристика по фразам, алерт владельцам."""

from __future__ import annotations

import pytest

from vkbot import db, safety


@pytest.fixture(autouse=True)
def _schema_and_cooldown():
    db.init()
    safety.reset_cooldowns()


def test_scan_crisis():
    hits = safety.scan("Мне так плохо, хочу умереть уже")
    assert [h.category for h in hits] == ["crisis"]


def test_scan_violence():
    hits = safety.scan("завтра убью преподавателя за зачёт")
    assert "violence" in {h.category for h in hits}


def test_scan_illegal():
    hits = safety.scan("ищу как сделать бомбу для опытов")
    assert [h.category for h in hits] == ["illegal"]


def test_scan_ignores_benign_study_phrases():
    assert safety.scan("надо убить время до пары") == []
    assert safety.scan("смертельно скучная лекция") == []
    assert safety.scan("конспект по матанализу, сдать лабу") == []


def test_snippet_trims():
    long = "слово " * 50
    s = safety.snippet(long, 40)
    assert len(s) <= 40
    assert s.endswith("…")


@pytest.mark.asyncio
async def test_maybe_alert_notifies_owners_once(monkeypatch):
    owner = 1001
    user = 4242
    sent: list[tuple[int, str]] = []

    async def fake_send(_bot, uid, text):
        sent.append((uid, text))
        return True

    monkeypatch.setattr(safety.sender, "send", fake_send)
    monkeypatch.setattr(safety, "owner_ids", lambda: [owner])

    hits = await safety.maybe_alert(
        None, user, "note", "хочу умереть, всё бессмысленно"
    )
    assert hits and hits[0].category == "crisis"
    assert len(sent) == 1
    assert sent[0][0] == owner
    body = sent[0][1]
    assert "Красный флаг" in body
    assert f"VK id{user}" in body
    assert "кризис" in body
    assert "умереть" in body

    # Повтор в пределах cooldown — без второго пуша.
    sent.clear()
    await safety.maybe_alert(None, user, "note", "хочу умереть снова")
    assert sent == []

    with db.connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action=?",
            ("safety.red_flag",),
        ).fetchone()[0]
    assert n == 1


@pytest.mark.asyncio
async def test_maybe_alert_skips_owner_self(monkeypatch):
    owner = 1001
    sent: list = []

    async def fake_send(_bot, uid, text):
        sent.append(uid)
        return True

    monkeypatch.setattr(safety, "owner_ids", lambda: [owner])
    monkeypatch.setattr(safety.sender, "send", fake_send)

    await safety.maybe_alert(None, owner, "reminder", "не хочу жить")
    assert sent == [], "владельцу не шлём алерт на его же текст"


@pytest.mark.asyncio
async def test_notes_handler_alerts_after_save(monkeypatch):
    from vkbot.handlers import notes as notes_h
    from vkbot.models import consents, notes as notes_m
    from vkbot.state import store

    uid = 5555
    consents.accept(uid, source="test")
    store[uid] = "add_note"

    alerts: list[tuple] = []

    async def fake_alert(bot, u, source, text):
        alerts.append((u, source, text))
        return []

    monkeypatch.setattr(notes_h.safety, "maybe_alert", fake_alert)

    class Msg:
        answers = []

        async def answer(self, text, keyboard=None):
            self.answers.append(text)

    msg = Msg()
    assert await notes_h.try_handle(None, msg, "add_note", "хочу умереть", uid)
    assert notes_m.list_for(uid)
    assert alerts == [(uid, "note", "хочу умереть")]
    assert any("сохранена" in a for a in msg.answers)
