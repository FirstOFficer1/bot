#!/bin/bash
# Бэкап данных бота и панели.
#
#   ./deploy/backup.sh [каталог-назначения]
#
# По умолчанию кладёт в /var/backups/vkbot. Каталог можно задать переменной
# BACKUP_DIR, глубину хранения — BACKUP_KEEP_DAYS (по умолчанию 14).
# Пути к самим базам берутся из тех же переменных, что и у приложения:
# DATA_DIR / NOTES_DB / SCHEDULE_DB / LEGACY_SCHEDULE_DB / SCHEDULE_VERSIONS_DIR.
#
# Копируются:
#   * notes.db      — заметки, напоминания, дедлайны, подписки, сессии панели, аудит
#   * sсhedule.db   — расписание (буква 'с' в имени КИРИЛЛИЧЕСКАЯ, так на проде)
#   * s.db          — база легаси Telegram-бота, если она есть
#   * schedule_versions/ — сохранённые Excel-файлы; без них не работает откат
#
# Базы копируются через sqlite3 .backup, а не cp: сервис останавливать не нужно,
# копия консистентна даже при активной записи. Простой cp в режиме WAL может
# забрать базу без хвоста транзакций из -wal.

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/vkbot}"
BACKUP_DIR="${1:-${BACKUP_DIR:-/var/backups/vkbot}}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
PYTHON="${PYTHON:-$PROJECT_DIR/.venv/bin/python}"

# Те же переопределения, что читает приложение (vkbot/config.py и web_panel.py).
# Иначе при DATA_DIR на отдельном томе бэкап честно отработает, скопировав ничего.
DATA_DIR="${DATA_DIR:-$PROJECT_DIR}"
NOTES_DB="${NOTES_DB:-$DATA_DIR/notes.db}"
SCHEDULE_DB="${SCHEDULE_DB:-$DATA_DIR/sсhedule.db}"
LEGACY_SCHEDULE_DB="${LEGACY_SCHEDULE_DB:-$DATA_DIR/s.db}"
SCHEDULE_VERSIONS_DIR="${SCHEDULE_VERSIONS_DIR:-$DATA_DIR/schedule_versions}"

STAMP="$(date +%F_%H%M%S)"
DEST="$BACKUP_DIR/$STAMP"

if [ ! -x "$PYTHON" ]; then
    echo "Не найден интерпретатор $PYTHON — задайте PYTHON=..." >&2
    exit 1
fi

mkdir -p "$DEST"

# Онлайн-копия SQLite средствами самого sqlite3 (модуль есть в stdlib —
# отдельная утилита sqlite3 на сервере не нужна).
backup_db() {
    local src="$1" name="$2"
    if [ ! -f "$src" ]; then
        echo "  · $name: файла нет, пропускаю"
        return
    fi
    "$PYTHON" - "$src" "$DEST/$name" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
s.close(); d.close()
PY
    echo "  · $name: $(du -h "$DEST/$name" | cut -f1)"
}

echo "Бэкап в $DEST"
backup_db "$NOTES_DB"            "notes.db"
backup_db "$SCHEDULE_DB"         "sсhedule.db"
backup_db "$LEGACY_SCHEDULE_DB"  "s.db"

if [ -d "$SCHEDULE_VERSIONS_DIR" ]; then
    versions_parent="$(dirname "$SCHEDULE_VERSIONS_DIR")"
    tar -czf "$DEST/schedule_versions.tar.gz" -C "$versions_parent" "$(basename "$SCHEDULE_VERSIONS_DIR")"
    echo "  · schedule_versions.tar.gz: $(du -h "$DEST/schedule_versions.tar.gz" | cut -f1)"
fi

# Проверяем, что копия читается и не пустая: бэкап, который не открывается,
# хуже отсутствующего — на него рассчитывают.
"$PYTHON" - "$DEST/notes.db" <<'PY'
import sqlite3, sys
path = sys.argv[1]
with sqlite3.connect(path) as conn:
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        sys.exit("копия notes.db не прошла integrity_check")
    tables = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]
conn.close()
if tables == 0:
    sys.exit("копия notes.db пустая: таблиц нет")
print(f"  · проверка: integrity_check ok, таблиц {tables}")
PY

# Ротация: удаляем каталоги старше KEEP_DAYS дней.
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -mtime "+$KEEP_DAYS" -exec rm -rf {} +

echo "Готово. Хранится копий: $(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d | wc -l)"
