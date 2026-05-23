"""VK-бот для учёбы. Точка входа.

Реализация разнесена по пакету ``vkbot/``:
    config       — env, пути БД, тайминги
    db           — SQLite (WAL, индексы, схема)
    state        — персистентные состояния пользователей
    keyboards    — VK-клавиатуры и календарь
    sender       — отправка сообщений с обработкой ошибок VK
    schedule/    — кэш направлений и hot-reload расписания
    models/      — CRUD по доменам (notes, reminders, deadlines, subs, …)
    workers/     — фоновые задачи (напоминания, дедлайны, пары, hot-reload)
    handlers/    — обработчики сообщений по доменам
    bot          — сборка и запуск

Запуск:
    python vk_bot.py
    # или
    python -m vkbot
"""

from vkbot.bot import main

if __name__ == "__main__":
    main()
