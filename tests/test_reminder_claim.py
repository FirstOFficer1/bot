"""Claim-before-send: напоминание не уходит дважды при гонке/рестарте."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from vkbot.models import reminders


def test_claim_sent_is_single_winner():
    reminders.add(42, "тест", "2030-01-01 10:00")
    rid = reminders.list_pending()[0][0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        wins = list(pool.map(lambda _: reminders.claim_sent(rid), range(8)))

    assert sum(1 for w in wins if w) == 1
    assert reminders.list_pending() == []


def test_unclaim_returns_reminder_to_pending():
    reminders.add(42, "тест", "2030-01-01 10:00")
    rid = reminders.list_pending()[0][0]
    assert reminders.claim_sent(rid)
    reminders.unclaim(rid)
    assert reminders.list_pending()[0][0] == rid
