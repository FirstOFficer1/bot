"""Хендлеры раздела «Расписание»."""

from __future__ import annotations

import asyncio
import datetime

from ..config import now_msk
from ..keyboards import DAY_KB, DAYS, MAIN_KB, build
from ..models import user_prefs
from ..schedule import current_week_type, repo, week_type_for
from ..state import store

_WEEKDAY_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

_WEEK_ORDER = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]

# VK обрезает сообщение примерно на 4096 символах. Неделя даже у плотной группы
# столько не занимает, но запас нужен: молча обрезанный день выглядит в чате
# ровно как «пар нет», и жалоба приходит не на длину, а на пропавшую пару.
_CHUNK_LIMIT = 3500


def _week_chunks(course: int, direction: str, week_type: str) -> list[str]:
    """Расписание на неделю, нарезанное по границам дней под лимит сообщения."""
    chunks: list[str] = []
    current = ""
    for day in _WEEK_ORDER:
        block = f"— {day} —\n{repo.get_day(course, direction, day, week_type)}"
        if current and len(current) + len(block) + 2 > _CHUNK_LIMIT:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)
    return chunks


async def _send_week(message, course: int, direction: str, week_type: str, keyboard: str) -> None:
    """Шлёт неделю одним или несколькими сообщениями; клавиатура — на последнем."""
    wlabel = "чётная" if week_type == "чёт" else "нечётная"
    # Внутри — шесть запросов к расписанию подряд; в потоке они не держат
    # event loop, пока человек ждёт неделю целиком.
    chunks = await asyncio.to_thread(_week_chunks, course, direction, week_type)
    chunks[0] = f"📖 Вся неделя ({wlabel})\n{course} курс · {direction}\n\n{chunks[0]}"
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await message.answer(chunk, keyboard=keyboard if is_last else None)


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
        ["📖 Вся неделя"],
        ["👀 Другое расписание"],
        ["🔄 Сменить курс/направление"],
        ["◀ Назад", "🏠 Меню"],
    )


def _view_day_kb() -> str:
    """День в режиме просмотра: тот же набор, но без смены сохранённой группы."""
    return build(
        ["Сегодня", "Завтра"],
        ["Понедельник", "Вторник"],
        ["Среда", "Четверг"],
        ["Пятница", "Суббота"],
        ["📖 Вся неделя"],
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
        if text == "📖 Вся неделя":
            user_prefs.set(uid, course, direction)
            store.pop(uid, None)
            await _send_week(message, course, direction, week_type, MAIN_KB)
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
        if text == "👀 Другое расписание":
            store[uid] = {"step": "view_course", "week_type": week_type}
            await message.answer(
                "Смотрим чужое расписание — твой выбор останется прежним.\n\nВыбери курс:",
                keyboard=_course_kb(),
            )
            return True
        if text == "📖 Вся неделя":
            store.pop(uid, None)
            await _send_week(message, course, direction, week_type, MAIN_KB)
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

    # ── Режим просмотра: любой курс и направление, свой выбор не трогаем ──────
    # Отличие от «🔄 Сменить курс/направление» в одном: здесь не вызывается
    # user_prefs.set(). Иначе взгляд на чужое расписание переписывал бы группу
    # человека, а панель при следующей смене группы сняла бы подписку на
    # «предыдущую» — ту, которую он никогда не выбирал.
    if isinstance(state, dict) and state.get("step") == "view_course":
        week_type = state["week_type"]
        if text == "◀ Назад":
            pref = user_prefs.get(uid)
            if pref:
                pref_course, pref_dir = pref
                store[uid] = {
                    "step": "quick_day",
                    "course": pref_course,
                    "direction": pref_dir,
                    "week_type": week_type,
                }
                await message.answer(
                    f"Твой выбор: {pref_course} курс — {pref_dir}\n\nВыбери день:",
                    keyboard=_quick_day_kb(),
                )
            else:
                store.pop(uid, None)
                await message.answer("Главное меню:", keyboard=MAIN_KB)
            return True
        if text.isdecimal() and int(text) in repo.directions_by_course:
            store.patch(uid, step="view_direction", course=int(text))
            await message.answer("Выбери направление:", keyboard=_direction_kb(int(text)))
            return True
        await message.answer("Выбери курс из кнопок ниже:", keyboard=_course_kb())
        return True

    if isinstance(state, dict) and state.get("step") == "view_direction":
        course = state["course"]
        if text == "◀ Назад":
            store.patch(uid, step="view_course")
            await message.answer("Выбери курс:", keyboard=_course_kb())
            return True
        direction = repo.resolve_direction(text, course)
        if direction:
            store.patch(uid, step="view_day", direction=direction)
            await message.answer(
                f"👀 {course} курс — {direction}\n\nВыбери день или смотри всю неделю:",
                keyboard=_view_day_kb(),
            )
            return True
        await message.answer("Выбери направление из кнопок:", keyboard=_direction_kb(course))
        return True

    if isinstance(state, dict) and state.get("step") == "view_day":
        course = state["course"]
        direction = state["direction"]
        week_type = state["week_type"]
        if text == "◀ Назад":
            store.patch(uid, step="view_direction")
            await message.answer("Выбери направление:", keyboard=_direction_kb(course))
            return True
        # Состояние не сбрасываем: из просмотра логично глянуть ещё день.
        if text == "📖 Вся неделя":
            await _send_week(message, course, direction, week_type, _view_day_kb())
            return True
        rel = _resolve_relative_day(text)
        if rel is not None:
            day, wt = rel
            if day == "Воскресенье":
                await message.answer(f"{text} — воскресенье, выходной 🎉", keyboard=_view_day_kb())
                return True
            wlabel = "чётная" if wt == "чёт" else "нечётная"
            await message.answer(
                f"👀 {text} — {day} ({wlabel} неделя)\n"
                f"{course} курс · {direction}\n\n"
                f"{repo.get_day(course, direction, day, wt)}",
                keyboard=_view_day_kb(),
            )
            return True
        if text in DAYS:
            wlabel = "чётная" if week_type == "чёт" else "нечётная"
            await message.answer(
                f"👀 {text} ({wlabel} неделя)\n"
                f"{course} курс · {direction}\n\n"
                f"{repo.get_day(course, direction, text, week_type)}",
                keyboard=_view_day_kb(),
            )
            return True
        await message.answer("Выбери день из кнопок:", keyboard=_view_day_kb())
        return True

    return False
