"""Работа с SQLite не держит event loop, а отметки читаются пачкой.

Бот однопоточный и асинхронный, а `db.connect()` ставит `busy_timeout=5000`.
Пока запрос к занятой базе шёл синхронно прямо в корутине, вставал не только
её воркер, но и разбор входящих сообщений — то есть весь бот. Отдельно
воркер уведомлений спрашивал «уже отправляли?» по каждому подписчику: у группы
в двести человек это двести открытий соединения на одну пару.

Проверяем не по таймингам (в CI они шаткие), а по идентификатору потока:
обращение к БД обязано выполняться не в потоке цикла событий.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from vkbot.models import sent_notifs
from vkbot.workers import classes, deadlines, reminders, schedule_reloader

KEY = "1|исит|матан|301"
DATE = "2026-09-15"
TIME = "08:00"


# ── Пакетное чтение и запись отметок ─────────────────────────────────────────

def test_sent_uids_returns_everyone_already_notified():
    sent_notifs.mark(1, KEY, DATE, TIME)
    sent_notifs.mark(2, KEY, DATE, TIME)
    sent_notifs.mark(3, KEY, DATE, "09:40")

    assert sent_notifs.sent_uids(KEY, DATE, TIME) == {1, 2}


def test_sent_uids_is_empty_when_nobody_notified():
    assert sent_notifs.sent_uids(KEY, DATE, TIME) == set()


def test_mark_many_marks_everyone():
    sent_notifs.mark_many([7, 8, 9], KEY, DATE, TIME)

    assert sent_notifs.sent_uids(KEY, DATE, TIME) == {7, 8, 9}
    assert sent_notifs.was_sent(8, KEY, DATE, TIME)


def test_mark_many_on_empty_list_does_nothing():
    sent_notifs.mark_many([], KEY, DATE, TIME)

    assert sent_notifs.sent_uids(KEY, DATE, TIME) == set()


def test_claim_many_is_single_winner_per_uid():
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: sent_notifs.claim_many([42, 43], KEY, DATE, TIME),
                range(8),
            )
        )
    claimed = [uid for batch in results for uid in batch]
    assert sorted(claimed) == [42, 43]
    assert sent_notifs.sent_uids(KEY, DATE, TIME) == {42, 43}


def test_unclaim_many_frees_slots_for_retry():
    assert sent_notifs.claim_many([7], KEY, DATE, TIME) == [7]
    sent_notifs.unclaim_many([7], KEY, DATE, TIME)
    assert sent_notifs.claim_many([7], KEY, DATE, TIME) == [7]


def test_two_hundred_subscribers_cost_one_connection(monkeypatch):
    """Раньше на каждого подписчика открывалось своё соединение."""
    uids = list(range(100, 300))
    sent_notifs.mark_many(uids, KEY, DATE, TIME)

    opened = 0
    original_connect = sent_notifs.connect

    def counting_connect(*args, **kwargs):
        nonlocal opened
        opened += 1
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sent_notifs, "connect", counting_connect)
    already = sent_notifs.sent_uids(KEY, DATE, TIME)

    assert already == set(uids)
    assert opened == 1, f"двести подписчиков стоили {opened} соединений вместо одного"


# ── Запросы уходят из event loop ─────────────────────────────────────────────

async def _thread_of_db_call(monkeypatch, worker, module, attr) -> int:
    """Прогоняет один тик воркера и возвращает поток, в котором дёрнули БД."""
    seen: dict[str, int] = {}

    def probe(*_a, **_kw):
        seen["thread"] = threading.get_ident()
        raise asyncio.CancelledError  # штатно обрываем цикл после замера

    async def _no_sleep(_sec):
        return None

    monkeypatch.setattr(module, attr, probe)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await worker.run(object())
    return seen["thread"]


@pytest.mark.asyncio
async def test_reminder_worker_queries_outside_the_loop(monkeypatch):
    thread = await _thread_of_db_call(
        monkeypatch, reminders, reminders.model, "list_pending"
    )
    assert thread != threading.get_ident()


@pytest.mark.asyncio
async def test_deadline_worker_queries_outside_the_loop(monkeypatch):
    thread = await _thread_of_db_call(
        monkeypatch, deadlines, deadlines.model, "list_pending"
    )
    assert thread != threading.get_ident()


@pytest.mark.asyncio
async def test_schedule_reloader_queries_outside_the_loop(monkeypatch):
    thread = await _thread_of_db_call(
        monkeypatch, schedule_reloader, schedule_reloader.repo, "check_reload_marker"
    )
    assert thread != threading.get_ident()


@pytest.mark.asyncio
async def test_class_worker_queries_outside_the_loop(monkeypatch):
    thread = await _thread_of_db_call(
        monkeypatch, classes, classes.sent_notifs, "cleanup_older_than"
    )
    assert thread != threading.get_ident()


@pytest.mark.asyncio
async def test_busy_database_does_not_freeze_the_whole_bot(monkeypatch):
    """Пока воркер ждёт занятую базу, бот обязан продолжать работать.

    Это и есть исходная жалоба: `busy_timeout` — пять секунд, и синхронный
    запрос в корутине останавливал не свой воркер, а разбор сообщений целиком.
    """
    ticks = 0
    real_sleep = asyncio.sleep

    async def counter():
        nonlocal ticks
        while True:
            await real_sleep(0.01)
            ticks += 1

    def slow_then_stop(*_a, **_kw):
        threading.Event().wait(0.3)  # занятая база
        raise asyncio.CancelledError  # обрываем цикл воркера

    async def _no_sleep(_sec):
        return None

    monkeypatch.setattr(reminders.model, "list_pending", slow_then_stop)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    spinner = asyncio.create_task(counter())
    with pytest.raises(asyncio.CancelledError):
        await reminders.run(object())
    spinner.cancel()

    assert ticks >= 5, f"бот стоял, пока воркер ждал базу: {ticks} тиков вместо ~30"
