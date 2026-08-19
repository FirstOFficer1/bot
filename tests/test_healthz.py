"""Проверка живости: /healthz и отметки воркеров.

Смысл маршрута — ловить отказ, который systemd не видит: процесс бота жив и
отвечает на сообщения, а фоновый воркер встал, и уведомления молча не приходят.
"""

from __future__ import annotations

import datetime

import pytest

from vkbot import config, db
from vkbot.models import heartbeats

import web_panel


@pytest.fixture
def client():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        yield c


def _mark_all(age_sec: int = 0) -> None:
    """Проставляет отметки всем воркерам, при необходимости — состаренные."""
    ts = (config.now_msk() - datetime.timedelta(seconds=age_sec)).isoformat(
        timespec="seconds"
    )
    with db.connect() as conn:
        for name in heartbeats.ALL:
            conn.execute(
                "INSERT INTO worker_heartbeats (name, last_tick, ticks) VALUES (?,?,1) "
                "ON CONFLICT(name) DO UPDATE SET last_tick=excluded.last_tick",
                (name, ts),
            )


def test_healthz_is_public(client):
    """Монитор дёргает его без авторизации — редиректа на /login быть не должно."""
    assert client.get("/healthz").status_code in (200, 503)


def test_healthz_degraded_without_heartbeats(client):
    resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["status"] == "degraded"
    assert all(w["state"] == "never" for w in body["checks"]["workers"].values())


def test_healthz_ok_when_workers_tick(client):
    _mark_all()
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert body["checks"]["db"] == "ok"
    assert set(body["checks"]["workers"]) == set(heartbeats.ALL)


def test_healthz_catches_stalled_worker(client):
    """Ровно тот случай, ради которого маршрут и нужен: воркер перестал тикать."""
    _mark_all()
    stale = config.REMINDER_POLL_SEC * 3 + 600
    ts = (config.now_msk() - datetime.timedelta(seconds=stale)).isoformat(
        timespec="seconds"
    )
    with db.connect() as conn:
        conn.execute(
            "UPDATE worker_heartbeats SET last_tick=? WHERE name=?",
            (ts, heartbeats.REMINDERS),
        )

    resp = client.get("/healthz")
    assert resp.status_code == 503
    workers = resp.get_json()["checks"]["workers"]
    assert workers[heartbeats.REMINDERS]["state"] == "stale"
    assert workers[heartbeats.CLASSES]["state"] == "ok", "остальные воркеры не при чём"


def test_healthz_leaks_nothing(client):
    """Маршрут открыт наружу: в ответе не должно быть путей, токенов и имён."""
    _mark_all()
    text = client.get("/healthz").get_data(as_text=True)
    for secret in (str(config.NOTES_DB), str(config.DATA_DIR), "token", "vk_id"):
        assert secret not in text


def test_heartbeat_mark_is_idempotent():
    heartbeats.mark(heartbeats.CLASSES)
    heartbeats.mark(heartbeats.CLASSES)
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT ticks FROM worker_heartbeats WHERE name=?", (heartbeats.CLASSES,)
        ).fetchall()
    assert len(rows) == 1, "на воркер должна быть ровно одна строка"
    assert rows[0][0] == 2, "счётчик тиков должен расти"


def test_age_seconds_handles_missing_and_broken():
    assert heartbeats.age_seconds(None) is None
    assert heartbeats.age_seconds("не дата") is None
    assert heartbeats.age_seconds(config.now_msk().isoformat()) < 5
