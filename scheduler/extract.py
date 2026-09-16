"""Сборка инстанса из выгрузки боевого расписания.

Текущее расписание — это допустимое решение задачи, которую мы хотим решать.
Значит, из него восстанавливается почти весь вход: какие занятия существуют,
кто их ведёт, какие есть аудитории. Недостающее (численность групп,
вместимость комнат, недоступность преподавателей) синтезируется помечено —
чтобы никто не принял эти числа за настоящие.

Главная тонкость — **потоки**. Лекция на пять направлений лежит в базе пятью
строками. Если считать их независимыми занятиями, ограничение «преподаватель
не в двух местах» немедленно растащит поток по разным парам, а на плотной
неделе сделает задачу неразрешимой. Поэтому строки с одним преподавателем,
предметом и слотом схлопываются в одно занятие на несколько групп.
"""

from __future__ import annotations

import collections
import zlib
from dataclasses import replace

from .instance import (
    KIND_GYM,
    KIND_LAB,
    KIND_LECTURE,
    KIND_PLAIN,
    SELF_STUDY,
    Group,
    Instance,
    Meeting,
    Room,
    Teacher,
    group_id,
    normalize_class_type,
    normalize_room,
    normalize_time,
    teacher_id,
)

#: Синтетика. Настоящие числа привезут из деканата; до тех пор они
#: детерминированы от имени, чтобы прогоны были воспроизводимы.
_SIZE_MIN, _SIZE_MAX = 14, 30

#: Нижняя граница вместимости по типу комнаты. Реальная считается по спросу:
#: комната обязана вмещать то, что в ней идёт сегодня, иначе текущее расписание
#: перестанет быть допустимым решением — а оно эталон, с которым мы сравниваем.
_CAPACITY_FLOOR = {KIND_GYM: 100, KIND_LAB: 16, KIND_LECTURE: 40, KIND_PLAIN: 25}


def _stable_int(key: str, lo: int, hi: int) -> int:
    return lo + zlib.crc32(key.encode("utf-8")) % (hi - lo + 1)


def build(rows: list[dict]) -> Instance:
    inst = Instance()

    # ── группы ───────────────────────────────────────────────────────────────
    for r in rows:
        gid = group_id(r["course"], r["direction"])
        if gid not in inst.groups:
            inst.groups[gid] = Group(
                id=gid,
                course=int(r["course"]),
                direction=r["direction"].strip(),
                size=_stable_int(gid, _SIZE_MIN, _SIZE_MAX),
            )

    # ── аудитории: тип выводим из того, как комнату используют сегодня ───────
    usage: dict[str, set[str]] = collections.defaultdict(set)
    for r in rows:
        rid, _ = normalize_room(r["room"])
        if rid:
            usage[rid].add(normalize_class_type(r["class_type"]))

    for rid, types in usage.items():
        building, number = rid.split(":", 1)
        if number == "спортзал":
            kind = KIND_GYM
        elif "лб" in types:
            kind = KIND_LAB
        elif "лк" in types:
            kind = KIND_LECTURE
        else:
            kind = KIND_PLAIN
        inst.rooms[rid] = Room(
            id=rid, building=building, number=number,
            kind=kind, capacity=_CAPACITY_FLOOR[kind],
        )

    # ── занятия ──────────────────────────────────────────────────────────────
    # Ключ потока: один человек, один предмет, один вид занятия, один слот.
    streams: dict[tuple, dict] = {}
    for r in rows:
        subject = (r["subject"] or "").strip()
        if not subject:
            inst.skipped.append((r["direction"], "пустой предмет"))
            continue
        if SELF_STUDY in subject.lower():
            # Не занятие: ни преподавателя, ни аудитории. Сетку не занимает.
            continue

        tid = teacher_id(r["teacher"])
        if not tid:
            inst.skipped.append((f"{subject} / {r['direction']}", "нет преподавателя"))
            continue

        day = r["day"].strip().capitalize()
        if day not in inst.days:
            inst.skipped.append((subject, f"неизвестный день {r['day']!r}"))
            continue
        period = normalize_time(r["time"])
        if period is None:
            inst.skipped.append((subject, f"неизвестное время {r['time']!r}"))
            continue

        ctype = normalize_class_type(r["class_type"])
        parity = (r["week"] or "").strip()
        rid, _sub = normalize_room(r["room"])

        if tid not in inst.teachers:
            inst.teachers[tid] = Teacher(id=tid, name=r["teacher"].strip())

        # Вид занятия в ключ НЕ входит. В выгрузке один и тот же поток бывает
        # размечен вперемешку («лк» у двух направлений, «пр» у двух других) —
        # физически это одна пара в одной аудитории, и разделив её, мы получили
        # бы преподавателя, занятого сам с собой.
        key = (tid, subject.lower(), inst.days.index(day), period, parity)
        entry = streams.setdefault(
            key, {"groups": [], "room": rid, "types": collections.Counter()}
        )
        entry["types"][ctype] += 1
        gid = group_id(r["course"], r["direction"])
        if gid not in entry["groups"]:
            entry["groups"].append(gid)
        if entry["room"] is None:
            entry["room"] = rid

    for idx, (key, entry) in enumerate(sorted(streams.items(), key=lambda kv: str(kv[0]))):
        tid, subject, day, period, parity = key
        ctype = _dominant_type(entry["types"])
        groups = tuple(entry["groups"])
        inst.meetings.append(Meeting(
            id=idx,
            groups=groups,
            subject=subject,
            teacher=tid,
            class_type=ctype,
            parity=parity,
            room_kind=_needed_kind(subject, ctype, entry["room"], inst, groups),
            origin=(day, period),
            origin_room=entry["room"] if entry["room"] in inst.rooms else None,
        ))

    _fit_capacities(inst)
    return inst


def _fit_capacities(inst: Instance) -> None:
    """Подгоняет вместимость под фактический спрос.

    Два требования. Комната вмещает то, что в ней идёт сейчас — иначе текущее
    расписание оказалось бы «невозможным», и сравнивать с ним было бы нельзя.
    И под каждое занятие должна найтись хоть одна подходящая комната — поток на
    сто человек надо где-то проводить, даже если в выгрузке аудитория не
    указана вовсе.
    """
    demand: dict[str, int] = {}
    biggest: dict[str, int] = {}
    for m in inst.meetings:
        size = sum(inst.groups[g].size for g in m.groups if g in inst.groups)
        if m.origin_room:
            demand[m.origin_room] = max(demand.get(m.origin_room, 0), size)
        biggest[m.room_kind] = max(biggest.get(m.room_kind, 0), size)

    for rid, room in list(inst.rooms.items()):
        need = max(room.capacity, demand.get(rid, 0))
        if need != room.capacity:
            inst.rooms[rid] = replace(room, capacity=need)

    # Самая большая комната каждого типа обязана принять самое большое занятие.
    for kind, size in biggest.items():
        pool = [r for r in inst.rooms.values() if r.kind == kind]
        if not pool:
            continue
        top = max(pool, key=lambda r: r.capacity)
        if top.capacity < size:
            inst.rooms[top.id] = replace(top, capacity=size)
            inst.notes.append(
                f"вместимость «{top.id}» поднята до {size} — под самый большой "
                f"поток типа «{kind}» (синтетика, настоящих цифр в базе нет)"
            )


#: Чем «специальнее» вид занятия, тем важнее его сохранить при разборе ничьей.
_TYPE_RANK = {"лб": 0, "пр": 1, "лк": 2, "": 3}


def _dominant_type(counts: collections.Counter) -> str:
    """Какой вид занятия считать настоящим, если в строках потока они разные."""
    return min(counts, key=lambda t: (-counts[t], _TYPE_RANK.get(t, 9)))


def _needed_kind(subject: str, ctype: str, rid: str | None, inst: Instance,
                 groups: tuple[str, ...]) -> str:
    """Какая аудитория нужна занятию.

    Требование выводим из вида занятия, а не из текущей комнаты: практика,
    которую сегодня поставили в лабораторию, не обязана идти в лаборатории —
    иначе мы бы закрепили случайность расстановки как жёсткое ограничение.
    Исключение — спортзал: физкультуре он действительно нужен.
    """
    if "физическая культура" in subject or (rid and inst.rooms.get(rid) and
                                            inst.rooms[rid].kind == KIND_GYM):
        return KIND_GYM
    if ctype == "лб":
        return KIND_LAB
    total = sum(inst.groups[g].size for g in groups if g in inst.groups)
    return KIND_LECTURE if total > _CAPACITY_FLOOR[KIND_PLAIN] else KIND_PLAIN
