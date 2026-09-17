"""Красные флаги в пользовательском тексте (заметки / напоминания / дедлайны).

Тихий скан после сохранения: пользователю ничего не говорим, владельцам —
короткий алерт (категория, фрагмент, id). Это эвристика по фразам, не
диагноз и не доказательство; ложные срабатывания возможны.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

from . import sender
from .config import env_owner_ids
from .ids import is_telegram, to_telegram
from .models import audit, panel_users

log = logging.getLogger(__name__)

# Не спамим одним и тем же (uid, category) чаще раза в час.
_COOLDOWN_SEC = 3600
_last_alert_at: dict[tuple[int, str], float] = {}

_SNIPPET_LEN = 140

CATEGORY_LABELS = {
    "crisis": "кризис / самоповреждение",
    "violence": "угроза насилия",
    "illegal": "возможное правонарушение",
}

# Фразы намеренно конкретные: короткие общие слова («смерть», «убить»)
# дают слишком много ложных срабатываний в учёбе («убить время»).
_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "crisis": tuple(
        re.compile(p, re.IGNORECASE | re.UNICODE)
        for p in (
            r"суицид",
            r"самоубий",
            r"поконч\w*\s+с\s+собой",
            r"хочу\s+умереть",
            r"не\s+хочу\s+жить",
            r"нет\s+смысла\s+жить",
            r"лучше\s+бы\s+я\s+умер",
            r"лучше\s+бы\s+меня\s+не\s+было",
            r"разрежу\s+вен",
            r"вскро\w*\s+вен",
            r"наглота\w*\s+таблет",
            r"прыгну\w*\s+с\s+(крыш|мост|окн)",
            r"самоповрежд",
            r"порезать\s+себя",
            r"режу\s+себя",
        )
    ),
    "violence": tuple(
        re.compile(p, re.IGNORECASE | re.UNICODE)
        for p in (
            r"убью\s+(его|её|ее|их|тебя|вас|всех|препод|учител|одногрупп)",
            r"убий\s+(его|её|ее|их|тебя|всех)",
            r"расстрел\w*\s+(школ|универ|одногрупп|препод)",
            r"взорву\s+(школ|универ|общежит|здани)",
            r"зарежу\s+(его|её|ее|их|тебя)",
            r"изобью\s+(его|её|ее|их|тебя)",
            r"угрожаю\s+убить",
            r"планирую\s+убить",
        )
    ),
    "illegal": tuple(
        re.compile(p, re.IGNORECASE | re.UNICODE)
        for p in (
            r"как\s+сделать\s+бомб",
            r"сварю\s+бомб",
            r"изготов\w*\s+взрывчат",
            r"куплю\s+оружие\s+без\s+лиценз",
            r"закладк\w*\s+(нарко|спайс|меф|соль)",
            r"свар\w*\s+(меф|амфет|мет)",
            r"прода\w*\s+(нарко|спайс|меф|героин|кокаин)",
            r"заказать\s+убийство",
            r"нанять\s+киллера",
            r"подготов\w*\s+теракт",
            r"устроить\s+теракт",
        )
    ),
}


@dataclass(frozen=True)
class Hit:
    category: str
    matched: str


def scan(text: str) -> list[Hit]:
    """Возвращает сработавшие категории (по одной на категорию, без дублей)."""
    if not text or not text.strip():
        return []
    found: list[Hit] = []
    seen: set[str] = set()
    for category, patterns in _PATTERNS.items():
        for pat in patterns:
            m = pat.search(text)
            if m:
                if category not in seen:
                    seen.add(category)
                    found.append(Hit(category=category, matched=m.group(0)[:80]))
                break
    return found


def snippet(text: str, limit: int = _SNIPPET_LEN) -> str:
    s = " ".join((text or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def owner_ids() -> list[int]:
    ids = set(env_owner_ids())
    try:
        ids |= panel_users.owner_ids()
    except Exception:
        log.exception("не удалось прочитать владельцев для safety-алерта")
    return sorted(ids)


def _user_label(uid: int) -> str:
    if is_telegram(uid):
        return f"Telegram id{to_telegram(uid)} (uid {uid})"
    return f"VK id{uid}"


def _source_label(source: str) -> str:
    return {
        "note": "заметка",
        "reminder": "напоминание",
        "deadline": "дедлайн",
    }.get(source, source)


def _should_alert(uid: int, category: str) -> bool:
    key = (uid, category)
    now = time.time()
    last = _last_alert_at.get(key, 0.0)
    if now - last < _COOLDOWN_SEC:
        return False
    _last_alert_at[key] = now
    return True


def reset_cooldowns() -> None:
    """Сброс антиспама — для тестов."""
    _last_alert_at.clear()


def _format_alert(uid: int, source: str, hits: list[Hit], text: str) -> str:
    cats = ", ".join(CATEGORY_LABELS.get(h.category, h.category) for h in hits)
    matched = ", ".join(repr(h.matched) for h in hits)
    return (
        "🚩 Красный флаг\n"
        f"Кто: {_user_label(uid)}\n"
        f"Где: {_source_label(source)}\n"
        f"Категория: {cats}\n"
        f"Совпадение: {matched}\n"
        f"Фрагмент: {snippet(text)}\n\n"
        "Это автоматическая эвристика, не диагноз. "
        "Пользователю ничего не сообщали."
    )


async def maybe_alert(bot, uid: int, source: str, text: str) -> list[Hit]:
    """Сканирует текст и при срабатывании пишет владельцам. Не бросает наружу."""
    try:
        hits = scan(text)
    except Exception:
        log.exception("safety.scan упал uid=%s source=%s", uid, source)
        return []
    if not hits:
        return []

    recipients = [oid for oid in owner_ids() if oid != uid]
    if not recipients:
        log.warning("safety: нет владельцев для алерта uid=%s", uid)
        return hits

    new_hits = [h for h in hits if _should_alert(uid, h.category)]
    if not new_hits:
        return hits

    body = _format_alert(uid, source, new_hits, text)
    try:
        cats = ",".join(h.category for h in new_hits)
        await asyncio.to_thread(
            audit.log, uid, "safety.red_flag", source,
            f"{cats}; {snippet(text, 80)}",
        )
    except Exception:
        log.exception("safety: audit не записался")

    for oid in recipients:
        try:
            await sender.send(bot, oid, body)
        except Exception:
            log.exception("safety: не удалось отправить алерт → %s", oid)
    return hits
