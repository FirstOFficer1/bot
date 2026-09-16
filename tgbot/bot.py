"""Telegram-бот: тот же пайплайн хендлеров, что у VK, без GPT и прочего легаси."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.types import Message

from vkbot import config, db, handlers, sender
from vkbot.ids import from_telegram
from vkbot.schedule import repo
from vkbot.state import store
from vkbot.workers import classes, deadlines, reminders, schedule_reloader

from .message import AdapterMessage

log = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        stream=sys.stdout,
    )


def build_dispatcher(bot: Bot) -> Dispatcher:
    """Собирает диспетчер с общим пайплайном vkbot.handlers."""
    dp = Dispatcher()

    @dp.message()
    async def on_message(message: Message) -> None:
        if message.chat.type != "private":
            await message.answer(
                "Персональные команды работают только в личных сообщениях с ботом."
            )
            return
        if not message.from_user:
            return
        uid = from_telegram(message.from_user.id)
        adapted = AdapterMessage(message, uid)
        try:
            await handlers.dispatch_pipeline(bot, adapted, uid, adapted.text)
        except Exception:
            log.exception("Ошибка обработки сообщения от uid=%s", uid)
            with contextlib.suppress(Exception):
                await message.answer(
                    "Что-то пошло не так. Напиши «Меню» или попробуй ещё раз чуть позже."
                )

    return dp


async def _run_workers(bot: Bot) -> None:
    """Те же воркеры, что у VK. Claim в БД не даёт дублей при двух процессах."""
    await asyncio.gather(
        reminders.run(bot),
        deadlines.run(bot),
        classes.run(bot),
        schedule_reloader.run(bot),
    )


def _register_vk_sender_if_possible() -> None:
    """Чтобы фидбэк с Telegram доходил владельцам во VK, если токен VK есть."""
    if not config.VK_TOKEN:
        return
    try:
        from vkbottle import Bot as VkBot

        sender.set_vk_bot(VkBot(token=config.VK_TOKEN))
    except Exception:
        log.exception("Не удалось зарегистрировать VK sender для фидбэка")


async def amain() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN не задан. Добавьте токен от @BotFather в .env "
            "(см. .env.example) и перезапустите: python tg_bot.py"
        )

    db.init()
    store.load_all()
    repo.reload()

    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    sender.set_telegram_bot(bot)
    _register_vk_sender_if_possible()
    dp = build_dispatcher(bot)

    worker_task = asyncio.create_task(_run_workers(bot), name="tg-workers")
    try:
        me = await bot.get_me()
        log.info("Telegram bot @%s starting...", me.username or me.id)
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        await bot.session.close()


def main() -> None:
    _setup_logging()
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("Telegram bot stopped")


if __name__ == "__main__":
    main()
