"""Telegram-бот на общем пайплайне vkbot (расписание, заметки, напоминания, дедлайны)."""

from .bot import amain, build_dispatcher, main

__all__ = ["amain", "build_dispatcher", "main"]
