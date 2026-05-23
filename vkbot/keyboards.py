"""Клавиатуры VK: статические и динамические (календарь)."""

from __future__ import annotations

import calendar as _cal
import datetime

from vkbottle import Keyboard, Text


def _kb(*rows: list[str], one_time: bool = False) -> str:
    kb = Keyboard(one_time=one_time)
    for i, row in enumerate(rows):
        if i > 0:
            kb.row()
        for label in row:
            kb.add(Text(label[:40]))
    return kb.get_json()


# Экспорт для хендлеров
build = _kb


MAIN_KB = _kb(
    ["📅 Расписание"],
    ["📝 Добавить заметку", "📋 Мои заметки"],
    ["⏰ Напоминание", "📌 Дедлайны"],
    ["🔔 Подписки на пары"],
    ["🔑 Войти в панель"],
    ["💬 Обратная связь", "❓ Помощь"],
)

DAY_KB = _kb(
    ["Сегодня", "Завтра"],
    ["Понедельник", "Вторник"],
    ["Среда", "Четверг"],
    ["Пятница", "Суббота"],
    ["◀ Назад", "🏠 Меню"],
)

CANCEL_KB = _kb(["❌ Отмена"], one_time=True)
BACK_KB = _kb(["◀ Назад"])

DAYS = {"Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"}

MONTH_NAMES = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def calendar_kb(year: int, month: int, min_day: int = 1) -> str:
    kb = Keyboard(one_time=False)
    kb.add(Text("◀"))
    kb.add(Text(f"{MONTH_NAMES[month - 1]} {year}"))
    kb.add(Text("▶"))
    _, days_in_month = _cal.monthrange(year, month)
    shown = 0
    for i in range(1, days_in_month + 1):
        if i < min_day:
            continue
        if shown % 5 == 0:
            kb.row()
        kb.add(Text(str(i)))
        shown += 1
    kb.row()
    kb.add(Text("❌ Отмена"))
    return kb.get_json()


def calendar_nav(state: dict, text: str, today: datetime.date) -> tuple[int, int, int]:
    """Обрабатывает кнопки ◀/▶ календаря.

    Чисто: возвращает новые (year, month, min_day) без побочных эффектов.
    Вызывающий должен сам сохранить их в state.
    """
    year, month = state["cal_year"], state["cal_month"]
    if text == "◀":
        new_month, new_year = month - 1, year
        if new_month < 1:
            new_month, new_year = 12, year - 1
        if (new_year, new_month) >= (today.year, today.month):
            year, month = new_year, new_month
    elif text == "▶":
        month += 1
        if month > 12:
            month, year = 1, year + 1
    min_day = today.day if (year == today.year and month == today.month) else 1
    return year, month, min_day
