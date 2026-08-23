"""Удаление всех своих данных из профиля.

Политика конфиденциальности обещает полное удаление по запросу — раньше это
была переписка с администратором. Тест держит обещание честным: если появится
новая таблица с данными пользователя, а в `user_data._USER_TABLES` её не
добавят, тест это поймает.
"""

from __future__ import annotations

import pytest

import web_panel
from vkbot.models import consents
from vkbot import db
from vkbot.models import audit, deadlines, notes, panel_codes, reminders
from vkbot.models import subscriptions, user_data

USER = 4242
OWNER = 1001


@pytest.fixture
def client():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        yield c


def _login(client, uid: int):
    code, _ttl = panel_codes.issue(uid)
    resp = client.post("/login/code", data={"code": code})
    # Согласие — обязательный шлюз (152-ФЗ, ст. 9): без него панель уводит
    # на /consent. Сам экран проверяется в tests/test_consent.py.
    consents.accept(uid, source="test")
    return resp


def _fill_data(uid: int) -> None:
    notes.add(uid, "конспект по матанализу")
    reminders.add(uid, "сдать лабу", "2026-09-01 10:00")
    deadlines.add(uid, "Курсовая", "черновик", "2026-12-01 23:59")
    subscriptions.add(uid, 3, "Информационные системы и Web-приложения")
    with db.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_prefs (user_id, course, direction) VALUES (?,?,?)",
            (uid, 3, "Информационные системы и Web-приложения"),
        )


def _rows_left(uid: int) -> dict[str, int]:
    return user_data.count_all(uid)


# ── Само удаление ────────────────────────────────────────────────────────────

def test_delete_all_removes_every_trace(client):
    _fill_data(USER)
    _login(client, USER)
    assert _rows_left(USER), "перед удалением данные должны быть"

    resp = client.post("/me/delete-all", data={"confirm": "yes"})

    assert resp.status_code in (302, 303)
    assert _rows_left(USER) == {}, f"осталось: {_rows_left(USER)}"


def test_delete_all_ends_the_session(client):
    _fill_data(USER)
    _login(client, USER)

    client.post("/me/delete-all", data={"confirm": "yes"})

    with client.session_transaction() as sess:
        assert sess.get("vk_id") is None, "сессия должна погаснуть вместе с данными"
    assert "/login" in client.get("/me").headers.get("Location", "")


def test_delete_all_requires_confirmation(client):
    _fill_data(USER)
    _login(client, USER)

    client.post("/me/delete-all", data={})

    assert _rows_left(USER), "без галочки подтверждения ничего удаляться не должно"


def test_delete_all_requires_login(client):
    _fill_data(USER)

    resp = client.post("/me/delete-all", data={"confirm": "yes"})

    assert resp.status_code in (302, 303)
    assert _rows_left(USER), "аноним не может удалить чужие данные"


def test_delete_all_touches_only_its_owner(client):
    _fill_data(USER)
    _fill_data(7777)
    _login(client, USER)

    client.post("/me/delete-all", data={"confirm": "yes"})

    assert _rows_left(USER) == {}
    assert _rows_left(7777), "данные другого пользователя трогать нельзя"


def test_deletion_is_recorded_in_audit(client):
    _fill_data(USER)
    _login(client, USER)

    client.post("/me/delete-all", data={"confirm": "yes"})

    actions = [e["action"] for e in audit.list_recent(limit=20)]
    assert "me.delete_all" in actions, "факт удаления обязан остаться в журнале"


# ── Обещание не должно устареть ──────────────────────────────────────────────

def test_every_user_table_is_covered():
    """Новая таблица с данными пользователя должна попасть в purge()."""
    known = {table for table, _col in user_data._USER_TABLES}
    # Таблицы без данных конкретного человека — их отсутствие в списке осознанно.
    not_personal = {
        "schedule_versions", "sqlite_sequence", "worker_heartbeats",
        "audit_log",  # журнал безопасности, см. модуль user_data
    }

    with db.connect() as conn:
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        personal = set()
        for table in tables - not_personal:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if cols & {"user_id", "vk_id"}:
                personal.add(table)

    assert personal <= known, f"не удаляются: {personal - known}"


def test_labels_cover_every_table():
    """У каждой таблицы есть человеческое название — его показывают в профиле."""
    for table, _col in user_data._USER_TABLES:
        assert table in user_data.LABELS, f"нет подписи для {table}"


def test_profile_shows_the_delete_block(client):
    _fill_data(OWNER)
    _login(client, OWNER)

    html = client.get("/me").get_data(as_text=True)

    assert "Удалить мои данные" in html
    assert "/me/delete-all" in html
    assert "заметки" in html, "перед удалением видно, что именно хранится"
