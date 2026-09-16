"""CRUD для ролей веб-панели и бота (мультироли).

Роли: ``admin``, ``owner``, ``support``. У одного ``vk_id`` может быть
несколько ролей сразу (например admin+support).

Владельцы бывают двух видов:

* **из env** (`ADMIN_ID` / `ADMIN_VK_ID` / `ADMIN_VK_IDS`) — несменяемый якорь.
* **из ``panel_user_roles``** (`role='owner'`) — выдаёт и снимает панель.

Профиль (имя, когда добавлен) живёт в ``panel_users``; источник истины по
правам — ``panel_user_roles``.
"""

from __future__ import annotations

from ..config import now_msk
from ..db import connect

ROLE_ADMIN = "admin"
ROLE_OWNER = "owner"
ROLE_SUPPORT = "support"

_ALL_ROLES = frozenset({ROLE_ADMIN, ROLE_OWNER, ROLE_SUPPORT})


def _now() -> str:
    return now_msk().isoformat(timespec="seconds")


def _ensure_profile(conn, vk_id: int, granted_by: int, name: str | None) -> None:
    row = conn.execute(
        "SELECT vk_id FROM panel_users WHERE vk_id=?", (vk_id,)
    ).fetchone()
    if row:
        if name:
            conn.execute("UPDATE panel_users SET name=? WHERE vk_id=?", (name, vk_id))
        return
    conn.execute(
        "INSERT INTO panel_users (vk_id, role, name, added_at, added_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (vk_id, ROLE_ADMIN, name, _now(), granted_by),
    )


def grant_role(
    vk_id: int, role: str, granted_by: int, name: str | None = None
) -> None:
    """Добавляет роль (идемпотентно). Создаёт профиль при необходимости."""
    if role not in _ALL_ROLES:
        raise ValueError(f"неизвестная роль: {role}")
    with connect() as conn:
        _ensure_profile(conn, vk_id, granted_by, name)
        conn.execute(
            "INSERT OR IGNORE INTO panel_user_roles "
            "(vk_id, role, granted_at, granted_by) VALUES (?, ?, ?, ?)",
            (vk_id, role, _now(), granted_by),
        )


def revoke_role(vk_id: int, role: str) -> None:
    """Снимает одну роль. Профиль удаляется, если ролей не осталось."""
    if role not in _ALL_ROLES:
        raise ValueError(f"неизвестная роль: {role}")
    with connect() as conn:
        conn.execute(
            "DELETE FROM panel_user_roles WHERE vk_id=? AND role=?", (vk_id, role)
        )
        left = conn.execute(
            "SELECT 1 FROM panel_user_roles WHERE vk_id=? LIMIT 1", (vk_id,)
        ).fetchone()
        if not left:
            conn.execute("DELETE FROM panel_users WHERE vk_id=?", (vk_id,))


def grant(
    vk_id: int, granted_by: int, name: str | None = None, role: str = ROLE_ADMIN
) -> None:
    """Совместимость: выдаёт одну роль (как раньше grant)."""
    grant_role(vk_id, role, granted_by, name=name)


def revoke(vk_id: int) -> None:
    """Снимает права админа. Support сохраняется, если был."""
    with connect() as conn:
        conn.execute(
            "DELETE FROM panel_user_roles WHERE vk_id=? AND role=?",
            (vk_id, ROLE_ADMIN),
        )
        # Владельца этим путём не трогаем — только admin_revoke для не-owner.
        left = conn.execute(
            "SELECT 1 FROM panel_user_roles WHERE vk_id=? LIMIT 1", (vk_id,)
        ).fetchone()
        if not left:
            conn.execute("DELETE FROM panel_users WHERE vk_id=?", (vk_id,))


def set_role(vk_id: int, role: str) -> None:
    """Совместимость для снятия владения: owner → admin (owner убираем, admin оставляем)."""
    if role == ROLE_ADMIN:
        with connect() as conn:
            conn.execute(
                "DELETE FROM panel_user_roles WHERE vk_id=? AND role=?",
                (vk_id, ROLE_OWNER),
            )
            _ensure_profile(conn, vk_id, 0, None)
            conn.execute(
                "INSERT OR IGNORE INTO panel_user_roles "
                "(vk_id, role, granted_at, granted_by) VALUES (?, ?, ?, ?)",
                (vk_id, ROLE_ADMIN, _now(), 0),
            )
        return
    grant_role(vk_id, role, granted_by=0)


def roles_of(vk_id: int) -> set[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT role FROM panel_user_roles WHERE vk_id=?", (vk_id,)
        ).fetchall()
    return {r[0] for r in rows}


def has_role(vk_id: int, role: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM panel_user_roles WHERE vk_id=? AND role=? LIMIT 1",
            (vk_id, role),
        ).fetchone() is not None


def role_of(vk_id: int) -> str | None:
    """Главная роль для отображения: owner > admin > support > None."""
    roles = roles_of(vk_id)
    if ROLE_OWNER in roles:
        return ROLE_OWNER
    if ROLE_ADMIN in roles:
        return ROLE_ADMIN
    if ROLE_SUPPORT in roles:
        return ROLE_SUPPORT
    return None


def is_admin(vk_id: int) -> bool:
    """Доступ к админке: admin или owner (support сам по себе — нет)."""
    roles = roles_of(vk_id)
    return ROLE_ADMIN in roles or ROLE_OWNER in roles


def is_owner(vk_id: int) -> bool:
    return has_role(vk_id, ROLE_OWNER)


def is_support(vk_id: int) -> bool:
    return has_role(vk_id, ROLE_SUPPORT)


def owner_ids() -> set[int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT vk_id FROM panel_user_roles WHERE role=?", (ROLE_OWNER,)
        ).fetchall()
    return {int(r[0]) for r in rows}


def support_ids() -> set[int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT vk_id FROM panel_user_roles WHERE role=?", (ROLE_SUPPORT,)
        ).fetchall()
    return {int(r[0]) for r in rows}


def admin_ids() -> set[int]:
    """vk_id с ролью admin (без owner — их показывают отдельно)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT vk_id FROM panel_user_roles WHERE role=?", (ROLE_ADMIN,)
        ).fetchall()
    return {int(r[0]) for r in rows}


def list_all() -> list[dict]:
    """Профили с набором ролей (для панели)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT vk_id, name, added_at, added_by FROM panel_users "
            "ORDER BY added_at DESC"
        ).fetchall()
        role_rows = conn.execute(
            "SELECT vk_id, role FROM panel_user_roles"
        ).fetchall()
    roles_map: dict[int, set[str]] = {}
    for vid, role in role_rows:
        roles_map.setdefault(int(vid), set()).add(role)
    out = []
    for r in rows:
        vid = int(r[0])
        roles = roles_map.get(vid, set())
        out.append(
            {
                "vk_id": vid,
                "name": r[1],
                "added_at": r[2],
                "added_by": r[3],
                "roles": sorted(roles),
                # Совместимость со старыми шаблонами
                "role": role_of(vid) or "user",
            }
        )
    return out


def update_name(vk_id: int, name: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE panel_users SET name=? WHERE vk_id=?", (name, vk_id))
