"""Действия специалистов техподдержки: ответ и закрытие тикетов."""

from __future__ import annotations

import asyncio
import logging
import re

from .. import sender
from ..config import MAX_INPUT_LEN, env_owner_ids
from ..keyboards import CANCEL_KB, MAIN_KB, build
from ..models import panel_users, tickets
from ..state import store

_REPLY_RE = re.compile(r"^(?:ответить|reply)\s+#?(\d+)$", re.IGNORECASE)
_CLOSE_RE = re.compile(r"^(?:закрыть|close)\s+#?(\d+)$", re.IGNORECASE)

STAFF_KB = build(
    ["📋 Открытые тикеты"],
    ["🏠 Меню"],
)


def _is_staff(uid: int) -> bool:
    if uid in env_owner_ids():
        return True
    try:
        return panel_users.is_support(uid) or panel_users.is_owner(uid)
    except Exception:
        logging.exception("проверка staff для %s", uid)
        return False


async def try_handle(bot, message, state, text, uid) -> bool:
    """Только для VK-специалистов (и владельцев). TG-саппорта нет по плану."""
    if uid < 0:
        return False

    # Состояние «пишу ответ на тикет N»
    if isinstance(state, dict) and state.get("mode") == "support_reply":
        return await _continue_reply(bot, message, state, text, uid)

    if not text:
        return False

    # Список тикетов — только staff
    if text in ("📋 Открытые тикеты", "/tickets"):
        if not await asyncio.to_thread(_is_staff, uid):
            return False
        open_list = await asyncio.to_thread(tickets.list_open_for_staff, 15)
        if not open_list:
            await message.answer("Открытых тикетов нет.", keyboard=STAFF_KB)
            return True
        lines = ["📋 Открытые тикеты:"]
        for t in open_list:
            lines.append(f"#{t['id']} · user {t['user_id']} · {t['updated_at']}")
        lines.append("\nОтветить N — ответить, Закрыть N — закрыть.")
        await message.answer("\n".join(lines), keyboard=STAFF_KB)
        return True

    m_reply = _REPLY_RE.match(text.strip())
    if m_reply:
        if not await asyncio.to_thread(_is_staff, uid):
            return False
        tid = int(m_reply.group(1))
        ticket = await asyncio.to_thread(tickets.get, tid)
        if not ticket or ticket["status"] != tickets.STATUS_OPEN:
            await message.answer(
                f"Тикет #{tid} не найден или уже закрыт.", keyboard=STAFF_KB
            )
            return True
        store[uid] = {"mode": "support_reply", "ticket_id": tid}
        await message.answer(
            f"✍️ Ответ на тикет #{tid}. Напиши текст (или «Отмена»):",
            keyboard=CANCEL_KB,
        )
        return True

    m_close = _CLOSE_RE.match(text.strip())
    if m_close:
        if not await asyncio.to_thread(_is_staff, uid):
            return False
        tid = int(m_close.group(1))
        ticket = await asyncio.to_thread(tickets.get, tid)
        if not ticket:
            await message.answer(f"Тикет #{tid} не найден.", keyboard=STAFF_KB)
            return True
        ok = await asyncio.to_thread(tickets.close, tid)
        if not ok:
            await message.answer(f"Тикет #{tid} уже закрыт.", keyboard=STAFF_KB)
            return True
        await message.answer(f"✅ Тикет #{tid} закрыт.", keyboard=STAFF_KB)
        try:
            await sender.send(
                bot,
                ticket["user_id"],
                f"✅ Обращение №{tid} закрыто поддержкой. "
                "Если вопрос остался — снова «Обратная связь».",
            )
        except Exception:
            logging.exception("не удалось уведомить user о закрытии #%s", tid)
        return True

    return False


async def _continue_reply(bot, message, state, text, uid) -> bool:
    if text in ("❌ Отмена", "Отмена", "🏠 Меню"):
        store.pop(uid, None)
        await message.answer("Отменено.", keyboard=MAIN_KB)
        return True
    if not text:
        await message.answer("Текст не может быть пустым.", keyboard=CANCEL_KB)
        return True
    if len(text) > MAX_INPUT_LEN:
        await message.answer(
            f"Слишком длинно (макс. {MAX_INPUT_LEN}).", keyboard=CANCEL_KB
        )
        return True

    tid = int(state["ticket_id"])
    ticket = await asyncio.to_thread(tickets.get, tid)
    if not ticket or ticket["status"] != tickets.STATUS_OPEN:
        store.pop(uid, None)
        await message.answer(
            f"Тикет #{tid} недоступен (закрыт или удалён).", keyboard=STAFF_KB
        )
        return True

    await asyncio.to_thread(
        tickets.add_message, tid, uid, tickets.DIR_STAFF, text
    )
    store.pop(uid, None)
    ok = await sender.send(
        bot,
        ticket["user_id"],
        f"💬 Ответ по обращению №{tid}:\n\n{text}",
    )
    if ok:
        await message.answer(f"✅ Ответ по тикету #{tid} отправлен.", keyboard=STAFF_KB)
    else:
        await message.answer(
            f"⚠️ Не удалось доставить пользователю ответ по #{tid}. "
            "Сообщение сохранено в тикете.",
            keyboard=STAFF_KB,
        )
    return True
