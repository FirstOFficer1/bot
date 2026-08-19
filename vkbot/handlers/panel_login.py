"""Хендлер: выдача кода для входа в веб-панель администраторам.

Любой может попросить код, но войти в админку через него смогут только VK ID
из ADMIN_VK_IDS (это уже проверяет web_panel). Так что код безопасно выдавать
кому угодно — он залогинит юзера в его собственный /me.
"""

from __future__ import annotations

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


_TRIGGERS = {"🔑 Войти в панель", "/login", "вход в панель", "войти в панель"}


async def try_handle(_bot, message, _state, text, uid) -> bool:
    if (text or "").strip().lower() not in {t.lower() for t in _TRIGGERS}:
        return False

    panel_url = os.getenv("PANEL_BASE_URL", "").rstrip("/") or "https://elschedule.ru"
    code, ttl = panel_codes.issue(uid)
    # Права бывают двух видов: из env (владельцы) и выданные в панели
    # (panel_users). Раньше учитывались только первые, и админ, которому выдали
    # доступ через панель, читал в боте, что он обычный пользователь.
    is_admin = uid in _admin_ids() or _is_panel_admin(uid)
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


# Код приходит отдельным сообщением, и его естественно хочется отправить обратно
# в чат — бот на это отвечал «Не понимаю эту команду». Подсказываем, куда его
# на самом деле вводить. Хендлер стоит в конце пайплайна, поэтому не перехватит
# цифры, которых ждёт активный диалог (номер заметки, время напоминания).
_SIX_DIGITS_RE = re.compile(r"^\D{0,2}(\d{6})\D{0,2}$")


async def try_code_hint(_bot, message, _state, text, uid) -> bool:
    """Пользователь прислал код входа в чат вместо сайта."""
    if not _SIX_DIGITS_RE.match((text or "").strip()):
        return False
    if not panel_codes.has_recent_code(uid):
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
