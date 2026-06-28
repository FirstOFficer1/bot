"""Главное меню, приветствие, помощь, обработка неизвестного ввода."""

from __future__ import annotations

from ..keyboards import MAIN_KB, MISC_KB, NOTES_KB, PLANNER_KB
from ..state import store

_INTRO_KEYWORDS = {"начать", "старт", "/start", "start", "меню", "главное меню"}


async def try_intro(_bot, message, _state, text, uid) -> bool:
    """Стартовое сообщение и сброс состояния по «Меню»."""
    if not text or text.lower() in _INTRO_KEYWORDS:
        store.pop(uid, None)
        await message.answer(
            "👋 Привет! Я бот-помощник для учёбы.\n\n"
            "Я помогу тебе:\n"
            "📅 Посмотреть расписание пар\n"
            "📝 Сохранить заметки\n"
            "⏰ Создать напоминание\n"
            "🔔 Получать уведомления о начале пар\n\n"
            "Выбери нужный раздел:",
            keyboard=MAIN_KB,
        )
        return True
    if text == "🏠 Меню":
        store.pop(uid, None)
        await message.answer("Главное меню:", keyboard=MAIN_KB)
        return True
    return False


_CATEGORIES = {
    "📝 Заметки": (NOTES_KB, "📝 Заметки:"),
    "⏰ Планировщик": (PLANNER_KB, "⏰ Планировщик:"),
    "⚙️ Прочее": (MISC_KB, "⚙️ Прочее:"),
}


async def try_category(_bot, message, _state, text, uid) -> bool:
    if text not in _CATEGORIES:
        return False
    kb, prompt = _CATEGORIES[text]
    store.pop(uid, None)
    await message.answer(prompt, keyboard=kb)
    return True


async def try_help(_bot, message, _state, text, _uid) -> bool:
    if text != "❓ Помощь":
        return False
    await message.answer(
        "📖 Краткое руководство:\n\n"
        "📅 Расписание\n"
        "Нажми «Расписание» — бот определит чётность недели.\n"
        "Бот запоминает последний выбор курса и направления — в следующий раз можно сразу выбрать день.\n"
        "На любом шаге «🏠 Меню» возвращает в главное меню.\n\n"
        "📝 Заметки\n"
        "«Добавить заметку» — введи текст, бот сохранит (лимит 50 заметок).\n"
        "«Мои заметки» — список с номерами; введи номер (или несколько через запятую) для удаления.\n\n"
        "⏰ Напоминания\n"
        "«Напоминание» — показывает список активных напоминаний, можно отменить по номеру.\n"
        "«Добавить напоминание» → введи текст → выбери дату → введи время (ЧЧ:ММ).\n"
        "В указанное время придёт сообщение.\n\n"
        "🔔 Подписки на пары\n"
        "«Подписки на пары» → добавь курс и направление.\n"
        "Бот будет напоминать за 10 минут до начала каждой пары.\n\n"
        "📌 Дедлайны\n"
        "«Дедлайны» → добавь предмет, описание задачи и дату сдачи.\n"
        "Бот напомнит за 1 день и за 1 час до срока.\n"
        "Рядом с каждым дедлайном видно, сколько времени осталось.\n\n"
        "💬 Обратная связь\n"
        "Напиши предложение, вопрос или сообщи об ошибке — администратор получит уведомление.\n\n"
        "Если что-то пошло не так — напиши «Меню» для возврата в главное меню.",
        keyboard=MISC_KB,
    )
    return True


async def fallback(_bot, message, _state, _text, _uid) -> bool:
    await message.answer(
        "Не понимаю эту команду. Воспользуйся кнопками меню:",
        keyboard=MAIN_KB,
    )
    return True
