#!/usr/bin/env bash
# Идемпотентная установка окружения для Cloud Agent.
# Ставит системные пакеты, venv, зависимости для разработки и браузер Playwright,
# а также создаёт локальный .env со сгенерированными секретами, если его ещё нет.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== Системные пакеты =="
sudo apt-get update -qq
# python3.12-venv нужен для `python -m venv` (в базовом образе ensurepip нет).
sudo apt-get install -y -qq python3.12-venv python3-pip

echo "== Виртуальное окружение =="
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip

echo "== Зависимости (runtime + dev + Telegram) =="
# requirements-dev.txt тянет requirements.txt, pytest, playwright, ortools, ruff.
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/pip install -r requirements-telegram.txt

echo "== Браузер для smoke-проверки панели =="
./.venv/bin/playwright install --with-deps chromium

echo "== Локальный .env =="
# .env в git не попадает (.gitignore). Генерируем dev-секреты, если файла нет.
# VK_TOKEN намеренно пустой: бот без него не стартует, но панель и тесты
# работают (VK_TOKEN="" отключает сетевые вызовы, ровно как в CI).
if [ ! -f .env ]; then
  cat > .env <<EOF
VK_TOKEN=
ADMIN_ID=1001
PANEL_SECRET=$(./.venv/bin/python -c "import secrets;print(secrets.token_hex(32))")
PANEL_BASE_URL=http://127.0.0.1:5000
PANEL_HOST=127.0.0.1
PANEL_PORT=5000
PANEL_TRUSTED_PROXIES=0
UPLOAD_API_TOKEN=$(./.venv/bin/python -c "import secrets;print(secrets.token_urlsafe(32))")
DATA_DIR=$(pwd)/.devdata
EOF
  echo "  создан .env со сгенерированными секретами"
else
  echo "  .env уже существует — оставляю как есть"
fi
mkdir -p .devdata

echo "== Готово =="
