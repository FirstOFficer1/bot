"""Персистентные состояния пользователей (для пошаговых диалогов).

Чтения идут из памяти, а записи — через фонового писателя, а не прямо из
корутины. Причина: состояние трогается на КАЖДОМ шаге любого диалога (в
хендлерах это под шестьдесят вызовов), бот однопоточный и асинхронный, а
`db.connect()` ставит `busy_timeout=5000`. Пока запись шла синхронно, занятая
база останавливала не свой диалог, а разбор сообщений целиком — весь бот.

Такой подход чинит все вызывающие места сразу и не требует `await` на каждом
`store[uid] = ...`: в памяти состояние обновляется мгновенно, а SQLite догоняет.

Плата — долговечность: при жёстком падении процесса теряются записи, которые
ещё не разобраны из очереди (миллисекунды). Для состояния диалога это приемлемо
— человек повторит последнее нажатие; ради этого не стоит держать весь бот.
Очередь разбирает один поток, поэтому порядок операций сохраняется.
"""

from __future__ import annotations

import atexit
import json
import logging
import queue
import threading
import time
from typing import Any

from .db import connect

_SET = "set"
_DELETE = "delete"


class StateStore:
    """In-memory кэш + отложенная persistence в SQLite таблице user_states."""

    #: сколько ждать разбора очереди в flush(); дальше запись догоняет сама
    _flush_timeout = 5.0
    #: повторов записи, прежде чем сдаться и записать в лог
    _write_attempts = 3

    def __init__(self) -> None:
        self._data: dict[int, Any] = {}
        self._queue: queue.Queue[tuple[str, int, str | None]] = queue.Queue()
        self._writer: threading.Thread | None = None
        self._writer_lock = threading.Lock()

    # ── фоновая запись ───────────────────────────────────────────────────────
    def _ensure_writer(self) -> None:
        """Поднимает писателя лениво и заново, если прошлый почему-то умер."""
        with self._writer_lock:
            if self._writer is not None and self._writer.is_alive():
                return
            self._writer = threading.Thread(
                target=self._drain, name="state-writer", daemon=True
            )
            self._writer.start()

    def _drain(self) -> None:
        while True:
            kind, uid, payload = self._queue.get()
            try:
                self._apply_with_retries(kind, uid, payload)
            finally:
                # Поток обязан пережить сбойную запись: иначе одна занятая база
                # молча похоронила бы persistence до перезапуска сервиса. И
                # task_done() обязателен в любом случае, иначе flush() ждал бы
                # операцию, которой уже никто не занимается.
                self._queue.task_done()

    def _apply_with_retries(self, kind: str, uid: int, payload: str | None) -> None:
        """Пробует записать несколько раз: «database is locked» обычно проходит.

        Исключение наружу не уходит — вызывающий давно вернулся. Если не вышло
        и с повторами, в памяти остаётся верное состояние, а в базе — старое;
        это видно в логах и переживается: после перезапуска диалог откатится
        на шаг, а не сломается.
        """
        for attempt in range(1, self._write_attempts + 1):
            try:
                self._apply(kind, uid, payload)
                return
            except Exception:
                if attempt == self._write_attempts:
                    logging.exception(
                        "Состояние uid=%s не сохранено за %s попыток", uid, attempt
                    )
                    return
                time.sleep(0.05 * attempt)

    def _apply(self, kind: str, uid: int, payload: str | None) -> None:
        with connect() as conn:
            if kind == _SET:
                conn.execute(
                    "INSERT OR REPLACE INTO user_states (user_id, state_json) VALUES (?,?)",
                    (uid, payload),
                )
            else:
                conn.execute("DELETE FROM user_states WHERE user_id=?", (uid,))

    def _enqueue(self, kind: str, uid: int, payload: str | None = None) -> None:
        self._queue.put((kind, uid, payload))
        self._ensure_writer()

    def flush(self) -> None:
        """Дожидается разбора очереди.

        Нужно там, где состояние обязано оказаться в базе прямо сейчас: удаление
        своих данных (152-ФЗ) и тесты, которые иначе ловили бы чужие записи,
        прилетевшие после очистки таблиц.
        """
        # unfinished_tasks, а не empty(): get() забирает операцию из очереди до
        # того, как она записана, поэтому пустая очередь ещё не значит, что
        # писатель закончил. Счётчик уменьшается только на task_done().
        if self._queue.unfinished_tasks == 0:
            return
        try:
            self._ensure_writer()
        except RuntimeError:
            # atexit на выходе из интерпретатора: новых потоков уже не создать,
            # поэтому дописываем очередь прямо здесь. Молча потерять последние
            # записи хуже — человек увидел бы диалог откатившимся на шаг назад.
            self._drain_remaining()
            return
        # queue.join() не умеет тайм-аут, а ждать бесконечно нельзя: очередь
        # общая, и застрявший на заблокированной базе писатель подвесил бы
        # вызывающего навсегда. Лучше вернуться и оставить запись догоняющей.
        deadline = time.monotonic() + self._flush_timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                logging.warning(
                    "Очередь состояний не разобрана за %s с — записи догонят позже",
                    self._flush_timeout,
                )
                return
            time.sleep(0.005)

    def _drain_remaining(self) -> None:
        while True:
            try:
                kind, uid, payload = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._apply(kind, uid, payload)
            except Exception:
                logging.exception("Не удалось сохранить состояние uid=%s", uid)
            finally:
                self._queue.task_done()

    # ── API ──────────────────────────────────────────────────────────────────
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
        self._enqueue(_SET, uid, json.dumps(state, ensure_ascii=False))

    def pop(self, uid: int, *args: Any) -> Any:
        result = self._data.pop(uid, *args)
        self._enqueue(_DELETE, uid)
        return result

    def patch(self, uid: int, **kwargs: Any) -> dict:
        """Мерджит словарь-состояние с новыми полями и сохраняет."""
        state = dict(self._data.get(uid) or {})
        state.update(kwargs)
        self[uid] = state
        return state


# Глобальный синглтон — импортируется хендлерами
store = StateStore()

# При штатной остановке (systemd шлёт SIGINT) успеваем дописать очередь.
atexit.register(store.flush)
