"""Одна группа — одна подписка, даже при одновременных сообщениях.

Проверка «уже подписан?» и вставка выполняются двумя отдельными шагами, между
которыми хендлер отдаёт управление (`await asyncio.to_thread`). Два сообщения
одного человека, обработанные одновременно, проходили проверку оба и заводили
две строки: дубли уведомлений, а кнопка отписки снимала только одну.

Гарантию даёт уникальный индекс в схеме, а не порядок вызовов в коде.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from vkbot import db
from vkbot.models import subscriptions as model

UID = 6006
COURSE = 2
DIRECTION = "Прикладная информатика"


def _count(uid: int = UID) -> int:
    with db.connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE user_id=?", (uid,)
        ).fetchone()[0]


def test_adding_twice_leaves_one_row():
    model.add(UID, COURSE, DIRECTION)
    model.add(UID, COURSE, DIRECTION)

    assert _count() == 1


def test_case_differences_do_not_create_a_second_row():
    """exists() сравнивает без учёта регистра — индекс обязан делать так же."""
    model.add(UID, COURSE, DIRECTION)
    model.add(UID, COURSE, DIRECTION.upper())
    model.add(UID, COURSE, DIRECTION.lower())

    assert _count() == 1


def test_concurrent_adds_leave_one_row():
    """Восемь одновременных попыток — ровно одна подписка."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: model.add(UID, COURSE, DIRECTION), range(8)))

    assert _count() == 1
    assert len(model.list_for(UID)) == 1


def test_different_groups_still_coexist():
    """Уникальность не должна мешать подписаться на несколько групп."""
    model.add(UID, COURSE, DIRECTION)
    model.add(UID, COURSE + 1, DIRECTION)
    model.add(UID, COURSE, "Юриспруденция")

    assert _count() == 3


def test_other_users_are_not_affected():
    model.add(UID, COURSE, DIRECTION)
    model.add(UID + 1, COURSE, DIRECTION)

    assert _count() == 1
    assert _count(UID + 1) == 1


def test_unsubscribing_lets_you_subscribe_again():
    model.add(UID, COURSE, DIRECTION)
    sid = model.list_for(UID)[0][0]

    model.delete(sid, UID)
    model.add(UID, COURSE, DIRECTION)

    assert _count() == 1
