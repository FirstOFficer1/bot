"""Применение расписания идёт по одному, а неудавшийся выход не врёт об успехе.

Находки совместного ревью:

* `loader.commit()` — пять шагов (бэкап, копия файла, импорт, запись версии,
  сигнал reload) не были сериализованы. Панель работает в четыре потока, и две
  одновременные загрузки перезаписывали общий `schedule_backup`, лили разные
  файлы в одну таблицу и записывали обе версии. Проявилось бы при откате: он
  поднял бы не то расписание, которое указано в журнале.
* `/logout/all` — отзыв токенов, отзыв сессий и аудит стояли в одном
  `except Exception: pass`. Если падал отзыв сессий, чужие устройства
  оставались залогиненными, а человек видел обычный успешный редирект.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import web_panel
from vkbot.models import consents, panel_codes, panel_remember, panel_sessions
from vkbot.schedule import loader


@pytest.fixture
def client():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        yield c


def _login(client, vk_id: int):
    code, _ttl = panel_codes.issue(vk_id)
    resp = client.post("/login/code", data={"code": code, "remember": "1"})
    consents.accept(vk_id, source="test")
    return resp


# ── Применение расписания сериализовано ──────────────────────────────────────

def test_commits_do_not_overlap(monkeypatch):
    """Второй коммит не должен начаться, пока не закончился первый."""
    overlaps = []
    inside = threading.Event()

    def slow_commit(*_a, **_kw):
        if inside.is_set():
            overlaps.append(1)  # кто-то уже внутри — сериализация не работает
        inside.set()
        threading.Event().wait(0.15)
        inside.clear()
        return {"row_count": 1, "saved_path": "x"}

    monkeypatch.setattr(loader, "_commit_locked", slow_commit)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: loader.commit(f"f{i}", "test", f"f{i}.xlsx"), range(4)))

    assert not overlaps, "два применения расписания шли одновременно"


def test_rollback_goes_through_the_same_lock():
    """Откат и быстрая загрузка обязаны идти тем же путём, что и коммит."""
    import inspect

    assert "_COMMIT_LOCK" in inspect.getsource(loader.commit)
    assert "commit(" in inspect.getsource(loader.rollback)
    assert "commit(" in inspect.getsource(loader.quick_apply)


# ── Неудавшийся выход со всех устройств ──────────────────────────────────────

def test_logout_all_reports_failure_instead_of_pretending(client, admin_id, monkeypatch):
    """Если отзыв сессий упал, человеку нельзя показывать обычный успех."""
    _login(client, admin_id)

    def boom(_uid):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(panel_sessions, "revoke_all", boom)

    resp = client.post("/logout/all")

    assert resp.status_code == 302
    assert "error=" in resp.headers["Location"], "о неудаче не сообщили"


def test_logout_all_still_logs_out_this_device_on_failure(client, admin_id, monkeypatch):
    """Даже при сбое текущее устройство должно выйти, а не остаться внутри."""
    _login(client, admin_id)
    monkeypatch.setattr(
        panel_sessions, "revoke_all", lambda _uid: (_ for _ in ()).throw(RuntimeError())
    )

    client.post("/logout/all")

    assert client.get("/").status_code == 302, "устройство осталось залогиненным"


def test_logout_all_succeeds_quietly_when_everything_works(client, admin_id):
    _login(client, admin_id)

    resp = client.post("/logout/all")

    assert resp.status_code == 302
    assert "error=" not in resp.headers["Location"]


def test_audit_failure_does_not_break_logout(client, admin_id, monkeypatch):
    """Журнал — не причина объявить выход неудавшимся."""
    _login(client, admin_id)
    monkeypatch.setattr(
        web_panel.audit, "log", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError())
    )

    resp = client.post("/logout/all")

    assert "error=" not in resp.headers["Location"]
    assert panel_remember.verify("whatever") is None
