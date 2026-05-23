"""Entry-point: инициализация бота, БД, воркеров и регистрация хендлеров."""

from __future__ import annotations

import logging

from vkbottle import Bot

from . import config, db, handlers
from .schedule import repo
from .state import store
from .workers import classes, deadlines, reminders, schedule_reloader


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )


def build_bot() -> Bot:
    """Создаёт сконфигурированного бота. Не запускает loop."""
    if not config.VK_TOKEN:
        raise RuntimeError(
            "VK_TOKEN не задан. Заполните .env или переменную окружения."
        )
    db.init()
    store.load_all()
    repo.reload()

    bot = Bot(token=config.VK_TOKEN)
    handlers.register(bot)
    return bot


def main() -> None:
    _setup_logging()
    bot = build_bot()
    bot.loop_wrapper.add_task(reminders.run(bot))
    bot.loop_wrapper.add_task(deadlines.run(bot))
    bot.loop_wrapper.add_task(classes.run(bot))
    bot.loop_wrapper.add_task(schedule_reloader.run(bot))
    logging.info("VK bot is starting...")
    bot.run_forever()


if __name__ == "__main__":
    main()
