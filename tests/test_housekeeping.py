"""Чистка служебных таблиц.

Регрессия: `panel_codes.cleanup_old()` и `cleanup_rate_limits()` были написаны,
но их никто не вызывал — таблица одноразовых кодов росла по строке на каждый
запрос входа, а журнал аудита не чистился вообще.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from vkbot import config, db
from vkbot.models import audit, panel_codes
from vkbot.workers import deadlines


def _age_row(table: str, column: str, days: int) -> None:
    old = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute(f"UPDATE {table} SET {column}=?", (old,))


def test_housekeeping_removes_stale_login_codes():
    panel_codes.issue(1001)
    _age_row("panel_login_codes", "created_at", config.PANEL_CODES_CLEANUP_DAYS + 1)

    deadlines._housekeeping()

    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM panel_login_codes").fetchone()[0] == 0


def test_housekeeping_keeps_fresh_login_codes():
    code, _ttl = panel_codes.issue(1001)

    deadlines._housekeeping()

    assert panel_codes.verify(code) == 1001, "свежий код должен остаться рабочим"


def test_housekeeping_trims_old_audit_but_keeps_recent():
    audit.log(1001, "test.old")
    _age_row("audit_log", "created_at", config.AUDIT_KEEP_DAYS + 1)
    audit.log(1001, "test.fresh")

    deadlines._housekeeping()

    actions = {e["action"] for e in audit.list_recent()}
    assert "test.fresh" in actions
    assert "test.old" not in actions


def test_audit_cleanup_reports_count():
    audit.log(1001, "test.old")
    _age_row("audit_log", "created_at", config.AUDIT_KEEP_DAYS + 5)
    assert audit.cleanup_older_than(config.AUDIT_KEEP_DAYS) == 1


@pytest.mark.asyncio
async def test_housekeeping_runs_on_worker_tick(monkeypatch):
    """Чистка должна вызываться из тика воркера, а не лежать мёртвым кодом."""
    import asyncio

    called: list[int] = []
    monkeypatch.setattr(deadlines, "_housekeeping", lambda: called.append(1))
    monkeypatch.setattr(deadlines.model, "list_pending", lambda: [])

    async def _stop_after_first(_sec):
        if called:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _stop_after_first)

    with pytest.raises(asyncio.CancelledError):
        await deadlines.run(object())

    assert called, "первый тик обязан запустить уборку"


def test_notif_index_migrated_to_class_key():
    """На старой БД индекс висел на schedule_row_id — CREATE IF NOT EXISTS его не менял."""
    db.init()  # идемпотентно, повторный вызов не должен ничего ломать
    with db.connect() as conn:
        cols = [
            r[2] for r in conn.execute("PRAGMA index_info('idx_sent_notifs_lookup')").fetchall()
        ]
    assert cols == ["user_id", "class_key", "class_date", "class_time"]
