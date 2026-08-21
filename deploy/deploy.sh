#!/bin/bash
# Обновление боевой машины: подтянуть origin/main и перезапустить сервисы.
#
#   cd /root/vkbot && bash deploy/deploy.sh
#
# Скрипт лежал только на сервере и в репозиторий не попадал — то есть процедура
# обновления существовала, но нигде не была записана. Здесь она же, плюс
# проверка, что после перезапуска сервис действительно отвечает: systemd
# считает «active» и тот процесс, который поднялся и молча не работает.

set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/root/vkbot}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/healthz}"

cd "$PROJECT_DIR"

echo "[deploy] было:  $(git rev-parse --short HEAD)"
git pull --ff-only origin main
echo "[deploy] стало: $(git rev-parse --short HEAD)"

echo "[deploy] перезапуск vkbot и vkpanel..."
systemctl restart vkbot.service vkpanel.service

# /healthz отвечает 200 только когда открывается БД и все воркеры бота отметились
# свежим heartbeat. Сразу после рестарта отметок ещё нет, поэтому даём время.
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
echo "  curl -s $HEALTH_URL   # какой именно воркер молчит"
exit 1
