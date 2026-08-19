"""Хендлеры раздела «Расписание»."""

from __future__ import annotations

import datetime

from ..config import now_msk
from ..keyboards import DAY_KB, DAYS, MAIN_KB, build
from ..models import user_prefs
from ..schedule import current_week_type, repo, week_type_for
from ..state import store

_WEEKDAY_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def _resolve_relative_day(text: str) -> tuple[str, str] | None:
    """Возвращает (день_недели, week_type) для 'Сегодня'/'Завтра' или None."""
    today = now_msk().date()
    if text == "Сегодня":
        target = today
    elif text == "Завтра":
        target = today + datetime.timedelta(days=1)
    else:
        return None
    day_name = _WEEKDAY_RU[target.weekday()]
    return day_name, week_type_for(target)


def _course_kb() -> str:
    rows = [[str(c)] for c in sorted(repo.directions_by_course)]
    rows.append(["◀ Назад", "🏠 Меню"])
    return build(*rows)


def _direction_kb(course: int) -> str:
    rows = [[label] for label in repo.label_to_full(course)]
    rows.append(["◀ Назад", "🏠 Меню"])
    return build(*rows)


def _quick_day_kb() -> str:
    return build(
        ["Сегодня", "Завтра"],
        ["Понедельник", "Вторник"],
        ["Среда", "Четверг"],
        ["Пятница", "Суббота"],
        ["🔄 Сменить курс/направление"],
        ["◀ Назад", "🏠 Меню"],
    )


async def try_handle(_bot, message, state, text, uid) -> bool:
    # Вход в раздел
    if text == "📅 Расписание":
        if not repo.directions_by_course:
            await message.answer(
                "База расписания пуста. Обратитесь к администратору.",
                keyboard=MAIN_KB,
            )
            return True
        wt = current_week_type()
        hint = "чётная" if wt == "чёт" else "нечётная"
        pref = user_prefs.get(uid)
        if pref:
            pref_course, pref_dir = pref
            store[uid] = {
                "step": "quick_day",
                "course": pref_course,
                "direction": pref_dir,
                "week_type": wt,
            }
            await message.answer(
                f"Сейчас идёт {hint} неделя.\n"
                f"Последний выбор: {pref_course} курс — {pref_dir}\n\nВыбери день:",
                keyboard=_quick_day_kb(),
            )
        else:
            store[uid] = {"step": "course", "week_type": wt}
            await message.answer(
                f"Сейчас идёт {hint} неделя.\nВыбери курс:",
                keyboard=_course_kb(),
            )
        return True

    # Шаг: выбор курса
    if isinstance(state, dict) and state.get("step") == "course":
        if text == "◀ Назад":
            store.pop(uid, None)
            await message.answer("Главное меню:", keyboard=MAIN_KB)
            return True
        if text.isdecimal() and int(text) in repo.directions_by_course:
            store.patch(uid, step="direction", course=int(text))
            await message.answer("Выбери направление:", keyboard=_direction_kb(int(text)))
            return True
        await message.answer("Выбери курс из кнопок ниже:", keyboard=_course_kb())
        return True

    # Шаг: выбор направления
    if isinstance(state, dict) and state.get("step") == "direction":
        course = state["course"]
        if text == "◀ Назад":
            store.patch(uid, step="course")
            await message.answer("Выбери курс:", keyboard=_course_kb())
            return True
        direction = repo.resolve_direction(text, course)
        if direction:
            store.patch(uid, step="day", direction=direction)
            await message.answer("Выбери день недели:", keyboard=DAY_KB)
            return True
        await message.answer("Выбери направление из кнопок:", keyboard=_direction_kb(course))
        return True

    # Шаг: выбор дня (длинный путь)
    if isinstance(state, dict) and state.get("step") == "day":
        course = state["course"]
        direction = state["direction"]
        week_type = state["week_type"]
        if text == "◀ Назад":
            store.patch(uid, step="direction")
            await message.answer("Выбери направление:", keyboard=_direction_kb(course))
            return True
        rel = _resolve_relative_day(text)
        if rel is not None:
            day, wt = rel
            if day == "Воскресенье":
                await message.answer(f"{text} — воскресенье, выходной 🎉", keyboard=DAY_KB)
                return True
            sched = repo.get_day(course, direction, day, wt)
            wlabel = "чётная" if wt == "чёт" else "нечётная"
            user_prefs.set(uid, course, direction)
            store.pop(uid, None)
            await message.answer(
                f"📅 {text} — {day} ({wlabel} неделя)\n"
                f"{course} курс · {direction}\n\n"
                f"{sched}",
                keyboard=MAIN_KB,
            )
            return True
        if text in DAYS:
            sched = repo.get_day(course, direction, text, week_type)
            wlabel = "чётная" if week_type == "чёт" else "нечётная"
            user_prefs.set(uid, course, direction)
            store.pop(uid, None)
            await message.answer(
                f"📅 {text} ({wlabel} неделя)\n"
                f"{course} курс · {direction}\n\n"
                f"{sched}",
                keyboard=MAIN_KB,
            )
            return True
        await message.answer("Выбери день из кнопок:", keyboard=DAY_KB)
        return True

    # Шаг: быстрый день (есть сохранённый выбор)
    if isinstance(state, dict) and state.get("step") == "quick_day":
        course = state["course"]
        direction = state["direction"]
        week_type = state["week_type"]
        if text == "◀ Назад":
            store.pop(uid, None)
            await message.answer("Главное меню:", keyboard=MAIN_KB)
            return True
        if text == "🔄 Сменить курс/направление":
            store.patch(uid, step="course")
            await message.answer("Выбери курс:", keyboard=_course_kb())
            return True
        rel = _resolve_relative_day(text)
        if rel is not None:
            day, wt = rel
            if day == "Воскресенье":
                await message.answer(f"{text} — воскресенье, выходной 🎉", keyboard=_quick_day_kb())
                return True
            sched = repo.get_day(course, direction, day, wt)
            wlabel = "чётная" if wt == "чёт" else "нечётная"
            store.pop(uid, None)
            await message.answer(
                f"📅 {text} — {day} ({wlabel} неделя)\n"
                f"{course} курс · {direction}\n\n"
                f"{sched}",
                keyboard=MAIN_KB,
            )
            return True
        if text in DAYS:
            sched = repo.get_day(course, direction, text, week_type)
            wlabel = "чётная" if week_type == "чёт" else "нечётная"
            store.pop(uid, None)
            await message.answer(
                f"📅 {text} ({wlabel} неделя)\n"
                f"{course} курс · {direction}\n\n"
                f"{sched}",
                keyboard=MAIN_KB,
            )
            return True
        await message.answer("Выбери день из кнопок:", keyboard=_quick_day_kb())
        return True

    return False
