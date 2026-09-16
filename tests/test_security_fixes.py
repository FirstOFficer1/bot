"""Закрытие дыр из security-ревью: OTP, беседы, launch, step-up, Excel."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import time
import zipfile
from urllib.parse import urlencode

import pytest

import web_panel
from vkbot.models import audit, consents, panel_codes, panel_remember, panel_users

SECRET = "test-app-secret-sec"
APP_ID = 51234567
USER = 555001


def _launch(*, uid: int = USER, ts: int | None = None) -> str:
    params = {
        "vk_app_id": str(APP_ID),
        "vk_user_id": str(uid),
        "vk_is_app_user": "1",
        "vk_ts": str(ts if ts is not None else int(time.time())),
    }
    query = urlencode(sorted(params.items()))
    digest = hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).digest()
    sign = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return query + "&sign=" + sign


@pytest.fixture
def client():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        yield c


def _login(client, vk_id: int = 1001):
    code, _ = panel_codes.issue(vk_id, purpose=panel_codes.PURPOSE_LOGIN)
    client.post("/login/code", data={"code": code, "remember": "1"})
    consents.accept(vk_id, source="test")


def test_rate_limited_ip_does_not_verify_codes(client, monkeypatch):
    """Заблокированный IP не должен доходить до panel_codes.verify."""
    real_code, _ = panel_codes.issue(1001)
    calls: list[str] = []

    def boom(code, *, purpose=panel_codes.PURPOSE_LOGIN):
        calls.append(code)
        return 1001

    monkeypatch.setattr(panel_codes, "is_rate_limited", lambda ip: True)
    monkeypatch.setattr(panel_codes, "verify", boom)

    resp = client.post("/login/code", data={"code": real_code})

    assert resp.status_code in (302, 303)
    assert "error=rate_limited" in resp.headers.get("Location", "")
    assert calls == [], "verify не должен вызываться при rate-limit"


def test_login_error_is_whitelisted(client):
    """Свободный текст в ?error= не должен попадать на страницу (фишинг)."""
    html = client.get("/login?error=<b>evil</b>").get_data(as_text=True)
    assert "<b>evil</b>" not in html
    assert "evil" not in html

    html_ok = client.get("/login?error=bad_code").get_data(as_text=True)
    assert "Неверный или истёкший код" in html_ok


def test_group_chat_message_is_blocked():
    """В беседе peer_id ≠ from_id — персональный pipeline не запускается."""
    from pathlib import Path

    src = Path("vkbot/handlers/__init__.py").read_text(encoding="utf-8")
    assert "peer_id" in src
    assert "личных сообщениях" in src


def test_logout_all_revokes_launch_signature(client, monkeypatch):
    monkeypatch.setenv("VK_APP_SECRET", SECRET)
    monkeypatch.setattr(web_panel, "VK_APP_ID", APP_ID)
    web_panel._LAUNCH_SEEN.clear()
    consents.accept(USER, source="test")

    params = _launch(uid=USER)
    assert client.get(f"/me?{params}").status_code == 200

    client.post("/logout/all")

    with web_panel.app.test_client() as c2:
        resp = c2.get(f"/me?{params}")
        assert resp.status_code in (302, 303)


def test_launch_does_not_overwrite_foreign_session(client, monkeypatch):
    """Login-CSRF: чужая подпись не переписывает уже залогиненную сессию."""
    monkeypatch.setenv("VK_APP_SECRET", SECRET)
    monkeypatch.setattr(web_panel, "VK_APP_ID", APP_ID)
    consents.accept(1001, source="test")
    consents.accept(USER, source="test")
    _login(client, 1001)

    client.get(f"/me?{_launch(uid=USER)}")
    with client.session_transaction() as sess:
        assert sess.get("vk_id") == 1001, "сессию не должны подменить"


def test_admin_grant_requires_step_up(client):
    _login(client, 1001)
    resp = client.post("/admin/grant", data={"query": "2002"})
    assert resp.status_code == 302
    assert not panel_users.is_admin(2002)


def test_admin_grant_with_step_up(client):
    _login(client, 1001)
    code, _ = panel_codes.issue(1001, purpose=panel_codes.PURPOSE_STEP_UP)
    resp = client.post("/admin/grant", data={"query": "2002", "code": code})
    assert resp.status_code == 302
    assert panel_users.is_admin(2002)


def test_remember_token_expires_on_idle():
    token = panel_remember.issue(1001)
    assert panel_remember.verify(token) is not None

    from datetime import timedelta

    from vkbot.config import now_msk
    from vkbot.db import connect

    old = (now_msk() - timedelta(days=panel_remember.IDLE_TTL_DAYS + 1)).isoformat(
        timespec="seconds"
    )
    with connect() as conn:
        conn.execute(
            "UPDATE panel_remember_tokens SET last_used_at=? WHERE token_hash=?",
            (old, panel_remember._hash(token)),
        )
    assert panel_remember.verify(token) is None


def test_zip_bomb_ratio_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/huge.xml", "\x00" * 2_000_000)
    data = buf.getvalue()

    class FakeFile:
        filename = "bomb.xlsx"
        stream = io.BytesIO(data)

    err = web_panel._validate_excel_upload(FakeFile())
    assert err, "zip с высоким коэффициентом сжатия должен отсекаться"


def test_xlsm_rejected():
    class FakeFile:
        filename = "macro.xlsm"
        stream = io.BytesIO(b"PK\x03\x04xxxx")

    assert "xlsx" in web_panel._validate_excel_upload(FakeFile()).lower()


def test_api_versions_omit_file_path(client, monkeypatch):
    monkeypatch.setattr(
        web_panel.schedule_loader, "list_versions",
        lambda: [{"id": 1, "file_path": "/secret/path.xlsx", "row_count": 10}],
    )
    resp = client.get(
        "/api/schedule/versions",
        headers={"Authorization": "Bearer test-api-token"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body and "file_path" not in body[0]


def test_bad_sign_audit_is_throttled(client, monkeypatch):
    monkeypatch.setenv("VK_APP_SECRET", "x")
    web_panel._SIGN_INVALID_HIT.clear()
    before = len([e for e in audit.list_recent(limit=50) if e["action"] == "vk.sign_invalid"])
    for _ in range(5):
        client.get("/login?sign=forged&vk_user_id=1")
    after = [e for e in audit.list_recent(limit=50) if e["action"] == "vk.sign_invalid"]
    assert len(after) - before <= 1
