"""Полное удаление данных пользователя по его запросу.

Политика конфиденциальности обещает удаление всех данных — до сих пор это был
ручной процесс («напишите боту»). Здесь одна функция, которая знает про все
таблицы: добавили таблицу с данными пользователя — добавьте её и сюда, иначе
обещание перестанет быть правдой.

Что НЕ удаляется и почему:
  * `audit_log` — журнал безопасности. Записи о входах и действиях админов
    нужны для расследований, и возможность стереть свой след через кнопку в
    интерфейсе сделала бы его бессмысленным. Личные данные там не хранятся,
    только VK ID и название действия; срок хранения ограничен AUDIT_KEEP_DAYS.
  * `panel_users` для владельцев из env — их роль задаётся файлом .env на
    сервере, панель её не контролирует (см. web_panel._is_owner).
"""

from __future__ import annotations

from ..db import connect

# (таблица, колонка с идентификатором). Порядок не важен — всё в одной транзакции.
_USER_TABLES: tuple[tuple[str, str], ...] = (
    ("notes", "user_id"),
    ("reminders", "user_id"),
    ("deadlines", "user_id"),
    ("subscriptions", "user_id"),
    ("user_prefs", "user_id"),
    ("user_states", "user_id"),
    ("sent_class_notifications", "user_id"),
    ("panel_login_codes", "user_id"),
    ("panel_remember_tokens", "vk_id"),
    ("seen_users", "vk_id"),
    ("panel_users", "vk_id"),
    ("user_consents", "vk_id"),
)

# Человеческие названия для отчёта пользователю — что именно удалили.
LABELS = {
    "notes": "заметки",
    "reminders": "напоминания",
    "deadlines": "дедлайны",
    "subscriptions": "подписки на пары",
    "user_prefs": "сохранённая группа",
    "user_states": "состояние диалога с ботом",
    "sent_class_notifications": "журнал отправленных уведомлений",
    "panel_login_codes": "коды входа",
    "panel_remember_tokens": "сессии «запомнить меня»",
    "seen_users": "запись о посещении",
    "panel_users": "роль в панели",
    "user_consents": "согласие на обработку данных",
}


def count_all(uid: int) -> dict[str, int]:
    """Сколько строк хранится о пользователе — для показа перед удалением."""
    out: dict[str, int] = {}
    with connect() as conn:
        for table, column in _USER_TABLES:
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (uid,)
            ).fetchone()[0]
            if n:
                out[table] = n
    return out


def purge(uid: int) -> dict[str, int]:
    """Удаляет все данные пользователя. Возвращает {таблица: сколько удалено}."""
    removed: dict[str, int] = {}
    with connect() as conn:
        for table, column in _USER_TABLES:
            cur = conn.execute(f"DELETE FROM {table} WHERE {column}=?", (uid,))
            if cur.rowcount:
                removed[table] = cur.rowcount
    return removed


def export_all(uid: int) -> dict:
    """Все данные пользователя в виде {таблица: [{колонка: значение}, ...]}.

    Право на доступ к своим данным (152-ФЗ, ст. 14) — вторая половина права на
    удаление, и список таблиц у них общий: то, что удаляется, должно и
    выгружаться. Расходиться они не могут, потому что оба идут по
    `_USER_TABLES`.

    Отдаём как есть, вместе с названиями колонок: выгрузка должна быть
    проверяемой, а не пересказом.
    """
    out: dict[str, list[dict]] = {}
    with connect() as conn:
        for table, column in _USER_TABLES:
            cur = conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (uid,))
            names = [d[0] for d in cur.description]
            rows = [dict(zip(names, r)) for r in cur.fetchall()]
            if rows:
                out[table] = rows
    return out
