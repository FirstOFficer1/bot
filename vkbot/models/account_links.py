"""Связь аккаунтов VK и Telegram (один человек — общие данные).

Канонический id — VK (положительный): панель и OTP уже на нём. Telegram
хранится как ``-(telegram_id)``. После привязки пайплайн и данные идут на
``vk_id``, пуши — на оба канала.
"""

from __future__ import annotations

import logging

from ..config import now_msk
from ..db import connect
from ..ids import is_telegram, is_vk

log = logging.getLogger(__name__)


def _now() -> str:
    return now_msk().isoformat(timespec="seconds")


def get_vk_for_telegram(telegram_uid: int) -> int | None:
    if not is_telegram(telegram_uid):
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT vk_id FROM account_links WHERE telegram_uid=?",
            (telegram_uid,),
        ).fetchone()
    return int(row[0]) if row else None


def get_telegram_for_vk(vk_id: int) -> int | None:
    if not is_vk(vk_id):
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT telegram_uid FROM account_links WHERE vk_id=?",
            (vk_id,),
        ).fetchone()
    return int(row[0]) if row else None


def canonical_uid(uid: int) -> int:
    """Uid, под которым лежат данные: для привязанного TG → VK."""
    if is_telegram(uid):
        vk = get_vk_for_telegram(uid)
        if vk is not None:
            return vk
    return uid


def delivery_targets(uid: int) -> list[int]:
    """Куда слать пуш: канонический + связанный канал, если есть."""
    canon = canonical_uid(uid)
    targets = [canon]
    if is_vk(canon):
        tg = get_telegram_for_vk(canon)
        if tg is not None:
            targets.append(tg)
    return targets


def status(uid: int) -> dict:
    """Сводка для UI."""
    if is_vk(uid):
        tg = get_telegram_for_vk(uid)
        return {
            "linked": tg is not None,
            "vk_id": uid,
            "telegram_uid": tg,
        }
    vk = get_vk_for_telegram(uid)
    return {
        "linked": vk is not None,
        "vk_id": vk,
        "telegram_uid": uid,
    }


def unlink(uid: int) -> bool:
    """Снимает связь. Данные остаются на VK (если был канон)."""
    with connect() as conn:
        if is_vk(uid):
            cur = conn.execute("DELETE FROM account_links WHERE vk_id=?", (uid,))
        else:
            cur = conn.execute(
                "DELETE FROM account_links WHERE telegram_uid=?", (uid,)
            )
        return cur.rowcount > 0


def _move_rows(conn, table: str, column: str, src: int, dst: int) -> int:
    """Переносит строки src→dst. При PK-конфликте оставляет dst, удаляет src."""
    # Сначала пробуем UPDATE; конфликты (PK/UNIQUE) ловим и чистим src.
    try:
        cur = conn.execute(
            f"UPDATE {table} SET {column}=? WHERE {column}=?",
            (dst, src),
        )
        return cur.rowcount or 0
    except Exception:
        # UNIQUE/PK: удаляем источник, данные dst уже есть.
        log.info("merge %s: конфликт при переносе %s→%s, оставляем dst", table, src, dst)
        cur = conn.execute(f"DELETE FROM {table} WHERE {column}=?", (src,))
        return -(cur.rowcount or 0)


def merge_telegram_into_vk(telegram_uid: int, vk_id: int) -> dict[str, int]:
    """Переносит данные TG-профиля на VK. Вызывать внутри связи."""
    from . import user_data

    if not is_telegram(telegram_uid) or not is_vk(vk_id):
        raise ValueError("нужны telegram_uid < 0 и vk_id > 0")

    moved: dict[str, int] = {}
    with connect() as conn:
        # Prefs / consent / seen: если у VK уже есть — дропаем TG, иначе переносим.
        for table, col in (
            ("user_prefs", "user_id"),
            ("user_consents", "vk_id"),
            ("seen_users", "vk_id"),
            ("panel_users", "vk_id"),
        ):
            dst_has = conn.execute(
                f"SELECT 1 FROM {table} WHERE {col}=? LIMIT 1", (vk_id,)
            ).fetchone()
            if dst_has:
                cur = conn.execute(f"DELETE FROM {table} WHERE {col}=?", (telegram_uid,))
                if cur.rowcount:
                    moved[table] = -cur.rowcount
            else:
                n = _move_rows(conn, table, col, telegram_uid, vk_id)
                if n:
                    moved[table] = n

        # Роли: OR — копируем недостающие, потом чистим TG.
        for role, granted_at, granted_by in conn.execute(
            "SELECT role, granted_at, granted_by FROM panel_user_roles WHERE vk_id=?",
            (telegram_uid,),
        ):
            conn.execute(
                "INSERT OR IGNORE INTO panel_user_roles "
                "(vk_id, role, granted_at, granted_by) VALUES (?,?,?,?)",
                (vk_id, role, granted_at, granted_by),
            )
        cur = conn.execute(
            "DELETE FROM panel_user_roles WHERE vk_id=?", (telegram_uid,)
        )
        if cur.rowcount:
            moved["panel_user_roles"] = cur.rowcount

        # Тикеты + сообщения: UPDATE user_id на тикетах.
        n = _move_rows(conn, "support_tickets", "user_id", telegram_uid, vk_id)
        if n:
            moved["support_tickets"] = n

        # Остальные таблицы из user_data (кроме уже обработанных).
        skip = {
            "user_prefs", "user_consents", "seen_users", "panel_users",
            "panel_user_roles", "support_tickets",
        }
        for table, column in user_data._USER_TABLES:
            if table in skip:
                continue
            n = _move_rows(conn, table, column, telegram_uid, vk_id)
            if n:
                moved[table] = n

        # Состояние диалога TG больше не нужно.
        conn.execute("DELETE FROM user_states WHERE user_id=?", (telegram_uid,))

    return moved


def link(vk_id: int, telegram_uid: int) -> None:
    """Пишет связь. Предполагается, что merge уже выполнен."""
    if not is_vk(vk_id) or not is_telegram(telegram_uid):
        raise ValueError("vk_id > 0 и telegram_uid < 0")
    with connect() as conn:
        # Уже связаны иначе?
        existing_vk = conn.execute(
            "SELECT vk_id FROM account_links WHERE telegram_uid=?",
            (telegram_uid,),
        ).fetchone()
        if existing_vk and int(existing_vk[0]) != vk_id:
            raise ValueError("этот Telegram уже привязан к другому VK")
        existing_tg = conn.execute(
            "SELECT telegram_uid FROM account_links WHERE vk_id=?",
            (vk_id,),
        ).fetchone()
        if existing_tg and int(existing_tg[0]) != telegram_uid:
            raise ValueError("этот VK уже привязан к другому Telegram")
        conn.execute(
            "INSERT OR REPLACE INTO account_links (vk_id, telegram_uid, linked_at) "
            "VALUES (?,?,?)",
            (vk_id, telegram_uid, _now()),
        )


def link_pair(issuer_uid: int, redeemer_uid: int) -> tuple[int, int]:
    """Связывает пару с разных платформ. Возвращает (vk_id, telegram_uid)."""
    if is_telegram(issuer_uid) == is_telegram(redeemer_uid):
        raise ValueError("код нужно ввести в боте другой платформы")
    vk_id = issuer_uid if is_vk(issuer_uid) else redeemer_uid
    tg_uid = issuer_uid if is_telegram(issuer_uid) else redeemer_uid

    st_vk = status(vk_id)
    st_tg = status(tg_uid)
    if st_vk["linked"] and st_vk["telegram_uid"] == tg_uid:
        return vk_id, tg_uid  # уже связаны друг с другом
    if st_vk["linked"] or st_tg["linked"]:
        raise ValueError("один из аккаунтов уже связан с кем-то другим")

    merge_telegram_into_vk(tg_uid, vk_id)
    link(vk_id, tg_uid)
    return vk_id, tg_uid
