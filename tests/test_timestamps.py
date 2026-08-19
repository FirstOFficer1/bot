"""Все отметки времени пишутся в МСК, а не в часовом поясе сервера.

Раньше предметная логика жила по `now_msk()`, а служебные отметки — аудит,
коды входа, `last_seen`, версии расписания, прогресс рассылки — по
`datetime.now()`. На UTC-сервере (а прод именно такой) журнал расходился с
расписанием на три часа, и корректность зависела от настройки машины, а не
от кода. Ещё хуже: сервер общий, и менять его таймзону ради нас нельзя.
"""

from __future__ import annotations

import datetime
import pathlib
import re

import pytest

from vkbot import config
from vkbot.models import audit, panel_codes, panel_remember, panel_users, seen_users

PROJECT = pathlib.Path(__file__).resolve().parent.parent
# Легаси-бот не поддерживается и живёт по своим правилам (см. CLAUDE.md).
SCANNED = [
    *sorted((PROJECT / "vkbot").rglob("*.py")),
    PROJECT / "web_panel.py",
    PROJECT / "import_excel.py",
]

_NAIVE_NOW = re.compile(r"(?<!_msk)\bdatetime\.now\(\)")


def test_no_server_local_timestamps_in_code():
    """Ни один модуль не должен звать datetime.now() напрямую."""
    offenders = []
    for path in SCANNED:
        text = path.read_text(encoding="utf-8")
        for n, line in enumerate(text.split("\n"), 1):
            if line.lstrip().startswith("#"):
                continue
            if _NAIVE_NOW.search(line):
                offenders.append(f"{path.relative_to(PROJECT)}:{n}: {line.strip()[:70]}")
    assert not offenders, "серверное время вместо now_msk():\n  " + "\n  ".join(offenders)


def _parse(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: (panel_codes.issue(1001), _last("panel_login_codes", "created_at"))[1],
                     id="код входа"),
        pytest.param(lambda: (audit.log(1001, "test.stamp"), _last("audit_log", "created_at"))[1],
                     id="аудит"),
        pytest.param(lambda: (seen_users.touch(1001), _last("seen_users", "last_seen"))[1],
                     id="последний визит"),
        pytest.param(
            lambda: (panel_users.grant(4004, granted_by=1001), _last("panel_users", "added_at"))[1],
            id="выдача админки",
        ),
        pytest.param(
            lambda: (panel_remember.issue(1001), _last("panel_remember_tokens", "created_at"))[1],
            id="токен «запомнить меня»",
        ),
    ],
)
def test_stamps_are_written_in_msk(make):
    """Записанная отметка должна совпадать с МСК, а не с временем машины."""
    stamp = _parse(make())
    delta = abs((config.now_msk() - stamp).total_seconds())
    assert delta < 60, f"отметка {stamp} разошлась с МСК на {delta:.0f} с"


def _last(table: str, column: str) -> str:
    from vkbot.db import connect

    with connect() as conn:
        row = conn.execute(
            f"SELECT {column} FROM {table} ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert row, f"в {table} ничего не записалось"
    return row[0]


def test_login_code_ttl_works_in_msk():
    """TTL считается от той же шкалы, что и запись, — код живёт свои 10 минут."""
    code, ttl = panel_codes.issue(1001)
    assert ttl == panel_codes.CODE_TTL_MIN
    assert panel_codes.verify(code) == 1001


def test_audit_retention_uses_same_scale():
    """Чистка сравнивает cutoff со строками той же шкалы — свежее не удаляем."""
    audit.log(1001, "test.fresh")
    audit.cleanup_older_than(config.AUDIT_KEEP_DAYS)
    assert any(e["action"] == "test.fresh" for e in audit.list_recent())
