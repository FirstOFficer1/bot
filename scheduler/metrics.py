"""Измерение качества расписания.

Все числа — за полный цикл из двух недель (чётная плюс нечётная). Так их можно
складывать и сравнивать между собой: пара «раз в две недели» даёт вклад один
раз, еженедельная — два, и это ровно то, что чувствует на себе человек.

Без этих чисел «эффективно» — пустое слово: любой вариант выглядит разумным,
пока его не с чем сравнить. Здесь же считается качество **текущего** расписания
вуза (`baseline`), и это главный ориентир: помощник имеет смысл ровно
настолько, насколько он обходит то, что уже составлено руками.
"""

from __future__ import annotations

from dataclasses import dataclass

from .instance import Instance
from .model import PARITIES, Solution, _serves


@dataclass
class Metrics:
    окна_групп: int = 0
    окна_преподов: int = 0
    дни_преподов: int = 0
    перегруженных_дней: int = 0
    макс_пар_в_день: int = 0
    поздних_пар: int = 0
    перебежек: int = 0
    кучность: int = 0

    def as_row(self) -> dict[str, int]:
        return dict(self.__dict__)


def _windows(busy_periods: list[int]) -> int:
    if len(busy_periods) < 2:
        return 0
    lo, hi = min(busy_periods), max(busy_periods)
    return (hi - lo + 1) - len(busy_periods)


def evaluate(inst: Instance, sol: Solution, *, day_cap: int = 4) -> Metrics:
    m = Metrics()
    last_period = len(inst.periods) - 1

    # Занятость считаем отдельно по чётности: пара «раз в две недели» не создаёт
    # окна на той неделе, когда её нет.
    by_group: dict[tuple[str, int, str], list[int]] = {}
    by_teacher: dict[tuple[str, int, str], list[int]] = {}
    for meet in inst.meetings:
        if meet.id not in sol.times:
            continue
        d, p = sol.times[meet.id]
        for w in PARITIES:
            if not _serves(meet, w):
                continue
            for g in meet.groups:
                by_group.setdefault((g, d, w), []).append(p)
            by_teacher.setdefault((meet.teacher, d, w), []).append(p)

    for (_g, _d, _w), periods in by_group.items():
        uniq = sorted(set(periods))
        m.окна_групп += _windows(uniq)
        m.макс_пар_в_день = max(m.макс_пар_в_день, len(uniq))
        if len(uniq) > day_cap:
            m.перегруженных_дней += 1
        if last_period in uniq:
            m.поздних_пар += 1

    for (_t, _d, _w), periods in by_teacher.items():
        m.окна_преподов += _windows(sorted(set(periods)))

    # Дни присутствия — как и всё остальное здесь, за полный двухнедельный
    # цикл. Делить пополам заманчиво, но тогда одна метрика считалась бы за
    # неделю, а окна рядом — за две, и таблица врала бы при сравнении.
    m.дни_преподов = len(by_teacher)

    # Кучность: одна дисциплина у одной группы дважды и более в один день.
    # Считаем по чётности: лекция «по чётным» и практика «по нечётным» стоят в
    # один день недели, но в жизни это разные дни, и кучностью не являются.
    clumps: dict[tuple[str, str, int, str], int] = {}
    for meet in inst.meetings:
        if meet.id not in sol.times:
            continue
        d, _p = sol.times[meet.id]
        for w in PARITIES:
            if not _serves(meet, w):
                continue
            for g in meet.groups:
                key = (g, meet.subject, d, w)
                clumps[key] = clumps.get(key, 0) + 1
    m.кучность = sum(v - 1 for v in clumps.values() if v > 1)

    m.перебежек = transitions(inst, sol)
    return m


def transitions(inst: Instance, sol: Solution) -> int:
    """Переходы группы между корпусами на соседних парах одного дня.

    Считаем отдельно по чётности: пара «по чётным» и пара «по нечётным» в один
    день недели в жизни не соседствуют, и перебежкой между ними быть не может.
    Еженедельная пара даёт вклад на обеих неделях — как и остальные метрики.
    """
    if not sol.rooms:
        return 0
    per_day: dict[tuple[str, int, str], list[tuple[int, str]]] = {}
    for meet in inst.meetings:
        if meet.id not in sol.times or meet.id not in sol.rooms:
            continue
        d, p = sol.times[meet.id]
        building = inst.rooms[sol.rooms[meet.id]].building
        for w in PARITIES:
            if not _serves(meet, w):
                continue
            for g in meet.groups:
                per_day.setdefault((g, d, w), []).append((p, building))

    total = 0
    for items in per_day.values():
        items.sort()
        for (p1, b1), (p2, b2) in zip(items, items[1:]):
            if p2 - p1 <= 2 and b1 != b2:
                total += 1
    return total


def baseline(inst: Instance) -> Solution:
    """Текущее расписание вуза как решение — чтобы было с чем сравнивать."""
    sol = Solution(status="ФАКТ")
    for meet in inst.meetings:
        if meet.origin is not None:
            sol.times[meet.id] = meet.origin
        if meet.origin_room is not None:
            sol.rooms[meet.id] = meet.origin_room
    return sol
