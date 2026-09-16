"""Решатель: CP-SAT в две фазы — сначала время, потом аудитории.

Почему две фазы, а не одна. В единой модели переменная выглядела бы как
`x[занятие][слот][аудитория]`: 372 × 36 × 38 ≈ полмиллиона булевых переменных,
и почти все они бессмысленны. Разнеся решения, получаем порядка тринадцати
тысяч на первой фазе и восьми на второй, а связь между ними держим
ограничением вместимости: в каждом слоте занятий, которым нужна лаборатория,
не больше, чем лабораторий. Это классическая декомпозиция timetabling, и она
же делает результат объяснимым: «время не сошлось» и «аудиторий не хватило» —
разные сообщения для разных людей.

Жёсткие ограничения (нарушать нельзя, иначе расписание недействительно) заданы
ограничениями модели. «Эффективность» — это уже целевая функция, а её веса
живут в `profiles.py`: они конфликтуют между собой, и выбор между ними —
решение вуза, а не алгоритма.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from .instance import KIND_GYM, KIND_LAB, Instance, Meeting

PARITIES = ["чёт", "нечет"]

#: Больше скольки пар в день у группы считаем перегрузом. В текущем расписании
#: встречаются дни по восемь пар, поэтому это мягкое ограничение, а не запрет.
SOFT_DAY_CAP = 4

#: Цена одной непоставленной пары при разборе неразрешимости. Должна быть выше
#: любого удобства, иначе солвер «решит» задачу, оставив занятия без аудиторий.
_SHORTAGE_COST = 10_000


@dataclass
class Solution:
    times: dict[int, tuple[int, int]] = field(default_factory=dict)   # id → (день, пара)
    rooms: dict[int, str] = field(default_factory=dict)               # id → id аудитории
    status: str = ""
    objective: float = 0.0
    walltime: float = 0.0


def _serves(m: Meeting, parity: str) -> bool:
    """Идёт ли занятие на неделе такой чётности."""
    return m.parity == "" or m.parity == parity


def compatible_rooms(inst: Instance, m: Meeting) -> list[str]:
    """Аудитории, куда это занятие в принципе можно поставить."""
    size = sum(inst.groups[g].size for g in m.groups if g in inst.groups)
    out = []
    for r in inst.rooms.values():
        if m.room_kind == KIND_GYM:
            if r.kind == KIND_GYM:
                out.append(r.id)
            continue
        if r.kind == KIND_GYM:
            continue
        if m.room_kind == KIND_LAB and r.kind != KIND_LAB:
            continue
        if r.capacity >= size:
            out.append(r.id)
    return out


# ── фаза 1: время ────────────────────────────────────────────────────────────

def solve_times(inst: Instance, weights: dict[str, int], *, time_limit: float = 30.0,
                locked: dict[int, tuple[int, int]] | None = None,
                relax: bool = False) -> tuple[Solution, dict]:
    """Расставляет занятия по слотам.

    `locked` — уже согласованные пары, их не двигаем: так работают реальные
    правки в сентябре, когда половина расписания уже объявлена.
    `relax` включает режим разбора неразрешимости: нехватка аудиторий
    перестаёт быть запретом и становится дорогой штрафной переменной, чтобы
    вместо «решения нет» показать, чего именно и когда не хватило.
    """
    model = cp_model.CpModel()
    D, P = len(inst.days), len(inst.periods)
    slots = [(d, p) for d in range(D) for p in range(P)]

    x = {}
    for m in inst.meetings:
        for (d, p) in slots:
            x[m.id, d, p] = model.NewBoolVar(f"x{m.id}_{d}_{p}")
        model.AddExactlyOne(x[m.id, d, p] for (d, p) in slots)

    for mid, (d, p) in (locked or {}).items():
        model.Add(x[mid, d, p] == 1)

    # ── жёсткое: никто не находится в двух местах одновременно ──────────────
    by_group: dict[str, list[Meeting]] = {g: [] for g in inst.groups}
    by_teacher: dict[str, list[Meeting]] = {t: [] for t in inst.teachers}
    for m in inst.meetings:
        for g in m.groups:
            by_group[g].append(m)
        by_teacher[m.teacher].append(m)

    for owner in (by_group, by_teacher):
        for meets in owner.values():
            for (d, p) in slots:
                for w in PARITIES:
                    active = [x[m.id, d, p] for m in meets if _serves(m, w)]
                    if len(active) > 1:
                        model.AddAtMostOne(active)

    # ── жёсткое: недоступность преподавателя ────────────────────────────────
    for t in inst.teachers.values():
        for (d, p) in t.unavailable:
            for m in by_teacher.get(t.id, []):
                model.Add(x[m.id, d, p] == 0)

    # ── связь с аудиторным фондом ───────────────────────────────────────────
    # Вложенные множества: занятий, которым подходит только комнаты из S,
    # в одном слоте не больше |S|. Считаем и по типу (спортзал/лаб), и по
    # вместимости — иначе первая фаза ставит время, под которое потом некуда
    # посадить поток, и assign_rooms падает на валидном инстансе.
    def _meeting_size(m: Meeting) -> int:
        return sum(inst.groups[g].size for g in m.groups if g in inst.groups)

    def _add_pool_caps(
        pool_rooms: list,
        demand_meets: list[Meeting],
        tag: str,
        *,
        by_capacity: bool,
    ) -> None:
        if not pool_rooms:
            # Комнат нет — в слот нельзя ставить никого из спроса (или slack).
            for (d, p) in slots:
                for w in PARITIES:
                    active = [x[m.id, d, p] for m in demand_meets if _serves(m, w)]
                    if not active:
                        continue
                    if relax:
                        s = model.NewIntVar(0, len(active), f"slack_{tag}_{d}_{p}_{w}")
                        room_slack[tag, d, p, w] = s
                        model.Add(sum(active) <= s)
                    else:
                        model.Add(sum(active) == 0)
            return

        # Пороги вместимости: занятия размера ≥ T делят только комнаты ≥ T.
        thresholds = [0]
        if by_capacity:
            thresholds = sorted({r.capacity for r in pool_rooms} | {0})
        for thr in thresholds:
            rooms_ok = [r for r in pool_rooms if r.capacity >= thr] if by_capacity else pool_rooms
            cap = len(rooms_ok)
            needy = (
                [m for m in demand_meets if _meeting_size(m) >= thr]
                if by_capacity
                else demand_meets
            )
            if not needy:
                continue
            for (d, p) in slots:
                for w in PARITIES:
                    active = [x[m.id, d, p] for m in needy if _serves(m, w)]
                    if not active:
                        continue
                    if relax:
                        s = model.NewIntVar(
                            0, len(active), f"slack_{tag}_{thr}_{d}_{p}_{w}"
                        )
                        room_slack[tag, thr, d, p, w] = s
                        model.Add(sum(active) <= cap + s)
                    else:
                        model.Add(sum(active) <= cap)

    room_slack: dict = {}
    gym_rooms = [r for r in inst.rooms.values() if r.kind == KIND_GYM]
    lab_rooms = [r for r in inst.rooms.values() if r.kind == KIND_LAB]
    plain_rooms = [r for r in inst.rooms.values() if r.kind != KIND_GYM]
    gym_demand = [m for m in inst.meetings if m.room_kind == KIND_GYM]
    lab_demand = [m for m in inst.meetings if m.room_kind == KIND_LAB]
    # Общий фонд (без спортзала) делят все не-физкультурные занятия; лабы
    # дополнительно ограничены lab_rooms выше.
    any_demand = [m for m in inst.meetings if m.room_kind != KIND_GYM]

    _add_pool_caps(gym_rooms, gym_demand, KIND_GYM, by_capacity=False)
    _add_pool_caps(lab_rooms, lab_demand, KIND_LAB, by_capacity=True)
    _add_pool_caps(plain_rooms, any_demand, "любая", by_capacity=True)

    # ── целевая функция: занятость, окна, дни, перегруз ─────────────────────
    penalties: list[tuple[int, object]] = []
    counter = [0]

    def busy_map(meets: list[Meeting], parity: str) -> dict:
        """Занятость по слотам на неделе заданной чётности.

        Считать надо именно по чётности, а не по всем занятиям сразу: пара «по
        чётным» закрывает дырку только на своей неделе, а на другой окно
        остаётся. Модель, не знающая об этом, честно сообщает «окон нет» —
        и расходится с тем, что увидят студенты.
        """
        counter[0] += 1
        tag = counter[0]
        busy = {}
        for (d, p) in slots:
            at = [x[m.id, d, p] for m in meets if _serves(m, parity)]
            v = model.NewBoolVar(f"busy{tag}_{d}_{p}")
            if at:
                model.AddMaxEquality(v, at)
            else:
                model.Add(v == 0)
            busy[d, p] = v
        return busy

    def add_windows(busy: dict, weight: int) -> None:
        """Окно — свободная пара, у которой есть занятия и до неё, и после."""
        if weight <= 0:
            return
        counter[0] += 1
        tag = counter[0]
        for d in range(D):
            for p in range(P):
                before = [busy[d, q] for q in range(p)]
                after = [busy[d, q] for q in range(p + 1, P)]
                if not before or not after:
                    continue
                b = model.NewBoolVar(f"bef{tag}_{d}_{p}")
                a = model.NewBoolVar(f"aft{tag}_{d}_{p}")
                model.AddMaxEquality(b, before)
                model.AddMaxEquality(a, after)
                gap = model.NewBoolVar(f"gap{tag}_{d}_{p}")
                # Цель минимизируется, поэтому нижней границы достаточно:
                # переменная сама сядет в ноль везде, где это допустимо.
                model.Add(gap >= b + a - busy[d, p] - 1)
                penalties.append((weight, gap))

    for meets in by_group.values():
        if not meets:
            continue
        for w in PARITIES:
            busy = busy_map(meets, w)
            add_windows(busy, weights.get("окна_групп", 0))
            if weights.get("перегруз", 0) > 0:
                for d in range(D):
                    load = sum(busy[d, p] for p in range(P))
                    over = model.NewIntVar(0, P, f"over{counter[0]}_{d}")
                    model.Add(over >= load - SOFT_DAY_CAP)
                    penalties.append((weights["перегруз"], over))
            if weights.get("поздние_пары", 0) > 0:
                for d in range(D):
                    penalties.append((weights["поздние_пары"], busy[d, P - 1]))

    for meets in by_teacher.values():
        if not meets:
            continue
        for w in PARITIES:
            busy = busy_map(meets, w)
            add_windows(busy, weights.get("окна_преподов", 0))
            if weights.get("дни_преподов", 0) > 0:
                for d in range(D):
                    day_used = model.NewBoolVar(f"day{counter[0]}_{d}")
                    model.AddMaxEquality(day_used, [busy[d, p] for p in range(P)])
                    penalties.append((weights["дни_преподов"], day_used))

    # ── дисциплина не должна собираться в один день ─────────────────────────
    if weights.get("кучность", 0) > 0:
        buckets: dict[tuple[str, str], list[Meeting]] = {}
        for m in inst.meetings:
            for g in m.groups:
                buckets.setdefault((g, m.subject), []).append(m)
        for idx, meets in enumerate(buckets.values()):
            if len(meets) < 2:
                continue
            for w in PARITIES:
                same_week = [m for m in meets if _serves(m, w)]
                if len(same_week) < 2:
                    continue
                for d in range(D):
                    same_day = sum(x[m.id, d, p] for m in same_week for p in range(P))
                    over = model.NewIntVar(0, len(same_week), f"clump{idx}_{d}_{w}")
                    model.Add(over >= same_day - 1)
                    penalties.append((weights["кучность"], over))

    terms = [w * v for w, v in penalties]
    if relax and room_slack:
        terms += [_SHORTAGE_COST * s for s in room_slack.values()]
    model.Minimize(sum(terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = 8
    status = solver.Solve(model)

    ok = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    sol = Solution(
        status=solver.StatusName(status),
        objective=solver.ObjectiveValue() if ok else 0.0,
        walltime=solver.WallTime(),
    )
    diag: dict = {}
    if ok:
        for m in inst.meetings:
            for (d, p) in slots:
                if solver.Value(x[m.id, d, p]):
                    sol.times[m.id] = (d, p)
                    break
        if relax:
            shortage = {}
            for key, s in room_slack.items():
                val = solver.Value(s)
                if val <= 0:
                    continue
                # Ключ: (pool, d, p, w) или (pool, thr, d, p, w) для порогов вместимости.
                if len(key) == 4:
                    pool, d, p, w = key
                    thr_s = ""
                else:
                    pool, thr, d, p, w = key
                    thr_s = f", ≥{thr} мест" if thr else ""
                label = (
                    f"{inst.days[d]}, {inst.periods[p]}, неделя {w}, "
                    f"фонд «{pool}»{thr_s}"
                )
                shortage[label] = val
            diag["нехватка_аудиторий"] = shortage
    return sol, diag


# ── фаза 2: аудитории ────────────────────────────────────────────────────────

def assign_rooms(inst: Instance, sol: Solution, *, time_limit: float = 20.0) -> bool:
    """Раскладывает занятия по комнатам, минимизируя перебежки между корпусами."""
    model = cp_model.CpModel()
    y: dict[tuple[int, str], object] = {}
    for m in inst.meetings:
        options = compatible_rooms(inst, m)
        if not options:
            return False
        for rid in options:
            y[m.id, rid] = model.NewBoolVar(f"y{m.id}_{rid}")
        model.AddExactlyOne(y[m.id, rid] for rid in options)

    # Одна комната — одно занятие в слоте, с учётом чётности недели.
    at_slot: dict[tuple[int, int, str], list[Meeting]] = {}
    for m in inst.meetings:
        d, p = sol.times[m.id]
        for w in PARITIES:
            if _serves(m, w):
                at_slot.setdefault((d, p, w), []).append(m)

    for meets in at_slot.values():
        for rid in inst.rooms:
            here = [y[m.id, rid] for m in meets if (m.id, rid) in y]
            if len(here) > 1:
                model.AddAtMostOne(here)

    # Перебежки между корпусами. Время уже зафиксировано первой фазой, поэтому
    # соседние пары известны заранее и переменная нужна только на переход.
    in_fifth = {}
    for m in inst.meetings:
        v = model.NewBoolVar(f"b{m.id}")
        fifth = [y[m.id, rid] for rid in inst.rooms
                 if (m.id, rid) in y and inst.rooms[rid].building == "5"]
        if fifth:
            model.AddMaxEquality(v, fifth)
        else:
            model.Add(v == 0)
        in_fifth[m.id] = v

    moves = []
    # Соседей ищем внутри одной чётности: иначе оптимизатор «лечит» перебежку
    # между парой по чётным и парой по нечётным, которой у студента нет.
    # Вес = число недель, где оба занятия реально соседствуют (еженедельная
    # пара даёт 2 — как в metrics.transitions).
    pair_weight: dict[tuple[int, int], int] = {}
    per_group_day: dict[tuple[str, int, str], list[tuple[int, Meeting]]] = {}
    for m in inst.meetings:
        d, p = sol.times[m.id]
        for w in PARITIES:
            if not _serves(m, w):
                continue
            for g in m.groups:
                per_group_day.setdefault((g, d, w), []).append((p, m))
    for items in per_group_day.values():
        items.sort(key=lambda it: it[0])
        for (p1, m1), (p2, m2) in zip(items, items[1:]):
            if p2 - p1 > 2:
                continue   # длинный разрыв: дорога между корпусами не в тягость
            pair = (m1.id, m2.id) if m1.id < m2.id else (m2.id, m1.id)
            pair_weight[pair] = pair_weight.get(pair, 0) + 1
    for (id1, id2), weight in pair_weight.items():
        t = model.NewBoolVar(f"move{id1}_{id2}")
        model.Add(t >= in_fifth[id1] - in_fifth[id2])
        model.Add(t >= in_fifth[id2] - in_fifth[id1])
        moves.append(weight * t)
    model.Minimize(sum(moves) if moves else 0)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = 8
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return False
    for m in inst.meetings:
        for rid in inst.rooms:
            if (m.id, rid) in y and solver.Value(y[m.id, rid]):
                sol.rooms[m.id] = rid
                break
    return True
