"""Удаление всех своих данных из бота (VK и Telegram — общий пайплайн).

Кнопка в «⚙️ Прочее». До согласия тот же текст ловит ``consent.try_handle``
(экран отказа). Здесь — путь для уже согласившихся: подтверждение → wipe.
"""

from __future__ import annotations

import asyncio
import logging

from ..keyboards import MAIN_KB, MISC_KB, build
from ..models import audit, user_data
from ..state import store

log = logging.getLogger(__name__)

PURGE_BTN = "🗑 Удалить мои данные"
CONFIRM_BTN = "✅ Да, удалить всё"
_MODE = "delete_my_data"

CONFIRM_KB = build([CONFIRM_BTN], ["❌ Отмена"], one_time=True)


def _summary(counts: dict[str, int]) -> str:
    if not counts:
        return "Сейчас о тебе почти ничего не хранится — только согласие и следы входа, если были."
    parts = []
    for table, n in counts.items():
        label = user_data.LABELS.get(table, table)
        parts.append(f"• {label} — {n}")
    return "Сейчас хранится:\n" + "\n".join(parts)


async def try_handle(_bot, message, state, text, uid) -> bool:
    text = (text or "").strip()

    if isinstance(state, dict) and state.get("mode") == _MODE:
        if text == CONFIRM_BTN:
            try:
                removed = await asyncio.to_thread(user_data.wipe_account, uid)
            except Exception:
                log.exception("не удалось удалить данные uid=%s", uid)
                await message.answer(
                    "Не получилось удалить, попробуй ещё раз чуть позже.",
                    keyboard=MISC_KB,
                )
                return True
            try:
                await asyncio.to_thread(
                    audit.log, uid, "me.delete_all", f"id={uid}",
                    ", ".join(f"{k}={v}" for k, v in removed.items())
                    or "нечего было удалять",
                )
            except Exception:
                log.exception("аудит me.delete_all не записался uid=%s", uid)
            await message.answer(
                "Готово, я всё про тебя забыл.\n"
                "Если передумаешь — напиши что угодно, и начнём заново "
                "(снова попросим согласие).",
                keyboard=MAIN_KB,
            )
            return True
        if text in ("❌ Отмена", "🏠 Меню", "Отмена"):
            store.pop(uid, None)
            await message.answer("Отменено — данные на месте.", keyboard=MISC_KB)
            return True
        await message.answer(
            "Чтобы стереть всё — нажми «Да, удалить всё». Или «Отмена».",
            keyboard=CONFIRM_KB,
        )
        return True

    if text != PURGE_BTN:
        return False

    counts = await asyncio.to_thread(user_data.count_all, uid)
    store[uid] = {"mode": _MODE}
    await message.answer(
        "⚠️ Удалю заметки, напоминания, дедлайны, подписки, выбранную группу, "
        "сессии входа и согласие — сразу и без возможности восстановить.\n\n"
        f"{_summary(counts)}\n\n"
        "В журнале безопасности останется только факт удаления.\n"
        "Точно удалить всё?",
        keyboard=CONFIRM_KB,
    )
    return True
