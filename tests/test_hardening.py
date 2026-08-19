"""Мелкие, но кусачие вещи из код-ревью перед запуском.

Каждый тест здесь описывает конкретный способ выстрелить себе в ногу,
который уже случился: заголовки, спорящие друг с другом; заголовок,
которому доверять нельзя; уборка, роняющая тик воркера; диагностика,
уводящая админа не туда.
"""

from __future__ import annotations

import pytest

import web_panel
from vkbot.models import panel_codes


@pytest.fixture
def client():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        yield c


# ── Заголовки безопасности ───────────────────────────────────────────────────

def test_no_xfo_conflict_with_frame_ancestors(client):
    """XFO не умеет списки доменов и блокировал бы iframe, разрешённый CSP.

    Панель встраивается в VK Mini App, и CSP это разрешает. Если рядом стоит
    X-Frame-Options: SAMEORIGIN, браузер, уважающий XFO, фрейм не покажет.
    """
    resp = client.get("/login")
    csp = resp.headers.get("Content-Security-Policy", "")
    xfo = resp.headers.get("X-Frame-Options")

    assert "frame-ancestors" in csp, "CSP должен управлять встраиванием"
    if "vk.com" in csp and web_panel.PANEL_BASE_URL.startswith("https://"):
        assert xfo is None, "XFO спорит с frame-ancestors и ломает Mini App"
    if xfo is not None:
        assert xfo == "DENY", "если XFO ставим, то только как запрет без исключений"


def test_no_xfo_when_embedded_in_vk(client, monkeypatch):
    """Боевая конфигурация: PANEL_BASE_URL на https, панель живёт в Mini App."""
    monkeypatch.setattr(web_panel, "PANEL_BASE_URL", "https://elschedule.ru")

    resp = client.get("/login")

    assert resp.headers.get("X-Frame-Options") is None, (
        "при https XFO не ставим вовсе — иначе он спорит с frame-ancestors"
    )
    assert "vk.com" in resp.headers.get("Content-Security-Policy", "")


def test_proxyfix_does_not_trust_forwarded_host_in_source():
    """Конфигурационное решение, которое легко откатить не глядя.

    Тест на живом приложении бесполезен: ProxyFix подключается только при
    PANEL_TRUSTED_PROXIES > 0, а в тестах прокси нет. Поэтому проверяем сам
    вызов — x_host обязан остаться нулём.
    """
    import pathlib
    import re

    source = pathlib.Path(web_panel.__file__).read_text(encoding="utf-8")
    call = re.search(r"ProxyFix\((.*?)\)", source, re.S)
    assert call, "вызов ProxyFix не найден"
    assert "x_host=0" in call.group(1).replace(" ", ""), (
        "X-Forwarded-Host не выставляется нашим nginx — доверять ему нельзя"
    )


def test_security_headers_still_present(client):
    resp = client.get("/login")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert "Content-Security-Policy" in resp.headers


# ── Доверие к заголовкам прокси ──────────────────────────────────────────────

def test_forwarded_host_is_not_trusted(client):
    """X-Forwarded-Host наш nginx не выставляет и не вырезает — он клиентский."""
    resp = client.get("/login", headers={"X-Forwarded-Host": "evil.example.com"})
    assert resp.status_code == 200
    assert b"evil.example.com" not in resp.data


def test_proxyfix_configured_without_host_trust():
    wsgi = web_panel.app.wsgi_app
    if hasattr(wsgi, "x_host"):
        assert wsgi.x_host == 0, "x_host должен быть 0: заголовок подделывается клиентом"


# ── Уборка не должна ронять тик воркера ──────────────────────────────────────

def test_housekeeping_survives_locked_database(monkeypatch):
    """Панель может держать write-lock; тик дедлайнов обязан пережить это."""
    import sqlite3

    from vkbot.workers import deadlines

    def boom(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(deadlines.panel_codes, "cleanup_old", boom)

    deadlines._housekeeping()  # не должно бросить


def test_housekeeping_still_prunes_audit_when_codes_fail(monkeypatch):
    import sqlite3

    from vkbot.models import audit
    from vkbot.workers import deadlines

    monkeypatch.setattr(
        deadlines.panel_codes, "cleanup_old",
        lambda *_a, **_kw: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    called: list[int] = []
    monkeypatch.setattr(audit, "cleanup_older_than", lambda days: called.append(days) or 0)
    monkeypatch.setattr(deadlines.audit, "cleanup_older_than", lambda days: called.append(days) or 0)

    deadlines._housekeeping()

    assert called, "чистка аудита не должна отменяться из-за ошибки в кодах"


# ── Диагностика запуска ──────────────────────────────────────────────────────

def test_owner_check_distinguishes_db_failure(monkeypatch):
    """Сбой чтения БД раньше сообщался как «владелец не настроен»."""
    monkeypatch.setattr(web_panel, "OWNER_VK_IDS", set())

    import vkbot.models.panel_users as pu

    def boom():
        raise OSError("disk I/O error")

    monkeypatch.setattr(pu, "owner_ids", boom)

    with pytest.raises(SystemExit) as exc:
        web_panel._assert_owner_exists()

    message = str(exc.value)
    assert "OSError" in message or "прочитать" in message
    assert "ADMIN_ID" not in message, "не отправляем админа править .env при сбое БД"


def test_owner_check_reports_missing_owner(monkeypatch):
    monkeypatch.setattr(web_panel, "OWNER_VK_IDS", set())

    import vkbot.models.panel_users as pu

    monkeypatch.setattr(pu, "owner_ids", lambda: [])

    with pytest.raises(SystemExit) as exc:
        web_panel._assert_owner_exists()

    assert "ADMIN_ID" in str(exc.value)


def test_owner_check_passes_with_env_owner():
    """В тестовом окружении ADMIN_ID задан — проверка должна молча проходить."""
    web_panel._assert_owner_exists()


# ── Одноразовый код остаётся одноразовым ─────────────────────────────────────

def test_login_code_single_use_after_changes(client):
    code, _ttl = panel_codes.issue(1001)
    assert client.post("/login/code", data={"code": code}).status_code in (302, 303)
    with web_panel.app.test_client() as c2:
        resp = c2.post("/login/code", data={"code": code})
        assert resp.status_code in (200, 302, 303)
        assert panel_codes.verify(code) is None
