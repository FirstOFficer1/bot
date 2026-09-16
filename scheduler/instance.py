"""Входные данные для составления расписания.

Здесь живёт то, чего в боевой базе нет: расписание в `schedule` — это уже
готовый *результат*, а солверу нужен *вход*. Поэтому инстанс собирается из
текущего расписания обратной разработкой: занятия, преподаватели и аудитории
там есть, а численность групп и вместимость аудиторий синтезируются
детерминированно (см. `synth.py`), пока их не привезут из деканата.

Отдельная и главная работа этого модуля — нормализация. В выгрузке 60 разных
строк-аудиторий, но комнат заметно меньше: одна и та же записана как
«5 корп 220», «5 уч.к 220» и «5 уч.к. 220». Для солвера это три разные
комнаты, и он спокойно посадит туда три группы одновременно — ошибка, которую
никто не заметит до сентября.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]

#: Пары в порядке начала. «12.00 - 13.29» — систематическая опечатка исходника
#: (20 строк, 16 направлений), это та же третья пара, что и «12.00 - 13.30».
PERIODS = ["8.15", "9.55", "12.00", "13.40", "15.20", "17.00"]

_PERIOD_BY_START = {p: i for i, p in enumerate(PERIODS)}

#: Псевдопара: место в сетке занимает, аудиторию и преподавателя — нет.
SELF_STUDY = "день самостоятельной работы"

# Типы аудиторий. Определяются по тому, как комнату используют сегодня:
# если в ней хоть раз шла лабораторная — значит, она оснащена.
KIND_GYM = "спортзал"
KIND_LAB = "лаборатория"
KIND_LECTURE = "лекционная"
KIND_PLAIN = "обычная"


@dataclass(frozen=True)
class Room:
    id: str
    building: str        # "гл" | "5"
    number: str
    kind: str
    capacity: int


@dataclass(frozen=True)
class Group:
    id: str
    course: int
    direction: str
    size: int


@dataclass(frozen=True)
class Teacher:
    id: str
    name: str
    #: слоты (день, пара), в которые преподаватель недоступен
    unavailable: frozenset[tuple[int, int]] = frozenset()


@dataclass(frozen=True)
class Meeting:
    """Одно занятие, которое надо поставить в сетку ровно один раз."""

    id: int
    #: групп может быть несколько — лекция читается потоком. Связанные группы
    #: обязаны стоять в одном слоте, иначе поток разъедется по неделе.
    groups: tuple[str, ...]
    subject: str
    teacher: str
    class_type: str      # лк | пр | лб | ""
    parity: str          # "" (каждую неделю) | "чёт" | "нечет"
    room_kind: str
    #: где занятие стоит в исходнике — чтобы сравнивать решение с фактом
    origin: tuple[int, int] | None = None   # (день, пара)
    origin_room: str | None = None


@dataclass
class Instance:
    groups: dict[str, Group] = field(default_factory=dict)
    teachers: dict[str, Teacher] = field(default_factory=dict)
    rooms: dict[str, Room] = field(default_factory=dict)
    meetings: list[Meeting] = field(default_factory=list)
    days: list[str] = field(default_factory=lambda: list(DAYS))
    periods: list[str] = field(default_factory=lambda: list(PERIODS))
    #: занятия, выброшенные при разборе, с причиной — для честного отчёта
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: что пришлось досинтезировать: этих данных в базе нет, и выдавать их
    #: за настоящие нельзя
    notes: list[str] = field(default_factory=list)

    def rooms_of_kind(self, kind: str) -> list[Room]:
        """Комнаты, пригодные под тип занятия (лекционная годится под всё)."""
        return [r for r in self.rooms.values() if _fits(r.kind, kind)]


def _fits(room_kind: str, need: str) -> bool:
    if need == KIND_GYM:
        return room_kind == KIND_GYM
    if room_kind == KIND_GYM:
        return False
    if need == KIND_LAB:
        return room_kind == KIND_LAB
    return True


# ── нормализация ─────────────────────────────────────────────────────────────

_SUBGROUP_RE = re.compile(r"\(\s*(\d)\s*подгруппа\s*\)", re.IGNORECASE)
_FIFTH_RE = re.compile(r"5\s*(?:корп\.?|уч\.?\s*к\.?)\s*", re.IGNORECASE)
_MAIN_RE = re.compile(r"^гл\.?\s*уч\.?\s*к\.?\s*$", re.IGNORECASE)


def normalize_room(raw: str) -> tuple[str | None, int | None]:
    """Строку аудитории → (канонический id, номер подгруппы).

    Возвращает (None, ...) для пустой строки: в исходнике 145 таких занятий.
    """
    text = (raw or "").strip()
    if not text:
        return None, None

    sub = None
    m = _SUBGROUP_RE.search(text)
    if m:
        sub = int(m.group(1))
        text = _SUBGROUP_RE.sub("", text).strip()

    if _MAIN_RE.match(text):
        return "гл:спортзал", sub

    # «5 уч.к 101а по 5 корп 220» — в одном поле две комнаты. Берём первую:
    # вторая означает перенос по датам, а не одновременное занятие в двух.
    text = re.split(r"\s+по\s+", text, maxsplit=1)[0]

    building = "гл"
    if _FIFTH_RE.search(text):
        building = "5"
        # Префикс бывает задвоен: «5 уч.к 5 уч.к 109».
        while _FIFTH_RE.search(text):
            text = _FIFTH_RE.sub("", text, count=1).strip()

    number = text.strip().lower().replace(" ", "")
    if not number:
        return None, sub
    return f"{building}:{number}", sub


def normalize_time(raw: str) -> int | None:
    """«9.55 - 11.25» → индекс пары. Ориентируемся только на начало."""
    start = (raw or "").split("-")[0].strip()
    return _PERIOD_BY_START.get(start)


def normalize_class_type(raw: str) -> str:
    value = (raw or "").strip().lower()
    return "лк" if value == "лекция" else value


def group_id(course: int, direction: str) -> str:
    return f"{course}:{direction.strip().lower()}"


def teacher_id(name: str) -> str:
    """Схлопывает «Матвеева Н.А.» и «Матвеева Н. А.» в одного человека."""
    return re.sub(r"\s+", "", (name or "").strip().lower())
