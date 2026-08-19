"""Скрипт бэкапа: он дважды ломался молча, и оба раза — на реальном сервере.

Первый раз искал интерпретатор только в `.venv` (на проде каталог зовётся
`venv`), второй — не мог открыть базу, потому что SQLite в WAL-режиме требует
записи даже для чтения. Бэкап, который «отработал» и ничего не скопировал,
хуже отсутствующего: на него рассчитывают.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT / "deploy" / "backup.sh"

bash = shutil.which("bash")

# Скрипт рассчитан на Linux-сервер. Под Windows его формально можно запустить
# через Git Bash, но тогда bash отдаёт POSIX-пути (/c/Users/...) в Windows-Python,
# и sqlite3 молча создаёт пустую базу вместо копии. Гоняем в CI на ubuntu.
pytestmark = [
    pytest.mark.skipif(bash is None, reason="нужен bash"),
    pytest.mark.skipif(os.name == "nt", reason="скрипт для Linux; проверяется в CI"),
]


def _make_db(path: Path, rows: int = 3) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(rows)])
    conn.close()


# conftest.py уводит базы приложения во временный каталог через эти переменные.
# Скрипт читает их же — и без очистки бэкапил базы тестового окружения вместо
# тех, что создал тест. Ошибка ровно того сорта, который скрипт и ловит: копия
# создаётся, «проверка ok», а внутри не те данные.
_INHERITED_DB_VARS = (
    "DATA_DIR", "NOTES_DB", "SCHEDULE_DB", "LEGACY_SCHEDULE_DB",
    "SCHEDULE_VERSIONS_DIR", "BACKUP_DIR", "BACKUP_KEEP_DAYS",
)


def _clean_env(**extra) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _INHERITED_DB_VARS}
    env.update(extra)
    return env


def _run(project: Path, dest: Path, **env_extra) -> subprocess.CompletedProcess:
    env = _clean_env(
        PROJECT_DIR=str(project),
        PYTHON=sys.executable,
        **env_extra,
    )
    return subprocess.run(
        [bash, str(SCRIPT), str(dest)],
        capture_output=True, text=True, env=env, timeout=120,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / "schedule_versions").mkdir(parents=True)
    _make_db(root / "notes.db")
    _make_db(root / "sсhedule.db")           # 'с' кириллическая, как на проде
    (root / "schedule_versions" / "v1.xlsx").write_text("демо", encoding="utf-8")
    return root


def test_backup_copies_databases_and_versions(project: Path, tmp_path: Path):
    dest = tmp_path / "out"

    result = _run(project, dest)

    assert result.returncode == 0, result.stderr
    made = list(dest.iterdir())
    assert len(made) == 1, "ожидалась одна папка со штампом времени"
    files = {f.name for f in made[0].iterdir()}
    assert "notes.db" in files
    assert "sсhedule.db" in files, "кириллическое имя базы не должно теряться"
    assert "schedule_versions.tar.gz" in files, "без версий откат расписания невозможен"


def test_backup_copies_are_readable(project: Path, tmp_path: Path):
    dest = tmp_path / "out"

    _run(project, dest)

    copy = next(dest.iterdir()) / "notes.db"
    assert copy.exists(), "копии notes.db нет — sqlite3.connect создал бы пустышку"
    conn = sqlite3.connect(copy)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3, (
        "в копии не те данные: похоже, бэкап взял базу из чужого окружения"
    )
    conn.close()


def test_backup_honours_data_dir_override(tmp_path: Path):
    """DATA_DIR уводит базы из каталога проекта — бэкап обязан идти следом."""
    project = tmp_path / "proj"
    project.mkdir()
    data = tmp_path / "data"
    (data / "schedule_versions").mkdir(parents=True)
    _make_db(data / "notes.db")
    dest = tmp_path / "out"

    result = _run(project, dest, DATA_DIR=str(data))

    assert result.returncode == 0, result.stderr
    files = {f.name for f in next(dest.iterdir()).iterdir()}
    assert "notes.db" in files, "при DATA_DIR бэкап копировал пустоту"


def test_backup_finds_interpreter_without_explicit_python(project: Path, tmp_path: Path):
    """PYTHON не задан: скрипт должен сам найти .venv, venv или python3."""
    dest = tmp_path / "out"
    env = _clean_env(PROJECT_DIR=str(project))
    env.pop("PYTHON", None)

    result = subprocess.run(
        [bash, str(SCRIPT), str(dest)],
        capture_output=True, text=True, env=env, timeout=120,
    )

    if result.returncode != 0 and "Не найден интерпретатор" in result.stderr:
        pytest.skip("в этом окружении нет python3 в PATH и venv рядом с проектом")
    assert result.returncode == 0, result.stderr


def test_backup_rotates_old_copies(project: Path, tmp_path: Path):
    """Хранение ограничено, иначе диск кончится тихо."""
    dest = tmp_path / "out"
    old = dest / "1999-01-01_000000"
    old.mkdir(parents=True)
    (old / "notes.db").write_text("старьё", encoding="utf-8")
    ancient = os.stat(old).st_atime - 60 * 60 * 24 * 400
    os.utime(old, (ancient, ancient))

    _run(project, dest, BACKUP_KEEP_DAYS="14")

    assert not old.exists(), "копия старше срока хранения должна удаляться"
