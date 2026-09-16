#!/bin/bash
# Внешняя проверка /healthz: ловит случай «процесс жив, воркер мёртв».
# Ставится таймером deploy/vkbot-healthcheck.timer (каждые 5 минут).
#
# Алерт в VK владельцу из ADMIN_ID — один раз на аварию; при восстановлении
# шлётся «ок» и счётчик сбрасывается. Повторные падения снова алертят.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/root/vkbot}"
HEALTHZ_URL="${HEALTHZ_URL:-http://127.0.0.1:8080/healthz}"
STATE_DIR="${STATE_DIR:-/var/lib/vkbot}"
STATE_FILE="${STATE_DIR}/healthz_down"
cd "$PROJECT_DIR"

mkdir -p "$STATE_DIR"

body=""
http_code=0
if body=$(curl -fsS -m 10 -w '\n%{http_code}' "$HEALTHZ_URL" 2>/dev/null); then
  http_code=$(printf '%s' "$body" | tail -n1)
  body=$(printf '%s' "$body" | sed '$d')
else
  http_code=0
  body='{"status":"unreachable"}'
fi

ok=0
if [ "$http_code" = "200" ]; then
  if printf '%s' "$body" | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
    ok=1
  fi
fi

notify() {
  local text=$1
  PROJECT_DIR="$PROJECT_DIR" "$PROJECT_DIR/venv/bin/python" - "$text" <<'PY'
import itertools, os, sys
from pathlib import Path

root = Path(os.environ.get("PROJECT_DIR", "/root/vkbot"))
sys.path.insert(0, str(root))
from dotenv import load_dotenv
load_dotenv(root / ".env")

from vkbot.notifier import send_one

uid = int(os.getenv("ADMIN_ID") or "0")
if not uid:
    raw = os.getenv("ADMIN_VK_IDS") or ""
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            uid = int(part)
            break
if not uid:
    print("healthcheck: no ADMIN_ID to notify", file=sys.stderr)
    sys.exit(0)
ok, err = send_one(uid, sys.argv[1], itertools.count())
if not ok:
    print(f"healthcheck: VK notify failed err={err}", file=sys.stderr)
    sys.exit(1)
PY
}

if [ "$ok" -eq 1 ]; then
  if [ -f "$STATE_FILE" ]; then
    rm -f "$STATE_FILE"
    notify "✅ elschedule.ru /healthz снова ok — воркеры отвечают." || true
    echo "healthcheck: recovered, notified"
  else
    echo "healthcheck: ok"
  fi
  exit 0
fi

# Падение
snippet=$(printf '%s' "$body" | tr '\n' ' ' | cut -c1-240)
echo "healthcheck: FAIL http=$http_code body=$snippet" >&2

if [ -f "$STATE_FILE" ]; then
  echo "healthcheck: already alerted, skip"
  exit 1
fi

touch "$STATE_FILE"
notify "⚠️ elschedule.ru /healthz не ok (HTTP ${http_code}). Проверь: systemctl status vkbot vkpanel; journalctl -u vkbot -n 50. Ответ: ${snippet}" || true
exit 1
