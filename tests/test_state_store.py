"""Состояние диалогов: в памяти сразу, в SQLite — фоновым потоком.

`StateStore` трогается на каждом шаге любого диалога — в хендлерах это под
шестьдесят вызовов. Пока запись шла синхронно из корутины, занятая база
останавливала не свой диалог, а разбор сообщений целиком. Теперь запись уходит
в очередь, и чинить каждое вызывающее место не нужно.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from vkbot import db
from vkbot.handlers import consent as consent_handler
from vkbot.models import consents, notes
from vkbot.state import StateStore, store

UID = 9009


def _row(uid: int) -> tuple | None:
    with db.connect() as conn:
        return conn.execute(
            "SELECT state_json FROM user_states WHERE user_id=?", (uid,)
        ).fetchone()


# ── Память и база ────────────────────────────────────────────────────────────

def test_state_is_readable_immediately():
    """Чтение идёт из памяти — ждать фонового писателя не нужно."""
    store[UID] = {"step": "day"}

    assert store.get(UID) == {"step": "day"}


def test_state_reaches_the_database():
    store[UID] = {"step": "day"}
    store.flush()

    assert _row(UID) is not None


def test_pop_removes_from_memory_and_database():
    store[UID] = {"step": "day"}
    store.flush()

    store.pop(UID, None)
    store.flush()

    assert store.get(UID) is None
    assert _row(UID) is None


def test_patch_merges_and_persists():
    store[UID] = {"step": "day", "course": 1}
    store.patch(UID, step="direction")
    store.flush()

    assert store.get(UID) == {"step": "direction", "course": 1}
    assert _row(UID) is not None


def test_operations_keep_their_order():
    """Очередь разбирает один поток, поэтому последним побеждает последний."""
    fresh = StateStore()
    for i in range(50):
        fresh[UID] = {"n": i}
    fresh.pop(UID, None)
    fresh[UID] = {"n": "последний"}
    fresh.flush()

    row = _row(UID)
    assert row is not None and "последний" in row[0]


def test_flush_on_empty_queue_is_a_noop():
    store.flush()
    store.flush()


def test_flush_waits_for_a_write_already_in_progress():
    """get() опустошает очередь до записи — flush() не должен на это купиться."""
    fresh = StateStore()
    started = threading.Event()
    original_apply = fresh._apply

    def slow_apply(kind, uid, payload):
        started.set()
        time.sleep(0.2)
        return original_apply(kind, uid, payload)

    fresh._apply = slow_apply
    fresh[UID] = {"step": "day"}
    started.wait(2)  # запись уже вынута из очереди и выполняется

    fresh.flush()

    assert _row(UID) is not None, "flush() вернулся, пока запись ещё шла"


def test_flush_gives_up_instead_of_hanging(monkeypatch):
    """Застрявший писатель не должен подвешивать вызывающего навсегда."""
    fresh = StateStore()
    monkeypatch.setattr(type(fresh), "_flush_timeout", 0.2)
    monkeypatch.setattr(fresh, "_apply", lambda *_a: time.sleep(5))

    fresh[UID] = {"step": "day"}
    began = time.monotonic()
    fresh.flush()

    assert time.monotonic() - began < 2, "flush() завис вместо того, чтобы сдаться"


def test_failed_write_does_not_kill_the_writer():
    """Одна сбойная запись не должна хоронить persistence до перезапуска."""
    fresh = StateStore()
    monkeypatch_calls: list[int] = []
    original_apply = fresh._apply

    def flaky(kind, uid, payload):
        monkeypatch_calls.append(1)
        if len(monkeypatch_calls) == 1:
            raise RuntimeError("database is locked")
        return original_apply(kind, uid, payload)

    fresh._apply = flaky
    fresh[UID] = {"step": "day"}
    fresh.flush()

    assert len(monkeypatch_calls) >= 2, "повтора не было"
    assert _row(UID) is not None, "запись потерялась после повтора"


# ── Не держим event loop ─────────────────────────────────────────────────────

def test_writing_does_not_touch_the_database_inline(monkeypatch):
    """Вызывающий не должен платить за поход в SQLite."""
    caller = threading.get_ident()
    seen: list[int] = []

    fresh = StateStore()
    original_apply = fresh._apply

    def spy(kind, uid, payload):
        seen.append(threading.get_ident())
        return original_apply(kind, uid, payload)

    monkeypatch.setattr(fresh, "_apply", spy)
    fresh[UID] = {"step": "day"}
    fresh.flush()

    assert seen and all(t != caller for t in seen), "запись пошла в потоке вызывающего"


@pytest.mark.asyncio
async def test_consent_gate_queries_outside_the_loop(monkeypatch):
    """Проверка согласия идёт на каждое сообщение — она обязана быть вне цикла."""
    seen: dict[str, int] = {}

    def probe(_uid):
        seen["thread"] = threading.get_ident()
        return True  # «согласие есть» — пропускаем дальше

    monkeypatch.setattr(consents, "accepted", probe)

    handled = await consent_handler.try_handle(None, object(), None, "привет", UID)

    assert handled is False
    assert seen["thread"] != threading.get_ident()


@pytest.mark.asyncio
async def test_notes_handler_queries_outside_the_loop(monkeypatch):
    seen: dict[str, int] = {}
    original = notes.list_for

    def probe(uid):
        seen["thread"] = threading.get_ident()
        return original(uid)

    monkeypatch.setattr(notes, "list_for", probe)

    class FakeMessage:
        async def answer(self, *_a, **_kw):
            return None

    from vkbot.handlers import notes as notes_handler

    await notes_handler.try_handle(None, FakeMessage(), None, "📋 Мои заметки", UID)

    assert seen["thread"] != threading.get_ident()


# ── Удаление своих данных не воскрешает состояние ────────────────────────────

@pytest.mark.asyncio
async def test_purge_is_not_undone_by_a_queued_write(monkeypatch):
    """Отложенная запись не должна вернуть строку после удаления данных."""
    consents.accept(UID, source="test")
    monkeypatch.setattr(consents, "accepted", lambda _uid: False)

    store[UID] = {"step": "day"}  # запись висит в очереди

    class FakeMessage:
        async def answer(self, *_a, **_kw):
            return None

    await consent_handler.try_handle(
        None, FakeMessage(), None, consent_handler.PURGE, UID
    )
    store.flush()
    await asyncio.sleep(0)

    assert _row(UID) is None, "состояние воскресло после удаления данных"
    assert store.get(UID) is None
