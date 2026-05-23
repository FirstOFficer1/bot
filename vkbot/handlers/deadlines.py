"""Хендлеры раздела «Дедлайны»."""

from __future__ import annotations

import calendar as _cal
import datetime

from ..config import MAX_INPUT_LEN, now_msk
from ..keyboards import CANCEL_KB, MAIN_KB, MONTH_NAMES, build, calendar_kb, calendar_nav
from ..models import deadlines as model
from ..state import store


def _menu_kb() -> str:
    return build(["➕ Добавить дедлайн"], ["◀ Назад"])


def _format_status(delta: datetime.timedelta) -> str:
    secs = delta.total_seconds()
    if secs < 0:
        return "🔴 просрочен"
    if secs < 3600:
        return "🟠 < 1 часа"
    if secs < 86400:
        return "🟡 < 1 дня"
    return f"🟢 {delta.days} дн."


async def try_handle(_bot, message, state, text, uid) -> bool:
    # ── Меню дедлайнов ───────────────────────────────────────────────────────
    if text == "📌 Дедлайны":
        deadlines = model.list_for(uid)
        now = now_msk()
        dl_map: dict[str, int] = {}
        if deadlines:
            lines = ["📌 Твои дедлайны:\n"]
            for i, (did, subj, desc, dl_at) in enumerate(deadlines, 1):
                dl_map[str(i)] = did
                try:
                    dl_dt = datetime.datetime.strptime(dl_at, "%Y-%m-%d %H:%M")
                    dl_fmt = dl_dt.strftime("%d.%m.%Y %H:%M")
                    status = _format_status(dl_dt - now)
                except ValueError:
                    dl_fmt = dl_at
                    status = ""
                desc_line = f"\n   {desc}" if desc else ""
                lines.append(f"[{i}] {subj}{desc_line}\n   📅 {dl_fmt}  {status}")
            ans = "\n\n".join(lines)
            ans += "\n\nВведи номер для удаления или добавь новый."
        else:
            ans = (
                "У тебя пока нет дедлайнов.\n\n"
                "Добавь первый — и я напомню за день и за час до срока."
            )
        store[uid] = {"state": "deadlines", "dl_map": dl_map}
        await message.answer(ans, keyboard=_menu_kb())
        return True

    if isinstance(state, dict) and state.get("state") == "deadlines":
        if text == "◀ Назад":
            store.pop(uid, None)
            await message.answer("Главное меню:", keyboard=MAIN_KB)
            return True
        if text == "➕ Добавить дедлайн":
            store[uid] = {"step": "dl_subject"}
            await message.answer(
                "📌 Добавление дедлайна\n\n"
                "Шаг 1/4 — Введи название предмета или задачи:\n"
                "Например: «Математика» или «Курсовая по физике»",
                keyboard=CANCEL_KB,
            )
            return True
        if text.isdigit():
            dl_map = state.get("dl_map", {})
            num = int(text)
            if str(num) in dl_map:
                model.delete(dl_map[str(num)])
                store.pop(uid, None)
                await message.answer(f"✅ Дедлайн [{num}] удалён.", keyboard=MAIN_KB)
            else:
                await message.answer(
                    "❌ Дедлайн с таким номером не найден.",
                    keyboard=_menu_kb(),
                )
            return True
        await message.answer(
            "Введи номер дедлайна для удаления или нажми кнопку.",
            keyboard=_menu_kb(),
        )
        return True

    # ── Шаги добавления ──────────────────────────────────────────────────────
    if isinstance(state, dict) and state.get("step") == "dl_subject":
        if text in ("❌ Отмена", "Отмена"):
            store.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return True
        if not text:
            await message.answer(
                "Название не может быть пустым. Попробуй ещё раз:", keyboard=CANCEL_KB
            )
            return True
        if len(text) > MAX_INPUT_LEN:
            await message.answer(
                f"Слишком длинный текст (максимум {MAX_INPUT_LEN} символов).",
                keyboard=CANCEL_KB,
            )
            return True
        store.patch(uid, step="dl_desc", dl_subject=text)
        await message.answer(
            f"📌 Предмет: {text}\n\n"
            "Шаг 2/4 — Добавь описание (необязательно):\n"
            "Например, «Решить задачи 1-5» или нажми «Пропустить»",
            keyboard=build(["⏩ Пропустить"], ["❌ Отмена"]),
        )
        return True

    if isinstance(state, dict) and state.get("step") == "dl_desc":
        if text in ("❌ Отмена", "Отмена"):
            store.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return True
        desc = "" if text == "⏩ Пропустить" else text
        if len(desc) > MAX_INPUT_LEN:
            await message.answer(
                f"Слишком длинное описание (максимум {MAX_INPUT_LEN} символов).",
                keyboard=build(["⏩ Пропустить"], ["❌ Отмена"]),
            )
            return True
        now = now_msk()
        store.patch(
            uid, step="dl_date", dl_desc=desc, cal_year=now.year, cal_month=now.month
        )
        await message.answer(
            "Шаг 3/4 — Выбери дату дедлайна:",
            keyboard=calendar_kb(now.year, now.month, now.day),
        )
        return True

    if isinstance(state, dict) and state.get("step") == "dl_date":
        if text in ("❌ Отмена", "Отмена"):
            store.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return True
        today = now_msk().date()
        state = store.get(uid)
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
            day = int(text)
            _, days_in_month = _cal.monthrange(year, month)
            selected = datetime.date(year, month, day)
            if 1 <= day <= days_in_month and selected >= today:
                date_str = f"{year:04d}-{month:02d}-{day:02d}"
                store.patch(uid, step="dl_time", dl_date=date_str)
                await message.answer(
                    f"✅ Дата: {day} {MONTH_NAMES[month - 1]} {year}\n\n"
                    "Шаг 4/4 — Введи время дедлайна в формате ЧЧ:ММ\n"
                    "Или нажми «Пропустить» (будет установлено 23:59)",
                    keyboard=build(["⏩ Пропустить"], ["❌ Отмена"]),
                )
                return True
        await message.answer(
            "📅 Выбери дату из кнопок:", keyboard=calendar_kb(year, month, min_day)
        )
        return True

    if isinstance(state, dict) and state.get("step") == "dl_time":
        if text in ("❌ Отмена", "Отмена"):
            store.pop(uid, None)
            await message.answer("Отменено.", keyboard=MAIN_KB)
            return True
        if text == "⏩ Пропустить":
            time_str = "23:59"
        else:
            try:
                datetime.datetime.strptime(text, "%H:%M")
                time_str = text
            except ValueError:
                await message.answer(
                    "Неверный формат. Введи время как ЧЧ:ММ\n"
                    "Пример: 18:00\nИли нажми «Пропустить»",
                    keyboard=build(["⏩ Пропустить"], ["❌ Отмена"]),
                )
                return True
        deadline_at = f"{state['dl_date']} {time_str}"
        deadline_dt = datetime.datetime.strptime(deadline_at, "%Y-%m-%d %H:%M")
        if deadline_dt <= now_msk():
            await message.answer(
                "⚠️ Это время уже прошло. Введи время в будущем, "
                "или нажми «Пропустить» (будет 23:59):",
                keyboard=build(["⏩ Пропустить"], ["❌ Отмена"]),
            )
            return True
        model.add(uid, state["dl_subject"], state.get("dl_desc", ""), deadline_at)
        store.pop(uid, None)
        desc_line = f"\n📝 {state['dl_desc']}" if state.get("dl_desc") else ""
        await message.answer(
            f"✅ Дедлайн добавлен!\n\n"
            f"📌 {state['dl_subject']}{desc_line}\n"
            f"🕐 {deadline_at}\n\n"
            f"Напомню за 1 день и за 1 час до срока.",
            keyboard=MAIN_KB,
        )
        return True

    return False
