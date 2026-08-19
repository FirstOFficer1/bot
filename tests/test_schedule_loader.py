"""Загрузка расписания: превью, применение, версии, откат, hot-reload.

Регрессии, которые здесь закрыты:
  * `from . import repo` возвращал не модуль, а экземпляр ScheduleRepo (пакетный
    __init__ экспортирует синглтон под тем же именем), поэтому commit() падал
    на signal_reload() уже ПОСЛЕ замены расписания в БД: админ видел ошибку,
    бот не перечитывал кэш, аудит и рассылка не срабатывали;
  * незакрытое sqlite-соединение не давало удалить временную БД превью
    (на Windows — WinError 32, превью не работало вовсе).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vkbot import config
from vkbot.schedule import loader
from vkbot.schedule.repo import repo

import tools.make_demo_schedule as demo


@pytest.fixture
def excel(tmp_path) -> str:
    return demo.build(str(tmp_path / "demo.xlsx"))


@pytest.fixture(autouse=True)
def _clean_schedule():
    yield
    with sqlite3.connect(config.SCHEDULE_DB) as conn:
        conn.execute("DELETE FROM schedule")
    conn.close()


def test_preview_does_not_touch_current_schedule(excel):
    before = _count()
    pv = loader.preview(excel)
    assert pv.records_after > 0
    assert _count() == before, "превью не должно писать в основную БД"


def test_preview_cleans_up_temp_db(excel):
    """Незакрытое соединение раньше оставляло временный файл (и падало на Windows)."""
    tmp_before = set(Path(__import__("tempfile").gettempdir()).glob("*.db"))
    loader.preview(excel)
    tmp_after = set(Path(__import__("tempfile").gettempdir()).glob("*.db"))
    assert tmp_after <= tmp_before, "временная БД превью должна удаляться"


def test_commit_imports_and_signals_reload(excel):
    marker = config.SCHEDULE_RELOAD_MARKER
    before_mtime = marker.stat().st_mtime if marker.exists() else 0

    result = loader.commit(excel, "test", "demo.xlsx")

    assert result["row_count"] > 0
    assert _count() == result["row_count"]
    assert marker.exists(), "файл-маркер hot-reload должен быть создан"
    assert marker.stat().st_mtime >= before_mtime
    # Кэш направлений перечитан в этом же процессе.
    assert repo.directions_by_course, "кэш направлений должен быть заполнен"


def test_commit_records_version(excel):
    loader.commit(excel, "test", "demo.xlsx")
    versions = loader.list_versions()
    assert versions, "загрузка должна попадать в историю версий"
    assert versions[0]["row_count"] > 0
    assert Path(versions[0]["file_path"]).exists(), "Excel версии должен сохраняться"


def test_commit_makes_backup_table(excel):
    loader.commit(excel, "test", "demo.xlsx")
    loader.commit(excel, "test", "demo2.xlsx")
    with sqlite3.connect(config.SCHEDULE_DB) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    conn.close()
    assert "schedule_backup" in tables


def test_rollback_to_previous_version(excel):
    loader.commit(excel, "test", "first.xlsx")
    first_id = loader.list_versions()[0]["id"]

    result = loader.rollback(first_id, uploaded_by="test")

    assert result["row_count"] > 0
    assert _count() == result["row_count"]


def test_rollback_unknown_version_raises():
    with pytest.raises(ValueError):
        loader.rollback(999_999)


def _count() -> int:
    with sqlite3.connect(config.SCHEDULE_DB) as conn:
        try:
            n = conn.execute("SELECT COUNT(*) FROM schedule").fetchone()[0]
        except sqlite3.OperationalError:
            n = 0
    conn.close()
    return n
