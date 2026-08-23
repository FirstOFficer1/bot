"""Логотип и фирменная палитра.

Логотип живёт файлами в `static/`, а не инлайном в разметке: 26 КБ на каждой
странице ради картинки, которая прекрасно кэшируется, — плохой обмен. Отсюда
и проверки: файлы на месте, отдаются, и на них действительно ссылаются.
"""

from __future__ import annotations

import pathlib
import re

import pytest

import web_panel
from vkbot.models import consents

PROJECT = pathlib.Path(__file__).resolve().parent.parent
STATIC = PROJECT / "static"

# Фирменный цвет ЧГПУ им. И. Я. Яковлева из официального логотипа.
BRAND = "#C21E41"


@pytest.fixture
def client():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        yield c


@pytest.mark.parametrize("name", ["logo-mark.svg", "logo-full.svg"])
def test_logo_files_exist_and_are_svg(name):
    path = STATIC / name
    assert path.exists(), f"нет файла {name}"
    text = path.read_text(encoding="utf-8")
    assert text.lstrip().startswith("<!--") or text.lstrip().startswith("<svg")
    assert "<svg" in text and "viewBox" in text
    assert BRAND in text, "логотип должен быть в фирменном цвете"


@pytest.mark.parametrize("name", ["logo-mark.svg", "logo-full.svg"])
def test_logo_is_served(client, name):
    resp = client.get(f"/static/{name}")
    try:
        assert resp.status_code == 200
        assert "svg" in resp.headers.get("Content-Type", "")
        assert len(resp.get_data()) > 1000, "файл отдался пустым"
    finally:
        # Статику Flask отдаёт файловой обёрткой: без close() дескриптор
        # остаётся открытым, и строгий фильтр ResourceWarning валит тест.
        resp.close()


def test_login_page_shows_logo_and_favicon(client):
    html = client.get("/login").get_data(as_text=True)
    assert "/static/logo-full.svg" in html, "на входе — полный логотип"
    assert 'rel="icon"' in html and "/static/logo-mark.svg" in html


def test_sidebar_shows_logo_mark(client):
    code, _ttl = web_panel.panel_codes.issue(1001)
    client.post("/login/code", data={"code": code})
    consents.accept(1001, source="test")

    html = client.get("/").get_data(as_text=True)

    assert "/static/logo-mark.svg" in html, "в сайдбаре — знак"
    assert ">Р</div>" not in html, "буква-заглушка должна была уйти"


def test_accent_is_brand_colour_everywhere(client):
    """Акцент задаётся в трёх шаблонах; расходиться они не должны."""
    source = pathlib.Path(web_panel.__file__).read_text(encoding="utf-8")
    accents = set(re.findall(r"--accent:\s*(#[0-9A-Fa-f]{6})", source))

    assert accents == {BRAND}, f"неожиданные значения акцента: {accents}"


def test_theme_colour_matches_brand():
    source = pathlib.Path(web_panel.__file__).read_text(encoding="utf-8")
    colours = set(re.findall(r'name="theme-color" content="(#[0-9A-Fa-f]{6})"', source))

    assert colours == {BRAND}, f"theme-color разошёлся с палитрой: {colours}"


def test_owner_badge_is_not_accent_coloured():
    """Роль владельца не должна сливаться с фирменным красным."""
    source = pathlib.Path(web_panel.__file__).read_text(encoding="utf-8")
    rule = re.search(r"\.role-pill\.owner\s*\{([^}]*)\}", source)

    assert rule, "правило для бейджа владельца не найдено"
    assert BRAND not in rule.group(1)
    assert "#E11D48" not in rule.group(1), "прежний красный возвращать не стоит"
