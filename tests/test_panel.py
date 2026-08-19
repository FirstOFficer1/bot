"""Веб-панель: авторизация, роли, изоляция данных, защита редиректов и API."""

from __future__ import annotations

from pathlib import Path

import pytest

from vkbot import config
from vkbot.models import audit, panel_codes, panel_users

import tools.make_demo_schedule as demo
import web_panel


@pytest.fixture
def client():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        yield c


def _login(client, vk_id: int):
    """Логин по настоящему одноразовому коду, как это делает бот."""
    code, _ttl = panel_codes.issue(vk_id)
    return client.post("/login/code", data={"code": code, "remember": "1"})


# ── Доступ без логина ────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", "/me", "/schedule", "/upload", "/admin/users"])
def test_anonymous_is_redirected_to_login(client, path):
    resp = client.get(path)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_login_page_is_public(client):
    assert client.get("/login").status_code == 200


@pytest.mark.parametrize("path", ["/privacy", "/terms", "/robots.txt"])
def test_legal_pages_are_public(client, path):
    assert client.get(path).status_code == 200


# ── Вход по одноразовому коду ────────────────────────────────────────────────

def test_login_with_valid_code(client, admin_id):
    resp = _login(client, admin_id)
    assert resp.status_code == 302
    assert client.get("/").status_code == 200


def test_login_with_wrong_code_is_rejected(client):
    resp = client.post("/login/code", data={"code": "000000"})
    assert resp.status_code == 302
    assert "error" in resp.headers["Location"]
    assert client.get("/").status_code == 302


def test_code_is_single_use(client, admin_id):
    code, _ = panel_codes.issue(admin_id)
    assert client.post("/login/code", data={"code": code}).status_code == 302
    client.post("/logout")
    second = client.post("/login/code", data={"code": code})
    assert "error" in second.headers["Location"], "код должен быть одноразовым"


def test_issuing_new_code_invalidates_previous(client, admin_id):
    first, _ = panel_codes.issue(admin_id)
    panel_codes.issue(admin_id)
    resp = client.post("/login/code", data={"code": first})
    assert "error" in resp.headers["Location"]


# ── Роли ─────────────────────────────────────────────────────────────────────

def test_plain_user_cannot_reach_admin_pages(client):
    _login(client, 424242)
    resp = client.get("/admin/users")
    assert resp.status_code == 302
    assert "/me" in resp.headers["Location"]


def test_plain_user_gets_own_profile(client):
    _login(client, 424242)
    assert client.get("/me").status_code == 200


def test_admin_cannot_manage_admins(client, admin_id):
    panel_users.grant(777, granted_by=admin_id, name="Админ")
    _login(client, 777)
    assert client.get("/admin/users").status_code == 200
    assert client.get("/admin/admins").status_code == 403


def test_owner_can_manage_admins(client, admin_id):
    _login(client, admin_id)
    assert client.get("/admin/admins").status_code == 200


# ── Изоляция данных пользователя ─────────────────────────────────────────────

def test_user_cannot_delete_another_users_note(client, admin_id):
    from vkbot.models import notes

    notes.add(4242, "Чужая заметка")
    victim_note_id = notes.list_for(4242)[0][0]

    _login(client, 999)
    client.post("/me/delete", data={"kind": "note", "id": victim_note_id})

    assert len(notes.list_for(4242)) == 1, "заметка чужого пользователя должна остаться"


def test_user_can_delete_own_note(client):
    from vkbot.models import notes

    notes.add(999, "Моя заметка")
    note_id = notes.list_for(999)[0][0]

    _login(client, 999)
    client.post("/me/delete", data={"kind": "note", "id": note_id})

    assert notes.list_for(999) == []


# ── Открытый редирект ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "hostile",
    ["//evil.example", "https://evil.example/x", "http://evil.example"],
)
def test_next_parameter_cannot_leave_the_site(client, admin_id, hostile):
    _login(client, admin_id)
    resp = client.post(
        "/admin/grant", data={"vk_id": "5555", "name": "X", "next": hostile}
    )
    assert resp.status_code == 302
    assert "evil.example" not in resp.headers["Location"]


def test_next_parameter_allows_local_path(client, admin_id):
    _login(client, admin_id)
    resp = client.post(
        "/admin/grant", data={"vk_id": "5556", "name": "X", "next": "/admin/users"}
    )
    assert resp.headers["Location"].startswith("/admin/users")


# ── Logout ───────────────────────────────────────────────────────────────────

def test_logout_requires_post(client, admin_id):
    _login(client, admin_id)
    assert client.get("/logout").status_code == 405


def test_logout_via_post_ends_session(client, admin_id):
    _login(client, admin_id)
    assert client.post("/logout").status_code == 302
    assert client.get("/").status_code == 302


# ── REST API ─────────────────────────────────────────────────────────────────

def test_api_requires_bearer_token(client):
    assert client.get("/api/schedule/versions").status_code == 401
    assert client.post("/api/schedule/commit", json={"token": "x"}).status_code == 401


def test_api_accepts_correct_token(client):
    resp = client.get(
        "/api/schedule/versions", headers={"Authorization": "Bearer test-api-token"}
    )
    assert resp.status_code == 200
    assert isinstance(resp.get_json(), list)


def test_api_rejects_wrong_token(client):
    resp = client.get(
        "/api/schedule/versions", headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401


# ── Заголовки безопасности ───────────────────────────────────────────────────

def test_security_headers_present(client):
    resp = client.get("/login")
    assert "Content-Security-Policy" in resp.headers
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "Referrer-Policy" in resp.headers


def test_upload_size_limit_configured():
    assert web_panel.app.config["MAX_CONTENT_LENGTH"] > 0


# ── API меняет расписание: аудит, ошибки, легаси-база ─────────────────────────
#
# Раньше «закалку» получили только маршруты панели: /api/schedule/* писал
# расписание без единой записи в аудите, не обновлял s.db при откате и отдавал
# наружу текст исключения.

API_AUTH = {"Authorization": "Bearer test-api-token"}


@pytest.fixture
def excel(tmp_path) -> str:
    return demo.build(str(tmp_path / "demo.xlsx"))


def _api_commit(client, excel_path: str):
    with open(excel_path, "rb") as fh:
        up = client.post(
            "/api/schedule/upload",
            headers=API_AUTH,
            data={"file": (fh, "demo.xlsx")},
            content_type="multipart/form-data",
        )
    assert up.status_code == 200, up.get_data(as_text=True)
    token = up.get_json()["token"]
    return client.post("/api/schedule/commit", headers=API_AUTH, json={"token": token})


def test_api_commit_is_recorded_in_audit(client, excel):
    resp = _api_commit(client, excel)
    assert resp.status_code == 200, resp.get_data(as_text=True)

    events = audit.list_recent(action_prefix="schedule.")
    assert [e for e in events if e["action"] == "schedule.upload" and "via=api" in e["details"]]


def test_api_rollback_is_recorded_in_audit(client, excel):
    assert _api_commit(client, excel).status_code == 200
    version_id = web_panel.schedule_loader.list_versions()[0]["id"]

    resp = client.post(f"/api/schedule/rollback/{version_id}", headers=API_AUTH)
    assert resp.status_code == 200, resp.get_data(as_text=True)

    events = audit.list_recent(action_prefix="schedule.rollback")
    assert [e for e in events if e["target"] == f"version={version_id}"]


def test_api_rollback_of_unknown_version_is_404(client):
    resp = client.post("/api/schedule/rollback/999999", headers=API_AUTH)
    assert resp.status_code == 404
    # Наружу — короткое сообщение, без путей и трейсбека.
    assert resp.get_json() == {"error": "version not found"}


def test_legacy_db_follows_data_dir(client, excel):
    """s.db должна лежать рядом с остальными базами, а не в корне проекта."""
    assert web_panel.SCHEDULE_DB_S.startswith(str(config.DATA_DIR))

    assert _api_commit(client, excel).status_code == 200
    assert Path(web_panel.SCHEDULE_DB_S).exists(), "легаси-база не обновилась через API"


def test_panel_rollback_updates_audit_and_legacy_db(client, admin_id, excel):
    """Откат из панели: аудит + s.db берутся из свежей копии Excel."""
    assert _api_commit(client, excel).status_code == 200
    version_id = web_panel.schedule_loader.list_versions()[0]["id"]
    _login(client, admin_id)

    resp = client.post(f"/upload/rollback/{version_id}")
    assert resp.status_code == 302

    events = audit.list_recent(action_prefix="schedule.rollback")
    assert [e for e in events if e["target"] == f"version={version_id}"]
    assert Path(web_panel.SCHEDULE_DB_S).exists()


# ── Разбор env и защита редиректа (находки код-ревью) ────────────────────────

def test_safe_next_rejects_backslash_host():
    """`/\\evil.com` браузер нормализует в `//evil.com` и уходит на чужой домен."""
    hostile = "/" + chr(92) + "evil.com"
    assert web_panel._safe_next(hostile, "/fallback") == "/fallback"
    assert web_panel._safe_next("/schedule?day=1", "/fallback") == "/schedule?day=1"


def test_int_env_survives_blank_values():
    """Заданная, но пустая переменная не должна ронять импорт панели и бота."""
    from vkbot import config

    import os as _os

    _os.environ["PROBE_INT"] = ""
    assert config.int_env("PROBE_INT", 7) == 7
    _os.environ["PROBE_INT"] = "мусор"
    assert config.int_env("PROBE_INT", 7) == 7
    _os.environ["PROBE_INT"] = "42"
    assert config.int_env("PROBE_INT", 7) == 42
    del _os.environ["PROBE_INT"]
