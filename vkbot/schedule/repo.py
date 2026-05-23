"""Репозиторий расписания: чтение из БД, кэширование направлений, hot-reload."""

from __future__ import annotations

import logging
import re
import threading

from .. import config
from ..db import connect
from .shortener import shorten_direction


class ScheduleRepo:
    """Кэширует список курсов/направлений и их короткие подписи.

    Hot-reload: после изменения файла-маркера `config.SCHEDULE_RELOAD_MARKER`
    или прямого вызова `reload()` кэш перестраивается.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._directions_by_course: dict[int, list[str]] = {}
        self._dir_label_to_full: dict[int, dict[str, str]] = {}
        self._reload_marker_mtime: float = 0.0

    # ── публичный API ────────────────────────────────────────────────────────
    @property
    def directions_by_course(self) -> dict[int, list[str]]:
        with self._lock:
            return dict(self._directions_by_course)

    def label_to_full(self, course: int) -> dict[str, str]:
        with self._lock:
            return dict(self._dir_label_to_full.get(course, {}))

    def resolve_direction(self, text: str, course: int) -> str | None:
        with self._lock:
            return self._dir_label_to_full.get(course, {}).get(text)

    def reload(self) -> None:
        """Перечитывает направления из БД и пересобирает label-map."""
        with self._lock:
            self._load()
            logging.info(
                "ScheduleRepo reloaded: %d courses, %d directions total",
                len(self._directions_by_course),
                sum(len(v) for v in self._directions_by_course.values()),
            )

    def check_reload_marker(self) -> bool:
        """Возвращает True, если файл-маркер обновился и мы перечитали кэш."""
        try:
            mtime = config.SCHEDULE_RELOAD_MARKER.stat().st_mtime
        except FileNotFoundError:
            return False
        if mtime > self._reload_marker_mtime:
            self._reload_marker_mtime = mtime
            self.reload()
            return True
        return False

    # ── внутреннее ────────────────────────────────────────────────────────────
    def _load(self) -> None:
        with connect(config.SCHEDULE_DB) as conn:
            rows = conn.execute(
                "SELECT DISTINCT course, direction FROM schedule ORDER BY course"
            ).fetchall()

        result: dict[int, list[str]] = {}
        for course, direction in rows:
            cleaned = re.sub(r",?\s*\d+\s*курс.*", "", direction).strip()
            result.setdefault(course, [])
            if cleaned not in result[course]:
                result[course].append(cleaned)

        labels: dict[int, dict[str, str]] = {}
        for course, dirs in result.items():
            mapping: dict[str, str] = {}
            used: set[str] = set()
            for d in dirs:
                label = shorten_direction(d)
                if label in used:
                    label = d[:40]
                used.add(label)
                mapping[label] = d
            labels[course] = mapping

        self._directions_by_course = result
        self._dir_label_to_full = labels

    # ── расписание конкретного дня ────────────────────────────────────────────
    def get_day(self, course: int, direction: str, day: str, week_type: str) -> str:
        with connect(config.SCHEDULE_DB) as conn:
            rows = conn.execute(
                """
                SELECT time, subject, teacher, room, class_type, date_range FROM schedule
                WHERE course = ? AND LOWER(direction) = LOWER(?) AND LOWER(day) = LOWER(?)
                  AND (week = '' OR week = ?)
                ORDER BY CAST(SUBSTR(time, 1, INSTR(time, ' ') - 1) AS INTEGER), time
                """,
                (course, direction, day, week_type),
            ).fetchall()

        if not rows:
            return "На этот день пар нет."

        lines = []
        for time_s, subject, teacher, room, class_type, date_range in rows:
            tags = [p for p in (class_type, date_range) if p]
            subj_str = f"{subject} [{', '.join(tags)}]" if tags else subject
            info = ", ".join(filter(None, [teacher, room]))
            lines.append(f"{time_s} — {subj_str}" + (f" ({info})" if info else ""))
        return "\n".join(lines)


# Глобальный синглтон. `bot.main()` вызывает repo.reload() при старте.
repo = ScheduleRepo()


def signal_reload() -> None:
    """Вызывается извне (например, веб-панелью) после успешного импорта.

    Касается файла-маркера; бот заметит изменение mtime в воркере.
    """
    config.SCHEDULE_RELOAD_MARKER.touch()
