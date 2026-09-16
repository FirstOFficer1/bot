"""Одноразовые коды для входа в веб-панель через ВК-бота.

Два назначения (purpose), чтобы код входа нельзя было использовать как
step-up для передачи владения и наоборот:

* ``login`` — вход на сайт (команда /login в боте);
* ``step_up`` — подтверждение опасной операции (команда /confirm).
"""

from __future__ import annotations

import time
from datetime import timedelta

from ..config import now_msk
from ..db import connect

CODE_TTL_MIN = 10
CODE_LEN = 8

PURPOSE_LOGIN = "login"
PURPOSE_STEP_UP = "step_up"
_PURPOSES = frozenset({PURPOSE_LOGIN, PURPOSE_STEP_UP})

# Sliding window: не более RL_MAX_ATTEMPTS неудачных попыток за RL_WINDOW_SEC
# с одного IP. Счётчик в SQLite — переживает рестарт панели.
RL_MAX_ATTEMPTS = 10
RL_WINDOW_SEC = 600  # 10 минут

# Глобальный circuit-breaker: >GLOBAL_MAX_FAILS неудач за окно по всем IP.
GLOBAL_MAX_FAILS = 100
GLOBAL_WINDOW_SEC = 600


def _now_iso() -> str:
    return now_msk().isoformat(timespec="seconds")


def _generate() -> str:
    """N-значный код из цифр (читабельно в боте)."""
    import secrets

    return "".join(secrets.choice("0123456789") for _ in range(CODE_LEN))


def issue(user_id: int, *, purpose: str = PURPOSE_LOGIN) -> tuple[str, int]:
    """Генерирует код для пользователя. Старые неиспользованные того же
    назначения — инвалидирует.

    Возвращает (code, ttl_minutes).
    """
    if purpose not in _PURPOSES:
        raise ValueError(f"неизвестное назначение кода: {purpose}")
    code = _generate()
    with connect() as conn:
        conn.execute(
            "UPDATE panel_login_codes SET used=1 "
            "WHERE user_id=? AND used=0 AND purpose=?",
            (user_id, purpose),
        )
        conn.execute(
            "INSERT INTO panel_login_codes (code, user_id, created_at, used, purpose) "
            "VALUES (?,?,?,0,?)",
            (code, user_id, _now_iso(), purpose),
        )
    return code, CODE_TTL_MIN


def verify(code: str, *, purpose: str = PURPOSE_LOGIN) -> int | None:
    """Проверяет код нужного назначения, помечает использованным, возвращает
    user_id или None.

    Гашение и проверка — один UPDATE: иначе два параллельных запроса могли
    прочитать один непогашенный код оба.
    """
    if purpose not in _PURPOSES:
        return None
    if not code or len(code) != CODE_LEN or not code.isdigit():
        return None
    cutoff = (now_msk() - timedelta(minutes=CODE_TTL_MIN)).isoformat(
        timespec="seconds"
    )
    with connect() as conn:
        rows = conn.execute(
            "UPDATE panel_login_codes SET used=1 "
            "WHERE code=? AND used=0 AND created_at >= ? AND purpose=? "
            "RETURNING user_id",
            (code, cutoff, purpose),
        ).fetchall()
    return int(rows[0][0]) if rows else None


def has_recent_code(user_id: int, minutes: int = 60) -> bool:
    """Выписывался ли этому пользователю код за последние N минут."""
    cutoff = (now_msk() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM panel_login_codes WHERE user_id=? AND created_at >= ? LIMIT 1",
            (user_id, cutoff),
        ).fetchone() is not None


def cleanup_old(days: int = 1) -> int:
    """Удаляет коды старше N дней. Возвращает количество удалённых."""
    cutoff = (now_msk() - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM panel_login_codes WHERE created_at < ?",
            (cutoff,),
        )
        return cur.rowcount


def is_rate_limited(ip: str) -> bool:
    """True если у этого IP уже >= RL_MAX_ATTEMPTS провалов за окно."""
    if not ip:
        return False
    cutoff = time.time() - RL_WINDOW_SEC
    with connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM login_failures WHERE ip=? AND failed_at >= ?",
            (ip, cutoff),
        ).fetchone()[0]
    return int(n) >= RL_MAX_ATTEMPTS


def record_failure(ip: str) -> None:
    """Логирует один провал входа."""
    if not ip:
        return
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO login_failures (ip, failed_at) VALUES (?, ?)",
            (ip, now),
        )
        # Не даём таблице расти без меры: чистим всё старше окна × 2.
        conn.execute(
            "DELETE FROM login_failures WHERE failed_at < ?",
            (now - RL_WINDOW_SEC * 2,),
        )


def cleanup_rate_limits() -> int:
    """Удаляет устаревшие записи о провалах. Возвращает кол-во удалённых."""
    cutoff = time.time() - RL_WINDOW_SEC * 2
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM login_failures WHERE failed_at < ?",
            (cutoff,),
        )
        return cur.rowcount or 0


def is_globally_locked() -> bool:
    """True если суммарно по всем IP >= GLOBAL_MAX_FAILS за окно."""
    cutoff = time.time() - GLOBAL_WINDOW_SEC
    with connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM login_failures WHERE failed_at >= ?",
            (cutoff,),
        ).fetchone()[0]
    return int(n) >= GLOBAL_MAX_FAILS
