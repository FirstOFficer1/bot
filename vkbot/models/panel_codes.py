"""Одноразовые коды для входа в веб-панель через ВК-бота."""

from __future__ import annotations

import collections
import secrets
import threading
import time
from datetime import timedelta

from ..config import now_msk

from ..db import connect

CODE_TTL_MIN = 10
CODE_LEN = 6

# ── Rate-limit для /login/code: брутфорс 6-значных кодов ──────────────────────
# Sliding window: не более RL_MAX_ATTEMPTS неудачных попыток за RL_WINDOW_SEC
# с одного IP. Атакующему остаётся 10 кодов / 10 мин = эффективно нереально
# подобрать (1М комбинаций × 10 мин = ~190 лет).
RL_MAX_ATTEMPTS = 10
RL_WINDOW_SEC = 600  # 10 минут

# Глобальный circuit-breaker — если ВСЯ система получила >GLOBAL_MAX_FAILS
# неудачных попыток за окно, временно блокируем любые проверки кода.
# Защищает от ботнета с разных IP, обходящих per-IP лимит.
GLOBAL_MAX_FAILS = 100
GLOBAL_WINDOW_SEC = 600
_GLOBAL_FAILS: collections.deque[float] = collections.deque()


_FAILED_ATTEMPTS: dict[str, collections.deque[float]] = {}
_RL_LOCK = threading.Lock()


def _prune(dq: collections.deque[float], now: float) -> None:
    cutoff = now - RL_WINDOW_SEC
    while dq and dq[0] < cutoff:
        dq.popleft()


def is_rate_limited(ip: str) -> bool:
    """True если у этого IP уже >= RL_MAX_ATTEMPTS провалов за окно."""
    if not ip:
        return False
    now = time.time()
    with _RL_LOCK:
        dq = _FAILED_ATTEMPTS.get(ip)
        if not dq:
            return False
        _prune(dq, now)
        return len(dq) >= RL_MAX_ATTEMPTS


def record_failure(ip: str) -> None:
    """Логирует один провал. Чистим устаревшие записи."""
    if not ip:
        return
    now = time.time()
    with _RL_LOCK:
        dq = _FAILED_ATTEMPTS.setdefault(ip, collections.deque())
        dq.append(now)
        _prune(dq, now)
        # Защита от ddos памятью: cap на размер очереди
        while len(dq) > RL_MAX_ATTEMPTS * 2:
            dq.popleft()
        # Глобальный счётчик
        _GLOBAL_FAILS.append(now)
        cutoff = now - GLOBAL_WINDOW_SEC
        while _GLOBAL_FAILS and _GLOBAL_FAILS[0] < cutoff:
            _GLOBAL_FAILS.popleft()


def cleanup_rate_limits() -> int:
    """Удаляет полностью пустые/устаревшие записи. Возвращает кол-во очищенных IP."""
    now = time.time()
    removed = 0
    with _RL_LOCK:
        for ip in list(_FAILED_ATTEMPTS.keys()):
            dq = _FAILED_ATTEMPTS[ip]
            _prune(dq, now)
            if not dq:
                del _FAILED_ATTEMPTS[ip]
                removed += 1
    return removed


def _now_iso() -> str:
    return now_msk().isoformat(timespec="seconds")


def _generate() -> str:
    """6-значный код из цифр (читабельно в боте)."""
    return "".join(secrets.choice("0123456789") for _ in range(CODE_LEN))


def issue(user_id: int) -> tuple[str, int]:
    """Генерирует код для пользователя. Старые неиспользованные — инвалидирует.

    Возвращает (code, ttl_minutes).
    """
    code = _generate()
    with connect() as conn:
        # Помечаем старые коды как использованные
        conn.execute(
            "UPDATE panel_login_codes SET used=1 WHERE user_id=? AND used=0",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO panel_login_codes (code, user_id, created_at, used) "
            "VALUES (?,?,?,0)",
            (code, user_id, _now_iso()),
        )
    return code, CODE_TTL_MIN


def verify(code: str) -> int | None:
    """Проверяет код, помечает использованным, возвращает user_id или None."""
    if not code or len(code) != CODE_LEN or not code.isdigit():
        return None
    cutoff = (now_msk() - timedelta(minutes=CODE_TTL_MIN)).isoformat(
        timespec="seconds"
    )
    with connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM panel_login_codes "
            "WHERE code=? AND used=0 AND created_at >= ?",
            (code, cutoff),
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE panel_login_codes SET used=1 WHERE code=?", (code,))
        return int(row[0])


def cleanup_old(days: int = 1) -> int:
    """Удаляет коды старше N дней. Возвращает количество удалённых."""
    cutoff = (now_msk() - timedelta(days=days)).isoformat(timespec="seconds")
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM panel_login_codes WHERE created_at < ?",
            (cutoff,),
        )
        return cur.rowcount

def is_globally_locked() -> bool:
    """True если суммарно по всем IP >= GLOBAL_MAX_FAILS за окно."""
    now = time.time()
    cutoff = now - GLOBAL_WINDOW_SEC
    with _RL_LOCK:
        while _GLOBAL_FAILS and _GLOBAL_FAILS[0] < cutoff:
            _GLOBAL_FAILS.popleft()
        return len(_GLOBAL_FAILS) >= GLOBAL_MAX_FAILS
