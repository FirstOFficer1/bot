"""Персистентные состояния пользователей (для пошаговых диалогов)."""

from __future__ import annotations

import json
from typing import Any

from .db import connect


class StateStore:
    """In-memory кэш + persistence в SQLite таблице user_states."""

    def __init__(self) -> None:
        self._data: dict[int, Any] = {}

    def load_all(self) -> None:
        with connect() as conn:
            rows = conn.execute("SELECT user_id, state_json FROM user_states").fetchall()
        for uid, json_str in rows:
            try:
                self._data[uid] = json.loads(json_str)
            except Exception:
                pass

    def get(self, uid: int, default: Any = None) -> Any:
        return self._data.get(uid, default)

    def __getitem__(self, uid: int) -> Any:
        return self._data[uid]

    def __setitem__(self, uid: int, state: Any) -> None:
        self._data[uid] = state
        with connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_states (user_id, state_json) VALUES (?,?)",
                (uid, json.dumps(state, ensure_ascii=False)),
            )

    def pop(self, uid: int, *args: Any) -> Any:
        result = self._data.pop(uid, *args)
        with connect() as conn:
            conn.execute("DELETE FROM user_states WHERE user_id=?", (uid,))
        return result

    def patch(self, uid: int, **kwargs: Any) -> dict:
        """Мерджит словарь-состояние с новыми полями и сохраняет."""
        state = dict(self._data.get(uid) or {})
        state.update(kwargs)
        self[uid] = state
        return state


# Глобальный синглтон — импортируется хендлерами
store = StateStore()
