"""Прототип составителя расписания.

Проверяется не «солвер что-то выдал», а три отдельные вещи: что вход разобран
правильно (нормализация и склейка потоков), что решение действительно
удовлетворяет жёстким ограничениям, и что метрики считают то, что обещают.
Самая дорогая ошибка здесь — забытое ограничение: оно выглядит как отличный
результат, поэтому решение проверяет независимый код в `validate.py`.
"""

from __future__ import annotations

import pytest

# ortools стоит только в requirements-dev: основная матрица CI ставит
# requirements.txt, и без этой строки она падала бы на импорте. Прототип
# при этом не остаётся без проверки — под него заведена отдельная работа
# в .github/workflows/ci.yml.
pytest.importorskip("ortools", reason="нужен ortools (requirements-dev.txt)")

from scheduler.extract import build  # noqa: E402
from scheduler.instance import (  # noqa: E402
    normalize_class_type,
    normalize_room,
    normalize_time,
)
from scheduler.metrics import baseline, evaluate  # noqa: E402
from scheduler.model import assign_rooms, solve_times  # noqa: E402
from scheduler.profiles import PROFILES  # noqa: E402
from scheduler.validate import violations  # noqa: E402


def row(course=1, direction="Транспорт", day="Понедельник", time="8.15 - 9.45",
        subject="Физика", teacher="Иванов И. И.", room="101", week="",
        class_type="лк", date_range=""):
    return dict(course=course, direction=direction, day=day, time=time,
                subject=subject, teacher=teacher, room=room, week=week,
                class_type=class_type, date_range=date_range)


# ── нормализация входа ───────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["5 корп 220", "5 уч.к 220", "5 уч.к. 220",
                                 "5 уч.к 5 уч.к 220", "5 уч.к.220"])
def test_one_room_written_five_ways_is_one_room(raw):
    """Иначе солвер посадит в одну комнату столько групп, сколько написаний."""
    assert normalize_room(raw)[0] == "5:220"


def test_room_with_subgroup_marker():
    assert normalize_room("326 ( 2 подгруппа)") == ("гл:326", 2)


def test_two_rooms_in_one_field_take_the_first():
    assert normalize_room("5 уч.к 101а по 5 корп 220")[0] == "5:101а"


def test_gym_is_recognised():
    assert normalize_room("гл. уч. к.")[0] == "гл:спортзал"


def test_typo_in_time_is_the_same_period():
    """«12.00 - 13.29» — опечатка исходника в двадцати строках."""
    assert normalize_time("12.00 - 13.29") == normalize_time("12.00 - 13.30")


def test_lecture_spelled_out_is_a_lecture():
    assert normalize_class_type("лекция") == "лк"


# ── склейка потоков ──────────────────────────────────────────────────────────

def test_stream_becomes_one_meeting():
    """Лекция на три направления — одна пара, а не три."""
    inst = build([
        row(direction="Транспорт"),
        row(direction="Физика и информатика"),
        row(direction="Математика и физика"),
    ])
    assert len(inst.meetings) == 1
    assert len(inst.meetings[0].groups) == 3


def test_stream_merges_even_when_class_type_differs():
    """В выгрузке один поток бывает размечен и «лк», и «пр» вперемешку.

    Разделив его, мы получили бы преподавателя, занятого сам с собой — ровно
    такие «нарушения» и всплыли на боевых данных.
    """
    inst = build([
        row(direction="Транспорт", class_type="лк"),
        row(direction="Физика и информатика", class_type="пр"),
    ])
    assert len(inst.meetings) == 1
    assert not violations(inst, baseline(inst), require_rooms=False)


def test_different_teachers_are_not_merged():
    inst = build([
        row(direction="Транспорт", teacher="Иванов И. И."),
        row(direction="Физика и информатика", teacher="Петров П. П."),
    ])
    assert len(inst.meetings) == 2


def test_teacher_spelled_with_and_without_spaces_is_one_person():
    inst = build([
        row(direction="Транспорт", teacher="Матвеева Н.А."),
        row(direction="Физика и информатика", teacher="Матвеева Н. А."),
    ])
    assert len(inst.teachers) == 1


def test_self_study_is_not_a_meeting():
    inst = build([row(subject="День самостоятельной работы", teacher="", room="")])
    assert inst.meetings == []


# ── жёсткие ограничения ──────────────────────────────────────────────────────

def _tiny_instance():
    """Две группы, один общий преподаватель — расставить можно только врозь."""
    rows = []
    for i, subj in enumerate(["Физика", "Алгебра", "Химия"]):
        rows.append(row(direction="Транспорт", subject=subj, teacher="Иванов И. И.",
                        day="Понедельник", time="8.15 - 9.45", room=f"10{i}"))
        rows.append(row(direction="Математика и физика", subject=subj + " (2)",
                        teacher="Иванов И. И.", day="Вторник",
                        time="9.55 - 11.25", room=f"20{i}"))
    return build(rows)


def test_solution_satisfies_hard_constraints():
    inst = _tiny_instance()
    sol, _ = solve_times(inst, PROFILES["баланс"], time_limit=10)
    assert sol.times, f"солвер не нашёл решения: {sol.status}"
    assert assign_rooms(inst, sol, time_limit=10)
    assert violations(inst, sol) == []


def test_every_meeting_is_placed_exactly_once():
    inst = _tiny_instance()
    sol, _ = solve_times(inst, PROFILES["баланс"], time_limit=10)
    assert len(sol.times) == len(inst.meetings)


def test_teacher_unavailability_is_respected():
    from dataclasses import replace

    inst = _tiny_instance()
    tid = next(iter(inst.teachers))
    busy = frozenset((0, p) for p in range(len(inst.periods)))   # весь понедельник
    inst.teachers[tid] = replace(inst.teachers[tid], unavailable=busy)

    sol, _ = solve_times(inst, PROFILES["баланс"], time_limit=10)
    assert sol.times
    assert all(day != 0 for day, _p in sol.times.values())
    assert violations(inst, sol, check_rooms=False) == []


def test_locked_meetings_stay_put():
    inst = _tiny_instance()
    locked = {inst.meetings[0].id: (3, 2)}

    sol, _ = solve_times(inst, PROFILES["баланс"], time_limit=10, locked=locked)
    assert sol.times[inst.meetings[0].id] == (3, 2)


def test_parity_pairs_may_share_a_slot():
    """«По чётным» и «по нечётным» — это разные недели, а не конфликт."""
    inst = build([
        row(subject="Физика", week="чёт", room="101"),
        row(subject="Алгебра", week="нечет", room="101"),
    ])
    assert violations(inst, baseline(inst), require_rooms=False) == []


def test_same_week_clash_is_reported():
    inst = build([
        row(subject="Физика", week="чёт", room="101", teacher="Иванов И. И."),
        row(subject="Алгебра", week="чёт", room="101", teacher="Петров П. П."),
    ])
    bad = violations(inst, baseline(inst), require_rooms=False)
    assert bad, "две пары у одной группы в один слот должны быть нарушением"


# ── метрики ──────────────────────────────────────────────────────────────────

def test_window_is_counted():
    """Первая и третья пара заняты, вторая свободна — это окно.

    Считается двойка, а не единица: метрики считают полный цикл из двух недель,
    а еженедельная пара стоит и на чётной, и на нечётной.
    """
    inst = build([
        row(subject="Физика", time="8.15 - 9.45", room="101"),
        row(subject="Алгебра", time="12.00 - 13.30", room="102",
            teacher="Петров П. П."),
    ])
    assert evaluate(inst, baseline(inst)).окна_групп == 2


def test_consecutive_pairs_have_no_window():
    inst = build([
        row(subject="Физика", time="8.15 - 9.45", room="101"),
        row(subject="Алгебра", time="9.55 - 11.25", room="102",
            teacher="Петров П. П."),
    ])
    assert evaluate(inst, baseline(inst)).окна_групп == 0


def test_parity_pair_does_not_plug_a_window():
    """Пара «по чётным» закрывает дырку только на своей неделе.

    Ровно на этом ошибалась модель: считала окна по всем занятиям сразу и
    отчитывалась «окон нет» там, где студенты их видят.
    """
    inst = build([
        row(subject="Физика", time="8.15 - 9.45", room="101"),
        row(subject="Алгебра", time="9.55 - 11.25", room="102",
            teacher="Петров П. П.", week="чёт"),
        row(subject="Химия", time="12.00 - 13.30", room="103",
            teacher="Сидоров С. С."),
    ])
    assert evaluate(inst, baseline(inst)).окна_групп == 1


def test_building_transition_is_counted():
    """Еженедельная перебежка считается дважды — по разу на каждую неделю цикла."""
    inst = build([
        row(subject="Физика", time="8.15 - 9.45", room="101"),
        row(subject="Алгебра", time="9.55 - 11.25", room="5 корп 220",
            teacher="Петров П. П."),
    ])
    assert evaluate(inst, baseline(inst)).перебежек == 2


def test_building_transition_ignores_different_weeks():
    """Пара по чётным и пара по нечётным в один день — не соседние в жизни."""
    inst = build([
        row(subject="Физика", time="8.15 - 9.45", room="101", week="чёт"),
        row(subject="Алгебра", time="9.55 - 11.25", room="5 корп 220",
            teacher="Петров П. П.", week="нечет"),
    ])
    assert evaluate(inst, baseline(inst)).перебежек == 0


def test_clumping_ignores_different_weeks():
    """Лекция по чётным и практика по нечётным — не «две пары в один день»."""
    inst = build([
        row(subject="Физика", time="8.15 - 9.45", room="101", week="чёт"),
        row(subject="Физика", time="9.55 - 11.25", room="101", week="нечет"),
    ])
    assert evaluate(inst, baseline(inst)).кучность == 0


def test_metrics_cover_the_two_week_cycle():
    """Пара «раз в две недели» даёт вклад один раз, еженедельная — два."""
    weekly = build([row(subject="Физика", room="101")])
    biweekly = build([row(subject="Физика", room="101", week="чёт")])

    assert evaluate(weekly, baseline(weekly)).дни_преподов == 2
    assert evaluate(biweekly, baseline(biweekly)).дни_преподов == 1
