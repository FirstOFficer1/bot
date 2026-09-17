"""Одноразовое переименование sсhedule.db (кириллица) → schedule.db."""

from __future__ import annotations

from pathlib import Path

from vkbot.config import (
    SCHEDULE_DB_NAME,
    _LEGACY_CYRILLIC_SCHEDULE_NAME,
    migrate_legacy_schedule_db,
)


def test_renames_cyrillic_and_wal_sidecars(tmp_path: Path):
    legacy = tmp_path / _LEGACY_CYRILLIC_SCHEDULE_NAME
    legacy.write_bytes(b"sqlite-main")
    (tmp_path / f"{_LEGACY_CYRILLIC_SCHEDULE_NAME}-wal").write_bytes(b"wal")
    (tmp_path / f"{_LEGACY_CYRILLIC_SCHEDULE_NAME}-shm").write_bytes(b"shm")

    result = migrate_legacy_schedule_db(tmp_path)

    assert result == tmp_path / SCHEDULE_DB_NAME
    assert result.read_bytes() == b"sqlite-main"
    assert not legacy.exists()
    assert (tmp_path / f"{SCHEDULE_DB_NAME}-wal").read_bytes() == b"wal"
    assert (tmp_path / f"{SCHEDULE_DB_NAME}-shm").read_bytes() == b"shm"
    assert not (tmp_path / f"{_LEGACY_CYRILLIC_SCHEDULE_NAME}-wal").exists()


def test_keeps_latin_if_both_exist(tmp_path: Path):
    latin = tmp_path / SCHEDULE_DB_NAME
    legacy = tmp_path / _LEGACY_CYRILLIC_SCHEDULE_NAME
    latin.write_bytes(b"new")
    legacy.write_bytes(b"old")

    assert migrate_legacy_schedule_db(tmp_path) is None
    assert latin.read_bytes() == b"new"
    assert legacy.read_bytes() == b"old", "не трогаем старый файл, если новый уже есть"


def test_noop_when_nothing_to_migrate(tmp_path: Path):
    assert migrate_legacy_schedule_db(tmp_path) is None
