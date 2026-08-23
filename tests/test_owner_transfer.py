"""Передача владения панелью.

Модель: владельцы из env несменяемы (якорь на сервере), владельцы из
panel_users выдаются и снимаются в интерфейсе — но только с подтверждением
одноразовым кодом из бота. Смысл в том, что угнанная сессия не может
разжаловать владельца из .env: максимум добавит совладельца, и это видно
в аудите.
"""

from __future__ import annotations

import pytest

from vkbot.models import audit, panel_codes, panel_users

import web_panel
from vkbot.models import consents

OWNER = 1001          # он же ADMIN_ID из conftest — владелец из env
SUCCESSOR = 2002
STRANGER = 3003


@pytest.fixture
def client():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        yield c


def _login(client, vk_id: int):
    code, _ttl = panel_codes.issue(vk_id)
    resp = client.post("/login/code", data={"code": code, "remember": "1"})
    # Согласие — обязательный шлюз (152-ФЗ, ст. 9): без него панель уводит
    # на /consent. Сам экран проверяется в tests/test_consent.py.
    consents.accept(vk_id, source="test")
    return resp


def _fresh_code(vk_id: int) -> str:
    code, _ttl = panel_codes.issue(vk_id)
    return code


def _grant_owner(client, target: int = SUCCESSOR, code: str | None = None):
    return client.post(
        "/admin/owner/grant",
        data={"query": str(target), "code": code if code is not None else _fresh_code(OWNER)},
    )


# ── Выдача владения ──────────────────────────────────────────────────────────

def test_owner_can_transfer_ownership(client):
    _login(client, OWNER)
    resp = _grant_owner(client)
    assert resp.status_code == 302

    assert panel_users.is_owner(SUCCESSOR)
    assert web_panel._is_owner(SUCCESSOR)
    assert [e for e in audit.list_recent(action_prefix="admin.owner_grant")]


def test_new_owner_can_manage_admins(client):
    """Смысл передачи: преемник действительно получает права владельца."""
    _login(client, OWNER)
    _grant_owner(client)

    with web_panel.app.test_client() as c2:
        _login(c2, SUCCESSOR)
        assert c2.get("/admin/admins").status_code == 200


def test_transfer_requires_code(client):
    _login(client, OWNER)
    resp = client.post("/admin/owner/grant", data={"query": str(SUCCESSOR)})
    assert resp.status_code == 302
    assert not panel_users.is_owner(SUCCESSOR), "без кода владение выдаваться не должно"


def test_transfer_rejects_wrong_code(client):
    _login(client, OWNER)
    _grant_owner(client, code="000000")
    assert not panel_users.is_owner(SUCCESSOR)


def test_transfer_rejects_code_of_another_account(client):
    """Код чужого аккаунта не подходит — иначе step-up обходится."""
    _login(client, OWNER)
    _grant_owner(client, code=_fresh_code(STRANGER))
    assert not panel_users.is_owner(SUCCESSOR)
    assert [e for e in audit.list_recent(action_prefix="auth.step_up_foreign_code")]


def test_code_is_single_use_for_step_up(client):
    """Один код — одна операция: повтор тем же кодом не проходит."""
    _login(client, OWNER)
    code = _fresh_code(OWNER)
    _grant_owner(client, code=code)
    panel_users.set_role(SUCCESSOR, panel_users.ROLE_ADMIN)

    _grant_owner(client, target=STRANGER, code=code)
    assert not panel_users.is_owner(STRANGER)


def test_admin_cannot_transfer_ownership(client):
    panel_users.grant(SUCCESSOR, granted_by=OWNER, name="Админ")
    with web_panel.app.test_client() as c2:
        _login(c2, SUCCESSOR)
        resp = c2.post(
            "/admin/owner/grant",
            data={"query": str(STRANGER), "code": _fresh_code(SUCCESSOR)},
        )
    assert resp.status_code == 403
    assert not panel_users.is_owner(STRANGER)


# ── Снятие владения ──────────────────────────────────────────────────────────

def test_env_owner_cannot_be_revoked(client):
    """Главная гарантия: через веб владельца из .env снять нельзя."""
    _login(client, OWNER)
    resp = client.post(
        "/admin/owner/revoke", data={"vk_id": str(OWNER), "code": _fresh_code(OWNER)}
    )
    assert resp.status_code == 302
    assert web_panel._is_owner(OWNER)


def test_revoke_demotes_to_admin(client):
    _login(client, OWNER)
    _grant_owner(client)

    resp = client.post(
        "/admin/owner/revoke",
        data={"vk_id": str(SUCCESSOR), "code": _fresh_code(OWNER)},
    )
    assert resp.status_code == 302
    assert not panel_users.is_owner(SUCCESSOR)
    assert panel_users.is_admin(SUCCESSOR), "снятие владения не должно отбирать админку"


def test_revoke_requires_code(client):
    _login(client, OWNER)
    _grant_owner(client)
    client.post("/admin/owner/revoke", data={"vk_id": str(SUCCESSOR)})
    assert panel_users.is_owner(SUCCESSOR)


def test_last_owner_cannot_be_revoked(monkeypatch, client):
    """Панель не должна остаться без владельца — иначе управлять ей некому."""
    _login(client, OWNER)
    _grant_owner(client)
    # Имитируем отсутствие env-владельцев: остаётся ровно один — из БД.
    monkeypatch.setattr(web_panel, "OWNER_VK_IDS", set())

    client.post(
        "/admin/owner/revoke",
        data={"vk_id": str(SUCCESSOR), "code": _fresh_code(SUCCESSOR)},
    )
    assert panel_users.is_owner(SUCCESSOR)


# ── Взаимодействие с обычной админкой ────────────────────────────────────────

def test_admin_revoke_does_not_touch_owner(client):
    _login(client, OWNER)
    _grant_owner(client)
    client.post("/admin/revoke", data={"vk_id": str(SUCCESSOR)})
    assert panel_users.is_owner(SUCCESSOR), "владельца нельзя снять маршрутом админов"


def test_admins_page_separates_owner_sources(client):
    _login(client, OWNER)
    _grant_owner(client)
    html = client.get("/admin/admins").get_data(as_text=True)
    assert "из .env" in html
    assert "выдан в панели" in html


def test_typo_in_recipient_does_not_burn_the_code(client):
    """Код одноразовый: опечатка в имени не должна его тратить."""
    _login(client, OWNER)
    code = _fresh_code(OWNER)

    client.post("/admin/owner/grant", data={"query": "нет такого имени", "code": code})
    # Тот же код обязан сработать со второй, уже корректной попытки.
    client.post("/admin/owner/grant", data={"query": str(SUCCESSOR), "code": code})
    assert panel_users.is_owner(SUCCESSOR)
