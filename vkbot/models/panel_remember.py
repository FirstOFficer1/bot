"""Долгоживущие токены «запомнить меня» для веб-панели.

Идея: при логине выдаём случайный 32-байтный токен, в БД храним только sha256-хеш.
Кука `vkbot_rm` — единственный источник правды по авторизации; Flask-сессия
используется как тонкий кеш на 1 запрос.

Сроки:
* абсолютный — TOKEN_TTL_DAYS (полгода): после него токен мёртв независимо от
  активности;
* простой — IDLE_TTL_DAYS (60 дней): если last_used_at старше — тоже мёртв.
  Без idle украденная/забытая кука жила бы весь абсолютный срок.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from ..config import now_msk
from ..db import connect

TOKEN_TTL_DAYS = 180
IDLE_TTL_DAYS = 60
COOKIE_NAME = "vkbot_rm"


def _now() -> str:
    return now_msk().isoformat(timespec="seconds")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _created_cutoff() -> str:
    return (now_msk() - timedelta(days=TOKEN_TTL_DAYS)).isoformat(timespec="seconds")


def _idle_cutoff() -> str:
    return (now_msk() - timedelta(days=IDLE_TTL_DAYS)).isoformat(timespec="seconds")


def issue(vk_id: int) -> str:
    """Создаёт новый токен, сохраняет его хеш, возвращает сам токен (в куку)."""
    token = secrets.token_urlsafe(32)
    with connect() as conn:
        conn.execute(
            "INSERT INTO panel_remember_tokens (token_hash, vk_id, created_at, last_used_at) "
            "VALUES (?, ?, ?, ?)",
            (_hash(token), vk_id, _now(), _now()),
        )
    return token


def verify(token: str, *, rotate: bool = False) -> dict | None:
    """Проверяет токен. Возвращает {'vk_id': int, 'new_token': str | None} или None.

    rotate=False — обычная проверка: обновляем last_used_at.
    rotate=True — удаляем старый, выдаём новый (для явного refresh).
    """
    if not token:
        return None
    h = _hash(token)
    created_cut = _created_cutoff()
    idle_cut = _idle_cutoff()
    with connect() as conn:
        conn.execute(
            "DELETE FROM panel_remember_tokens "
            "WHERE created_at < ? OR last_used_at < ?",
            (created_cut, idle_cut),
        )
        row = conn.execute(
            "SELECT vk_id, created_at, last_used_at FROM panel_remember_tokens "
            "WHERE token_hash=?",
            (h,),
        ).fetchone()
        if not row:
            return None
        vk_id, created_at, last_used_at = row
        if created_at < created_cut or last_used_at < idle_cut:
            conn.execute("DELETE FROM panel_remember_tokens WHERE token_hash=?", (h,))
            return None
        if rotate:
            conn.execute("DELETE FROM panel_remember_tokens WHERE token_hash=?", (h,))
            new_token = secrets.token_urlsafe(32)
            conn.execute(
                "INSERT INTO panel_remember_tokens (token_hash, vk_id, created_at, last_used_at) "
                "VALUES (?, ?, ?, ?)",
                (_hash(new_token), vk_id, created_at, _now()),
            )
            return {"vk_id": int(vk_id), "new_token": new_token}
        conn.execute(
            "UPDATE panel_remember_tokens SET last_used_at=? WHERE token_hash=?",
            (_now(), h),
        )
    return {"vk_id": int(vk_id), "new_token": None}


def revoke(token: str) -> None:
    """Удаляет конкретный токен (вызывается при logout)."""
    if not token:
        return
    with connect() as conn:
        conn.execute("DELETE FROM panel_remember_tokens WHERE token_hash=?", (_hash(token),))


def revoke_all(vk_id: int) -> None:
    """Удаляет все токены пользователя (выход со всех устройств)."""
    with connect() as conn:
        conn.execute("DELETE FROM panel_remember_tokens WHERE vk_id=?", (vk_id,))


def verify_no_rotate(token: str) -> int | None:
    """DEPRECATED. Используй `verify(token)`."""
    info = verify(token, rotate=False)
    return info["vk_id"] if info else None


def verify_and_rotate(token: str) -> tuple[int, str] | None:
    """DEPRECATED. Используй `verify(token, rotate=True)`."""
    info = verify(token, rotate=True)
    if not info:
        return None
    return info["vk_id"], info["new_token"]  # type: ignore[return-value]
