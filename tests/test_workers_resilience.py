"""Воркеры переживают сбойный тик.

Регрессия: защита `try/except` вокруг тела тика была только у workers/classes.
У остальных любая ошибка (БД занята, битая строка) навсегда гасила корутину —
бот продолжал отвечать на сообщения, но напоминания, дедлайны и hot-reload
расписания молча переставали работать до перезапуска сервиса.
"""

from __future__ import annotations

import asyncio

import pytest

from vkbot import config
from vkbot.workers import deadlines, reminders, schedule_reloader


async def _run_two_ticks(monkeypatch, worker, target_module, attr):
    """Первый тик падает, второй — гасит цикл. Считаем, дошло ли до второго."""
    calls: list[int] = []

    def boom(*_a, **_kw):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite_error()
        raise asyncio.CancelledError  # штатно завершаем цикл

    monkeypatch.setattr(target_module, attr, boom)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await worker.run(object())
    return calls


def sqlite_error() -> Exception:
    import sqlite3

    return sqlite3.OperationalError("database is locked")


async def _no_sleep(_sec):
    return None


@pytest.mark.asyncio
async def test_reminder_worker_survives_failed_tick(monkeypatch):
    calls = await _run_two_ticks(
        monkeypatch, reminders, reminders.model, "list_pending"
    )
    assert len(calls) == 2, "после сбойного тика воркер обязан прийти на следующий"


@pytest.mark.asyncio
async def test_deadline_worker_survives_failed_tick(monkeypatch):
    calls = await _run_two_ticks(
        monkeypatch, deadlines, deadlines.model, "cleanup_older_than"
    )
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_schedule_reloader_survives_failed_tick(monkeypatch):
    calls = await _run_two_ticks(
        monkeypatch, schedule_reloader, schedule_reloader.repo, "check_reload_marker"
    )
    assert len(calls) == 2


def test_poll_intervals_are_sane():
    """Тик не должен быть нулевым — иначе цикл сожрёт CPU."""
    for name in (
        "REMINDER_POLL_SEC", "DEADLINE_POLL_SEC",
        "CLASS_NOTIFY_POLL_SEC", "SCHEDULE_RELOAD_POLL_SEC",
    ):
        assert getattr(config, name) > 0, name


@pytest.mark.asyncio
async def test_successful_tick_marks_heartbeat(monkeypatch):
    """Отметка живости появляется по факту успешного тика — её читает /healthz."""
    from vkbot.models import heartbeats

    calls: list[int] = []

    def once(*_a, **_kw):
        calls.append(1)
        if len(calls) > 1:
            raise asyncio.CancelledError
        return False

    monkeypatch.setattr(schedule_reloader.repo, "check_reload_marker", once)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await schedule_reloader.run(object())

    assert heartbeats.SCHEDULE_RELOADER in heartbeats.all_ticks()
