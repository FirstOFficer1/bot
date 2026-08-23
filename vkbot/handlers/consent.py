"""Согласие на обработку персональных данных перед первым же диалогом.

Панель спрашивает согласие своим экраном, но данные создаются в основном
здесь: заметки, напоминания, дедлайны и подписки заводят в чате, а не на
сайте. Пока согласия нет, бот не делает ничего, кроме как показывает текст —
иначе первое же «Привет» пришлось бы записать в базу без основания.

Хранилище общее с панелью (`models/consents`), поэтому согласие, данное здесь,
открывает и сайт, и наоборот. Версия тоже общая: меняется текст — обе
поверхности спрашивают заново.

Этот хендлер стоит в `_PIPELINE` первым. Любой новый хендлер, поставленный
выше, окажется доступен без согласия — это и будет ошибкой.
"""

from __future__ import annotations

import logging

from .. import config
from ..keyboards import build as _kb
from ..models import audit, consents, user_data

log = logging.getLogger(__name__)

ACCEPT = "✅ Принимаю"
DECLINE = "❌ Не принимаю"
PURGE = "🗑 Удалить мои данные"

CONSENT_KB = _kb([ACCEPT], [DECLINE])
DECLINED_KB = _kb([ACCEPT], [PURGE])


def _links() -> str:
    """Ссылки на полные документы. Без адреса панели — просто без ссылок."""
    base = config.PANEL_BASE_URL
    if not base:
        return ""
    return (
        f"\n\nПолный текст согласия: {base}/consent"
        f"\nПолитика конфиденциальности: {base}/privacy"
    )


def _offer() -> str:
    return (
        "📄 Прежде чем начнём — одна формальность.\n\n"
        "Чтобы бот работал, он сохраняет о тебе:\n"
        "• идентификатор ВКонтакте и имя;\n"
        "• выбранные курс и направление;\n"
        "• то, что ты создашь сам — заметки, напоминания, дедлайны, подписки на пары.\n\n"
        "Данные никому не передаются и для рекламы не используются. "
        "Удалить всё можно в любой момент — кнопкой в этом же чате."
        + _links()
        + "\n\nНажми «Принимаю», чтобы продолжить."
    )


async def try_handle(_bot, message, _state, text, uid) -> bool:
    """Пропускает дальше только тех, кто согласился (152-ФЗ, ст. 9)."""
    if consents.accepted(uid):
        return False

    text = (text or "").strip()

    if text == ACCEPT:
        consents.accept(uid, source="bot")
        try:
            audit.log(uid, "consent.accept", f"id={uid}",
                      f"версия {consents.VERSION}, бот")
        except Exception:
            # Журнал — не причина не пустить человека дальше.
            log.exception("не удалось записать согласие в аудит")
        await message.answer(
            "Спасибо! Согласие записано — можно пользоваться.\n"
            "Напиши «Меню» или нажми кнопку ниже.",
            keyboard=_kb(["🏠 Меню"]),
        )
        return True

    if text == PURGE:
        try:
            removed = user_data.purge(uid)
        except Exception:
            log.exception("не удалось удалить данные по запросу из бота")
            await message.answer(
                "Не получилось удалить, попробуй ещё раз чуть позже.",
                keyboard=DECLINED_KB,
            )
            return True
        audit.log(uid, "me.delete_all", f"id={uid}",
                  ", ".join(f"{k}={v}" for k, v in removed.items())
                  or "нечего было удалять")
        await message.answer(
            "Готово, я всё про тебя забыл.\n"
            "Если передумаешь — напиши что угодно, и начнём заново.",
            keyboard=CONSENT_KB,
        )
        return True

    if text == DECLINE:
        await message.answer(
            "Понял. Без согласия бот работать не может: обрабатывать данные "
            "без основания нельзя, а вся его работа — это работа с ними.\n\n"
            "Передумаешь — нажми «Принимаю». А если хочешь, чтобы я забыл и то, "
            "что уже сохранил, нажми «Удалить мои данные».",
            keyboard=DECLINED_KB,
        )
        return True

    await message.answer(_offer(), keyboard=CONSENT_KB)
    return True
