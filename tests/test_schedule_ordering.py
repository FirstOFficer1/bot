"""Порядок пар внутри дня — в панели и в боте одинаково.

Регрессия: панель сортировала по колонке `time` как по тексту. В расписании
2026/27 время записано как «8.15 - 9.45», и лексикографически «8» больше «1»,
поэтому утренние пары показывались после дневных. В боте сортировка была
правильной, и порядок в VK расходился с порядком в панели.
"""

from __future__ import annotations

import pytest

from vkbot import config, db
from vkbot.schedule.repo import repo

import web_panel

@pytest.fixture
def client_owner():
    """Залогиненный владелец: страницы расписания требуют входа."""
    from vkbot.models import panel_codes

    web_panel.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with web_panel.app.test_client() as c:
        code, _ttl = panel_codes.issue(1001)
        c.post("/login/code", data={"code": code})
        yield c


DIRECTION = "Информационные технологии и веб-приложения"
DAY = "Среда"

# В базах вуза встречаются три записи времени, и порядок вставки перемешан.
# Формат без пробела — тот, что лежит на проде: именно на нём прошлая версия
# сортировки молча ломалась (INSTR(time,' ') = 0 для каждой строки).
FORMATS = {
    "без пробела (прод)": [
        ("13:20-14:50", "История"),
        ("8:00-9:30", "Иностранный язык"),
        ("11:20-12:50", "Математика"),
        ("9:40-11:10", "Физика"),
        ("15:00-16:30", "Философия"),
    ],
    "с пробелами (файл 2026/27)": [
        ("12.00 - 13.30", "Математика"),
        ("8.15 - 9.45", "Иностранный язык"),
        ("15.20 - 16.50", "Философия"),
        ("9.55 - 11.25", "Физика"),
        ("13.40 - 15.10", "История"),
    ],
    "с номером пары (легаси)": [
        ("3 пара 11:20-12:50", "Математика"),
        ("1 пара 08:00-09:30", "Иностранный язык"),
        ("5 пара 15:00-16:30", "Философия"),
        ("2 пара 09:40-11:10", "Физика"),
        ("4 пара 13:20-14:50", "История"),
    ],
}

PAIRS = FORMATS["с пробелами (файл 2026/27)"]

EXPECTED = ["Иностранный язык", "Физика", "Математика", "История", "Философия"]


@pytest.fixture(autouse=True)
def _schedule():
    with db.connect(config.SCHEDULE_DB) as conn:
        conn.execute("DELETE FROM schedule")
        for time_s, subject in PAIRS:
            conn.execute(
                "INSERT INTO schedule (course, direction, day, time, subject, teacher, room, week) "
                "VALUES (1, ?, ?, ?, ?, 'Иванов И. И.', '400', '')",
                (DIRECTION, DAY, time_s, subject),
            )
    yield
    with db.connect(config.SCHEDULE_DB) as conn:
        conn.execute("DELETE FROM schedule")


def _ordered_subjects(order_sql: str) -> list[str]:
    with db.connect(config.SCHEDULE_DB) as conn:
        return [
            r[0]
            for r in conn.execute(
                f"SELECT subject FROM schedule WHERE day=? ORDER BY {order_sql}", (DAY,)
            )
        ]


def test_panel_orders_pairs_by_start_time():
    assert _ordered_subjects(web_panel._TIME_ORDER_SQL) == EXPECTED


def test_plain_text_order_would_be_wrong():
    """Фиксируем причину бага, чтобы сортировку не «упростили» обратно."""
    assert _ordered_subjects("time") != EXPECTED


def test_bot_and_panel_agree():
    """Расписание в VK и в панели должно идти в одном порядке."""
    text = repo.get_day(1, DIRECTION, DAY, "чёт")
    order_in_bot = [s for s in EXPECTED if s in text]
    positions = [text.index(s) for s in order_in_bot]
    assert positions == sorted(positions)
    assert order_in_bot == EXPECTED


def test_legacy_time_format_still_ordered():
    """Старый формат «1 пара 08:00-09:30» тоже должен сортироваться верно."""
    with db.connect(config.SCHEDULE_DB) as conn:
        conn.execute("DELETE FROM schedule")
        for n, (time_s, subject) in enumerate(
            [
                ("3 пара 12:00-13:30", "Третья"),
                ("1 пара 08:00-09:30", "Первая"),
                ("2 пара 09:55-11:25", "Вторая"),
            ]
        ):
            conn.execute(
                "INSERT INTO schedule (course, direction, day, time, subject, week) "
                "VALUES (1, ?, ?, ?, ?, '')",
                (DIRECTION, DAY, time_s, subject),
            )

    assert _ordered_subjects(web_panel._TIME_ORDER_SQL) == ["Первая", "Вторая", "Третья"]


# ── Страница расписания открывается на сегодняшнем дне ───────────────────────

def test_schedule_page_defaults_to_today(client_owner):
    """Без фильтров страница отдавала всё расписание: ~1 МБ HTML на телефон."""
    resp = client_owner.get("/schedule")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    # Активна вкладка «Сегодня», а не «Вся неделя»
    assert "quick=all" in html, "ссылка на полное расписание должна остаться"


def test_schedule_page_all_still_available(client_owner):
    resp = client_owner.get("/schedule?quick=all")
    assert resp.status_code == 200


# ── Порядок держится на всех форматах времени, которые есть в базах ──────────

@pytest.mark.parametrize("label", list(FORMATS))
def test_order_holds_for_every_time_format(label):
    """Регрессия: выражение через INSTR(time,' ') работало только с пробелами."""
    with db.connect(config.SCHEDULE_DB) as conn:
        conn.execute("DELETE FROM schedule")
        for time_s, subject in FORMATS[label]:
            conn.execute(
                "INSERT INTO schedule (course, direction, day, time, subject, week) "
                "VALUES (1, ?, ?, ?, ?, '')",
                (DIRECTION, DAY, time_s, subject),
            )

    assert _ordered_subjects(web_panel._TIME_ORDER_SQL) == EXPECTED, (
        f"неверный порядок для формата «{label}»"
    )


@pytest.mark.parametrize("label", list(FORMATS))
def test_bot_matches_panel_for_every_format(label):
    """Расписание в VK и в панели не должно расходиться ни на одном формате."""
    with db.connect(config.SCHEDULE_DB) as conn:
        conn.execute("DELETE FROM schedule")
        for time_s, subject in FORMATS[label]:
            conn.execute(
                "INSERT INTO schedule (course, direction, day, time, subject, week) "
                "VALUES (1, ?, ?, ?, ?, '')",
                (DIRECTION, DAY, time_s, subject),
            )

    text = repo.get_day(1, DIRECTION, DAY, "чёт")
    positions = [text.index(s) for s in EXPECTED]
    assert positions == sorted(positions), f"бот выдал иной порядок для «{label}»"
