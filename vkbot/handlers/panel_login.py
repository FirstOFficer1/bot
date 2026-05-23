"""Хендлер: выдача кода для входа в веб-панель администраторам.

Любой может попросить код, но войти в админку через него смогут только VK ID
из ADMIN_VK_IDS (это уже проверяет web_panel). Так что код безопасно выдавать
кому угодно — он залогинит юзера в его собственный /me.
"""

from __future__ import annotations

import os

from ..keyboards import MAIN_KB
from ..models import panel_codes


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


_TRIGGERS = {"🔑 Войти в панель", "/login", "вход в панель", "войти в панель"}


async def try_handle(_bot, message, _state, text, uid) -> bool:
    if (text or "").strip().lower() not in {t.lower() for t in _TRIGGERS}:
        return False

    panel_url = os.getenv("PANEL_BASE_URL", "").rstrip("/") or "https://elschedule.ru"
    code, ttl = panel_codes.issue(uid)
    is_admin = uid in _admin_ids()
    role = "администратор" if is_admin else "обычный пользователь"

    # 1) Информационное сообщение
    await message.answer(
        f"🔑 Код для входа в веб-панель\n\n"
        f"⏱ Действителен {ttl} мин., одноразовый.\n"
        f"👤 Войдёшь как: {role}\n\n"
        f"Открой: {panel_url}/login\n\n"
        f"⬇ Код — отдельным сообщением ниже.\n"
        f"Зажми его → «Скопировать».",
        keyboard=MAIN_KB,
    )
    # 2) Сам код — отдельным сообщением, чтобы long-press копировал ровно 6 цифр
    # без префиксов/пояснений/пробелов.
    await message.answer(code)
    return True
