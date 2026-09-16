"""Студенческий shell: нижние табы и лента «Сегодня», админ — сайдбар."""

from __future__ import annotations

import pytest

import web_panel
from vkbot.models import consents, panel_codes, panel_users

STUDENT = 4242
OWNER = 1001  # ADMIN_ID из conftest


@pytest.fixture
def student_client():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        code, _ttl = panel_codes.issue(STUDENT)
        c.post("/login/code", data={"code": code})
        consents.accept(STUDENT, source="test")
        yield c


@pytest.fixture
def owner_client():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        code, _ttl = panel_codes.issue(OWNER)
        c.post("/login/code", data={"code": code})
        consents.accept(OWNER, source="test")
        yield c


def test_student_gets_bottom_tabs_not_sidebar(student_client):
    html = student_client.get("/").get_data(as_text=True)

    assert 'class="app-shell student-shell"' in html
    assert "stu-tabbar" in html
    assert "Сегодня" in html
    assert 'class="sidebar desktop-sb"' not in html
    assert "stu-timeline" in html or "stu-empty" in html


def test_student_tabs_on_profile_and_schedule(student_client):
    me = student_client.get("/me").get_data(as_text=True)
    sched = student_client.get("/schedule").get_data(as_text=True)

    assert "stu-tabbar" in me and "stu-tabbar" in sched
    assert 'class="app-shell student-shell"' in me


def test_owner_keeps_admin_sidebar(owner_client):
    html = owner_client.get("/").get_data(as_text=True)

    assert 'class="app-shell student-shell"' not in html
    assert 'class="sidebar desktop-sb"' in html
    assert "Дашборд" in html
    assert "Строк расписания" in html  # роли наконец доходят до контента


def test_panel_admin_also_keeps_sidebar(student_client):
    """panel_users.admin — тоже сайдбар, не student-shell."""
    panel_users.grant(STUDENT, OWNER, role=panel_users.ROLE_ADMIN)
    html = student_client.get("/").get_data(as_text=True)

    assert 'class="app-shell student-shell"' not in html
    assert 'class="sidebar desktop-sb"' in html


def test_login_has_no_indigo_glow():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        html = c.get("/login").get_data(as_text=True)
    assert "#6366F1" not in html
