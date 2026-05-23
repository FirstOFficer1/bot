"""Загрузка нового расписания из Excel: версионирование, превью, бэкап, hot-reload.

Высокоуровневый API:
    preview(excel_path)             — посчитать, что изменится; ничего не пишет
    commit(excel_path, who, name)   — заменить расписание + бэкап + версия + reload
    list_versions()                 — история загрузок
    rollback(version_id)            — откат к версии (по сохранённому Excel)
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import config
from ..db import connect
from . import repo as repo_mod


@dataclass
class Preview:
    courses_before: int
    courses_after: int
    directions_before: int
    directions_after: int
    records_before: int
    records_after: int
    new_directions: list[str]
    removed_directions: list[str]


def _import(excel_path: str, db_path: str) -> int:
    """Прокси к существующему импортёру. Возвращает число записей."""
    # Импорт лежит в корне проекта, не в пакете
    from import_excel import import_schedule  # type: ignore[import-not-found]
    return import_schedule(excel_path, db_path)


def _snapshot_directions(db_path: str) -> tuple[int, set[tuple[int, str]], int]:
    """Возвращает (records, set((course, direction)), courses)."""
    with sqlite3.connect(db_path) as conn:
        try:
            records = conn.execute("SELECT COUNT(*) FROM schedule").fetchone()[0]
            pairs = set(
                conn.execute(
                    "SELECT DISTINCT course, direction FROM schedule"
                ).fetchall()
            )
        except sqlite3.OperationalError:
            return 0, set(), 0
    courses = {c for c, _ in pairs}
    return records, pairs, len(courses)


def preview(excel_path: str) -> Preview:
    """Парсит Excel в одноразовую БД и считает дельту с текущей."""
    cur_records, cur_pairs, cur_courses = _snapshot_directions(config.SCHEDULE_DB)

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_db = tmp.name
    try:
        new_records = _import(excel_path, tmp_db)
        _, new_pairs, new_courses = _snapshot_directions(tmp_db)
    finally:
        Path(tmp_db).unlink(missing_ok=True)

    added = [f"{c} курс — {d}" for c, d in sorted(new_pairs - cur_pairs)]
    removed = [f"{c} курс — {d}" for c, d in sorted(cur_pairs - new_pairs)]

    return Preview(
        courses_before=cur_courses,
        courses_after=new_courses,
        directions_before=len(cur_pairs),
        directions_after=len(new_pairs),
        records_before=cur_records,
        records_after=new_records,
        new_directions=added,
        removed_directions=removed,
    )


def commit(excel_path: str, uploaded_by: str, original_filename: str) -> dict:
    """Атомарная замена расписания + бэкап + версия + hot-reload.

    Шаги:
      1. Бэкапим текущую таблицу schedule → schedule_backup
      2. Сохраняем Excel в schedule_versions/<timestamp>_<name>
      3. Импортируем новый Excel в основную БД
      4. Создаём запись в schedule_versions
      5. Триггерим hot-reload через файл-маркер
    """
    from datetime import datetime

    # 1. Бэкап
    with sqlite3.connect(config.SCHEDULE_DB) as conn:
        conn.executescript("""
            DROP TABLE IF EXISTS schedule_backup;
            CREATE TABLE schedule_backup AS SELECT * FROM schedule;
        """)

    # 2. Сохраняем оригинальный Excel
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in original_filename)
    saved_path = config.SCHEDULE_VERSIONS_DIR / f"{ts}_{safe_name}"
    shutil.copyfile(excel_path, saved_path)

    # 3. Импорт
    count = _import(str(saved_path), config.SCHEDULE_DB)

    # 4. Запись в журнал
    with connect() as conn:
        conn.execute(
            "INSERT INTO schedule_versions "
            "(uploaded_at, original_filename, row_count, uploaded_by, file_path) "
            "VALUES (?,?,?,?,?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                original_filename,
                count,
                uploaded_by,
                str(saved_path),
            ),
        )

    # 5. Hot-reload: бот заметит изменение mtime в SCHEDULE_RELOAD_POLL_SEC
    repo_mod.signal_reload()
    # И сразу обновим in-process кэш для текущего процесса (если бот в нём)
    try:
        repo_mod.repo.reload()
    except Exception:
        pass

    return {"row_count": count, "saved_path": str(saved_path)}


def list_versions() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, uploaded_at, original_filename, row_count, uploaded_by, file_path "
            "FROM schedule_versions ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return [
        {
            "id": r[0],
            "uploaded_at": r[1],
            "original_filename": r[2],
            "row_count": r[3],
            "uploaded_by": r[4],
            "file_path": r[5],
        }
        for r in rows
    ]


def rollback(version_id: int, uploaded_by: str = "rollback") -> dict:
    """Применяет ранее сохранённый Excel-файл из version_id."""
    with connect() as conn:
        row = conn.execute(
            "SELECT original_filename, file_path FROM schedule_versions WHERE id=?",
            (version_id,),
        ).fetchone()
    if not row:
        raise ValueError(f"version {version_id} not found")
    original_filename, file_path = row
    if not Path(file_path).exists():
        raise FileNotFoundError(f"saved file gone: {file_path}")
    return commit(file_path, uploaded_by, f"rollback:{original_filename}")


# Совместимость: позволяет вызвать модуль без вебки
def quick_apply(excel_path: str, who: str = "cli") -> int:
    """Быстрая загрузка без превью (для CLI/cron). Возвращает кол-во записей."""
    result = commit(excel_path, who, Path(excel_path).name)
    return result["row_count"]
