"""Согласие на обработку персональных данных (152-ФЗ, ст. 9).

Закон требует согласия конкретного, информированного и **однозначного** — то
есть отдельного действия человека. Формулировка «продолжая пользоваться, вы
соглашаетесь» под это не подходит: она не даёт ни момента, ни доказательства.
Поэтому здесь хранится строка на каждого, кто нажал кнопку: когда, какую
версию текста и откуда.

Версия — главное в этой таблице. Меняется текст согласия — поднимается
`VERSION`, и все соглашаются заново: иначе человек числился бы согласившимся
с документом, которого не видел.

Согласие — такие же данные пользователя, как заметки: `user_data.purge()` его
удаляет вместе с остальным. Факт удаления остаётся в журнале аудита.
"""

from __future__ import annotations

from .. import config
from ..db import connect

# Дата последней редакции текста согласия (см. web_panel._CONSENT_BODY).
# Поднимать при любом изменении смысла: состава данных, целей, сроков.
VERSION = "2026-08-22"


def accepted(vk_id: int, version: str | None = None) -> bool:
    """Дано ли согласие на текущую редакцию текста.

    Версия читается в момент вызова, а не подставляется значением по умолчанию:
    иначе она замерзала бы на импорте модуля, и подменить её (в тестах или на
    ходу) стало бы нельзя.
    """
    version = version or VERSION
    with connect() as conn:
        row = conn.execute(
            "SELECT version FROM user_consents WHERE vk_id=?", (vk_id,)
        ).fetchone()
    return bool(row) and row[0] == version


def get(vk_id: int) -> dict | None:
    """Что именно принято — для показа в профиле и выгрузки данных."""
    with connect() as conn:
        row = conn.execute(
            "SELECT vk_id, version, accepted_at, ip, source "
            "FROM user_consents WHERE vk_id=?", (vk_id,)
        ).fetchone()
    if not row:
        return None
    return {
        "vk_id": row[0], "version": row[1], "accepted_at": row[2],
        "ip": row[3], "source": row[4],
    }


def accept(vk_id: int, *, ip: str | None = None, source: str = "panel",
           version: str | None = None) -> None:
    """Записывает согласие. Повторное согласие обновляет строку, а не плодит их."""
    version = version or VERSION
    with connect() as conn:
        conn.execute(
            "INSERT INTO user_consents (vk_id, version, accepted_at, ip, source) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(vk_id) DO UPDATE SET "
            "version=excluded.version, accepted_at=excluded.accepted_at, "
            "ip=excluded.ip, source=excluded.source",
            (vk_id, version, config.now_msk().strftime("%Y-%m-%dT%H:%M:%S"), ip, source),
        )


def withdraw(vk_id: int) -> bool:
    """Отзыв согласия. Возвращает True, если было что отзывать.

    Отзыв сам по себе данные не удаляет — это делает `user_data.purge()`,
    который вызывается следом. Отдельная функция нужна, чтобы отзыв можно
    было записать в журнал до того, как строки исчезнут.
    """
    with connect() as conn:
        return conn.execute(
            "DELETE FROM user_consents WHERE vk_id=?", (vk_id,)
        ).rowcount > 0
