"""Соответствие правилам размещения на платформе VK Mini Apps.

Проверяются требования, которые видны в коде и разметке — те, что модерация
смотрит в первую очередь. Ссылки на пункты правил (редакция от 17.03.2026):
https://dev.vk.com/ru/mini-apps-rules
"""

from __future__ import annotations

import time

import pytest

import web_panel
from vkbot.models import panel_codes


@pytest.fixture
def client():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        code, _ttl = panel_codes.issue(1001)
        c.post("/login/code", data={"code": code})
        yield c


@pytest.fixture
def anon():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        yield c


# ── 1.1.4: документы доступны внутри приложения ──────────────────────────────

def test_legal_documents_are_reachable_from_inside(client):
    """Внутри VK пользователь входит бесшовно и страницу входа не видит —
    значит ссылки обязаны быть в самом приложении."""
    html = client.get("/").get_data(as_text=True)

    assert "/privacy" in html, "нет ссылки на политику конфиденциальности"
    assert "/terms" in html, "нет ссылки на пользовательское соглашение"


@pytest.mark.parametrize("path", ["/privacy", "/terms"])
def test_legal_pages_are_public(anon, path):
    """Документы принимают до начала использования — значит без авторизации."""
    resp = anon.get(path)
    assert resp.status_code == 200


# ── 2.4.1: способ связи с поддержкой ─────────────────────────────────────────

def test_support_contact_is_available(client):
    html = client.get("/").get_data(as_text=True)

    assert "поддержку" in html.lower() or "vk.com/im" in html
    assert web_panel.SUPPORT_URL.startswith("https://vk.com/")


# ── 2.2.1: VK Bridge подключён и инициализирован ─────────────────────────────

def _has_bridge(html: str) -> None:
    assert "vk-bridge" in html, "библиотека VK Bridge не подключена"
    assert "VKWebAppInit" in html, "VK Bridge не инициализируется"


def test_vk_bridge_on_login_page(anon):
    _has_bridge(anon.get("/login").get_data(as_text=True))


def test_vk_bridge_inside_the_app(client):
    _has_bridge(client.get("/").get_data(as_text=True))


def test_csp_allows_vk_bridge(anon):
    """CSP не должна блокировать саму библиотеку — иначе инициализации не будет."""
    csp = anon.get("/login").headers.get("Content-Security-Policy", "")

    assert "unpkg.com" in csp
    assert "frame-ancestors" in csp and "vk.com" in csp


# ── 3.2.2: безопасные зоны экрана ────────────────────────────────────────────

@pytest.mark.parametrize("page", ["app", "login"])
def test_safe_areas_are_respected(client, anon, page):
    html = (client.get("/") if page == "app" else anon.get("/login")).get_data(as_text=True)

    assert "viewport-fit=cover" in html, "без этого системные зоны не отдаются приложению"
    assert "env(safe-area-inset" in html, "контент уедет под вырез и полосу жестов"


# ── 1.2.6: лимиты на частоту запросов ────────────────────────────────────────

def test_rate_limiter_counts_and_cuts_off(monkeypatch):
    monkeypatch.setattr(web_panel, "PANEL_RATE_LIMIT_RPM", 3)
    web_panel._req_hits.clear()

    assert web_panel._too_many_requests("10.0.0.1") is False
    assert web_panel._too_many_requests("10.0.0.1") is False
    assert web_panel._too_many_requests("10.0.0.1") is False
    assert web_panel._too_many_requests("10.0.0.1") is True, "лимит должен сработать"
    assert web_panel._too_many_requests("10.0.0.2") is False, "лимит считается по IP"


def test_rate_limiter_forgets_old_requests(monkeypatch):
    monkeypatch.setattr(web_panel, "PANEL_RATE_LIMIT_RPM", 2)
    web_panel._req_hits.clear()

    web_panel._too_many_requests("10.0.0.3")
    web_panel._too_many_requests("10.0.0.3")
    assert web_panel._too_many_requests("10.0.0.3") is True

    # Сдвигаем отметки на минуту назад — окно должно освободиться.
    web_panel._req_hits["10.0.0.3"] = type(web_panel._req_hits["10.0.0.3"])(
        [t - 61 for t in web_panel._req_hits["10.0.0.3"]]
    )
    assert web_panel._too_many_requests("10.0.0.3") is False


def test_monitoring_is_not_rate_limited(monkeypatch):
    """/healthz дёргает монитор по расписанию — его нельзя запирать лимитом."""
    monkeypatch.setattr(web_panel, "PANEL_RATE_LIMIT_RPM", 1)
    web_panel._req_hits.clear()
    web_panel.app.config.update(TESTING=False)
    try:
        with web_panel.app.test_client() as c:
            for _ in range(5):
                assert c.get("/healthz").status_code in (200, 503)
    finally:
        web_panel.app.config.update(TESTING=True)


# ── 1.2.7: ошибки не раскрывают внутренности ────────────────────────────────

def test_unknown_path_gives_clean_404(anon):
    resp = anon.get("/такой-страницы-нет")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 404
    for leak in ("Traceback", "web_panel.py", "/root/", "C:\\"):
        assert leak not in body, f"в ответе видно {leak!r}"


def test_api_errors_do_not_leak_internals(anon):
    resp = anon.post("/api/schedule/rollback/999999",
                     headers={"Authorization": "Bearer test-api-token"})
    body = resp.get_data(as_text=True)

    assert "Traceback" not in body and "sqlite" not in body.lower()


# ── 1.2.5: на клиенте нет секретов ──────────────────────────────────────────

def test_no_secrets_in_markup(client, anon):
    import os

    secrets_in_env = [
        os.environ.get("PANEL_SECRET", ""),
        os.environ.get("UPLOAD_API_TOKEN", ""),
        os.environ.get("VK_APP_SECRET", ""),
        os.environ.get("VK_TOKEN", ""),
    ]
    pages = [client.get("/").get_data(as_text=True),
             client.get("/me").get_data(as_text=True),
             anon.get("/login").get_data(as_text=True)]

    for value in [s for s in secrets_in_env if s and len(s) > 6]:
        for html in pages:
            assert value not in html, "секрет попал в разметку"


# ── 1.2.2: авторизация по параметрам запуска ────────────────────────────────

def test_launch_params_are_validated(anon):
    """Подпись обязана проверяться — без неё vk_user_id ничего не значит."""
    forged = "vk_app_id=1&vk_user_id=1&vk_ts=%d&sign=forged" % int(time.time())

    resp = anon.get(f"/me?{forged}")

    assert resp.status_code in (302, 303), "подделанная подпись не должна пускать"


# ── 1.1.4: свой документ должен описывать реальные процессы ──────────────────

def test_privacy_names_the_operator(anon):
    """Типовая политика требует, чтобы разработчик был назван в приложении.

    Мы применяем собственный документ (типовая, п. 2.1, на таких не
    распространяется) — значит назвать оператора обязаны сами.
    """
    html = anon.get("/privacy").get_data(as_text=True)

    assert "Кто обрабатывает данные" in html
    assert web_panel.DEVELOPER_NAME in html
    assert "{{" not in html, "шаблонные подстановки должны быть отрендерены"


def test_privacy_retention_matches_the_code(anon):
    """Сроки в документе не должны расходиться с тем, что делает уборка."""
    from vkbot import config

    html = anon.get("/privacy").get_data(as_text=True)

    for value in (config.AUDIT_KEEP_DAYS, config.SENT_NOTIFS_CLEANUP_DAYS,
                  config.DEADLINE_CLEANUP_DAYS):
        assert str(value) in html, f"срок {value} не указан в политике"


def test_privacy_describes_self_service_deletion(anon):
    """Кнопка удаления появилась — обещание в документе должно совпадать."""
    import re

    # В исходнике текста есть переносы строк — сравниваем по смыслу, не побайтно.
    html = re.sub(r"\s+", " ", anon.get("/privacy").get_data(as_text=True))

    assert "Удалить мои данные" in html
    assert "напишите боту" not in html.lower(), "старое обещание про переписку устарело"
