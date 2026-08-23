"""Выбор группы в панели: что обещано в тексте, то и происходит.

Панель писала выбранную группу только в `user_prefs` и при этом обещала
«уведомления в боте». Воркер уведомлений читает `subscriptions` — то есть
напоминания не приходили никому, кто выбрал группу на сайте, а не в боте.

Здесь проверяется, что обе стороны идут вместе: выбрал группу — появились
напоминания, сменил — старые ушли, отключил — не осталось ничего.
"""

from __future__ import annotations

import pytest

import web_panel
from vkbot.models import consents
from vkbot.models import panel_codes, subscriptions

UID = 1001
COURSE, DIRECTION = 3, "Информационные технологии (бизнес-аналитика)"
OTHER_COURSE, OTHER_DIRECTION = 1, "Математика и физика"
LONG_DIRECTION = (
    "Информационные технологии (бизнес-аналитика на базе систем "
    "искусственного интеллекта)"
)


@pytest.fixture
def client():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        code, _ttl = panel_codes.issue(UID)
        c.post("/login/code", data={"code": code})
        consents.accept(UID, source="test")
        yield c


def _subscribe(client, course=COURSE, direction=DIRECTION):
    return client.post(
        "/me/subscribe", data={"course": str(course), "direction": direction}
    )


# ── Обещание из текста выполняется ───────────────────────────────────────────

def test_choosing_group_enables_bot_reminders(client):
    _subscribe(client)

    assert subscriptions.exists(UID, COURSE, DIRECTION), (
        "выбор группы обязан включать напоминания бота — это обещано в интерфейсе"
    )
    assert web_panel._get_pref(UID) == (COURSE, DIRECTION)


def test_choosing_group_twice_does_not_duplicate(client):
    _subscribe(client)
    _subscribe(client)

    assert len(subscriptions.list_for(UID)) == 1, "повторный выбор не должен дублировать"


def test_switching_group_moves_reminders(client):
    _subscribe(client)
    _subscribe(client, OTHER_COURSE, OTHER_DIRECTION)

    assert subscriptions.exists(UID, OTHER_COURSE, OTHER_DIRECTION)
    assert not subscriptions.exists(UID, COURSE, DIRECTION), (
        "после смены группы напоминания по старой приходить не должны"
    )


def test_switching_keeps_subscriptions_made_in_the_bot(client):
    """Подписки, заведённые в боте на другие группы, панель не трогает."""
    subscriptions.add(UID, 5, "Физика и информатика")
    _subscribe(client)
    _subscribe(client, OTHER_COURSE, OTHER_DIRECTION)

    assert subscriptions.exists(UID, 5, "Физика и информатика")


def test_turning_off_removes_reminders(client):
    _subscribe(client)

    client.post("/me/unsubscribe")

    assert web_panel._get_pref(UID) is None
    assert not subscriptions.exists(UID, COURSE, DIRECTION)
    assert subscriptions.list_for(UID) == []


def test_removing_reminder_in_profile_clears_the_group(client):
    """Крестик в профиле снимает и отметку группы — иначе дашборд врёт."""
    _subscribe(client)
    sub_id = subscriptions.list_for(UID)[0][0]

    client.post("/me/delete", data={"kind": "subscription", "id": str(sub_id)})

    assert subscriptions.list_for(UID) == []
    assert web_panel._get_pref(UID) is None


# ── Тексты объясняют, что произойдёт ─────────────────────────────────────────

def test_dashboard_explains_what_the_choice_changes(client):
    html = client.get("/").get_data(as_text=True)

    assert "напоминание" in html.lower(), "текст должен упоминать напоминания"
    assert str(web_panel._bot_config.CLASS_NOTIFY_BEFORE_MIN) in html, (
        "срок напоминания берётся из конфига, а не пишется руками"
    )
    assert "Отключить можно в любой момент" in html


def test_profile_explains_reminders(client):
    _subscribe(client)

    html = client.get("/me").get_data(as_text=True)

    assert "Напоминания о парах" in html
    assert "напоминание за" in html


# ── Длинное название группы не ломает вёрстку ────────────────────────────────

def test_long_direction_renders_in_a_wrapping_chip(client):
    _subscribe(client, 4, LONG_DIRECTION)

    html = client.get("/me").get_data(as_text=True)

    assert "sub-chip" in html, "плашка подписки должна быть переносимой"
    assert "badge bg-primary" not in html, (
        "bootstrap-badge не переносит текст — на телефоне он уезжал за экран"
    )
    assert LONG_DIRECTION in html
