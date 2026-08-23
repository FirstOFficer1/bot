"""Согласие на обработку персональных данных и выгрузка своих данных.

152-ФЗ требует согласия «конкретного, предметного, информированного,
сознательного и однозначного» (ст. 9) — то есть отдельного действия человека,
а не строчки «продолжая пользоваться, вы соглашаетесь». Отсюда шлюз: пока
галочка не поставлена, панель не показывает ничего, кроме самого согласия,
документов и кнопки «удалить мои данные».

Шлюзов два, потому что поверхностей две: панель и бот. Данные создаются в
основном в чате, так что экран на сайте закрывал бы половину дыры.

Вторая половина требования — право на доступ к своим данным (ст. 14): выгрузка
идёт по тому же списку таблиц, что и удаление, поэтому разойтись они не могут.
"""

from __future__ import annotations

import json

import pytest

import web_panel
from vkbot.models import audit, consents, notes, panel_codes, panel_users, subscriptions

USER = 8801
OTHER = 8802
OWNER = 1001


@pytest.fixture
def client():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        yield c


def _login(client, uid: int):
    code, _ttl = panel_codes.issue(uid)
    return client.post("/login/code", data={"code": code})


def _login_consented(client, uid: int):
    _login(client, uid)
    consents.accept(uid, source="test")


# ── Шлюз ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", "/me", "/schedule"])
def test_without_consent_everything_leads_to_the_form(client, path):
    _login(client, USER)

    resp = client.get(path)

    assert resp.status_code in (302, 303)
    assert "/consent" in resp.headers.get("Location", "")


def test_consent_screen_shows_the_document(client):
    _login(client, USER)

    html = client.get("/consent").get_data(as_text=True)

    assert "Согласие на обработку персональных данных" in html
    assert web_panel.DEVELOPER_NAME in html, "оператор должен быть назван"
    assert 'name="agree"' in html, "нет самой галочки — согласие нечем дать"
    assert consents.VERSION in html, "редакция должна быть видна"


def test_accepting_opens_the_panel(client):
    _login(client, USER)

    resp = client.post("/consent", data={"agree": "yes", "next": "/me"})

    assert resp.status_code in (302, 303)
    assert resp.headers["Location"].endswith("/me")
    assert consents.accepted(USER)
    assert client.get("/me").status_code == 200


def test_unchecked_box_is_not_consent(client):
    _login(client, USER)

    resp = client.post("/consent", data={"next": "/"})

    assert resp.status_code == 400
    assert not consents.accepted(USER), "согласие без галочки записываться не должно"


def test_consent_is_recorded_with_evidence(client):
    _login(client, USER)

    client.post("/consent", data={"agree": "yes"})

    row = consents.get(USER)
    assert row["version"] == consents.VERSION
    assert row["accepted_at"], "без времени согласие ничего не доказывает"
    assert row["ip"], "IP — часть доказательства, что согласие давал этот человек"


def test_consent_is_audited(client):
    _login(client, USER)

    client.post("/consent", data={"agree": "yes"})

    actions = [e["action"] for e in audit.list_recent(limit=10)]
    assert "consent.accept" in actions


def test_new_version_is_asked_again(client, monkeypatch):
    """Меняется текст — согласие спрашивается заново, иначе человек числился бы
    согласившимся с документом, которого не видел."""
    _login_consented(client, USER)
    assert client.get("/me").status_code == 200

    monkeypatch.setattr(consents, "VERSION", "2099-01-01")

    resp = client.get("/me")
    assert resp.status_code in (302, 303)
    assert "/consent" in resp.headers.get("Location", "")


def test_next_cannot_leave_the_site(client):
    """Поле next приходит из формы — открытый редирект здесь был бы подарком."""
    _login(client, USER)

    resp = client.post("/consent", data={"agree": "yes", "next": "https://evil.example"})

    assert "evil.example" not in resp.headers.get("Location", "")


def test_consent_text_is_readable_without_login(client):
    """Бот даёт ссылку на этот текст до того, как человек что-либо о себе
    сообщил. Требовать согласия с документом, который нельзя прочитать, — абсурд."""
    html = client.get("/consent").get_data(as_text=True)

    assert "Согласие на обработку персональных данных" in html
    assert 'name="agree"' not in html, "анониму принимать нечего — формы быть не должно"


@pytest.mark.parametrize("path", ["/privacy", "/terms"])
def test_documents_are_readable_before_consent(client, path):
    """Нельзя требовать согласия с текстом, который нельзя прочитать."""
    _login(client, USER)

    assert client.get(path).status_code == 200


def test_refusing_and_deleting_works_without_consent(client):
    """Отказаться и стереть себя человек должен мочь, ничего не подписывая."""
    notes.add(USER, "конспект")
    _login(client, USER)

    resp = client.post("/me/delete-all", data={"confirm": "yes"})

    assert resp.status_code in (302, 303)
    assert web_panel.user_data.count_all(USER) == {}


def test_consent_screen_initialises_vk_bridge(client):
    """Внутри VK экран согласия может открыться первым — без VKWebAppInit
    пользователь увидит «Приложение не инициализировано» вместо него."""
    _login(client, USER)

    html = client.get("/consent").get_data(as_text=True)

    assert "VKWebAppInit" in html
    assert "env(safe-area-inset" in html


def test_purge_removes_the_consent(client):
    """Отзыв согласия — это удаление данных, включая саму запись о согласии."""
    _login_consented(client, USER)

    client.post("/me/delete-all", data={"confirm": "yes"})

    assert consents.get(USER) is None


# ── Выгрузка ─────────────────────────────────────────────────────────────────

def _export(client) -> dict:
    resp = client.get("/me/export")
    assert resp.status_code == 200, resp.status_code
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    return json.loads(resp.get_data(as_text=True))


def test_export_returns_everything_stored(client):
    notes.add(USER, "конспект по матанализу")
    subscriptions.add(USER, 3, "Информационные системы")
    _login_consented(client, USER)

    data = _export(client)["данные"]

    assert [n["note_text"] for n in data["notes"]] == ["конспект по матанализу"]
    assert data["subscriptions"][0]["direction"] == "Информационные системы"


def test_export_names_the_operator_and_the_consent(client):
    _login_consented(client, USER)

    payload = _export(client)

    assert payload["оператор"] == web_panel.DEVELOPER_NAME
    assert payload["согласие"]["version"] == consents.VERSION


def test_export_carries_column_names(client):
    """Выгрузку должно быть можно проверить, а не принять на слово."""
    notes.add(USER, "текст")
    _login_consented(client, USER)

    row = _export(client)["данные"]["notes"][0]

    assert {"id", "user_id", "note_text", "timestamp"} <= set(row)


def test_export_is_scoped_to_its_owner(client):
    notes.add(USER, "моё")
    notes.add(OTHER, "чужое")
    _login_consented(client, USER)

    dumped = json.dumps(_export(client), ensure_ascii=False)

    assert "моё" in dumped
    assert "чужое" not in dumped


def test_export_requires_login(client):
    resp = client.get("/me/export")

    assert resp.status_code in (302, 303)
    assert "/login" in resp.headers.get("Location", "")


def test_export_is_audited(client):
    _login_consented(client, USER)

    _export(client)

    assert "me.export" in [e["action"] for e in audit.list_recent(limit=10)]


def test_export_and_deletion_cover_the_same_tables():
    """Выгружаем ровно то, что удаляем: оба идут по одному списку таблиц."""
    from vkbot.models import user_data

    panel_users.grant(USER, granted_by=OWNER, name="Тест")
    consents.accept(USER, source="test")
    notes.add(USER, "текст")

    exported = set(user_data.export_all(USER))
    counted = set(user_data.count_all(USER))

    assert exported == counted, f"расхождение: {exported ^ counted}"


def test_profile_offers_the_download(client):
    _login_consented(client, OWNER)

    html = client.get("/me").get_data(as_text=True)

    assert "Скачать мои данные" in html
    assert "/me/export" in html


# ── Бот ──────────────────────────────────────────────────────────────────────
#
# Данные создаются в основном в чате, а не на сайте: заметки, напоминания и
# подписки заводят через бота. Экран в панели закрывал бы половину дыры.

class FakeMessage:
    """Минимальная замена vkbottle Message: копит ответы."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, keyboard=None) -> None:
        self.answers.append(text)


async def _say(text: str, uid: int = USER) -> FakeMessage:
    """Прогоняет сообщение через весь пайплайн, как это делает диспетчер."""
    from vkbot.handlers import _PIPELINE
    from vkbot.state import store

    msg = FakeMessage(text)
    state = store.get(uid)
    for handler in _PIPELINE:
        if await handler(None, msg, state, text, uid):
            break
    return msg


def test_consent_handler_runs_first():
    """Хендлер выше согласия окажется доступен без согласия — это и будет
    ошибкой, поэтому порядок закреплён тестом, а не комментарием."""
    from vkbot.handlers import _PIPELINE
    from vkbot.handlers import consent as consent_handler

    assert _PIPELINE[0] is consent_handler.try_handle


@pytest.mark.asyncio
async def test_bot_asks_before_anything_else():
    msg = await _say("Привет")

    assert not consents.accepted(USER)
    assert "Принимаю" in msg.answers[0]
    assert "заметки" in msg.answers[0].lower(), "человек должен видеть состав данных"


@pytest.mark.asyncio
async def test_bot_accepts_and_lets_through():
    await _say("✅ Принимаю")

    assert consents.accepted(USER)
    row = consents.get(USER)
    assert row["source"] == "bot"

    menu = await _say("Привет")
    assert "формальность" not in menu.answers[0], "второй раз спрашивать не должны"


@pytest.mark.asyncio
async def test_bot_refusal_records_nothing():
    await _say("❌ Не принимаю")

    assert not consents.accepted(USER)


@pytest.mark.asyncio
async def test_bot_can_delete_without_consenting():
    notes.add(USER, "конспект")

    msg = await _say("🗑 Удалить мои данные")

    assert web_panel.user_data.count_all(USER) == {}
    assert "забыл" in msg.answers[0]


@pytest.mark.asyncio
async def test_note_cannot_be_created_before_consent():
    """Главная причина шлюза в боте: заметки заводят здесь, а не на сайте."""
    await _say("📝 Добавить заметку")
    await _say("секретный текст")

    assert web_panel.user_data.count_all(USER).get("notes") is None


@pytest.mark.asyncio
async def test_consent_from_the_panel_opens_the_bot():
    """Хранилище общее: согласие, данное на сайте, второй раз не спрашивают."""
    consents.accept(USER, source="panel")

    msg = await _say("Привет")

    assert "формальность" not in msg.answers[0]


@pytest.mark.asyncio
async def test_bot_shows_the_full_text_link(monkeypatch):
    from vkbot import config
    from vkbot.handlers import consent as consent_handler

    monkeypatch.setattr(config, "PANEL_BASE_URL", "https://elschedule.ru")

    msg = await _say("что-нибудь")

    assert "https://elschedule.ru/consent" in msg.answers[0]
    assert "https://elschedule.ru/privacy" in msg.answers[0]
    assert consent_handler.ACCEPT  # кнопка существует и подписана
