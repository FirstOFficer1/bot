"""Отзыв Flask-сессий панели («выйти со всех устройств»).

RM-токен лежит в БД, поэтому отозвать его можно на любом устройстве. Flask-сессия
устроена иначе: это подписанная кука, которая целиком живёт у клиента, и сервер
не может её удалить. Пока `_load_current_user()` пускает по такой сессии как по
запасному пути, `/logout/all` не выгонял бы чужое устройство — там просто нет
RM-токена, и вход продолжался бы по куке ещё год.

Решение — отметка времени отзыва на пользователя. Сессия несёт момент выдачи
(`auth_at`); если он раньше отметки, вход по ней больше не принимается.

Время пишется с микросекундами (в отличие от остальных таблиц, где хватает
секунд): вход сразу после «выйти со всех устройств» попадает в ту же секунду, и
при секундной точности пользователь выбросил бы сам себя.
"""

from __future__ import annotations

from ..config import now_msk
from ..db import connect


def _now() -> str:
    return now_msk().isoformat()


def revoke_all(vk_id: int) -> None:
    """Отзывает все ранее выданные сессии пользователя."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO panel_session_revocations (vk_id, revoked_at) VALUES (?, ?) "
            "ON CONFLICT(vk_id) DO UPDATE SET revoked_at=excluded.revoked_at",
            (vk_id, _now()),
        )


def revoked_at(vk_id: int) -> str | None:
    """Момент последнего отзыва или None, если пользователь ни разу не выходил."""
    with connect() as conn:
        row = conn.execute(
            "SELECT revoked_at FROM panel_session_revocations WHERE vk_id=?",
            (vk_id,),
        ).fetchone()
    return row[0] if row else None


def issued_now() -> str:
    """Метка выдачи для новой сессии — кладётся в неё при логине."""
    return _now()


def is_live(vk_id: int, issued_at: str | None) -> bool:
    """Пускать ли по сессии, выданной в момент `issued_at`.

    Сессия без метки считается отозванной, но только когда отзыв вообще был:
    иначе куки, выданные до появления этой проверки, разлогинили бы всех разом
    на ровном месте.
    """
    revoked = revoked_at(vk_id)
    if not revoked:
        return True
    if not issued_at:
        return False
    return issued_at > revoked
