"""Остаток находок совместного ревью.

* до согласия человек не попадает в `seen_users` — это персональные данные,
  и `consent.py` прямо обещает ничего не писать без основания;
* удаление данных не полагается на тайм-аут `flush()`: `forget()` выкидывает
  пользователя и из памяти, и из очереди, поэтому воскрешать нечего;
* неделя режется под лимит сообщения даже если один день сам длиннее лимита,
  и с учётом заголовка;
* `patch()` не теряет обновления при параллельных вызовах из потоков.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from vkbot import db
from vkbot.handlers import consent as consent_handler
from vkbot.handlers import schedule as sched_handler
from vkbot.models import consents
from vkbot.state import StateStore

UID = 4242


class FakeMessage:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, keyboard=None) -> None:
        self.answers.append(text)


def _seen(uid: int) -> bool:
    with db.connect() as conn:
        return conn.execute(
            "SELECT 1 FROM seen_users WHERE vk_id=?", (uid,)
        ).fetchone() is not None


# ── Согласие раньше записи ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_is_not_recorded_before_consent():
    """Первое «Привет» без согласия не должно оставлять следа в базе."""
    await consent_handler.try_handle(None, FakeMessage(), None, "привет", UID)

    assert not _seen(UID), "человека записали в seen_users до согласия"


@pytest.mark.asyncio
async def test_user_is_recorded_once_consent_exists():
    consents.accept(UID, source="test")

    handled = await consent_handler.try_handle(None, FakeMessage(), None, "привет", UID)

    assert handled is False, "согласившегося надо пропускать дальше"
    assert _seen(UID), "согласившегося не отметили в seen_users"


def test_seen_users_is_still_listed_as_personal_data():
    """Если таблицу уберут из выгрузки/удаления, тест обязан упасть."""
    from vkbot.models import user_data

    assert ("seen_users", "vk_id") in user_data._USER_TABLES


# ── Удаление данных не полагается на тайм-аут ────────────────────────────────

def test_forget_drops_queued_writes():
    """Отложенная запись не должна пережить forget()."""
    fresh = StateStore()
    started = threading.Event()
    release = threading.Event()
    original_apply = fresh._apply

    def blocking_apply(kind, uid, payload):
        started.set()
        release.wait(2)
        return original_apply(kind, uid, payload)

    fresh._apply = blocking_apply
    fresh[999] = {"чужая": "запись"}   # писатель займётся ею и встанет
    started.wait(2)
    fresh[UID] = {"step": "day"}        # наша запись ещё в очереди
    release.set()

    fresh.forget(UID)
    fresh.flush()

    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM user_states WHERE user_id=?", (UID,)
        ).fetchone()
    assert row is None, "запись воскресла после forget()"
    assert fresh.get(UID) is None


def test_forget_leaves_other_users_alone():
    fresh = StateStore()
    fresh[UID] = {"step": "day"}
    fresh[UID + 1] = {"step": "direction"}

    fresh.forget(UID)
    fresh.flush()

    with db.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM user_states WHERE user_id=?", (UID + 1,)
        ).fetchone() is not None, "forget() задел чужое состояние"


# ── patch() не теряет обновления ─────────────────────────────────────────────

def test_concurrent_patches_do_not_lose_fields():
    """Каждое поле обязано доехать, даже если patch зовут из разных потоков."""
    fresh = StateStore()
    fresh[UID] = {}

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: fresh.patch(UID, **{f"k{i}": i}), range(8)))

    state = fresh.get(UID)
    assert state is not None
    missing = [f"k{i}" for i in range(8) if f"k{i}" not in state]
    assert not missing, f"потеряны поля: {missing}"


# ── Лимит сообщения ──────────────────────────────────────────────────────────

def test_single_overlong_day_is_split(monkeypatch):
    """День длиннее лимита раньше уходил целиком и обрезался у VK молча."""
    monkeypatch.setattr(
        sched_handler.repo, "get_day",
        lambda *_a, **_kw: "\n".join(f"{i:02d}:00 — пара номер {i}" for i in range(400)),
    )

    chunks = sched_handler._week_chunks(1, "Направление", "чёт")

    assert all(len(c) <= sched_handler._CHUNK_LIMIT for c in chunks), (
        f"кусок длиннее лимита: {max(len(c) for c in chunks)}"
    )


def test_header_is_accounted_for(monkeypatch):
    """Заголовок добавляется к первому куску — место под него надо оставить."""
    monkeypatch.setattr(sched_handler.repo, "get_day", lambda *_a, **_kw: "x" * 560)
    reserve = 120

    chunks = sched_handler._week_chunks(1, "Направление", "чёт", reserve)

    assert len(chunks[0]) + reserve <= sched_handler._CHUNK_LIMIT


def test_unbreakable_line_is_cut_rather_than_dropped(monkeypatch):
    """Одна строка длиннее лимита не должна потеряться целиком."""
    monkeypatch.setattr(sched_handler.repo, "get_day", lambda *_a, **_kw: "я" * 9000)

    chunks = sched_handler._week_chunks(1, "Направление", "чёт")

    assert all(len(c) <= sched_handler._CHUNK_LIMIT for c in chunks)
    assert sum(c.count("я") for c in chunks) == 9000 * 6, "часть расписания пропала"
