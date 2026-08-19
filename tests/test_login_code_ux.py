"""Путь входа глазами пользователя: код из бота — в поле на сайте.

Живая жалоба: «бот не распознаёт код для входа в панель». Оказалось, код
присылают обратно в чат — он приходит отдельным сообщением, и это выглядит
как следующий шаг диалога, — а бот отвечал «Не понимаю эту команду».
Вторая половина той же проблемы: при копировании из VK к коду прилипают
пробелы, и панель отвечала «неверный код» на визуально верные шесть цифр.
"""

from __future__ import annotations

import pytest

import web_panel
from vkbot.handlers import _PIPELINE, panel_login
from vkbot.models import panel_codes
from vkbot.state import store

UID = 555


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def answer(self, text, **_kw):
        self.replies.append(text)
        return True


async def _dispatch(text: str, uid: int = UID) -> tuple[str | None, list[str]]:
    """Прогоняет сообщение по настоящему пайплайну хендлеров."""
    message = FakeMessage()
    state = store.get(uid)
    for handler in _PIPELINE:
        if await handler(None, message, state, text, uid):
            return handler.__name__, message.replies
    return None, message.replies


# ── Бот подсказывает, куда вводить код ───────────────────────────────────────

@pytest.mark.asyncio
async def test_code_sent_to_chat_gets_a_hint():
    code, _ttl = panel_codes.issue(UID)

    handler, replies = await _dispatch(code)

    assert handler == "try_code_hint", "код в чате не должен доходить до fallback"
    assert replies, "бот обязан ответить"
    assert "/login" in replies[0], "в подсказке должна быть ссылка на панель"
    assert "Не понимаю" not in replies[0]


@pytest.mark.asyncio
async def test_hint_works_for_expired_code_too():
    """Код мог протухнуть, пока пользователь искал, куда его вводить."""
    code, _ttl = panel_codes.issue(UID)
    panel_codes.verify(code)  # погасили — теперь он «использован»

    handler, replies = await _dispatch(code)

    assert handler == "try_code_hint"
    assert "новый" in replies[0].lower(), "стоит подсказать, как получить новый код"


@pytest.mark.asyncio
async def test_random_six_digits_without_request_go_to_fallback():
    """Тем, кто код не запрашивал, подсказка не нужна — это обычное число."""
    handler, _replies = await _dispatch("123456", uid=999)

    assert handler == "fallback"


@pytest.mark.asyncio
async def test_hint_does_not_steal_input_from_dialogs():
    """Цифры, которых ждёт активный диалог, перехватывать нельзя."""
    from vkbot.models import notes

    panel_codes.issue(UID)
    notes.add(UID, "первая заметка")
    store.patch(UID, action="delete_note")   # диалог ждёт номер заметки

    handler, _replies = await _dispatch("1")

    assert handler != "try_code_hint"
    store.pop(UID)


# ── Панель принимает код так, как его скопировали ────────────────────────────

@pytest.fixture
def client():
    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        yield c


@pytest.mark.parametrize(
    "decorate",
    [
        pytest.param(lambda c: c, id="как есть"),
        pytest.param(lambda c: f" {c} ", id="с пробелами по краям"),
        pytest.param(lambda c: f"{c[:3]} {c[3:]}", id="с пробелом внутри"),
        pytest.param(lambda c: f"{c}\n", id="с переводом строки"),
        pytest.param(lambda c: f"\xa0{c}", id="с неразрывным пробелом"),
        pytest.param(lambda c: f"{c}​", id="с невидимым символом"),
        pytest.param(lambda c: f"Код: {c}", id="вместе с подписью"),
    ],
)
def test_pasted_code_is_accepted(client, decorate):
    code, _ttl = panel_codes.issue(1001)

    resp = client.post("/login/code", data={"code": decorate(code)})

    assert resp.status_code in (302, 303), "код должен приниматься как скопирован"
    with client.session_transaction() as sess:
        assert sess.get("vk_id") == 1001


def test_normalization_does_not_accept_wrong_code(client):
    panel_codes.issue(1001)

    resp = client.post("/login/code", data={"code": "000 000"})

    assert resp.status_code == 200 or "login" in resp.headers.get("Location", "")
    with client.session_transaction() as sess:
        assert sess.get("vk_id") is None


def test_normalization_does_not_glue_extra_digits(client):
    """«12345678» — это не шестизначный код, а другое число."""
    code, _ttl = panel_codes.issue(1001)

    resp = client.post("/login/code", data={"code": code + "99"})

    with client.session_transaction() as sess:
        assert sess.get("vk_id") is None, "лишние цифры не должны отбрасываться"
    assert resp.status_code in (200, 302, 303)
