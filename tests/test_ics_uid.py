"""UID событий в /calendar.ics должен переживать рестарт панели.

Раньше использовался встроенный hash(), который в CPython рандомизируется
на каждый процесс — Google/Apple Calendar после деплоя видели те же пары
как новые события.
"""

from __future__ import annotations

import re

import web_panel


def test_ics_event_uid_is_stable_across_calls():
    pair = (
        1, "Прикладная информатика", "Понедельник",
        "8.15 - 9.45", "Математика", "Иванов И. И.", "301",
        "", "лк", "",
    )
    a = web_panel._build_ics([pair], weeks_ahead=1)
    b = web_panel._build_ics([pair], weeks_ahead=1)

    uids_a = re.findall(r"^UID:(.+)$", a, re.M)
    uids_b = re.findall(r"^UID:(.+)$", b, re.M)
    assert uids_a, "ожидалось хотя бы одно событие"
    assert uids_a == uids_b
    assert all("@" in u for u in uids_a)
    assert not any(u.startswith("None") for u in uids_a)
