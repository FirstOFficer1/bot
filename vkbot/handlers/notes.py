"""Хендлеры раздела «Заметки»."""

from __future__ import annotations

import re

from ..config import MAX_INPUT_LEN, NOTES_LIMIT
from ..keyboards import BACK_KB, CANCEL_KB, MAIN_KB
from ..models import notes as model
from ..state import store


async def try_handle(_bot, message, state, text, uid) -> bool:
    # ── Добавление ────────────────────────────────────────────────────────────
    if text == "📝 Добавить заметку":
        store[uid] = "add_note"
        await message.answer("✏️ Введи текст заметки:", keyboard=CANCEL_KB)
        return True

    if state == "add_note":
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
        if len(model.list_for(uid)) >= NOTES_LIMIT:
            store.pop(uid, None)
            await message.answer(
                f"❌ Достигнут лимит ({NOTES_LIMIT} заметок). "
                "Удали старые, чтобы добавить новые.",
                keyboard=MAIN_KB,
            )
            return True
        model.add(uid, text)
        store.pop(uid, None)
        await message.answer("✅ Заметка сохранена!", keyboard=MAIN_KB)
        return True

    # ── Список и удаление ────────────────────────────────────────────────────
    if text == "📋 Мои заметки":
        notes = model.list_for(uid)
        if not notes:
            await message.answer("У тебя пока нет заметок.", keyboard=MAIN_KB)
            return True
        ans = "📋 Твои заметки:\n\n"
        note_map: dict[str, int] = {}
        for i, (nid, note_text, ts) in enumerate(notes, 1):
            note_map[str(i)] = nid
            ans += f"[{i}] {note_text}\n📅 {ts}\n\n"
        ans += "Введи номер заметки для удаления (можно несколько через запятую).\nИли нажми «◀ Назад»."
        store[uid] = {"state": "view_notes", "note_map": note_map}
        await message.answer(ans, keyboard=BACK_KB)
        return True

    if isinstance(state, dict) and state.get("state") == "view_notes":
        if text == "◀ Назад":
            store.pop(uid, None)
            await message.answer("Главное меню:", keyboard=MAIN_KB)
            return True
        parts = re.split(r"[\s,]+", text)
        try:
            nums = [int(p) for p in parts if p.strip()]
        except ValueError:
            await message.answer(
                "Введи номер(а) заметки цифрами или нажми «◀ Назад».", keyboard=BACK_KB
            )
            return True
        if not nums:
            await message.answer(
                "Введи номер(а) заметки цифрами или нажми «◀ Назад».", keyboard=BACK_KB
            )
            return True
        note_map = state["note_map"]
        to_del_ids = [note_map[str(n)] for n in nums if str(n) in note_map]
        not_found = [n for n in nums if str(n) not in note_map]
        if to_del_ids:
            model.delete(to_del_ids, uid)
        resp_parts = []
        if to_del_ids:
            deleted_nums = ", ".join(str(n) for n in nums if str(n) in note_map)
            resp_parts.append(f"✅ Удалены заметки: {deleted_nums}")
        if not_found:
            resp_parts.append(f"❌ Не найдены: {', '.join(map(str, not_found))}")
        remaining = model.list_for(uid)
        if remaining:
            rem = "\nОставшиеся заметки:\n\n"
            for i, (nid, nt, ts) in enumerate(remaining, 1):
                rem += f"[{i}] {nt}\n📅 {ts}\n\n"
            resp_parts.append(rem)
        else:
            resp_parts.append("Заметок больше нет.")
        store.pop(uid, None)
        await message.answer("\n".join(resp_parts), keyboard=MAIN_KB)
        return True

    return False
