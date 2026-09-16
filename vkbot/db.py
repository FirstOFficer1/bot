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
                used INTEGER DEFAULT 0,
                purpose TEXT NOT NULL DEFAULT 'login'
            );
            CREATE INDEX IF NOT EXISTS idx_panel_codes_user ON panel_login_codes(user_id);
            -- Провалы входа: per-IP и глобальный rate-limit переживают рестарт.
            CREATE TABLE IF NOT EXISTS login_failures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT NOT NULL,
                failed_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_login_failures_ip_ts
                ON login_failures(ip, failed_at);
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
            -- «Выйти со всех устройств» отзывает RM-токены, но Flask-сессия —
            -- подписанная кука на стороне клиента, удалить её на чужом
            -- устройстве нельзя. Здесь лежит момент отзыва: сессия, выданная
            -- раньше него, к fallback-входу больше не допускается.
            CREATE TABLE IF NOT EXISTS panel_session_revocations (
                vk_id INTEGER PRIMARY KEY,
                revoked_at TEXT NOT NULL
            );
            -- Согласие на обработку персональных данных (152-ФЗ, ст. 9).
            -- Хранится версия текста: меняем документ — поднимаем версию, и
            -- согласие спрашивается заново, иначе человек считался бы
            -- согласившимся с тем, чего не читал. IP и время — доказательство
            -- того, что согласие было дано, его требуют при проверке.
            CREATE TABLE IF NOT EXISTS user_consents (
                vk_id INTEGER PRIMARY KEY,
                version TEXT NOT NULL,
                accepted_at TEXT NOT NULL,
                ip TEXT,
                source TEXT
            );
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
            -- Дедупликация перед уникальным индексом: дубли могли накопиться,
            -- пока защиты не было. Оставляем активную строку, а не просто
            -- первую по id — иначе отключённая подписка пережила бы рабочую.
            DELETE FROM subscriptions WHERE id NOT IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY user_id, course, LOWER(direction)
                        ORDER BY COALESCE(disabled, 0) ASC, id ASC
                    ) AS rn
                    FROM subscriptions
                )
                WHERE rn = 1
            );
            -- Проверка «уже подписан?» и вставка идут двумя отдельными шагами,
            -- между которыми хендлер отдаёт управление: два одновременных
            -- сообщения одного человека проходили проверку оба. Гарантию даёт
            -- база, а не порядок вызовов.
            --
            -- LOWER — чтобы совпадать с exists(), который сравнивает так же.
            -- Но учтите: LOWER() и COLLATE NOCASE в SQLite работают только для
            -- ASCII, кириллицу они не трогают. То есть для русских названий обе
            -- стороны регистрозависимы, и «Прикладная» с «ПРИКЛАДНАЯ» считаются
            -- разными. На практике это не всплывает: направления приходят из
            -- одного и того же импорта Excel одной и той же строкой. Понадобится
            -- настоящая нечувствительность к регистру — нормализовать нужно в
            -- Python (casefold) на входе, а не здесь.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_subs_unique
                ON subscriptions(user_id, course, LOWER(direction));
            CREATE INDEX IF NOT EXISTS idx_reminders_pending ON reminders(notified, remind_at);
            CREATE INDEX IF NOT EXISTS idx_deadlines_at ON deadlines(deadline_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_sent_notifs_lookup
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

        # purpose у кодов входа: login vs step_up (чтобы OTP входа не годился
        # для передачи владения). Старые строки получают 'login'.
        code_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(panel_login_codes)").fetchall()
        }
        if "purpose" not in code_cols:
            conn.execute(
                "ALTER TABLE panel_login_codes ADD COLUMN purpose TEXT NOT NULL DEFAULT 'login'"
            )

        # CREATE INDEX IF NOT EXISTS не переопределяет уже существующий индекс,
        # поэтому на старой БД idx_sent_notifs_lookup остался бы висеть на
        # schedule_row_id — и новый запрос дедупликации шёл бы мимо него.
        # Плюс индекс обязан быть UNIQUE: иначе claim-before-send не держит гонку.
        idx_cols = [
            row[2]
            for row in conn.execute("PRAGMA index_info('idx_sent_notifs_lookup')").fetchall()
        ]
        idx_unique = any(
            row[1] == "idx_sent_notifs_lookup" and row[2]
            for row in conn.execute("PRAGMA index_list('sent_class_notifications')")
        )
        if "class_key" not in idx_cols or not idx_unique:
            conn.execute("DROP INDEX IF EXISTS idx_sent_notifs_lookup")
            # Перед UNIQUE убираем дубли, иначе CREATE UNIQUE INDEX упадёт на
            # боевой базе, где гонка уже успела записать одну пару дважды.
            conn.execute(
                "DELETE FROM sent_class_notifications WHERE id NOT IN ("
                "  SELECT MIN(id) FROM sent_class_notifications "
                "  GROUP BY user_id, class_key, class_date, class_time"
                ")"
            )
            conn.execute(
                "CREATE UNIQUE INDEX idx_sent_notifs_lookup "
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
