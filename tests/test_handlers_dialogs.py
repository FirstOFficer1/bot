"""Пошаговые диалоги бота: ввод, который раньше ронял хендлер.

Регрессия: в шагах выбора даты `datetime.date(year, month, day)` строился до
проверки диапазона, поэтому «31» в 30-дневном месяце бросало ValueError —
диалог зависал молча. Плюс `isdigit()` пропускает «²», на котором падает int().
"""

from __future__ import annotations

import datetime

import pytest

from vkbot.config import now_msk
from vkbot.handlers import deadlines as dl_handler
from vkbot.handlers import reminders as rem_handler
from vkbot.models import deadlines as dl_model
from vkbot.models import reminders as rem_model
from vkbot.state import store

UID = 5005


class FakeMessage:
    """Минимальная замена vkbottle Message: копит ответы."""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.answers: list[str] = []

    async def answer(self, text: str, keyboard=None) -> None:
        self.answers.append(text)


def _next_month_with_30_days() -> tuple[int, int]:
    """Возвращает (год, месяц) будущего месяца, в котором ровно 30 дней."""
    d = now_msk().date().replace(day=1)
    for _ in range(24):
        d = (d + datetime.timedelta(days=31)).replace(day=1)
        import calendar
        if calendar.monthrange(d.year, d.month)[1] == 30:
            return d.year, d.month
    raise AssertionError("не нашли 30-дневный месяц")


@pytest.fixture(autouse=True)
def _clean_state():
    store.pop(UID, None)
    yield
    store.pop(UID, None)


# ── Дедлайны ─────────────────────────────────────────────────────────────────

async def _drive_to_deadline_date_step() -> None:
    """Проходит диалог до шага выбора даты, оставляя календарь на 30-дневном месяце."""
    year, month = _next_month_with_30_days()
    m = FakeMessage("📌 Дедлайны")
    await dl_handler.try_handle(None, m, store.get(UID), m.text, UID)
    m = FakeMessage("➕ Добавить дедлайн")
    await dl_handler.try_handle(None, m, store.get(UID), m.text, UID)
    m = FakeMessage("Курсовая")
    await dl_handler.try_handle(None, m, store.get(UID), m.text, UID)
    m = FakeMessage("⏩ Пропустить")
    await dl_handler.try_handle(None, m, store.get(UID), m.text, UID)
    store.patch(UID, cal_year=year, cal_month=month)


@pytest.mark.asyncio
async def test_deadline_day_31_in_30_day_month_does_not_crash():
    await _drive_to_deadline_date_step()
    m = FakeMessage("31")
    handled = await dl_handler.try_handle(None, m, store.get(UID), m.text, UID)
    assert handled is True
    assert m.answers, "хендлер должен ответить, а не молча упасть"
    assert "Выбери дату" in m.answers[-1]
    # Состояние осталось на шаге даты — диалог не сломан.
    assert store.get(UID)["step"] == "dl_date"


@pytest.mark.asyncio
async def test_deadline_day_zero_does_not_crash():
    await _drive_to_deadline_date_step()
    m = FakeMessage("0")
    assert await dl_handler.try_handle(None, m, store.get(UID), m.text, UID) is True
    assert "Выбери дату" in m.answers[-1]


@pytest.mark.asyncio
async def test_deadline_superscript_digit_does_not_crash():
    """'²'.isdigit() == True, но int('²') падает — вход должен обрабатываться."""
    await _drive_to_deadline_date_step()
    m = FakeMessage("²")
    assert await dl_handler.try_handle(None, m, store.get(UID), m.text, UID) is True
    assert "Выбери дату" in m.answers[-1]


@pytest.mark.asyncio
async def test_deadline_valid_day_advances_to_time_step():
    await _drive_to_deadline_date_step()
    m = FakeMessage("15")
    assert await dl_handler.try_handle(None, m, store.get(UID), m.text, UID) is True
    assert store.get(UID)["step"] == "dl_time"


@pytest.mark.asyncio
async def test_deadline_delete_scoped_to_owner():
    """Чужой дедлайн не удаляется, даже если id угадан."""
    dl_model.add(UID, "Мой", "", "2030-01-01 10:00")
    dl_model.add(9999, "Чужой", "", "2030-01-01 10:00")
    other_id = [r[0] for r in dl_model.list_for(9999)][0]

    dl_model.delete(other_id, UID)

    assert len(dl_model.list_for(9999)) == 1, "чужая запись должна остаться"


# ── Напоминания ──────────────────────────────────────────────────────────────

async def _drive_to_reminder_date_step() -> None:
    year, month = _next_month_with_30_days()
    m = FakeMessage("⏰ Напоминание")
    await rem_handler.try_handle(None, m, store.get(UID), m.text, UID)
    m = FakeMessage("➕ Добавить напоминание")
    await rem_handler.try_handle(None, m, store.get(UID), m.text, UID)
    m = FakeMessage("Позвонить в деканат")
    await rem_handler.try_handle(None, m, store.get(UID), m.text, UID)
    store.patch(UID, cal_year=year, cal_month=month)


@pytest.mark.asyncio
async def test_reminder_day_31_in_30_day_month_does_not_crash():
    await _drive_to_reminder_date_step()
    m = FakeMessage("31")
    assert await rem_handler.try_handle(None, m, store.get(UID), m.text, UID) is True
    assert "Выбери дату" in m.answers[-1]
    assert store.get(UID)["step"] == "rem_date"


@pytest.mark.asyncio
async def test_reminder_valid_day_advances_to_clock_step():
    await _drive_to_reminder_date_step()
    m = FakeMessage("15")
    assert await rem_handler.try_handle(None, m, store.get(UID), m.text, UID) is True
    assert store.get(UID)["step"] == "rem_clock"


@pytest.mark.asyncio
async def test_reminder_delete_scoped_to_owner():
    rem_model.add(9999, "Чужое", "2030-01-01 10:00")
    other_id = [r[0] for r in rem_model.list_for(9999)][0]

    rem_model.delete(other_id, UID)

    assert len(rem_model.list_for(9999)) == 1
