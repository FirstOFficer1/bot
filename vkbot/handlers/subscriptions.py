"""Хендлеры раздела «Подписки на пары»."""

from __future__ import annotations

from ..keyboards import MAIN_KB, build
from ..models import subscriptions as model
from ..models import user_prefs
from ..schedule import repo
from ..state import store


def _menu_kb() -> str:
    return build(["➕ Добавить подписку"], ["◀ Назад"])


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


def _reset_to_menu(uid: int) -> None:
    subs = model.list_for(uid)
    sub_map = {str(i): sid for i, (sid, _, _) in enumerate(subs, 1)}
    store[uid] = {"state": "subs", "sub_map": sub_map}


async def try_handle(_bot, message, state, text, uid) -> bool:
    # ── Меню подписок ────────────────────────────────────────────────────────
    if text == "🔔 Подписки на пары":
        subs = model.list_for(uid)
        sub_map: dict[str, int] = {}
        if subs:
            ans = "🔔 Твои подписки на уведомления о парах:\n\n"
            for i, (sid, course, direction) in enumerate(subs, 1):
                sub_map[str(i)] = sid
                ans += f"[{i}] {course} курс — {direction}\n"
            ans += "\nВведи номер для удаления подписки, или добавь новую."
        else:
            ans = (
                "У тебя пока нет подписок.\n\n"
                "Добавь подписку — и я буду напоминать о каждой паре за 10 минут."
            )
        store[uid] = {"state": "subs", "sub_map": sub_map}
        await message.answer(ans, keyboard=_menu_kb())
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
        if text.isdecimal():
            sub_map = state.get("sub_map", {})
            num = int(text)
            if str(num) in sub_map:
                model.delete(sub_map[str(num)], uid)
                store.pop(uid, None)
                await message.answer(f"✅ Подписка [{num}] удалена.", keyboard=MAIN_KB)
            else:
                await message.answer(
                    "❌ Подписка с таким номером не найдена.",
                    keyboard=_menu_kb(),
                )
            return True
        await message.answer(
            "Введи номер подписки для удаления или нажми кнопку.",
            keyboard=_menu_kb(),
        )
        return True

    # ── Быстрая подписка (по сохранённому prefer) ────────────────────────────
    if isinstance(state, dict) and state.get("step") == "sub_quick":
        pref_course = state["pref_course"]
        pref_dir = state["pref_dir"]
        if text == "◀ Назад":
            _reset_to_menu(uid)
            await message.answer("Управление подписками:", keyboard=_menu_kb())
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
            _reset_to_menu(uid)
            await message.answer("Управление подписками:", keyboard=_menu_kb())
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
