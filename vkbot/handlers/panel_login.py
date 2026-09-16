"""Хендлер: выдача кода для входа в веб-панель и step-up подтверждений.

Любой может попросить код, но войти в админку через него смогут только те,
кому панель выдала права. Код безопасно выдавать кому угодно — он залогинит
юзера в его собственный /me.

Два назначения:
* /login — код входа (purpose=login);
* /confirm — код подтверждения опасных операций в панели (purpose=step_up).
  Код входа намеренно не подходит для step-up: иначе перехваченный OTP
  сразу открывал бы передачу владения.
"""

from __future__ import annotations

import asyncio
import os
import re

from ..keyboards import MAIN_KB
from ..models import panel_codes, panel_users


def _admin_ids() -> set[int]:
    ids: set[int] = set()
    raw = os.getenv("ADMIN_VK_IDS", "")
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    for key in ("ADMIN_VK_ID", "ADMIN_ID"):
        val = os.getenv(key, "").strip()
        if val.isdigit() and int(val) > 0:
            ids.add(int(val))
    return ids


def _is_panel_admin(uid: int) -> bool:
    """Админ или владелец, выданный через панель. Та же БД, что у web_panel."""
    try:
        return panel_users.is_admin(uid)
    except Exception:
        return False


_LOGIN_TRIGGERS = {"🔑 Войти в панель", "/login", "вход в панель", "войти в панель"}
_CONFIRM_TRIGGERS = {
    "🔐 Код подтверждения", "/confirm", "код подтверждения", "подтверждение",
}


async def try_handle(_bot, message, _state, text, uid) -> bool:
    raw = (text or "").strip()
    low = raw.lower()

    if low in {t.lower() for t in _CONFIRM_TRIGGERS}:
        code, ttl = await asyncio.to_thread(
            panel_codes.issue, uid, purpose=panel_codes.PURPOSE_STEP_UP,
        )
        await message.answer(
            "🔐 Код подтверждения для панели\n\n"
            f"⏱ Действителен {ttl} мин., одноразовый.\n"
            "Нужен для опасных операций: передача владения, выдача админа, "
            "рассылка.\n\n"
            "Код входа (/login) сюда не подходит — это другой код.\n"
            "⬇ Код — отдельным сообщением ниже.",
            keyboard=MAIN_KB,
        )
        await message.answer(code)
        return True

    if low not in {t.lower() for t in _LOGIN_TRIGGERS}:
        return False

    panel_url = os.getenv("PANEL_BASE_URL", "").rstrip("/") or "https://elschedule.ru"
    code, ttl = await asyncio.to_thread(
        panel_codes.issue, uid, purpose=panel_codes.PURPOSE_LOGIN,
    )
    is_admin = uid in _admin_ids() or await asyncio.to_thread(_is_panel_admin, uid)
    role = "администратор" if is_admin else "обычный пользователь"

    await message.answer(
        f"🔑 Код для входа в веб-панель\n\n"
        f"⏱ Действителен {ttl} мин., одноразовый.\n"
        f"👤 Войдёшь как: {role}\n\n"
        f"Открой: {panel_url}/login\n\n"
        f"⬇ Код — отдельным сообщением ниже.\n"
        f"Зажми его → «Скопировать».",
        keyboard=MAIN_KB,
    )
    await message.answer(code)
    return True


# Код приходит отдельным сообщением, и его естественно хочется отправить обратно
# в чат — бот на это отвечал «Не понимаю эту команду». Подсказываем, куда его
# на самом деле вводить. Хендлер стоит в конце пайплайна, поэтому не перехватит
# цифры, которых ждёт активный диалог (номер заметки, время напоминания).
_CODE_RE = re.compile(
    rf"^\D{{0,2}}(\d{{{panel_codes.CODE_LEN}}})\D{{0,2}}$"
)


async def try_code_hint(_bot, message, _state, text, uid) -> bool:
    """Пользователь прислал код входа в чат вместо сайта."""
    if not _CODE_RE.match((text or "").strip()):
        return False
    if not await asyncio.to_thread(panel_codes.has_recent_code, uid):
        return False

    panel_url = os.getenv("PANEL_BASE_URL", "").rstrip("/") or "https://elschedule.ru"
    await message.answer(
        "Этот код вводится не здесь, а на сайте панели:\n"
        f"{panel_url}/login\n\n"
        "Открой ссылку, вставь код в поле на странице и нажми «Войти».\n"
        "Если код уже просрочен — нажми «🔑 Войти в панель», выдам новый.",
        keyboard=MAIN_KB,
    )
    return True
