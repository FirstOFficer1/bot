"""Определение чётности недели."""

from __future__ import annotations

import datetime

from ..config import now_msk


def week_type_for(date: datetime.date) -> str:
    """Возвращает 'чёт' или 'нечет' для произвольной даты."""
    return "нечет" if date.isocalendar()[1] % 2 == 0 else "чёт"


def current_week_type() -> str:
    """Возвращает 'чёт' или 'нечет' по ISO-номеру недели.

    Логика сохранена 1-в-1 как в исходнике: 'нечет' если ISO-неделя чётная,
    иначе 'чёт'. Это связано с привязкой к семестру конкретного вуза;
    при необходимости здесь же легко перейти на дату начала семестра.
    """
    return week_type_for(now_msk().date())
