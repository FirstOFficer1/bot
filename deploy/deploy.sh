#!/bin/bash
# Обновление боевой машины: подтянуть origin/main и перезапустить сервисы.
#
#   cd /root/vkbot && bash deploy/deploy.sh

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/root/vkbot}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/healthz}"

cd "$PROJECT_DIR"

echo "[deploy] было:  $(git rev-parse --short HEAD)"
git pull --ff-only origin main
echo "[deploy] стало: $(git rev-parse --short HEAD)"

echo "[deploy] зависимости..."
# shellcheck disable=SC1091
source venv/bin/activate
pip install -q -r requirements.txt

echo "[deploy] перезапуск vkbot и vkpanel..."
systemctl restart vkbot.service vkpanel.service

# Telegram-бот пока отключён: unit не трогаем. Если когда-то был включён — гасим.
if systemctl list-unit-files tgbot.service 2>/dev/null | grep -q tgbot.service; then
  if systemctl is-active --quiet tgbot.service 2>/dev/null; then
    echo "[deploy] останавливаем tgbot (временно отключён)..."
    systemctl stop tgbot.service || true
    systemctl disable tgbot.service || true
  fi
fi

echo -n "[deploy] ждём /healthz"
for _ in $(seq 1 30); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)"
    if [ "$code" = "200" ]; then
        echo " — 200"
        systemctl is-active vkbot.service vkpanel.service
        echo "[deploy] готово."
        exit 0
    fi
    echo -n "."
    sleep 2
done

echo " — не дождались (последний код: ${code:-нет ответа})"
echo "[deploy] СБОЙ. Что смотреть:"
echo "  journalctl -u vkpanel -n 50 --no-pager"
echo "  journalctl -u vkbot -n 50 --no-pager"
echo "  curl -s $HEALTH_URL"
exit 1
