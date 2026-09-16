"""Запуск: считает несколько вариантов расписания и показывает их рядом.

    python -m scheduler --dump выгрузка.json
    python -m scheduler --dump выгрузка.json --profiles баланс,студентам -t 60

Вариантов намеренно несколько: единственного «эффективного» расписания не
существует, есть несколько разных компромиссов. Рядом с ними печатается
строка ФАКТ — качество текущего расписания вуза, иначе цифры не с чем
сравнить.
"""

from __future__ import annotations

import argparse
import json
import sys

from .extract import build
from .instance import Instance
from .metrics import Metrics, baseline, evaluate
from .model import PARITIES, Solution, _serves, assign_rooms, solve_times
from .profiles import PROFILES
from .validate import violations

_COLUMNS = [
    ("окна_групп", "окна групп"),
    ("окна_преподов", "окна преп."),
    ("дни_преподов", "дней преп."),
    ("перегруженных_дней", "перегруж."),
    ("макс_пар_в_день", "макс/день"),
    ("поздних_пар", "поздних"),
    ("перебежек", "перебежки"),
    ("кучность", "кучность"),
]


def describe(inst: Instance) -> str:
    lines = [
        f"групп {len(inst.groups)}, преподавателей {len(inst.teachers)}, "
        f"аудиторий {len(inst.rooms)}, занятий {len(inst.meetings)}",
        f"сетка: {len(inst.days)} дней × {len(inst.periods)} пар = "
        f"{len(inst.days) * len(inst.periods)} слотов",
    ]
    if inst.skipped:
        lines.append(f"не разобрано строк: {len(inst.skipped)}")
    return "\n".join(lines)


def table(rows: list[tuple[str, Metrics, float]]) -> str:
    head = f"{'вариант':<16}" + "".join(f"{title:>12}" for _key, title in _COLUMNS)
    head += f"{'сек':>8}"
    out = [head, "─" * len(head)]
    for name, m, secs in rows:
        data = m.as_row()
        line = f"{name:<16}" + "".join(f"{data[key]:>12}" for key, _t in _COLUMNS)
        out.append(line + f"{secs:>8.1f}")
    return "\n".join(out)


def render_group(inst: Instance, sol: Solution, needle: str) -> str:
    """Готовая неделя одной группы — чтобы результат можно было посмотреть глазами."""
    matches = [g for g in inst.groups.values() if needle.lower() in g.direction.lower()]
    if not matches:
        return f"группа по запросу «{needle}» не найдена"
    group = matches[0]

    out = [f"{group.course} курс, {group.direction} ({group.size} чел.)"]
    for w in PARITIES:
        out.append(f"  ── неделя {w} ──")
        for d, day in enumerate(inst.days):
            items = []
            for m in inst.meetings:
                if m.id not in sol.times or group.id not in m.groups:
                    continue
                if not _serves(m, w):
                    continue
                dd, pp = sol.times[m.id]
                if dd != d:
                    continue
                room = sol.rooms.get(m.id) or inst.rooms.get(m.origin_room or "", None)
                room_s = room if isinstance(room, str) else (room.id if room else "—")
                items.append((pp, f"{inst.periods[pp]:>6}  {m.subject[:44]:<44} "
                                  f"{m.class_type:<3} {room_s}"))
            if not items:
                continue
            out.append(f"  {day}")
            for _p, line in sorted(items):
                out.append(f"      {line}")
    return "\n".join(out)


def explain_infeasible(inst: Instance, weights: dict[str, int], limit: float) -> str:
    """Разбирает, почему не сошлось: «решения нет» — бесполезный ответ."""
    sol, diag = solve_times(inst, weights, time_limit=limit, relax=True)
    shortage = diag.get("нехватка_аудиторий") or {}
    if not shortage:
        return ("Не сошлось даже с послаблением по аудиториям — значит, упирается "
                "в занятость людей: у кого-то из преподавателей больше занятий, "
                "чем слотов в неделе, либо его недоступность перекрывает нагрузку.")
    lines = ["Не хватает аудиторий. Где именно:"]
    for where, n in sorted(shortage.items(), key=lambda kv: -kv[1])[:10]:
        lines.append(f"  • {where}: не хватает {n}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Прототип составителя расписания")
    ap.add_argument("--dump", required=True, help="JSON-выгрузка таблицы schedule")
    ap.add_argument("--profiles", default="all", help="через запятую или all")
    ap.add_argument("-t", "--time-limit", type=float, default=30.0,
                    help="секунд на фазу времени (по умолчанию 30)")
    ap.add_argument("--rooms-time-limit", type=float, default=20.0)
    ap.add_argument("--show", metavar="НАПРАВЛЕНИЕ",
                    help="показать готовую неделю этой группы в каждом варианте")
    args = ap.parse_args(argv)

    with open(args.dump, encoding="utf-8") as fh:
        rows = json.load(fh)
    inst = build(rows)
    print(describe(inst))
    for note in inst.notes:
        print(f"  ! {note}")
    print()

    names = list(PROFILES) if args.profiles == "all" else [
        n.strip() for n in args.profiles.split(",") if n.strip()
    ]
    unknown = [n for n in names if n not in PROFILES]
    if unknown:
        print(f"неизвестные профили: {', '.join(unknown)}", file=sys.stderr)
        return 2

    base = baseline(inst)
    if args.show:
        print("── как сейчас ──")
        print(render_group(inst, base, args.show))
        print()
    base_bad = violations(inst, base, require_rooms=False)
    if base_bad:
        print(f"конфликтов в текущем расписании: {len(base_bad)} "
              f"(показаны первые {min(6, len(base_bad))})")
        for line in base_bad[:6]:
            print(f"    • {line}")
        print("  Это либо накладки в самом расписании, либо ошибки разбора")
        print("  Excel — и то, и другое стоит посмотреть глазами.")
        print()
    results: list[tuple[str, Metrics, float]] = [("ФАКТ (сейчас)", evaluate(inst, base), 0.0)]

    for name in names:
        sol, _ = solve_times(inst, PROFILES[name], time_limit=args.time_limit)
        if not sol.times:
            print(f"[{name}] {sol.status}")
            print(explain_infeasible(inst, PROFILES[name], args.time_limit))
            print()
            continue
        placed = assign_rooms(inst, sol, time_limit=args.rooms_time_limit)
        if not placed:
            print(f"[{name}] время расставлено, но аудитории не разложились")
        bad = violations(inst, sol)
        mark = "✓ проверено" if not bad else f"✗ НАРУШЕНИЙ: {len(bad)}"
        print(f"[{name}] {sol.status}, цель {sol.objective:.0f} — {mark}")
        for line in bad[:5]:
            print(f"    {line}")
        results.append((name, evaluate(inst, sol), sol.walltime))
        if args.show:
            print(render_group(inst, sol, args.show))
            print()

    print(table(results))
    print()
    print("Меньше — лучше во всех колонках. Все числа — за полный цикл из двух")
    print("недель; «дней преп.» — суммарно по всем преподавателям.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
