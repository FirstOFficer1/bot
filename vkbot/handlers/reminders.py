"""Хендлеры раздела «Напоминания»."""

from __future__ import annotations

import datetime

from ..config import MAX_INPUT_LEN, now_msk
from ..keyboards import CANCEL_KB, MAIN_KB, MONTH_NAMES, build, calendar_kb, calendar_nav
from ..models import reminders as model
from ..state import store


def _menu_kb() -> str:
    return build(["➕ Добавить напоминание"], ["◀ Назад"])


async def try_handle(_bot, message, state, text, uid) -> bool:
    # ── Меню напоминаний ─────────────────────────────────────────────────────
    if text == "⏰ Напоминание":
        rems = model.list_for(uid)
        rem_map: dict[str, int] = {}
        if rems:
            ans = "⏰ Твои активные напоминания:\n\n"
            for i, (rid, rtext, rat) in enumerate(rems, 1):
                rem_map[str(i)] = rid
                try:
                    rat_fmt = datetime.datetime.strptime(rat, "%Y-%m-%d %H:%M").strftime(
                        "%d.%m.%Y %H:%M"
                    )
                except ValueError:
                    rat_fmt = rat
                ans += f"[{i}] {rtext}\n   📅 {rat_fmt}\n\n"
            ans += "Введи номер для отмены или добавь новое."
        else:
            ans = "У тебя нет активных напоминаний.\n\nДобавь первое!"
        store[uid] = {"state": "reminders", "rem_map": rem_map}
        await message.answer(ans, keyboard=_menu_kb())
        return True

    if isinstance(state, dict) and state.get("state") == "reminders":
        if text == "◀ Назад":
            store.pop(uid, None)
            await message.answer("Главное меню:", keyboard=MAIN_KB)
            return True
        if text == "➕ Добавить напоминание":
            store[uid] = {"step": "rem_text"}
            await message.answer("✏️ Введи текст напоминания:", keyboard=CANCEL_KB)
            return True
        if text.isdigit():
            rem_map = state.get("rem_map", {})
            num = int(text)
            if str(num) in rem_map:
                model.delete(rem_map[str(num)])
                store.pop(uid, None)
                await message.answer(
                    f"✅ Напоминание [{num}] отменено.", keyboard=MAIN_KB
                )
            else:
                await message.answer(
                    "❌ Напоминание с таким номером не найдено.",
                    keyboard=_menu_kb(),
                )
            return True
        await message.answer(
            "Введи номер напоминания для отмены или нажми кнопку.",
            keyboard=_menu_kb(),
        )
        return True

    # ── Шаги добавления ──────────────────────────────────────────────────────
    if isinstance(state, dict) and state.get("step") == "rem_text":
        if text in ("❌ Отмена", "Отмена"):
            store.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return True
        if not text:
            await message.answer(
                "Текст не может быть пустым. Попробуй ещё раз:", keyboard=CANCEL_KB
            )
            return True
        if len(text) > MAX_INPUT_LEN:
            await message.answer(
                f"Слишком длинный текст (максимум {MAX_INPUT_LEN} символов).",
                keyboard=CANCEL_KB,
            )
            return True
        now = now_msk()
        store.patch(
            uid,
            step="rem_date",
            reminder_text=text,
            cal_year=now.year,
            cal_month=now.month,
        )
        await message.answer(
            "📅 Выбери дату:", keyboard=calendar_kb(now.year, now.month, now.day)
        )
        return True

    if isinstance(state, dict) and state.get("step") == "rem_date":
        if text in ("❌ Отмена", "Отмена"):
            store.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return True
        today = now_msk().date()
        state = store.get(uid)  # перечитываем актуальное состояние
        if text in ("◀", "▶"):
            year, month, min_day = calendar_nav(state, text, today)
            store.patch(uid, cal_year=year, cal_month=month)
            await message.answer("📅 Выбери дату:", keyboard=calendar_kb(year, month, min_day))
            return True
        year, month = state["cal_year"], state["cal_month"]
        min_day = today.day if (year == today.year and month == today.month) else 1
        if text == f"{MONTH_NAMES[month - 1]} {year}":
            await message.answer("📅 Выбери дату:", keyboard=calendar_kb(year, month, min_day))
            return True
        if text.isdigit():
            import calendar as _cal
            day = int(text)
            _, days_in_month = _cal.monthrange(year, month)
            selected = datetime.date(year, month, day)
            if 1 <= day <= days_in_month and selected >= today:
                date_str = f"{year:04d}-{month:02d}-{day:02d}"
                store.patch(uid, step="rem_clock", date_str=date_str)
                await message.answer(
                    f"✅ Дата: {day} {MONTH_NAMES[month - 1]} {year}\n\n"
                    "⏰ Теперь введи время в формате ЧЧ:ММ\nПример: 09:00",
                    keyboard=CANCEL_KB,
                )
                return True
        await message.answer("📅 Выбери дату из кнопок:", keyboard=calendar_kb(year, month, min_day))
        return True

    if isinstance(state, dict) and state.get("step") == "rem_clock":
        if text in ("❌ Отмена", "Отмена"):
            store.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return True
        try:
            datetime.datetime.strptime(text, "%H:%M")
        except ValueError:
            await message.answer(
                "Неверный формат. Введи время как ЧЧ:ММ\nПример: 09:00",
                keyboard=CANCEL_KB,
            )
            return True
        remind_at = f"{state['date_str']} {text}"
        remind_dt = datetime.datetime.strptime(remind_at, "%Y-%m-%d %H:%M")
        if remind_dt <= now_msk():
            await message.answer(
                "⚠️ Это время уже прошло. Введи время в будущем:",
                keyboard=CANCEL_KB,
            )
            return True
        rem_text = state["reminder_text"]
        model.add(uid, rem_text, remind_at)
        store.pop(uid, None)
        await message.answer(
            f"✅ Напоминание создано!\n📝 {rem_text}\n📅 {remind_at}",
            keyboard=MAIN_KB,
        )
        return True

    return False
