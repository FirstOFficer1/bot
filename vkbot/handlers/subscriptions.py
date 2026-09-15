"""Хендлеры раздела «Подписки на пары»."""

from __future__ import annotations

import re

from ..keyboards import MAIN_KB, build
from ..models import subscriptions as model
from ..models import user_prefs
from ..schedule import repo
from ..state import store


def _menu_kb(subs: list[tuple]) -> str:
    """Меню подписок: по кнопке на каждую подписку плюс «добавить».

    Отписка раньше существовала только как ввод номера текстом — подсказку
    в сообщении никто не читал, и функция считалась отсутствующей. Номер в
    начале подписи не случаен: `build` режет подпись до 40 символов, и длинное
    направление обрезается, а «❌ 2.» остаётся на месте и по нему же опознаётся
    нажатие.
    """
    rows = [
        [f"❌ {i}. {course} курс — {direction}"]
        for i, (_sid, course, direction) in enumerate(subs, 1)
    ]
    rows.append(["➕ Добавить подписку"])
    rows.append(["◀ Назад"])
    return build(*rows)


def _chosen_number(text: str) -> str | None:
    """Номер подписки из нажатой кнопки «❌ 2. …» или из введённой цифры."""
    m = re.match(r"^❌\s*(\d+)\.", text)
    if m:
        return m.group(1)
    return text if text.isdecimal() else None


def _subs_screen(uid: int) -> tuple[str, str, dict[str, int]]:
    """Текст, клавиатура и карта номеров для экрана подписок."""
    subs = model.list_for(uid)
    sub_map = {str(i): sid for i, (sid, _c, _d) in enumerate(subs, 1)}
    if subs:
        lines = "\n".join(
            f"[{i}] {course} курс — {direction}"
            for i, (_sid, course, direction) in enumerate(subs, 1)
        )
        text = (
            "🔔 Твои подписки на уведомления о парах:\n\n"
            f"{lines}\n\n"
            "Нажми ❌ на подписке, чтобы отписаться."
        )
    else:
        text = (
            "У тебя пока нет подписок.\n\n"
            "Добавь подписку — и я буду напоминать о каждой паре за 10 минут."
        )
    return text, _menu_kb(subs), sub_map


def _course_kb() -> str:
    rows = [[str(c)] for c in sorted(repo.directions_by_course)]
    rows.append(["◀ Назад", "🏠 Меню"])
    return build(*rows)


def _direction_kb(course: int) -> str:
    rows = [[label] for label in repo.label_to_full(course)]
    rows.append(["◀ Назад", "🏠 Меню"])
    return build(*rows)


def _quick_kb(pref_course: int, pref_dir: str) -> str:
    return build(
        [f"✅ {pref_course} курс — {pref_dir[:25]}"],
        ["🔄 Выбрать другое"],
        ["◀ Назад"],
    )


def _reset_to_menu(uid: int) -> tuple[str, str]:
    """Возвращает пользователя на экран подписок; отдаёт текст и клавиатуру."""
    text, keyboard, sub_map = _subs_screen(uid)
    store[uid] = {"state": "subs", "sub_map": sub_map}
    return text, keyboard


async def try_handle(_bot, message, state, text, uid) -> bool:
    # ── Меню подписок ────────────────────────────────────────────────────────
    if text == "🔔 Подписки на пары":
        ans, keyboard, sub_map = _subs_screen(uid)
        store[uid] = {"state": "subs", "sub_map": sub_map}
        await message.answer(ans, keyboard=keyboard)
        return True

    if isinstance(state, dict) and state.get("state") == "subs":
        if text == "◀ Назад":
            store.pop(uid, None)
            await message.answer("Главное меню:", keyboard=MAIN_KB)
            return True
        if text == "➕ Добавить подписку":
            pref = user_prefs.get(uid)
            if pref:
                pref_course, pref_dir = pref
                store[uid] = {
                    "step": "sub_quick",
                    "pref_course": pref_course,
                    "pref_dir": pref_dir,
                }
                await message.answer(
                    f"Подписаться на уведомления?\n\n"
                    f"📌 {pref_course} курс — {pref_dir}",
                    keyboard=_quick_kb(pref_course, pref_dir),
                )
            else:
                store[uid] = {"step": "sub_course"}
                await message.answer("Выбери курс:", keyboard=_course_kb())
            return True
        num = _chosen_number(text)
        if num is not None:
            sub_map = state.get("sub_map", {})
            if num in sub_map:
                model.delete(sub_map[num], uid)
                # Остаёмся на экране подписок: отписка редко бывает одиночной,
                # да и подтверждение видно сразу над обновлённым списком.
                ans, keyboard, new_map = _subs_screen(uid)
                store[uid] = {"state": "subs", "sub_map": new_map}
                await message.answer(f"✅ Отписка выполнена.\n\n{ans}", keyboard=keyboard)
            else:
                ans, keyboard, new_map = _subs_screen(uid)
                store[uid] = {"state": "subs", "sub_map": new_map}
                await message.answer(
                    f"❌ Подписка с таким номером не найдена.\n\n{ans}", keyboard=keyboard
                )
            return True
        ans, keyboard, sub_map = _subs_screen(uid)
        store[uid] = {"state": "subs", "sub_map": sub_map}
        await message.answer(f"Нажми кнопку ниже.\n\n{ans}", keyboard=keyboard)
        return True

    # ── Быстрая подписка (по сохранённому prefer) ────────────────────────────
    if isinstance(state, dict) and state.get("step") == "sub_quick":
        pref_course = state["pref_course"]
        pref_dir = state["pref_dir"]
        if text == "◀ Назад":
            ans, keyboard = _reset_to_menu(uid)
            await message.answer(ans, keyboard=keyboard)
            return True
        if text == "🔄 Выбрать другое":
            store[uid] = {"step": "sub_course"}
            await message.answer("Выбери курс:", keyboard=_course_kb())
            return True
        if text.startswith("✅"):
            if model.exists(uid, pref_course, pref_dir):
                store.pop(uid, None)
                await message.answer(
                    f"ℹ️ Ты уже подписан на {pref_course} курс — {pref_dir}.",
                    keyboard=MAIN_KB,
                )
            else:
                model.add(uid, pref_course, pref_dir)
                store.pop(uid, None)
                await message.answer(
                    f"✅ Подписка добавлена!\n{pref_course} курс — {pref_dir}\n\n"
                    "Буду напоминать о каждой паре за 10 минут до начала.",
                    keyboard=MAIN_KB,
                )
            return True
        await message.answer("Нажми кнопку ниже:", keyboard=_quick_kb(pref_course, pref_dir))
        return True

    # ── Выбор курса/направления вручную ──────────────────────────────────────
    if isinstance(state, dict) and state.get("step") == "sub_course":
        if text == "◀ Назад":
            ans, keyboard = _reset_to_menu(uid)
            await message.answer(ans, keyboard=keyboard)
            return True
        if text.isdecimal() and int(text) in repo.directions_by_course:
            store.patch(uid, step="sub_direction", course=int(text))
            await message.answer("Выбери направление:", keyboard=_direction_kb(int(text)))
            return True
        await message.answer("Выбери курс из кнопок:", keyboard=_course_kb())
        return True

    if isinstance(state, dict) and state.get("step") == "sub_direction":
        course = state["course"]
        if text == "◀ Назад":
            store.patch(uid, step="sub_course")
            await message.answer("Выбери курс:", keyboard=_course_kb())
            return True
        direction = repo.resolve_direction(text, course)
        if direction:
            if model.exists(uid, course, direction):
                store.pop(uid, None)
                await message.answer(
                    f"ℹ️ Ты уже подписан на {course} курс — {direction}.",
                    keyboard=MAIN_KB,
                )
            else:
                model.add(uid, course, direction)
                store.pop(uid, None)
                await message.answer(
                    f"✅ Подписка добавлена!\n{course} курс — {direction}\n\n"
                    "Буду напоминать о каждой паре за 10 минут до начала.",
                    keyboard=MAIN_KB,
                )
            return True
        await message.answer("Выбери направление из кнопок:", keyboard=_direction_kb(course))
        return True

    return False
