"""Бесшовный вход из VK Mini App по подписанным launch-параметрам.

Требование площадки (правила VK Mini Apps, п. 1.1.2): внутри VK пользователь
авторизуется по `vk_user_id`, просить код или VK ID избыточно. Раньше подпись
только проверялась и писалась в аудит, а доступ всё равно шёл через код.

Тут проверяется главное: подпись пускает внутрь ровно тогда, когда ей можно
верить. Это код аутентификации — каждая дырка в нём становится входом без
пароля, поэтому негативных случаев здесь больше, чем позитивных.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import urlencode

import pytest

import web_panel
from vkbot.models import audit, panel_users

SECRET = "test-app-secret"
APP_ID = 51234567
USER = 555001
ADMIN = 555002


@pytest.fixture(autouse=True)
def _vk_app(monkeypatch):
    monkeypatch.setenv("VK_APP_SECRET", SECRET)
    monkeypatch.setattr(web_panel, "VK_APP_ID", APP_ID)
    web_panel._LAUNCH_SEEN.clear()
    yield
    web_panel._LAUNCH_SEEN.clear()


@pytest.fixture
def client():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        yield c


def launch_params(
    *, uid: int = USER, app_id: int | None = None, ts: int | None = None,
    secret: str = SECRET, extra: dict | None = None,
) -> str:
    """Собирает launch-строку ровно так, как её присылает VK."""
    params = {
        "vk_app_id": str(app_id if app_id is not None else APP_ID),
        "vk_user_id": str(uid),
        "vk_is_app_user": "1",
        "vk_language": "ru",
        "vk_platform": "mobile_android",
        "vk_ts": str(ts if ts is not None else int(time.time())),
    }
    params.update(extra or {})
    vk_params = sorted((k, v) for k, v in params.items() if k.startswith("vk_"))
    query = urlencode(vk_params)
    digest = hmac.new(secret.encode(), query.encode(), hashlib.sha256).digest()
    sign = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return urlencode(params) + "&sign=" + sign


# ── Пускает, когда должен ────────────────────────────────────────────────────

def test_valid_launch_logs_in_without_a_code(client):
    resp = client.get(f"/me?{launch_params()}")

    assert resp.status_code == 200, "подписанный запуск должен открывать профиль сразу"


def test_login_page_redirects_inside_vk(client):
    """Форму с кодом внутри VK показывать нельзя — это и есть п. 1.1.2."""
    resp = client.get(f"/login?{launch_params()}")

    assert resp.status_code in (302, 303)
    assert "/login" not in resp.headers.get("Location", "")


def test_roles_still_apply(client):
    """Бесшовный вход даёт ровно те права, что у этого VK ID в базе."""
    panel_users.grant(ADMIN, granted_by=1001, name="Админ")

    assert client.get(f"/admin/users?{launch_params(uid=ADMIN)}").status_code == 200
    plain = client.get(f"/admin/users?{launch_params(uid=USER)}")
    assert plain.status_code in (302, 303), "обычный пользователь в админку не попадает"


def test_launch_is_audited_once_per_session(client):
    params = launch_params()
    for _ in range(3):
        client.get(f"/me?{params}")

    logins = [e for e in audit.list_recent(limit=50)
              if e["action"] == "auth.login" and "Mini App" in (e["target"] or "")]
    assert len(logins) == 1, "один запуск — одна запись в журнале, а не по одной на запрос"


def test_launch_records_the_platform(client):
    """Без платформы в журнале нельзя отличить «телефон до нас не дошёл» от
    «дошёл, но что-то сломалось у нас»."""
    client.get(f"/me?{launch_params(extra={'vk_platform': 'mobile_iphone'})}")

    logins = [e for e in audit.list_recent(limit=20) if e["action"] == "auth.login"]
    assert logins and "mobile_iphone" in (logins[0]["details"] or "")


def test_works_without_cookies(client):
    """Браузеры режут куки в iframe: подпись должна работать и без сессии."""
    params = launch_params()
    client.get(f"/me?{params}")
    client.delete_cookie("session")

    assert client.get(f"/me?{params}").status_code == 200


# ── Не пускает, когда верить нельзя ──────────────────────────────────────────

def test_broken_signature_is_rejected(client):
    params = launch_params()[:-4] + "xxxx"

    resp = client.get(f"/me?{params}")

    assert resp.status_code in (302, 303), "испорченная подпись не должна пускать"


def test_foreign_app_secret_is_rejected(client):
    resp = client.get(f"/me?{launch_params(secret='someone-elses-secret')}")

    assert resp.status_code in (302, 303)


def test_other_app_id_is_rejected(client):
    """Чужое приложение с валидной для себя подписью не должно открывать нашу панель."""
    resp = client.get(f"/me?{launch_params(app_id=999999)}")

    assert resp.status_code in (302, 303)


def test_stale_launch_is_rejected(client):
    """Старый launch-URL мог утечь через историю браузера или скриншот."""
    old = int(time.time()) - web_panel.VK_LAUNCH_MAX_AGE_SEC - 60

    resp = client.get(f"/me?{launch_params(ts=old)}")

    assert resp.status_code in (302, 303)


def test_launch_from_the_future_is_rejected(client):
    resp = client.get(f"/me?{launch_params(ts=int(time.time()) + 3600)}")

    assert resp.status_code in (302, 303)


def test_tampered_user_id_is_rejected(client):
    """Подмена vk_user_id в подписанной строке ломает подпись — так и должно быть."""
    params = launch_params(uid=USER).replace(f"vk_user_id={USER}", "vk_user_id=1")

    resp = client.get(f"/me?{params}")

    assert resp.status_code in (302, 303)
    assert any(e["action"] == "vk.sign_invalid" for e in audit.list_recent(limit=20))


def test_no_secret_configured_means_no_seamless_login(client, monkeypatch):
    """Без VK_APP_SECRET проверить подпись нечем — пускать нельзя."""
    monkeypatch.setenv("VK_APP_SECRET", "")

    resp = client.get(f"/me?{launch_params()}")

    assert resp.status_code in (302, 303)


def test_plain_visit_without_sign_still_asks_for_code(client):
    """Обычный браузер вне VK — прежний путь со входом по коду."""
    resp = client.get("/me")

    assert resp.status_code in (302, 303)
    assert "/login" in resp.headers.get("Location", "")


# ── Опасные операции остаются за подтверждением ──────────────────────────────

def test_owner_actions_still_need_step_up(client):
    """Бесшовный вход не должен открывать передачу владения без кода из бота."""
    resp = client.post(
        f"/admin/owner/grant?{launch_params(uid=1001)}",
        data={"query": str(USER)},
    )

    assert resp.status_code in (302, 303)
    assert not panel_users.is_owner(USER), "владение без step-up выдаваться не должно"
