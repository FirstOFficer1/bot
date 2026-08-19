"""CRUD для админов и владельцев веб-панели.

Ролей две: `admin` и `owner`. Владельцы бывают двух видов:

* **из env** (`ADMIN_ID` / `ADMIN_VK_ID` / `ADMIN_VK_IDS`) — несменяемый якорь.
  Панель их не выдаёт и не снимает, только сервер. Это то, что нельзя отобрать
  через угнанную веб-сессию: максимум, что успеет злоумышленник, — добавить
  совладельца, и это видно в аудите и снимается одной кнопкой.
* **из этой таблицы** (`role='owner'`) — их владелец выдаёт и снимает прямо в
  панели, с подтверждением одноразовым кодом из бота.

Здесь живут только вторые: про env знает `web_panel`, он же объединяет оба
множества в `_is_owner()`.
"""

from __future__ import annotations


from ..config import now_msk

from ..db import connect

ROLE_ADMIN = "admin"
ROLE_OWNER = "owner"


def _now() -> str:
    return now_msk().isoformat(timespec="seconds")


def grant(
    vk_id: int, granted_by: int, name: str | None = None, role: str = ROLE_ADMIN
) -> None:
    """Выдаёт роль. Повторная выдача перезаписывает запись (в т.ч. повышает роль)."""
    if role not in (ROLE_ADMIN, ROLE_OWNER):
        raise ValueError(f"неизвестная роль: {role}")
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO panel_users (vk_id, role, name, added_at, added_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (vk_id, role, name, _now(), granted_by),
        )


def revoke(vk_id: int) -> None:
    """Убирает пользователя из панели совсем (и админа, и владельца)."""
    with connect() as conn:
        conn.execute("DELETE FROM panel_users WHERE vk_id=?", (vk_id,))


def set_role(vk_id: int, role: str) -> None:
    """Меняет роль существующей записи, не трогая остальные поля."""
    if role not in (ROLE_ADMIN, ROLE_OWNER):
        raise ValueError(f"неизвестная роль: {role}")
    with connect() as conn:
        conn.execute("UPDATE panel_users SET role=? WHERE vk_id=?", (role, vk_id))


def role_of(vk_id: int) -> str | None:
    """Роль пользователя в таблице или None, если его там нет."""
    with connect() as conn:
        row = conn.execute(
            "SELECT role FROM panel_users WHERE vk_id=?", (vk_id,)
        ).fetchone()
    return row[0] if row else None


def is_admin(vk_id: int) -> bool:
    """Есть ли вообще доступ к админке (владелец из таблицы — тоже админ)."""
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM panel_users WHERE vk_id=?", (vk_id,)
        ).fetchone() is not None


def is_owner(vk_id: int) -> bool:
    """Владелец ли — по этой таблице. Владельцев из env здесь нет."""
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM panel_users WHERE vk_id=? AND role=?", (vk_id, ROLE_OWNER)
        ).fetchone() is not None


def owner_ids() -> set[int]:
    """VK ID владельцев из таблицы (без env-владельцев)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT vk_id FROM panel_users WHERE role=?", (ROLE_OWNER,)
        ).fetchall()
    return {int(r[0]) for r in rows}


def list_all() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT vk_id, role, name, added_at, added_by "
            "FROM panel_users ORDER BY added_at DESC"
        ).fetchall()
    return [
        {
            "vk_id": r[0],
            "role": r[1],
            "name": r[2],
            "added_at": r[3],
            "added_by": r[4],
        }
        for r in rows
    ]


def update_name(vk_id: int, name: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE panel_users SET name=? WHERE vk_id=?", (name, vk_id))
