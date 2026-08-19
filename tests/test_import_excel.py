"""Парсер расписания из Excel — самая хрупкая часть проекта.

Здесь только чистые функции: БД и файлы не нужны.
"""

from __future__ import annotations

import pytest

from import_excel import clean_text, parse_cell, parse_direction_course


# ── Шапка колонки → (направление, курс) ───────────────────────────────────────

@pytest.mark.parametrize(
    "header,expected",
    [
        ("Инжиниринг ИС \n1 курс", ("Инжиниринг ИС", 1)),
        ("Информационные системы и технологии, 2 курс", ("Информационные системы и технологии", 2)),
        ("Прикладная математика   \n  3  курс", ("Прикладная математика", 3)),
        ("Без номера курса", ("", 0)),
        ("", ("", 0)),
    ],
)
def test_parse_direction_course(header, expected):
    assert parse_direction_course(header) == expected


# ── Нормализация текста ──────────────────────────────────────────────────────

def test_clean_text_reconstructs_spaced_line():
    spaced = "Ф   и  л   о   с   о   ф   и   я,     С   т   е   п   а   н   о  в"
    assert clean_text(spaced) == "Философия, Степанов"


def test_clean_text_collapses_spaces_and_strips():
    assert clean_text("  Базы    данных  ") == "Базы данных"


def test_clean_text_handles_empty():
    assert clean_text("") == ""
    assert clean_text(None) == ""


# ── Разбор ячейки ────────────────────────────────────────────────────────────

def test_parse_cell_basic():
    (rec,) = parse_cell("Геометрия, Абруков Д. А. пр 408")
    assert rec["subject"] == "Геометрия"
    assert rec["teacher"] == "Абруков Д. А."
    assert rec["room"] == "408"
    assert rec["class_type"] == "пр"
    assert rec["week"] == ""


def test_parse_cell_keeps_comma_inside_subject():
    """Запятая в названии предмета не должна съедаться как граница преподавателя."""
    (rec,) = parse_cell("Здания, сооружения и материалы, Иванов Л. Н. лк 414")
    assert rec["subject"] == "Здания, сооружения и материалы"
    assert rec["teacher"] == "Иванов Л. Н."
    assert rec["room"] == "414"


def test_parse_cell_week_markers():
    recs = parse_cell(
        "* Психология, Вишневская М. Н. лк 400\n** Основы права, Григорьев Ю. В. лб 425"
    )
    by_subject = {r["subject"]: r for r in recs}
    assert by_subject["Психология"]["week"] == "нечет"
    assert by_subject["Основы права"]["week"] == "чёт"


def test_parse_cell_splits_room_by_week_marker():
    """«330 ** пр 409» — одна аудитория на нечётной, другая на чётной неделе."""
    recs = parse_cell("Матанализ, Иванов А. Б. пр 330 ** пр 409")
    weeks = {r["week"]: r["room"] for r in recs}
    assert weeks.get("чёт") == "409"
    assert "330" in weeks.values()


def test_parse_cell_extracts_date_range():
    (rec,) = parse_cell("Физика, Петров В. Г. до 20.03 лк 210")
    assert rec["teacher"] == "Петров В. Г."
    assert rec["date_range"] == "до 20.03"


def test_parse_cell_parenthesised_room():
    (rec,) = parse_cell("Английский язык, Новикова Ф. Х. (210)")
    assert rec["subject"] == "Английский язык"
    assert rec["teacher"] == "Новикова Ф. Х."
    assert rec["room"] == "210"


def test_parse_cell_without_comma_before_teacher():
    (rec,) = parse_cell("Практика (учебные работы) Фадеев И. В. 109")
    assert rec["teacher"] == "Фадеев И. В."
    assert rec["room"] == "109"


def test_parse_cell_no_room():
    (rec,) = parse_cell("Физическая культура, Орлов О. П. пр")
    assert rec["subject"] == "Физическая культура"
    assert rec["class_type"] == "пр"


def test_parse_cell_empty():
    assert parse_cell("") == []
    assert parse_cell("   ") == []


def test_parse_cell_fallback_keeps_subject():
    """Ничего не распознали — предмет всё равно не теряем."""
    (rec,) = parse_cell("Консультация")
    assert rec["subject"] == "Консультация"
    assert rec["teacher"] == ""


def test_clean_text_strips_invisible_chars():
    """Регулярка «мусорных» символов записана escape-ами — проверяем, что работает.

    Раньше в исходнике стояли сами невидимые символы: код нельзя было проверить
    глазами, а любой редактор мог их незаметно съесть.
    """
    from import_excel import clean_text

    invisible = "".join(chr(c) for c in (0xFEFF, 0x200B, 0x200C, 0x200D, 0x2060, 0xAD))
    assert clean_text(f"Фил{invisible}ософия") == "Философия"
    # Неразрывный пробел — не «мусор», а пробел: он схлопывается в обычный.
    assert clean_text("Фил" + chr(0xA0) + "ософия") == "Фил ософия"


# ── Ячейки из утверждённого расписания на 2026/27 ────────────────────────────
#
# Три случая, на которых прежний разбор давал мусор. Файл править нельзя —
# расписание утверждено, поэтому импортёр обязан понимать его как есть.

def test_parity_applies_to_class_type():
    """«* лк ** лб 426» — одна пара, но тип занятия зависит от недели."""
    from import_excel import parse_cell

    recs = parse_cell("Дизайн информационных систем,\nЕвграфов Д. В. * лк ** лб 426")

    assert len(recs) == 2
    by_week = {r["week"]: r for r in recs}
    assert by_week["нечет"]["class_type"] == "лк"
    assert by_week["чёт"]["class_type"] == "лб"
    for r in recs:
        assert r["subject"] == "Дизайн информационных систем"
        assert r["teacher"] == "Евграфов Д. В."
        assert r["room"] == "426", "аудитория общая у обеих недель"


def test_parity_types_without_space_after_marker():
    """Встречается слитно: «*лк ** пр 424»."""
    from import_excel import parse_cell

    recs = parse_cell(
        "Исследование профессии и планирование карьеры, Бельчусов А. А. *лк ** пр 424"
    )
    assert {r["week"] for r in recs} == {"нечет", "чёт"}
    assert {r["class_type"] for r in recs} == {"лк", "пр"}
    assert all(r["room"] == "424" for r in recs)


def test_type_line_after_marker_is_not_a_new_class():
    """Перенос строки перед «* лк ** пр» не должен рождать пару-призрак."""
    from import_excel import parse_cell

    recs = parse_cell(
        "Исследование профессии и планирование карьеры, Павлова С. В.\n"
        "* лк ** пр 5 корп 102"
    )
    assert len(recs) == 2
    assert all(r["subject"] == "Исследование профессии и планирование карьеры" for r in recs)
    assert all(r["room"] == "5 корп 102" for r in recs)


def test_date_scheduled_class_stays_one_record():
    """Физкультура расписана по датам: раньше вторая строка становилась «парой»."""
    from import_excel import parse_cell

    recs = parse_cell(
        "Физическая культура и спорт - лек  8 ч.: 2, 9, 16, 23.09\n"
        "Матвеева Н.А. (гл. уч. к.)\n"
        "пр. 30.09,7,14,21.10;\n"
        "Матвеева Н.А. (5 уч. к.)"
    )

    assert len(recs) == 1, "перечисление дат — это одна пара, а не две"
    rec = recs[0]
    assert rec["subject"] == "Физическая культура и спорт"
    assert rec["teacher"] == "Матвеева Н.А."
    assert rec["date_range"], "даты должны попасть в date_range"
    assert "Матвеева" not in rec["subject"]


def test_second_class_after_dated_one_is_kept():
    """В той же ячейке за физкультурой идёт обычная пара — она не должна пропасть."""
    from import_excel import parse_cell

    recs = parse_cell(
        "Физическая культура и спорт - лек  8 ч.: 2, 9, 16, 23.09\n"
        "Матвеева Н.А. (гл. уч. к.)\n"
        "пр.  25.11,2,9,16.12  Матвеева Н.А. (5 уч. к.)\n"
        "Основы материаловедения, \n"
        "Фадеев И. В. лк 30.09 по 18. 11 5 уч.к 101"
    )

    subjects = [r["subject"] for r in recs]
    assert "Основы материаловедения" in subjects
    mat = next(r for r in recs if r["subject"] == "Основы материаловедения")
    assert mat["room"] == "5 уч.к 101", "диапазон дат не должен оставаться в аудитории"
    assert "30.09" in mat["date_range"]


def test_two_classes_by_parity_still_split():
    """Регрессия наоборот: настоящие две пары по чётности разбираются как раньше."""
    from import_excel import parse_cell

    recs = parse_cell(
        "* Основы российской государственности, \nГанин М. В. лк 330\n"
        "** Общий курс транспорта, \nКириллов Н. В. лк 5 уч.к 110"
    )
    assert len(recs) == 2
    by_week = {r["week"]: r["subject"] for r in recs}
    assert by_week["нечет"] == "Основы российской государственности"
    assert by_week["чёт"] == "Общий курс транспорта"
