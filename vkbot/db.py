"""Низкоуровневый доступ к SQLite: контекст-менеджер, PRAGMA, схема, индексы."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config


@contextmanager
def connect(path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Открывает соединение, включает WAL и busy_timeout, коммитит на выходе."""
    conn = sqlite3.connect(path or config.NOTES_DB)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    """Создаёт схему и индексы. Идемпотентно — безопасно вызывать на каждом старте."""
    with connect() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_states (
                user_id INTEGER PRIMARY KEY,
                state_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, note_text TEXT, timestamp TEXT
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, reminder_text TEXT,
                remind_at TEXT, notified INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, course INTEGER, direction TEXT,
                disabled INTEGER DEFAULT 0
            );
            -- class_key — стабильный отпечаток пары (курс|направление|предмет|
            -- аудитория). Раньше здесь был schedule_row_id, но переимпорт
            -- расписания перезаписывает таблицу и раздаёт новые rowid, поэтому
            -- загрузка в середине дня рассылала уведомления по второму разу.
            CREATE TABLE IF NOT EXISTS sent_class_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, schedule_row_id INTEGER,
                class_key TEXT,
                class_date TEXT, class_time TEXT, sent_at TEXT
            );
            CREATE TABLE IF NOT EXISTS deadlines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                subject TEXT,
                description TEXT,
                deadline_at TEXT,
                notified_1day INTEGER DEFAULT 0,
                notified_1hour INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id INTEGER PRIMARY KEY,
                course INTEGER,
                direction TEXT
            );
            CREATE TABLE IF NOT EXISTS schedule_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uploaded_at TEXT,
                original_filename TEXT,
                row_count INTEGER,
                uploaded_by TEXT,
                file_path TEXT
            );
            CREATE TABLE IF NOT EXISTS panel_login_codes (
                code TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                used INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_panel_codes_user ON panel_login_codes(user_id);
            CREATE TABLE IF NOT EXISTS panel_users (
                vk_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL DEFAULT 'admin',
                name TEXT,
                added_at TEXT NOT NULL,
                added_by INTEGER
            );
            CREATE TABLE IF NOT EXISTS seen_users (
                vk_id INTEGER PRIMARY KEY,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_seen_users_last ON seen_users(last_seen DESC);
            CREATE TABLE IF NOT EXISTS panel_remember_tokens (
                token_hash TEXT PRIMARY KEY,
                vk_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rm_tokens_user ON panel_remember_tokens(vk_id);
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_vk_id INTEGER NOT NULL DEFAULT 0,
                action TEXT NOT NULL,
                target TEXT,
                details TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_vk_id);
            -- Отметки живости фоновых воркеров: по ним /healthz понимает, что
            -- процесс бота жив, но, например, рассылка напоминаний встала.
            CREATE TABLE IF NOT EXISTS worker_heartbeats (
                name TEXT PRIMARY KEY,
                last_tick TEXT NOT NULL,
                ticks INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions(user_id);
            CREATE INDEX IF NOT EXISTS idx_reminders_pending ON reminders(notified, remind_at);
            CREATE INDEX IF NOT EXISTS idx_deadlines_at ON deadlines(deadline_at);
            CREATE INDEX IF NOT EXISTS idx_sent_notifs_lookup
                ON sent_class_notifications(user_id, class_key, class_date, class_time);
            CREATE INDEX IF NOT EXISTS idx_sent_notifs_date ON sent_class_notifications(class_date);
            CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id);
        """)

        # Миграция: добавить колонку disabled, если её ещё нет (старая БД)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(subscriptions)").fetchall()}
        if "disabled" not in cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN disabled INTEGER DEFAULT 0")

        # Миграция: class_key вместо schedule_row_id в журнале уведомлений.
        # Старые строки остаются с NULL — их за двое суток уберёт штатная чистка.
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(sent_class_notifications)").fetchall()
        }
        if "class_key" not in cols:
            conn.execute("ALTER TABLE sent_class_notifications ADD COLUMN class_key TEXT")

        # CREATE INDEX IF NOT EXISTS не переопределяет уже существующий индекс,
        # поэтому на старой БД idx_sent_notifs_lookup остался бы висеть на
        # schedule_row_id — и новый запрос дедупликации шёл бы мимо него.
        idx_cols = [
            row[2]
            for row in conn.execute("PRAGMA index_info('idx_sent_notifs_lookup')").fetchall()
        ]
        if "class_key" not in idx_cols:
            conn.execute("DROP INDEX IF EXISTS idx_sent_notifs_lookup")
            conn.execute(
                "CREATE INDEX idx_sent_notifs_lookup "
                "ON sent_class_notifications(user_id, class_key, class_date, class_time)"
            )

    # Индексы и WAL для базы расписания
    with connect(config.SCHEDULE_DB) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course INTEGER, direction TEXT, day TEXT,
                time TEXT, subject TEXT, teacher TEXT, room TEXT,
                week TEXT DEFAULT '',
                class_type TEXT DEFAULT '',
                date_range TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_sched_course_dir_day
                ON schedule(course, direction, day);
        """)
