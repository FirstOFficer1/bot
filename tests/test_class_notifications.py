"""Уведомления о парах: чётность недели и группировка подписчиков.

Регрессия: воркер отсекал чужую неделю по префиксу «[нечет]» в названии
предмета — конвенции, которую актуальный импортёр не создаёт. Из-за этого
подписчик получал уведомления о парах чужой недели. Фильтр должен идти по
колонке `week`, как это делает repo.get_day().
"""

from __future__ import annotations

import pytest

from vkbot import config, db
from vkbot.models import subscriptions
from vkbot.workers import classes

COURSE = 1
DIRECTION = "Информационные системы и технологии"
DAY = "Понедельник"


def _add(subject: str, week: str, time: str = "1 пара 08:00-09:30") -> None:
    with db.connect(config.SCHEDULE_DB) as conn:
        conn.execute(
            "INSERT INTO schedule (course, direction, day, time, subject, teacher, room, week) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (COURSE, DIRECTION, DAY, time, subject, "Иванов А. Б.", "301", week),
        )


@pytest.fixture(autouse=True)
def _clean_schedule():
    with db.connect(config.SCHEDULE_DB) as conn:
        conn.execute("DELETE FROM schedule")
    yield
    with db.connect(config.SCHEDULE_DB) as conn:
        conn.execute("DELETE FROM schedule")


def test_even_week_excludes_odd_week_classes():
    _add("Каждую неделю", "")
    _add("Только чётная", "чёт")
    _add("Только нечётная", "нечет")

    subjects = {r[1] for r in classes._day_rows(COURSE, DIRECTION, DAY, "чёт")}
    assert subjects == {"Каждую неделю", "Только чётная"}


def test_odd_week_excludes_even_week_classes():
    _add("Каждую неделю", "")
    _add("Только чётная", "чёт")
    _add("Только нечётная", "нечет")

    subjects = {r[1] for r in classes._day_rows(COURSE, DIRECTION, DAY, "нечет")}
    assert subjects == {"Каждую неделю", "Только нечётная"}


def test_legacy_subject_prefix_still_filtered():
    """Старые строки с префиксом в названии предмета тоже должны отсекаться."""
    _add("[чёт] Правоведение", "")
    _add("[нечет] Философия", "")

    subjects = {r[1] for r in classes._day_rows(COURSE, DIRECTION, DAY, "чёт")}
    assert subjects == {"[чёт] Правоведение"}


def test_day_and_direction_are_scoped():
    _add("Нужная пара", "")
    with db.connect(config.SCHEDULE_DB) as conn:
        conn.execute(
            "INSERT INTO schedule (course, direction, day, time, subject, week) "
            "VALUES (?,?,?,?,?,?)",
            (COURSE, DIRECTION, "Вторник", "1 пара 08:00-09:30", "Другой день", ""),
        )
        conn.execute(
            "INSERT INTO schedule (course, direction, day, time, subject, week) "
            "VALUES (?,?,?,?,?,?)",
            (2, DIRECTION, DAY, "1 пара 08:00-09:30", "Другой курс", ""),
        )

    subjects = {r[1] for r in classes._day_rows(COURSE, DIRECTION, DAY, "чёт")}
    assert subjects == {"Нужная пара"}


def test_subscriptions_grouped_by_course_and_direction():
    """Один запрос на группу вместо запроса на каждую подписку."""
    subscriptions.add(11, COURSE, DIRECTION)
    subscriptions.add(22, COURSE, DIRECTION)
    subscriptions.add(33, 2, DIRECTION)

    groups = classes._group_subscriptions()

    assert set(groups) == {(COURSE, DIRECTION), (2, DIRECTION)}
    assert sorted(groups[(COURSE, DIRECTION)]) == [11, 22]
    assert groups[(2, DIRECTION)] == [33]


def test_disabled_subscriptions_are_not_notified():
    subscriptions.add(11, COURSE, DIRECTION)
    subscriptions.disable_all_for_user(11)

    assert classes._group_subscriptions() == {}


@pytest.mark.parametrize(
    "time_field,expected",
    [
        ("1 пара 08:00-09:30", (8, 0)),
        ("8:00-9:30", (8, 0)),
        ("13.20-14.50", (13, 20)),
        ("без времени", None),
        ("", None),
        (None, None),
        ("99:99", None),
    ],
)
def test_parse_start(time_field, expected):
    got = classes._parse_start(time_field)
    if expected is None:
        assert got is None
    else:
        assert (got.hour, got.minute) == expected


# ── Дедупликация переживает переимпорт расписания ────────────────────────────

def test_dedup_key_survives_schedule_reimport():
    """Раньше ключом был rowid, и загрузка расписания среди дня дублировала пуши."""
    from vkbot.models import sent_notifs

    _add("Философия", "")
    before = classes._day_rows(COURSE, DIRECTION, DAY, "чёт")[0]
    key_before = sent_notifs.class_key(COURSE, DIRECTION, before[1], before[3])
    sent_notifs.mark(11, key_before, "2026-09-01", "08:00")

    # Переимпорт: таблица переписывается целиком, rowid становятся другими.
    with db.connect(config.SCHEDULE_DB) as conn:
        conn.execute("DELETE FROM schedule")
    _add("Другая пара", "")   # сдвигает нумерацию строк
    _add("Философия", "")

    after = [r for r in classes._day_rows(COURSE, DIRECTION, DAY, "чёт") if r[1] == "Философия"][0]
    key_after = sent_notifs.class_key(COURSE, DIRECTION, after[1], after[3])

    assert key_after == key_before
    assert sent_notifs.was_sent(11, key_after, "2026-09-01", "08:00"), (
        "после переимпорта уведомление не должно уйти повторно"
    )


def test_dedup_key_normalizes_case_and_spaces():
    from vkbot.models import sent_notifs

    a = sent_notifs.class_key(1, "ИСиТ ", "Философия", "301")
    b = sent_notifs.class_key(1, "исит", " философия", "301")
    assert a == b


def test_dedup_key_separates_different_classes():
    from vkbot.models import sent_notifs

    keys = {
        sent_notifs.class_key(1, "ИСиТ", "Философия", "301"),
        sent_notifs.class_key(1, "ИСиТ", "Матанализ", "301"),
        sent_notifs.class_key(2, "ИСиТ", "Философия", "301"),
        sent_notifs.class_key(1, "Педагогика", "Философия", "301"),
    }
    assert len(keys) == 4
