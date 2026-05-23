#!/bin/bash
# Запуск VK-бота. Для Telegram используется bot.py отдельным процессом.

set -e
echo "Starting VK bot..."
python3 vk_bot.py
