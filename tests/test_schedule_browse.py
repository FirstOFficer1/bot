"""Просмотр чужого расписания и отписка кнопкой.

Две жалобы из чата. Первая: посмотреть расписание другой группы было нельзя,
не потеряв свою — любой просмотр через длинный путь звал `user_prefs.set()`.
Цена ошибки выше, чем кажется: панель при следующей смене группы снимает
подписку на «предыдущую», то есть на ту, которую человек никогда не выбирал.
Вторая: отписаться можно было только введя номер текстом — кнопки не было,
и функция считалась отсутствующей.
"""

from __future__ import annotations

import json

import pytest

from vkbot import config, db
from vkbot.handlers import schedule as sched_handler
from vkbot.handlers import subscriptions as subs_handler
from vkbot.models import subscriptions as subs_model
from vkbot.models import user_prefs
from vkbot.schedule import repo
from vkbot.state import store

UID = 7007

COURSE_1 = (1, "Прикладная информатика")
COURSE_2 = (2, "Юриспруденция")
WEEK_DAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота")


class FakeMessage:
    """Минимальная замена vkbottle Message: копит ответы и клавиатуры."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[str] = []
        self.keyboards: list[str | None] = []

    async def answer(self, text: str, keyboard=None) -> None:
        self.answers.append(text)
        self.keyboards.append(keyboard)


@pytest.fixture(autouse=True)
def _schedule_rows():
    with db.connect(config.SCHEDULE_DB) as conn:
        for course, direction in (COURSE_1, COURSE_2):
            for day in WEEK_DAYS:
                conn.execute(
                    "INSERT INTO schedule (course, direction, day, time, subject, week) "
                    "VALUES (?,?,?,?,?,'')",
                    (course, direction, day, "08:00", f"{direction}, {day}"),
                )
    repo.reload()
    yield
    with db.connect(config.SCHEDULE_DB) as conn:
        conn.execute("DELETE FROM schedule")
    repo.reload()
    store.pop(UID, None)


async def _feed(handler, text: str) -> FakeMessage:
    m = FakeMessage(text)
    await handler(None, m, store.get(UID), m.text, UID)
    return m


def _label(course: int) -> str:
    """Подпись кнопки направления — её же присылает VK при нажатии."""
    return next(iter(repo.label_to_full(course)))


# ── Просмотр чужого расписания ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_browsing_other_group_keeps_saved_choice():
    user_prefs.set(UID, *COURSE_1)

    await _feed(sched_handler.try_handle, "📅 Расписание")
    await _feed(sched_handler.try_handle, "👀 Другое расписание")
    await _feed(sched_handler.try_handle, "2")
    await _feed(sched_handler.try_handle, _label(2))
    m = await _feed(sched_handler.try_handle, "Вторник")

    assert "Юриспруденция" in m.answers[-1], "показали не то расписание"
    assert tuple(user_prefs.get(UID)) == COURSE_1, "просмотр перезаписал выбор"


@pytest.mark.asyncio
async def test_browsing_lets_you_look_at_another_day():
    """После просмотра остаёмся в режиме просмотра, а не вываливаемся в меню."""
    user_prefs.set(UID, *COURSE_1)

    await _feed(sched_handler.try_handle, "📅 Расписание")
    await _feed(sched_handler.try_handle, "👀 Другое расписание")
    await _feed(sched_handler.try_handle, "2")
    await _feed(sched_handler.try_handle, _label(2))
    await _feed(sched_handler.try_handle, "Вторник")
    m = await _feed(sched_handler.try_handle, "Среда")

    assert "Среда" in m.answers[-1]
    assert tuple(user_prefs.get(UID)) == COURSE_1


@pytest.mark.asyncio
async def test_switching_group_still_updates_choice():
    """Явная смена группы обязана менять выбор — её мы ломать не собирались."""
    user_prefs.set(UID, *COURSE_1)

    await _feed(sched_handler.try_handle, "📅 Расписание")
    await _feed(sched_handler.try_handle, "🔄 Сменить курс/направление")
    await _feed(sched_handler.try_handle, "2")
    await _feed(sched_handler.try_handle, _label(2))
    await _feed(sched_handler.try_handle, "Вторник")

    assert tuple(user_prefs.get(UID)) == COURSE_2


# ── Вся неделя ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_week_view_covers_every_day():
    user_prefs.set(UID, *COURSE_1)

    await _feed(sched_handler.try_handle, "📅 Расписание")
    m = await _feed(sched_handler.try_handle, "📖 Вся неделя")

    whole = "\n".join(m.answers)
    for day in WEEK_DAYS:
        assert day in whole, f"в выдаче нет дня: {day}"


@pytest.mark.asyncio
async def test_week_view_in_browse_mode_keeps_saved_choice():
    user_prefs.set(UID, *COURSE_1)

    await _feed(sched_handler.try_handle, "📅 Расписание")
    await _feed(sched_handler.try_handle, "👀 Другое расписание")
    await _feed(sched_handler.try_handle, "2")
    await _feed(sched_handler.try_handle, _label(2))
    m = await _feed(sched_handler.try_handle, "📖 Вся неделя")

    assert "Юриспруденция" in "\n".join(m.answers)
    assert tuple(user_prefs.get(UID)) == COURSE_1


def test_long_week_is_split_under_the_message_limit(monkeypatch):
    """Длинная неделя режется по дням, а не обрезается молча на лимите VK."""
    monkeypatch.setattr(sched_handler.repo, "get_day", lambda *_a, **_kw: "x" * 1000)

    chunks = sched_handler._week_chunks(1, COURSE_1[1], "чёт")

    assert len(chunks) > 1, "шесть длинных дней обязаны разъехаться по сообщениям"
    assert all(len(c) <= sched_handler._CHUNK_LIMIT for c in chunks)


# ── Отписка кнопкой ──────────────────────────────────────────────────────────

def test_subscriptions_menu_has_a_button_per_subscription():
    subs_model.add(UID, *COURSE_1)
    subs_model.add(UID, *COURSE_2)

    kb = json.loads(subs_handler._menu_kb(subs_model.list_for(UID)))
    labels = [b["action"]["label"] for row in kb["buttons"] for b in row]

    assert sum(lbl.startswith("❌") for lbl in labels) == 2, "кнопок отписки нет"
    assert any(lbl.startswith("➕") for lbl in labels)


@pytest.mark.asyncio
async def test_unsubscribe_button_removes_only_that_subscription():
    subs_model.add(UID, *COURSE_1)
    subs_model.add(UID, *COURSE_2)

    await _feed(subs_handler.try_handle, "🔔 Подписки на пары")
    await _feed(subs_handler.try_handle, "❌ 1. 1 курс — Прикладная информатика")

    left = [(c, d) for _sid, c, d in subs_model.list_for(UID)]
    assert left == [COURSE_2]


@pytest.mark.asyncio
async def test_unsubscribe_button_survives_label_truncation():
    """`build` режет подпись до 40 символов, поэтому опознаём нажатие по номеру."""
    subs_model.add(UID, 1, "Прикладная информатика")

    await _feed(subs_handler.try_handle, "🔔 Подписки на пары")
    await _feed(subs_handler.try_handle, "❌ 1. 1 курс — Прикладная инфор")

    assert subs_model.list_for(UID) == []


@pytest.mark.asyncio
async def test_typing_the_number_still_works():
    """Старый способ остаётся: кто привык вводить цифру — не сломается."""
    subs_model.add(UID, *COURSE_1)

    await _feed(subs_handler.try_handle, "🔔 Подписки на пары")
    await _feed(subs_handler.try_handle, "1")

    assert subs_model.list_for(UID) == []


@pytest.mark.asyncio
async def test_menu_stays_open_after_unsubscribing():
    """Отписка редко бывает одиночной — список обновляется на месте."""
    subs_model.add(UID, *COURSE_1)
    subs_model.add(UID, *COURSE_2)

    await _feed(subs_handler.try_handle, "🔔 Подписки на пары")
    await _feed(subs_handler.try_handle, "❌ 1. 1 курс — Прикладная информатика")
    m = await _feed(subs_handler.try_handle, "❌ 1. 2 курс — Юриспруденция")

    assert subs_model.list_for(UID) == []
    assert "нет подписок" in m.answers[-1]
