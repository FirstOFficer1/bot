"""Привязка VK ↔ Telegram через одноразовый код."""

from __future__ import annotations

import asyncio
import logging
import re

from ..ids import is_telegram, is_vk
from ..keyboards import MAIN_KB, MISC_KB, build
from ..models import account_links, audit, panel_codes
from ..state import store

log = logging.getLogger(__name__)

LINK_BTN = "🔗 Связать аккаунты"
GET_CODE = "🔑 Получить код"
ENTER_CODE = "✏️ Ввести код"
UNLINK_BTN = "🔓 Отвязать"
_MODE_MENU = "account_link_menu"
_MODE_ENTER = "account_link_enter"

LINK_MENU_KB = build([GET_CODE], [ENTER_CODE], ["❌ Отмена"], one_time=True)
LINKED_KB = build([UNLINK_BTN], ["🏠 Меню"], one_time=True)
ENTER_KB = build(["❌ Отмена"], one_time=True)

_CODE_RE = re.compile(rf"^\D{{0,2}}(\d{{{panel_codes.CODE_LEN}}})\D{{0,2}}$")


def _other_platform(uid: int) -> str:
    return "Telegram-боте" if is_vk(uid) else "VK-боте"


async def try_handle(_bot, message, state, text, uid) -> bool:
    text = (text or "").strip()

    if isinstance(state, dict) and state.get("mode") == _MODE_ENTER:
        if text in ("❌ Отмена", "🏠 Меню", "Отмена"):
            store.pop(uid, None)
            await message.answer("Отменено.", keyboard=MISC_KB)
            return True
        m = _CODE_RE.match(text)
        if not m:
            await message.answer(
                f"Нужен {panel_codes.CODE_LEN}-значный код из другого бота "
                "(или «Отмена»).",
                keyboard=ENTER_KB,
            )
            return True
        return await _redeem(message, uid, m.group(1))

    if isinstance(state, dict) and state.get("mode") == _MODE_MENU:
        if text in ("❌ Отмена", "🏠 Меню", "Отмена"):
            store.pop(uid, None)
            await message.answer("Отменено.", keyboard=MISC_KB)
            return True
        if text == GET_CODE:
            return await _issue(message, uid)
        if text == ENTER_CODE:
            store[uid] = {"mode": _MODE_ENTER}
            await message.answer(
                f"Введи {panel_codes.CODE_LEN}-значный код из {_other_platform(uid)}:",
                keyboard=ENTER_KB,
            )
            return True
        if text == UNLINK_BTN:
            return await _unlink(message, uid)
        await message.answer("Выбери кнопку ниже.", keyboard=LINK_MENU_KB)
        return True

    if text != LINK_BTN:
        return False

    st = await asyncio.to_thread(account_links.status, uid)
    if st["linked"]:
        store[uid] = {"mode": _MODE_MENU}
        other = (
            f"Telegram id {-st['telegram_uid']}"
            if is_vk(uid)
            else f"VK id {st['vk_id']}"
        )
        await message.answer(
            f"Аккаунты уже связаны ({other}).\n"
            "Данные общие: подписки, заметки, группа.\n"
            "Отвязать можно кнопкой ниже — данные останутся на VK.",
            keyboard=LINKED_KB,
        )
        return True

    store[uid] = {"mode": _MODE_MENU}
    await message.answer(
        "🔗 Связь VK и Telegram\n\n"
        "После привязки заметки, подписки и группа будут общими, "
        "уведомления о парах — в оба мессенджера.\n\n"
        f"• «Получить код» — покажу код, введи его в {_other_platform(uid)}.\n"
        f"• «Ввести код» — если код уже взял в {_other_platform(uid)}.",
        keyboard=LINK_MENU_KB,
    )
    return True


async def _issue(message, uid: int) -> bool:
    code, ttl = await asyncio.to_thread(
        panel_codes.issue, uid, purpose=panel_codes.PURPOSE_LINK,
    )
    store.pop(uid, None)
    await message.answer(
        f"🔑 Код для связи аккаунтов\n\n"
        f"⏱ {ttl} мин., одноразовый.\n"
        f"Открой {_other_platform(uid)} → «⚙️ Прочее» → «🔗 Связать аккаунты» "
        f"→ «Ввести код» и вставь его.\n\n"
        f"⬇ Код — отдельным сообщением.",
        keyboard=MISC_KB,
    )
    await message.answer(code)
    return True


async def _redeem(message, uid: int, code: str) -> bool:
    issuer = await asyncio.to_thread(
        panel_codes.verify, code, purpose=panel_codes.PURPOSE_LINK,
    )
    if issuer is None:
        await message.answer(
            "Код неверный или просрочен. Возьми новый в другом боте.",
            keyboard=MISC_KB,
        )
        store.pop(uid, None)
        return True
    try:
        vk_id, tg_uid = await asyncio.to_thread(
            account_links.link_pair, issuer, uid,
        )
    except ValueError as e:
        store.pop(uid, None)
        await message.answer(f"Не вышло: {e}", keyboard=MISC_KB)
        return True
    except Exception:
        log.exception("link_pair failed issuer=%s redeemer=%s", issuer, uid)
        store.pop(uid, None)
        await message.answer(
            "Не удалось связать, попробуй ещё раз чуть позже.",
            keyboard=MISC_KB,
        )
        return True

    store.pop(uid, None)
    # После merge состояние TG должно жить на VK — forget TG, keep VK.
    if is_telegram(uid):
        store.forget(uid)
    try:
        await asyncio.to_thread(
            audit.log, vk_id, "account.link",
            f"vk={vk_id}", f"tg={tg_uid}",
        )
    except Exception:
        log.exception("audit account.link")

    await message.answer(
        "✅ Готово! Аккаунты связаны.\n"
        "Подписки и заметки теперь общие, пуши о парах приходят и сюда, и туда.",
        keyboard=MAIN_KB,
    )
    return True


async def _unlink(message, uid: int) -> bool:
    ok = await asyncio.to_thread(account_links.unlink, uid)
    store.pop(uid, None)
    if ok:
        try:
            await asyncio.to_thread(
                audit.log, uid, "account.unlink", f"id={uid}", "",
            )
        except Exception:
            log.exception("audit account.unlink")
        await message.answer(
            "Связь снята. Данные остались на стороне VK; "
            "Telegram снова отдельный профиль, пока не привяжешь заново.",
            keyboard=MISC_KB,
        )
    else:
        await message.answer("Связи и не было.", keyboard=MISC_KB)
    return True
