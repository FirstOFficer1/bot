"""Проверка решения на жёсткие ограничения.

Красивая таблица метрик ничего не стоит, пока не доказано, что расписание
вообще действительное. Солверу мы верим, но ошибка живёт не в нём, а в том,
как мы задали модель: забытое ограничение выглядит как прекрасный результат.
Поэтому решение проверяется независимым кодом, который ничего не знает про
CP-SAT и просто пересчитывает всё по определению.
"""

from __future__ import annotations

from .instance import Instance
from .model import PARITIES, Solution, _serves, compatible_rooms


def violations(inst: Instance, sol: Solution, *, check_rooms: bool = True,
               require_rooms: bool = True) -> list[str]:
    """Возвращает список нарушений. Пустой список — расписание действительное.

    `require_rooms=False` для текущего расписания вуза: там у 13 занятий
    аудитория в базе просто не указана, и это пробел в данных, а не конфликт.
    """
    bad: list[str] = []

    missing = [m.id for m in inst.meetings if m.id not in sol.times]
    if missing:
        bad.append(f"не расставлено занятий: {len(missing)}")

    for w in PARITIES:
        busy_group: dict[tuple[str, int, int], str] = {}
        busy_teacher: dict[tuple[str, int, int], str] = {}
        busy_room: dict[tuple[str, int, int], str] = {}

        for m in inst.meetings:
            if m.id not in sol.times or not _serves(m, w):
                continue
            d, p = sol.times[m.id]

            for g in m.groups:
                key = (g, d, p)
                if key in busy_group:
                    bad.append(
                        f"группа {g} занята дважды: {inst.days[d]}, "
                        f"{inst.periods[p]}, неделя {w} "
                        f"({busy_group[key]} и {m.subject})"
                    )
                busy_group[key] = m.subject

            key = (m.teacher, d, p)
            if key in busy_teacher:
                bad.append(
                    f"преподаватель {inst.teachers[m.teacher].name} занят дважды: "
                    f"{inst.days[d]}, {inst.periods[p]}, неделя {w} "
                    f"({busy_teacher[key]} и {m.subject})"
                )
            busy_teacher[key] = m.subject

            if check_rooms and m.id in sol.rooms:
                rkey = (sol.rooms[m.id], d, p)
                if rkey in busy_room:
                    bad.append(
                        f"аудитория {sol.rooms[m.id]} занята дважды: "
                        f"{inst.days[d]}, {inst.periods[p]}, неделя {w}"
                    )
                busy_room[rkey] = m.subject

    if check_rooms:
        for m in inst.meetings:
            rid = sol.rooms.get(m.id)
            if rid is None:
                if sol.rooms and require_rooms:
                    bad.append(f"занятию «{m.subject}» не назначена аудитория")
                continue
            if rid not in compatible_rooms(inst, m):
                room = inst.rooms[rid]
                size = sum(inst.groups[g].size for g in m.groups if g in inst.groups)
                bad.append(
                    f"«{m.subject}» ({m.room_kind}, {size} чел.) стоит в "
                    f"{rid} ({room.kind}, {room.capacity} мест)"
                )

    for m in inst.meetings:
        if m.id not in sol.times:
            continue
        if sol.times[m.id] in inst.teachers[m.teacher].unavailable:
            bad.append(
                f"{inst.teachers[m.teacher].name} занят в недоступный слот "
                f"({m.subject})"
            )

    return bad
