"""Отзыв Flask-сессий и launch-подписей Mini App («выйти со всех устройств»).

RM-токен лежит в БД, поэтому отозвать его можно на любом устройстве. Flask-сессия
и подписанный launch-URL устроены иначе: они живут у клиента. Здесь — отметка
времени отзыва: сессия с `auth_at` раньше отметки и launch с `vk_ts` не новее
её больше не принимаются.

Время пишется с микросекундами: вход сразу после «выйти со всех» попадает в ту
же секунду, и при секундной точности пользователь выбросил бы сам себя.
"""

from __future__ import annotations

from datetime import datetime

from ..config import now_msk
from ..db import connect


def _now() -> str:
    return now_msk().isoformat()


def revoke_all(vk_id: int) -> None:
    """Отзывает все ранее выданные сессии и launch-подписи пользователя."""
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
    """Пускать ли по сессии, выданной в момент `issued_at`."""
    revoked = revoked_at(vk_id)
    if not revoked:
        return True
    if not issued_at:
        return False
    return issued_at > revoked


def launch_is_live(vk_id: int, vk_ts: int | str | None) -> bool:
    """Пускать ли по launch-подписи с меткой vk_ts (unix seconds).

    После logout_all подпись с vk_ts не новее момента отзыва должна отвалиться —
    иначе украденный URL из access-лога снова пускал бы до истечения окна.
    """
    if vk_ts is None:
        return False
    try:
        ts = int(vk_ts)
    except (TypeError, ValueError):
        return False
    revoked = revoked_at(vk_id)
    if not revoked:
        return True
    try:
        # ISO от now_msk() — naive MSK. vk_ts — unix UTC. Сравниваем через UTC.
        revoked_dt = datetime.fromisoformat(revoked)
        # now_msk() naive = MSK; трактуем revoked как MSK (как пишется) → UTC.
        from ..config import MSK

        revoked_aware = revoked_dt.replace(tzinfo=MSK)
        revoked_unix = int(revoked_aware.timestamp())
    except (TypeError, ValueError):
        return False
    return ts > revoked_unix
