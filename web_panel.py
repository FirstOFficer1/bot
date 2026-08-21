"""
Веб-панель VK-бота: трёхуровневая модель прав.

Роли:
    owner — VK ID из env ADMIN_ID. Может управлять админами.
    admin — добавляется owner'ом через панель, хранится в panel_users.
    user  — любой залогиненный, видит только свой /me.

Авторизация — только через одноразовый код от бота (команда /login).

Переменные окружения (.env):
    PANEL_SECRET    — секрет Flask-сессий (обязателен для persistent cookies)
    ADMIN_ID        — VK user_id владельца панели (super-admin)
    PANEL_BASE_URL  — базовый URL панели, напр. http://109.73.205.252:8080
    UPLOAD_API_TOKEN — Bearer-токен для REST API загрузки расписания

Запуск:
    python web_panel.py                  # порт 5000
    python web_panel.py --port 8080      # свой порт
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import secrets
import sqlite3
import sys
import tempfile
import threading
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    g,
    jsonify,
    make_response,
    redirect,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)

from vkbot import config as _bot_config
from vkbot import notifier, vk_names
from vkbot.models import (
    audit, heartbeats, panel_codes, panel_remember, panel_users, seen_users,
    subscriptions, user_data,
)
from vkbot.schedule import loader as schedule_loader

load_dotenv()

app = Flask(__name__)
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from flask_wtf.csrf import CSRFProtect, CSRFError
csrf = CSRFProtect(app)
app.config["WTF_CSRF_TIME_LIMIT"] = None  # держим токен пока живёт сессия
# Загрузка расписания читается в память для валидации zip-структуры, поэтому
# ограничиваем размер запроса. Реальный файл расписания — сотни килобайт.
app.config["MAX_CONTENT_LENGTH"] = _bot_config.int_env("PANEL_MAX_UPLOAD_MB", 16) * 1024 * 1024


@app.errorhandler(413)
def _too_large(_e):
    limit_mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return (f"Файл слишком большой (лимит {limit_mb} МБ).", 413)


@app.errorhandler(CSRFError)
def _csrf_error(e):
    return ("CSRF token missing or invalid. Reload the page and try again.", 400)

_panel_secret = os.getenv("PANEL_SECRET")
if not _panel_secret:
    raise SystemExit("FATAL: PANEL_SECRET env var is required (set 32+ random hex chars)")
app.secret_key = _panel_secret

# Persistent sessions: cookie живёт 30 дней, не сбрасывается при закрытии браузера
import datetime as _dt

app.config["PERMANENT_SESSION_LIFETIME"] = _dt.timedelta(days=365)
app.config["SESSION_COOKIE_HTTPONLY"] = True
# SAMESITE=None требует Secure (иначе браузер молча дропнет cookie).
# На локалке без https используем Lax (Mini App не заработает локально — ок).
_secure_cookies = os.getenv("PANEL_BASE_URL", "").startswith("https://")
app.config["SESSION_COOKIE_SECURE"] = _secure_cookies
app.config["SESSION_COOKIE_SAMESITE"] = "None" if _secure_cookies else "Lax"

# _client_ip() читает X-Real-IP / X-Forwarded-For — без ProxyFix эти заголовки
# можно подделать любым запросом и обойти per-IP лимит на /login/code.
# PANEL_TRUSTED_PROXIES = число реальных прокси перед приложением (nginx → 1).
# 0 (по умолчанию) = приложение смотрит в интернет напрямую, заголовкам не верим.
_TRUSTED_PROXIES = max(0, _bot_config.int_env("PANEL_TRUSTED_PROXIES", 0))
if _TRUSTED_PROXIES:
    from werkzeug.middleware.proxy_fix import ProxyFix

    # x_host=0 намеренно: наш nginx проставляет X-Real-IP и X-Forwarded-Proto,
    # но X-Forwarded-Host не выставляет и не вырезает — значит, этот заголовок
    # пришёл бы от клиента и мог подменить Host в генерации ссылок.
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=_TRUSTED_PROXIES, x_proto=_TRUSTED_PROXIES,
        x_host=0, x_prefix=0,
    )


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-ancestors https://vk.com https://*.vk.com https://vk.ru https://*.vk.ru; "
        "base-uri 'self'; "
        "form-action 'self';")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # X-Frame-Options не умеет списки доменов, а нам нужен iframe VK Mini App.
    # Он и CSP frame-ancestors противоречили друг другу: браузер, уважающий XFO,
    # блокировал ровно тот фрейм, который разрешает CSP. Оставляем один источник
    # правды — frame-ancestors выше; для древних браузеров без CSP-3 ставим
    # DENY только когда встраивание в VK не нужно (панель не за https).
    if not PANEL_BASE_URL.startswith("https://"):
        resp.headers.setdefault("X-Frame-Options", "DENY")
    if _secure_cookies:
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return resp


@app.route("/robots.txt")
def robots_txt():
    """Запрещаем поисковикам индексировать панель целиком."""
    body = "User-agent: *\nDisallow: /\n"
    return body, 200, {"Content-Type": "text/plain; charset=utf-8"}


# ── Проверка живости для внешнего монитора ────────────────────────────────────
#
# systemd видит только смерть процесса. Отказ, который реально случался, —
# воркер бота молча встал: процесс жив, бот отвечает на сообщения, а напоминания
# и пуши о парах не приходят. Здесь это видно — воркеры отмечаются в
# worker_heartbeats, и просроченная отметка роняет ответ в 503.
#
# Маршрут открыт без авторизации: его дёргает монитор. Наружу отдаём только
# факты живости — ни пользовательских данных, ни путей, ни версий.

def _worker_limits() -> dict[str, int]:
    """Имя воркера → предельный возраст отметки: три такта опроса плюс запас."""
    c = _bot_config
    return {
        heartbeats.REMINDERS: c.REMINDER_POLL_SEC * 3 + 60,
        heartbeats.DEADLINES: c.DEADLINE_POLL_SEC * 3 + 60,
        heartbeats.CLASSES: c.CLASS_NOTIFY_POLL_SEC * 3 + 60,
        heartbeats.SCHEDULE_RELOADER: c.SCHEDULE_RELOAD_POLL_SEC * 3 + 60,
    }


@app.route("/healthz")
def healthz():
    """200 — всё живо, 503 — что-то встало. В теле JSON с деталями по узлам."""
    checks: dict[str, object] = {}
    healthy = True

    # Заодно подчищаем счётчики rate-limit: они живут в памяти этого процесса,
    # растут по записи на каждый уникальный IP и больше ниоткуда не убираются.
    # Монитор дёргает /healthz регулярно — удобная точка для такой уборки.
    try:
        panel_codes.cleanup_rate_limits()
    except Exception:
        logging.exception("healthz: не удалось почистить счётчики rate-limit")

    try:
        with _notes_conn() as conn:
            conn.execute("SELECT 1").fetchone()
        checks["db"] = "ok"
    except Exception:
        logging.exception("healthz: notes.db недоступна")
        checks["db"] = "fail"
        healthy = False

    ticks = heartbeats.all_ticks()
    workers: dict[str, object] = {}
    for name, limit in _worker_limits().items():
        age = heartbeats.age_seconds(ticks.get(name))
        if age is None:
            # Ни одной отметки: бот не запущен или поднялся только что.
            workers[name] = {"state": "never", "limit_sec": limit}
            healthy = False
        elif age > limit:
            workers[name] = {"state": "stale", "age_sec": int(age), "limit_sec": limit}
            healthy = False
        else:
            workers[name] = {"state": "ok", "age_sec": int(age), "limit_sec": limit}
    checks["workers"] = workers

    return jsonify({"status": "ok" if healthy else "degraded", "checks": checks}), (
        200 if healthy else 503
    )


# Remember-me — кука с долгоживущим токеном (1 год), авто-восстанавливает сессию
_REMEMBER_COOKIE = panel_remember.COOKIE_NAME
_REMEMBER_TTL_S = panel_remember.TOKEN_TTL_DAYS * 24 * 3600


def _set_remember_cookie(resp, token: str) -> None:
    resp.set_cookie(
        _REMEMBER_COOKIE, token,
        max_age=_REMEMBER_TTL_S,
        httponly=True,
        samesite=app.config["SESSION_COOKIE_SAMESITE"],
        secure=app.config["SESSION_COOKIE_SECURE"],
        path="/",
    )


def _clear_remember_cookie(resp) -> None:
    resp.delete_cookie(_REMEMBER_COOKIE, path="/")

PANEL_BASE_URL = os.getenv("PANEL_BASE_URL", "http://localhost:5000").rstrip("/")
UPLOAD_API_TOKEN = os.getenv("UPLOAD_API_TOKEN", "")


def _parse_owner_ids() -> set[int]:
    """Owner — VK ID из ADMIN_ID и/или ADMIN_VK_IDS env. Хардкод, нельзя удалить."""
    ids: set[int] = set()
    raw = os.getenv("ADMIN_VK_IDS", "")
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    for key in ("ADMIN_VK_ID", "ADMIN_ID"):
        val = os.getenv(key, "").strip()
        if val.isdigit() and int(val) > 0:
            ids.add(int(val))
    return ids


OWNER_VK_IDS: set[int] = _parse_owner_ids()


def _normalize_code(raw: str | None) -> str:
    """Оставляет от введённого кода только цифры.

    Код приходит из чата VK, и при копировании к нему цепляются пробелы,
    неразрывный пробел, а иногда невидимые символы. Пользователь видит
    «правильные» шесть цифр и получает «неверный код» — самая обидная ошибка
    на входе. Внутри всё равно проверяется, что цифр ровно шесть.
    """
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def _assert_owner_exists() -> None:
    """Панель без единого владельца неуправляема — падаем сразу, а не потом."""
    if OWNER_VK_IDS:
        return
    try:
        from vkbot.models import panel_users as _pu

        if _pu.owner_ids():
            return
    except Exception as exc:
        # Раньше сбой чтения БД сообщался как «владелец не настроен», и админ
        # шёл править .env вместо того, чтобы чинить базу.
        logging.exception("Не удалось проверить владельцев в panel_users")
        raise SystemExit(
            f"FATAL: не удалось прочитать список владельцев из {NOTES_DB}: {exc.__class__.__name__}. "
            "Проверьте, что файл БД доступен на чтение и запись, и повторите запуск."
        ) from exc
    raise SystemExit(
        "FATAL: не задан ни один владелец панели. "
        "Укажите ADMIN_ID (или ADMIN_VK_IDS) в .env — иначе управлять админами будет некому."
    )

# Пути к БД берём из конфига пакета бота (импортирован выше): он собирает их
# абсолютными от корня проекта. Относительные пути ломались, если сервис
# стартовал не из корня — панель и бот открывали разные файлы notes.db.
NOTES_DB       = _bot_config.NOTES_DB
SCHEDULE_DB_VK = _bot_config.SCHEDULE_DB   # 'с' в имени файла — кириллица
# База легаси Telegram-бота. Путь переопределяется так же, как остальные:
# при другом DATA_DIR (тесты, dev-запуск) панель иначе писала бы в файл в
# корне проекта мимо всех остальных баз.
SCHEDULE_DB_S  = os.getenv("LEGACY_SCHEDULE_DB") or str(_bot_config.DATA_DIR / "s.db")


# Схема БД создаётся здесь же: панель может быть поднята раньше бота, а таблицы
# panel_login_codes / panel_remember_tokens нужны ей для самого логина.
# vkbot.db.init() идемпотентен — безопасно вызывать на каждом старте.
try:
    from vkbot import db as _bot_db

    _bot_db.init()
except Exception:
    logging.exception("Не удалось инициализировать схему БД при старте панели")

_assert_owner_exists()


# ── DB-хелперы ────────────────────────────────────────────────────────────────


class _Conn(sqlite3.Connection):
    """Соединение, которое на выходе из `with` не только коммитит, но и закрывается.

    Штатный sqlite3.Connection.__exit__ управляет только транзакцией, поэтому
    `with sqlite3.connect(...) as conn:` оставлял открытый дескриптор до GC.
    """

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _notes_conn():
    return sqlite3.connect(NOTES_DB, factory=_Conn)


def _sched_conn(vk: bool = True):
    return sqlite3.connect(SCHEDULE_DB_VK if vk else SCHEDULE_DB_S, factory=_Conn)


# ── Авторизация и роли ────────────────────────────────────────────────────────
#
# DB-токен (panel_remember_tokens) — единственный источник правды по авторизации.
# Flask-сессия — тонкий fallback для самого первого редиректа после /login/code
# (когда RM-куки ещё не вернулась от браузера) и для flash-сообщений.
# Роли (is_admin/is_owner) ВСЕГДА читаются свежими из БД в g.user — никаких
# кешей в session.


def _db_owner_ids() -> set[int]:
    """Владельцы, выданные через панель (panel_users.role='owner')."""
    try:
        return panel_users.owner_ids()
    except Exception:
        logging.exception("Не удалось прочитать владельцев из panel_users")
        return set()


def _all_owner_ids() -> set[int]:
    """Полный состав владельцев: несменяемые из env плюс выданные в панели."""
    return OWNER_VK_IDS | _db_owner_ids()


def _is_owner(uid: int | None) -> bool:
    return uid is not None and uid in _all_owner_ids()


def _is_admin(uid: int | None) -> bool:
    """Owner всегда admin. Иначе — смотрим panel_users."""
    if uid is None:
        return False
    if uid in OWNER_VK_IDS:
        return True
    try:
        return panel_users.is_admin(uid)
    except Exception:
        return False


def _build_user(uid: int) -> dict:
    """Собирает свежий снимок пользователя для g.user."""
    return {
        "vk_id": uid,
        "is_admin": _is_admin(uid),
        "is_owner": _is_owner(uid),
        "name": vk_names.resolve_one(uid),
    }


def _current_vk_id() -> int | None:
    """Возвращает VK ID текущего юзера (через g.user)."""
    user = getattr(g, "user", None)
    return user["vk_id"] if user else None


def verify_vk_launch_sign(args, secret: str) -> bool:
    """Проверяет подпись launch-параметров VK Mini App (HMAC-SHA256).

    VK при открытии Mini App передаёт параметры vk_* и поле `sign`. Подпись:
    отсортированные vk_*-параметры → urlencode → HMAC-SHA256 на «защищённом
    ключе» приложения → base64-urlsafe без padding. Сравнение константное.

    Возвращает True только если sign присутствует и совпадает. Если sign нет
    (обычный заход не из VK) — False (вызывающий решает, что делать).
    """
    if not secret:
        return False
    sign = args.get("sign", "")
    if not sign:
        return False
    vk_params = sorted((k, v) for k, v in args.items() if k.startswith("vk_"))
    if not vk_params:
        return False
    from urllib.parse import urlencode
    query = urlencode(vk_params)
    digest = hmac.new(secret.encode(), query.encode(), hashlib.sha256).digest()
    expected = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return hmac.compare_digest(expected, sign)


# Правила VK Mini Apps, п. 1.1.2: внутри VK пользователь должен авторизоваться
# бесшовно по vk_user_id из подписанных launch-параметров. Просить у него код,
# почту или VK ID — избыточно, модерация такое не принимает.
# Правила VK Mini Apps, п. 2.4.1: в приложении должен быть доступный способ
# связи. Ведём в диалог с сообществом бота — это и есть наша поддержка.
# Кто обрабатывает данные — это должно быть написано в самом приложении
# (типовая политика VK, п. 1.3). Задаётся в .env: для 152-ФЗ нужны ФИО или
# наименование оператора, а не название сообщества.
DEVELOPER_NAME = os.getenv("DEVELOPER_NAME") or "администратор сервиса «Электронное расписание»"

VK_GROUP_ID = _bot_config.int_env("VK_GROUP_ID")
SUPPORT_URL = os.getenv("SUPPORT_URL") or (
    f"https://vk.com/im?sel=-{VK_GROUP_ID}" if VK_GROUP_ID else "https://vk.com/im"
)

VK_APP_ID = _bot_config.int_env("VK_APP_ID")

# Насколько старый запуск ещё пускаем внутрь. Подпись сама по себе не истекает,
# а launch-URL вместе с ней остаётся в истории браузера и на скриншотах,
# поэтому ограничиваем окно. Сутки — компромисс: сессия Mini App живёт долго,
# а куки в iframe браузеры режут, и подпись может остаться единственным
# доказательством личности на протяжении всего сеанса.
VK_LAUNCH_MAX_AGE_SEC = _bot_config.int_env("VK_LAUNCH_MAX_AGE_SEC", 24 * 3600)


def vk_launch_user_id(args, secret: str, *, now: float | None = None) -> int | None:
    """VK ID пользователя из launch-параметров — или None, если верить нечему.

    Требуем три вещи: валидную подпись, наш vk_app_id (чужое приложение не
    должно пускать в нашу панель) и свежесть запуска по vk_ts.
    """
    if not verify_vk_launch_sign(args, secret):
        return None

    if VK_APP_ID:
        try:
            if int(args.get("vk_app_id", 0)) != VK_APP_ID:
                return None
        except (TypeError, ValueError):
            return None

    ts_raw = args.get("vk_ts")
    if ts_raw:
        try:
            age = (now if now is not None else time.time()) - int(ts_raw)
        except (TypeError, ValueError):
            return None
        if age > VK_LAUNCH_MAX_AGE_SEC or age < -300:
            return None

    try:
        uid = int(args.get("vk_user_id", 0))
    except (TypeError, ValueError):
        return None
    return uid or None


# Подписи уже виденных запусков: только чтобы не дублировать запись о входе.
# Живёт в памяти процесса — панель работает строго в одном (см. заголовок файла).
_LAUNCH_SEEN: dict[str, float] = {}
_LAUNCH_SEEN_TTL_SEC = 3600


def _launch_is_new(sign: str) -> bool:
    """True, если этот запуск ещё не отмечали в журнале за последний час."""
    if not sign:
        return False
    now = time.time()
    for old_sign, seen_at in list(_LAUNCH_SEEN.items()):
        if now - seen_at > _LAUNCH_SEEN_TTL_SEC:
            _LAUNCH_SEEN.pop(old_sign, None)
    if sign in _LAUNCH_SEEN:
        return False
    _LAUNCH_SEEN[sign] = now
    return True


# Правила VK Mini Apps, п. 1.2.6: приложение должно ограничивать частоту
# запросов и переживать их превышение. До сих пор лимит стоял только на входе
# по коду — то есть флуд по любой другой странице ничем не сдерживался.
# Счётчик в памяти процесса: панель работает строго в одном (см. заголовок).
PANEL_RATE_LIMIT_RPM = _bot_config.int_env("PANEL_RATE_LIMIT_RPM", 240)
_req_hits: dict[str, deque] = defaultdict(deque)


def _too_many_requests(ip: str) -> bool:
    """True, если этот IP превысил лимит запросов за минуту."""
    if PANEL_RATE_LIMIT_RPM <= 0:
        return False
    now = time.time()
    hits = _req_hits[ip]
    while hits and now - hits[0] > 60:
        hits.popleft()
    if len(hits) >= PANEL_RATE_LIMIT_RPM:
        return True
    hits.append(now)
    if len(_req_hits) > 5000:          # чистим словарь, чтобы не рос бесконечно
        for stale_ip in [k for k, v in _req_hits.items() if not v or now - v[-1] > 300]:
            _req_hits.pop(stale_ip, None)
    return False


@app.before_request
def _rate_limit():
    """Общий лимит частоты запросов на IP.

    Мониторинг и статику не считаем: /healthz дёргают по расписанию, а картинки
    и шрифты — часть одной страницы, и на них лимит расходовать бессмысленно.
    """
    if app.config.get("TESTING"):
        return
    if request.path.startswith("/static/") or request.path == "/healthz":
        return
    if _too_many_requests(_client_ip()):
        return make_response(
            "Слишком много запросов. Подождите минуту и повторите.", 429,
            {"Retry-After": "60", "Content-Type": "text/plain; charset=utf-8"},
        )


@app.before_request
def _check_vk_sign():
    """Бесшовный вход из VK Mini App по подписанным launch-параметрам.

    Подпись проверяется на каждом запросе, а не только при первом: куки в
    iframe браузеры блокируют всё чаще, и тогда launch-параметры остаются
    единственным, чем пользователь может себя подтвердить.

    Подделки по-прежнему пишутся в аудит: это и наблюдаемость, и доказательство
    для модерации VK, что приложение launch-параметры действительно проверяет.
    """
    g.vk_sign_ok = False
    if "sign" not in request.args:
        return
    secret = os.getenv("VK_APP_SECRET", "")
    uid = vk_launch_user_id(request.args, secret)
    g.vk_sign_ok = uid is not None
    if uid is None:
        audit.log(None, "vk.sign_invalid", _client_ip(), request.path)
        return

    g.vk_launch_uid = uid

    # Сессию ставим всегда (это дёшево и идемпотентно), а вот запись в аудит и
    # отметку о посещении — один раз на запуск. Если браузер режет куки в
    # iframe, сессия не переживёт запрос, и без этой защиты каждый запрос
    # писал бы «вход» в журнал.
    if session.get("vk_id") != uid:
        session.clear()
        session["logged_in"] = True
        session["vk_id"] = uid
        session.permanent = True

    if _launch_is_new(request.args.get("sign", "")):
        seen_users.touch(uid)
        audit.log(uid, "auth.login", "via VK Mini App", "seamless")


@app.before_request
def _load_current_user():
    """Загружает текущего юзера в g.user.

    Источник правды — RM-токен в БД. Если токена нет, fallback на Flask-сессию
    (нужен только для первого редиректа после /login/code, пока кука не
    вернулась с браузера).
    """
    g.user = None
    token = request.cookies.get(_REMEMBER_COOKIE)
    if token:
        info = panel_remember.verify(token, rotate=False)
        if info:
            g.user = _build_user(info["vk_id"])
            return
        # Битый/просроченный токен — пометим, чтобы after_request почистил куку
        g.rm_clear = True
    # Fallback: Flask-сессия (только для самого первого запроса после login)
    if session.get("logged_in") and session.get("vk_id"):
        g.user = _build_user(int(session["vk_id"]))
        return
    # Запуск из VK Mini App: подпись уже проверена в _check_vk_sign. Куки в
    # iframe могут быть заблокированы браузером — тогда это единственный путь.
    launch_uid = getattr(g, "vk_launch_uid", None)
    if launch_uid:
        g.user = _build_user(launch_uid)


@app.after_request
def _flush_remember_cookie(resp):
    """Чистит протухшую куку, если before_request пометил её мёртвой."""
    if getattr(g, "rm_clear", False):
        _clear_remember_cookie(resp)
    return resp


def login_required(f):
    """Любой залогиненный."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not getattr(g, "user", None):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    """Только admin или owner. Роли всегда свежие (читаются в before_request)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = getattr(g, "user", None)
        if not user:
            return redirect(url_for("login"))
        if not user["is_admin"]:
            return redirect(url_for("me_page"))
        return f(*args, **kwargs)
    return decorated


def owner_required(f):
    """Только owner."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = getattr(g, "user", None)
        if not user:
            return redirect(url_for("login"))
        if not user["is_owner"]:
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ── Статистика ────────────────────────────────────────────────────────────────

def _get_stats() -> dict:
    stats = {
        "schedule_vk": 0, "schedule_s": 0, "users": 0,
        "notes": 0, "reminders": 0, "deadlines": 0, "subscriptions": 0,
        "subs_disabled": 0,
        "top_directions": [],       # [(label, course, count), ...]
        "courses_distribution": [], # [(course, n_subs, n_directions, n_records), ...]
        "recent_uploads": [],       # [(uploaded_at, original_filename, row_count, uploaded_by), ...]
        "recent_users": [],         # [(uid, last_seen, kind), ...]
    }
    for key, vk in (("schedule_vk", True), ("schedule_s", False)):
        c = None
        try:
            c = _sched_conn(vk)
            stats[key] = c.execute("SELECT COUNT(*) FROM schedule").fetchone()[0]
        except Exception:
            pass
        finally:
            if c is not None:
                c.close()
    conn = None
    try:
        conn = _notes_conn()
        uids: set = set()
        # Явный whitelist — никакой подстановки имён таблиц в SQL.
        for stmt in (
            "SELECT DISTINCT user_id FROM notes",
            "SELECT DISTINCT user_id FROM reminders",
            "SELECT DISTINCT user_id FROM subscriptions",
            "SELECT DISTINCT user_id FROM deadlines",
            "SELECT DISTINCT user_id FROM user_prefs",
        ):
            try:
                for (uid,) in conn.execute(stmt).fetchall():
                    uids.add(uid)
            except Exception:
                pass
        stats["users"] = len(uids)
        stats["notes"] = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        stats["reminders"] = conn.execute(
            "SELECT COUNT(*) FROM reminders WHERE notified=0"
        ).fetchone()[0]
        try:
            stats["deadlines"] = conn.execute("SELECT COUNT(*) FROM deadlines").fetchone()[0]
        except Exception:
            pass
        stats["subscriptions"] = conn.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE COALESCE(disabled,0)=0"
        ).fetchone()[0]
        try:
            stats["subs_disabled"] = conn.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE COALESCE(disabled,0)=1"
            ).fetchone()[0]
        except Exception:
            pass
        # Топ направлений по подпискам
        try:
            stats["top_directions"] = conn.execute(
                "SELECT direction, course, COUNT(*) AS n FROM subscriptions "
                "WHERE COALESCE(disabled,0)=0 GROUP BY course, direction "
                "ORDER BY n DESC LIMIT 10"
            ).fetchall()
        except Exception:
            pass
        # Последние загрузки
        try:
            stats["recent_uploads"] = conn.execute(
                "SELECT uploaded_at, original_filename, row_count, uploaded_by "
                "FROM schedule_versions ORDER BY id DESC LIMIT 5"
            ).fetchall()
        except Exception:
            pass
        # Недавние пользователи: берём из reminders/deadlines/notes (max created_at)
        try:
            rows = conn.execute(
                "SELECT user_id, MAX(ts) AS last_seen, kind FROM ("
                "  SELECT user_id, timestamp AS ts, 'note' AS kind FROM notes "
                "  UNION ALL "
                "  SELECT user_id, remind_at AS ts, 'reminder' AS kind FROM reminders "
                "  UNION ALL "
                "  SELECT user_id, deadline_at AS ts, 'deadline' AS kind FROM deadlines "
                ") GROUP BY user_id ORDER BY last_seen DESC LIMIT 10"
            ).fetchall()
            stats["recent_users"] = rows
        except Exception:
            pass
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()
    # Распределение по курсам: пары (количество_подписок, направлений, записей)
    conn = None
    sc = None
    try:
        conn = _notes_conn()
        subs_by_course: dict[int, int] = {}
        try:
            for c, n in conn.execute(
                "SELECT course, COUNT(*) FROM subscriptions "
                "WHERE COALESCE(disabled,0)=0 GROUP BY course"
            ).fetchall():
                subs_by_course[c] = n
        except Exception:
            pass
        sc = _sched_conn(vk=True)
        course_rows = sc.execute(
            "SELECT course, COUNT(DISTINCT direction) AS dirs, COUNT(*) AS rows "
            "FROM schedule GROUP BY course ORDER BY course"
        ).fetchall()
        stats["courses_distribution"] = [
            (c, subs_by_course.get(c, 0), dirs, rows)
            for (c, dirs, rows) in course_rows
        ]
    except Exception:
        pass
    finally:
        for handle in (conn, sc):
            if handle is not None:
                handle.close()
    return stats


def _get_user_data(uid: int) -> dict:
    data: dict = {
        "notes": [], "reminders": [], "reminders_past": [],
        "deadlines": [], "subscriptions": [], "pref": None,
    }
    conn = None
    try:
        conn = _notes_conn()
        data["notes"] = conn.execute(
            "SELECT id, note_text, timestamp FROM notes WHERE user_id=? ORDER BY id DESC",
            (uid,),
        ).fetchall()
        data["reminders"] = conn.execute(
            "SELECT id, reminder_text, remind_at FROM reminders "
            "WHERE user_id=? AND notified=0 ORDER BY remind_at",
            (uid,),
        ).fetchall()
        data["reminders_past"] = conn.execute(
            "SELECT id, reminder_text, remind_at FROM reminders "
            "WHERE user_id=? AND notified=1 ORDER BY remind_at DESC LIMIT 10",
            (uid,),
        ).fetchall()
        data["deadlines"] = conn.execute(
            "SELECT id, subject, description, deadline_at FROM deadlines "
            "WHERE user_id=? ORDER BY deadline_at",
            (uid,),
        ).fetchall()
        data["subscriptions"] = conn.execute(
            "SELECT id, course, direction FROM subscriptions "
            "WHERE user_id=? AND COALESCE(disabled,0)=0",
            (uid,),
        ).fetchall()
        data["pref"] = conn.execute(
            "SELECT course, direction FROM user_prefs WHERE user_id=?",
            (uid,),
        ).fetchone()
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()
    return data


# ── Шаблоны ───────────────────────────────────────────────────────────────────

_BASE_TPL = """
<!doctype html>
<html lang="ru" data-theme="light" data-bs-theme="light">
<head>
  <meta charset="utf-8">
  <script src="https://unpkg.com/@vkontakte/vk-bridge/dist/browser.min.js"></script>
  <script>try{if(window.vkBridge)vkBridge.send("VKWebAppInit").catch(function(){});}catch(e){}</script>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#C21E41">
  <meta name="robots" content="noindex, nofollow, noarchive">
  <meta name="googlebot" content="noindex, nofollow">
  <title>{{ page_title }} — Электронное расписание</title>
  <link rel="icon" href="/static/logo-mark.svg" type="image/svg+xml">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <style>
    /* ============================================================
       Design tokens (from schedule-site design system)
       ============================================================ */
    :root {
      --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px;
      --space-5: 22px; --space-6: 28px; --space-7: 40px; --space-8: 56px;
      --radius-sm: 6px; --radius: 10px; --radius-md: 14px; --radius-lg: 18px; --radius-xl: 24px;
      --font: 'Manrope', ui-sans-serif, system-ui, sans-serif;
      --font-mono: 'JetBrains Mono', ui-monospace, monospace;
      --fs-xs: 12px; --fs-sm: 13px; --fs-base: 14px; --fs-md: 15px;
      --fs-lg: 17px; --fs-xl: 20px; --fs-2xl: 26px; --fs-3xl: 34px;
      /* Фирменный цвет ЧГПУ им. И. Я. Яковлева — тот же, что в логотипе
         (chgpu.edu.ru/uploads/logotip.svg). Остальная палитра выводится из него
         через color-mix, поэтому смена акцента — правка одной строки. */
      --accent: #C21E41;
      --accent-soft: color-mix(in srgb, var(--accent) 14%, transparent);
      --accent-soft-2: color-mix(in srgb, var(--accent) 6%, transparent);
      --accent-fg: #ffffff;
      --accent-ring: color-mix(in srgb, var(--accent) 30%, transparent);
    }
    [data-theme="light"] {
      --bg: #FAFAF7; --bg-2: #F4F3EE;
      --surface: #FFFFFF; --surface-2: #F8F7F4; --surface-3: #EFEEE9;
      --border: #E7E5DE; --border-strong: #D6D3CB;
      --text: #1A1A17; --text-2: #4B4A45; --text-3: #7A7872; --text-4: #A6A39B;
      --sidebar-bg: #FFFFFF; --sidebar-fg: #1A1A17; --sidebar-fg-muted: #7A7872;
      --sidebar-border: #ECEAE3;
      --sidebar-active: var(--accent-soft); --sidebar-active-fg: var(--accent);
      --shadow-sm: 0 1px 2px rgba(20,20,17,.04), 0 1px 1px rgba(20,20,17,.02);
      --shadow: 0 1px 3px rgba(20,20,17,.06), 0 6px 18px -8px rgba(20,20,17,.08);
      --scrim: rgba(20,20,17,.45);
      color-scheme: light;
    }
    [data-theme="dark"] {
      --bg: #0E0F0D; --bg-2: #131512;
      --surface: #181A17; --surface-2: #1F211D; --surface-3: #262924;
      --border: #2A2D27; --border-strong: #3A3D36;
      --text: #F2F1EC; --text-2: #C4C3BB; --text-3: #8A8980; --text-4: #5E5D55;
      --sidebar-bg: #131512; --sidebar-fg: #F2F1EC; --sidebar-fg-muted: #8A8980;
      --sidebar-border: #22241F;
      --sidebar-active: color-mix(in srgb, var(--accent) 18%, transparent);
      --sidebar-active-fg: color-mix(in srgb, var(--accent) 80%, white);
      --shadow-sm: 0 1px 2px rgba(0,0,0,.3);
      --shadow: 0 4px 16px rgba(0,0,0,.35);
      --scrim: rgba(0,0,0,.6);
      color-scheme: dark;
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; padding: 0; }
    body {
      font-family: var(--font); font-size: var(--fs-base);
      background: var(--bg); color: var(--text);
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
    }

    /* ============================================================
       Shell
       ============================================================ */
    .app-shell { display: grid; grid-template-columns: 256px 1fr; min-height: 100vh; }
    .sidebar {
      position: sticky; top: 0; height: 100vh;
      background: var(--sidebar-bg); color: var(--sidebar-fg);
      border-right: 1px solid var(--sidebar-border);
      display: flex; flex-direction: column;
      overflow: hidden; z-index: 20;
    }
    .sb-brand {
      display: flex; align-items: center; gap: 10px;
      padding: 20px 18px 18px;
      border-bottom: 1px solid var(--sidebar-border);
    }
    .sb-logo {
      width: 36px; height: 36px; flex: none;
      object-fit: contain;
      /* Знак — тонкая линейная графика: подложка под ним только мешает,
         а на тёмной теме фирменный красный читается сам по себе. */
    }
    .sb-brand-text { min-width: 0; flex: 1; }
    .sb-brand-name { font-weight: 700; font-size: 13.5px; letter-spacing: -.005em; }
    .sb-brand-sub { font-size: 11px; color: var(--sidebar-fg-muted); }
    .sb-nav {
      flex: 1; min-height: 0; overflow-y: auto;
      padding: 12px 12px; display: flex; flex-direction: column; gap: 2px;
      scrollbar-width: thin;
    }
    .sb-section {
      font-size: 10.5px; font-weight: 700;
      letter-spacing: .1em; text-transform: uppercase;
      color: var(--sidebar-fg-muted);
      padding: 14px 10px 6px;
    }
    .sb-item {
      display: flex; align-items: center; gap: 12px;
      padding: 9px 10px; border-radius: 10px;
      color: var(--sidebar-fg); text-decoration: none;
      cursor: pointer;
      font-size: 13.5px; font-weight: 500;
      transition: background 120ms, color 120ms;
    }
    .sb-item:hover { background: color-mix(in srgb, var(--sidebar-fg) 5%, transparent); color: var(--sidebar-fg); }
    .sb-item.active { background: var(--sidebar-active); color: var(--sidebar-active-fg); font-weight: 600; }
    .sb-icon { width: 22px; flex: none; font-size: 16px; text-align: center; opacity: .85; }
    .sb-item.active .sb-icon { opacity: 1; }
    .sb-foot { border-top: 1px solid var(--sidebar-border); padding: 12px; display: flex; flex-direction: column; gap: 8px; }
    .sb-user { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-radius: 10px; }
    .sb-avatar {
      width: 32px; height: 32px; border-radius: 50%; flex: none;
      background: linear-gradient(135deg, color-mix(in srgb, var(--accent) 90%, white), var(--accent));
      color: var(--accent-fg);
      display: grid; place-items: center;
      font-size: 12px; font-weight: 700;
    }
    .sb-user-info { min-width: 0; flex: 1; }
    .sb-user-name { font-size: 12.5px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .sb-user-id { font-size: 11px; color: var(--sidebar-fg-muted); }
    .sb-user-id a { color: inherit; text-decoration: none; }
    .sb-user-id a:hover { color: var(--accent); }
    .sb-logout {
      appearance: none; border: 1px solid var(--sidebar-border); background: transparent;
      color: var(--sidebar-fg-muted); height: 32px; border-radius: 8px;
      font: inherit; font-size: 12px; font-weight: 500;
      display: inline-flex; align-items: center; justify-content: center; gap: 6px;
      cursor: pointer; text-decoration: none;
      transition: background 120ms, color 120ms;
    }
    .sb-logout:hover { background: color-mix(in srgb, var(--sidebar-fg) 6%, transparent); color: var(--sidebar-fg); }

    /* Role pill */
    .role-pill {
      display: inline-flex; align-items: center;
      height: 20px; padding: 0 9px;
      border-radius: 999px; font-size: 10px; font-weight: 800;
      text-transform: uppercase; letter-spacing: .05em;
    }
    /* Раньше бейдж был красным. После перехода на фирменный красный роль
       сливалась с акцентом и переставала читаться как отдельная метка —
       владелец теперь золотой, а красный остаётся за акцентом и опасными
       действиями. */
    .role-pill.owner { background: linear-gradient(135deg, #F4C245, #C99A21); color: #3B2C05; }
    .role-pill.admin { background: linear-gradient(135deg, #818CF8, #6366F1); color: white; }
    .role-pill.user  { background: var(--surface-3); color: var(--text-2); }

    /* ============================================================
       Main area
       ============================================================ */
    .main { min-width: 0; display: flex; flex-direction: column; }
    .topbar {
      height: 60px; flex: none;
      display: flex; align-items: center; gap: 12px;
      padding: 0 var(--space-7);
      border-bottom: 1px solid var(--border);
      background: color-mix(in srgb, var(--bg) 85%, transparent);
      backdrop-filter: saturate(150%) blur(8px);
      -webkit-backdrop-filter: saturate(150%) blur(8px);
      position: sticky; top: 0; z-index: 10;
    }
    /* overflow+nowrap обязательны: без них длинное название страницы вылезало
       за пределы своей колонки и печаталось поверх плашки чётности. */
    .topbar-crumbs {
      display: flex; align-items: center; gap: 8px; font-size: 13px;
      color: var(--text-3); flex: 1 1 auto; min-width: 0;
      overflow: hidden; white-space: nowrap;
    }
    .topbar-crumbs strong {
      color: var(--text); font-weight: 600;
      overflow: hidden; text-overflow: ellipsis;
    }
    .topbar-crumbs .sep { opacity: .45; }
    /* Подписка на группу: название направления бывает под 90 символов,
       поэтому плашка обязана переносить текст и жить в ширине экрана. */
    .sub-chip {
      display: flex; align-items: flex-start; gap: 10px;
      max-width: 100%; box-sizing: border-box;
      padding: 10px 12px; border-radius: 12px;
      background: var(--accent-soft);
      border: 1px solid color-mix(in srgb, var(--accent) 22%, transparent);
    }
    .sub-chip-text {
      min-width: 0; flex: 1;
      color: var(--text); font-size: 13.5px; line-height: 1.45;
      overflow-wrap: anywhere;
    }
    .sub-chip-text strong { color: var(--accent); font-weight: 700; }
    .sub-chip-x {
      flex: none; appearance: none; border: 0; background: transparent;
      color: var(--text-3); font-size: 15px; line-height: 1; cursor: pointer;
      padding: 2px 4px; border-radius: 6px;
    }
    .sub-chip-x:hover {
      color: var(--accent);
      background: color-mix(in srgb, var(--accent) 12%, transparent);
    }

    /* Плашка чётности не сжимается и не переносится — она короткая и важная. */
    .app-foot {
      margin-top: 22px; padding-top: 14px;
      border-top: 1px solid var(--border);
      display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
      font-size: 12.5px; color: var(--text-3);
    }
    .app-foot a { color: var(--text-3); text-decoration: underline; text-underline-offset: 2px; }
    .app-foot a:hover { color: var(--accent); }

    /* Безопасные зоны выреза и «домашней» полосы: viewport-fit=cover отдаёт нам
       всю площадь экрана, включая области под системными элементами. */
    .topbar { padding-top: env(safe-area-inset-top, 0px); }
    .page {
      padding-left: max(var(--space-7), env(safe-area-inset-left, 0px));
      padding-right: max(var(--space-7), env(safe-area-inset-right, 0px));
      padding-bottom: max(var(--space-7), calc(env(safe-area-inset-bottom, 0px) + 16px));
    }
    .sb-foot { padding-bottom: max(14px, env(safe-area-inset-bottom, 0px)); }

    .week-chip {
      flex: none; white-space: nowrap;
      background: var(--accent-soft); color: var(--accent); border-color: transparent;
      height: 26px; padding: 0 12px; border-radius: 999px;
      display: inline-flex; align-items: center; gap: 4px;
      font-size: 11.5px; font-weight: 600; letter-spacing: .03em; text-transform: uppercase;
    }
    .hamb {
      appearance: none; border: 1px solid var(--border);
      background: var(--surface); color: var(--text-2);
      width: 38px; height: 38px; border-radius: 10px;
      display: none; align-items: center; justify-content: center;
      cursor: pointer; font-size: 18px;
    }
    .hamb:active { transform: scale(.97); }
    .page { padding: var(--space-7); display: flex; flex-direction: column; gap: var(--space-6); flex: 1; }

    /* ============================================================
       Bootstrap component overrides → match design tokens
       ============================================================ */
    .card {
      background: var(--surface) !important;
      border: 1px solid var(--border) !important;
      border-radius: var(--radius-md) !important;
      box-shadow: var(--shadow-sm);
      color: var(--text);
    }
    .card-header {
      background: transparent !important;
      border-bottom: 1px solid var(--border) !important;
      padding: 14px 18px;
      font-weight: 600; font-size: 14px;
      color: var(--text);
    }
    .card-body { padding: var(--space-5); }
    .card-footer { background: transparent !important; border-top: 1px solid var(--border) !important; }

    .btn {
      font-family: var(--font); font-weight: 500; font-size: 13.5px;
      border-radius: 10px; padding: 8px 14px;
      transition: background 120ms, border-color 120ms, transform 80ms;
    }
    .btn:active { transform: translateY(1px); }
    .btn-sm { font-size: 12.5px; padding: 6px 10px; border-radius: 8px; }
    .btn-primary {
      background: var(--accent) !important;
      border-color: transparent !important;
      color: var(--accent-fg) !important;
      box-shadow: 0 1px 0 rgba(255,255,255,.18) inset,
                  0 4px 14px -4px color-mix(in srgb, var(--accent) 60%, transparent);
    }
    .btn-primary:hover { background: color-mix(in srgb, var(--accent) 90%, black) !important; }
    .btn-outline-secondary {
      background: var(--surface) !important;
      border-color: var(--border) !important;
      color: var(--text-2) !important;
    }
    .btn-outline-secondary:hover { background: var(--surface-2) !important; border-color: var(--border-strong) !important; color: var(--text) !important; }
    .btn-secondary {
      background: var(--surface-2) !important;
      border-color: var(--border) !important;
      color: var(--text) !important;
    }
    .btn-danger { background: #DC2626 !important; border-color: #DC2626 !important; }
    .btn-success { background: #10B981 !important; border-color: #10B981 !important; }
    .btn-outline-danger { color: #DC2626 !important; border-color: color-mix(in srgb, #DC2626 30%, var(--border)) !important; }
    .btn-outline-danger:hover { background: color-mix(in srgb, #DC2626 8%, var(--surface)) !important; color: #DC2626 !important; }

    .table, table.table {
      color: var(--text);
      --bs-table-bg: transparent;
      --bs-table-color: var(--text);
      --bs-table-border-color: var(--border);
      --bs-table-hover-bg: var(--surface-2);
      --bs-table-hover-color: var(--text);
      font-size: 13.5px;
    }
    .table > :not(caption) > * > * { padding: 12px 14px; }
    .table thead th {
      font-size: 11px; font-weight: 600;
      letter-spacing: .06em; text-transform: uppercase;
      color: var(--text-3);
      border-bottom: 1px solid var(--border);
    }
    .table tbody tr { transition: background 120ms; }

    .form-control, .form-select {
      background: var(--surface) !important;
      border: 1px solid var(--border) !important;
      color: var(--text) !important;
      border-radius: 10px;
      font-family: var(--font);
      transition: border-color 120ms, box-shadow 120ms;
    }
    .form-control:focus, .form-select:focus {
      border-color: var(--accent) !important;
      box-shadow: 0 0 0 3px var(--accent-ring) !important;
    }
    .form-control::placeholder { color: var(--text-4); }
    .form-label { font-size: 12.5px; font-weight: 500; color: var(--text-2); }
    .form-check-input:checked { background-color: var(--accent); border-color: var(--accent); }
    .form-check-input:focus { box-shadow: 0 0 0 3px var(--accent-ring); border-color: var(--accent); }

    .alert {
      border-radius: var(--radius);
      border: 1px solid var(--border);
      font-size: 13.5px;
    }
    .alert-danger { background: color-mix(in srgb, #DC2626 10%, var(--surface)); border-color: color-mix(in srgb, #DC2626 25%, var(--border)); color: #B91C1C; }
    .alert-success { background: var(--accent-soft); border-color: color-mix(in srgb, var(--accent) 30%, var(--border)); color: color-mix(in srgb, var(--accent) 80%, black); }
    .alert-warning { background: color-mix(in srgb, #F59E0B 10%, var(--surface)); border-color: color-mix(in srgb, #F59E0B 25%, var(--border)); color: #B45309; }
    .alert-info { background: color-mix(in srgb, #3B82F6 8%, var(--surface)); border-color: color-mix(in srgb, #3B82F6 25%, var(--border)); color: #1D4ED8; }
    .alert-light { background: var(--surface-2); border-color: var(--border); color: var(--text-2); }

    .badge { font-weight: 600; font-size: 11px; padding: 4px 8px; border-radius: 6px; }
    .badge.bg-primary, .bg-primary { background: var(--accent) !important; color: var(--accent-fg) !important; }
    .badge.bg-secondary, .bg-secondary { background: var(--surface-3) !important; color: var(--text-2) !important; }
    .badge.bg-success, .bg-success { background: var(--accent) !important; color: var(--accent-fg) !important; }

    .progress { background: var(--surface-2); border-radius: 999px; height: 6px; overflow: hidden; }
    .progress-bar { background: var(--accent) !important; }

    /* Stat cards retain color accents from existing templates */
    .stat-card {
      background: var(--surface) !important;
      border: 1px solid var(--border) !important;
      border-left-width: 4px !important;
      border-radius: var(--radius-md) !important;
      transition: border-color 160ms, transform 160ms;
    }
    .stat-card:hover { border-color: var(--border-strong) !important; transform: translateY(-1px); }
    .stat-card.blue   { border-left-color: #3B82F6 !important; }
    .stat-card.green  { border-left-color: var(--accent) !important; }
    .stat-card.orange { border-left-color: #F59E0B !important; }
    .stat-card.red    { border-left-color: #DC2626 !important; }
    .stat-card.purple { border-left-color: #8B5CF6 !important; }
    .stat-card.teal   { border-left-color: #14B8A6 !important; }
    .stat-card .text-muted, .stat-card .text-muted small { color: var(--text-3) !important; }
    .stat-card .fs-2, .stat-card .fs-3 { font-weight: 700; letter-spacing: -.02em; color: var(--text); }

    .text-muted, small.text-muted { color: var(--text-3) !important; }
    .text-success { color: var(--accent) !important; }
    .text-danger { color: #DC2626 !important; }

    pre {
      background: var(--surface-3); color: var(--text);
      padding: 14px; border-radius: var(--radius);
      max-height: 420px; overflow-y: auto; font-size: 12.5px;
      font-family: var(--font-mono);
      border: 1px solid var(--border);
    }
    code { color: var(--text); background: var(--surface-2); padding: 1px 6px; border-radius: 5px; font-family: var(--font-mono); font-size: .9em; }

    .page-title h1, .page-title h2, .page-title h3 { font-weight: 700; letter-spacing: -.02em;
      color: var(--text); font-size: 28px; margin: 0; }

    /* ============================================================
       Responsive: Tablet (≤ 1024px) — narrow rail sidebar
       ============================================================ */
    @media (max-width: 1024px) and (min-width: 768px) {
      .app-shell { grid-template-columns: 72px 1fr; }
      .sb-brand { padding: 18px 0; justify-content: center; }
      .sb-brand-text { display: none; }
      .sb-section { padding: 14px 0 4px; text-align: center; }
      .sb-section-label { display: none; }
      .sb-section::before {
        content: ""; display: block;
        width: 20px; height: 1px; background: var(--sidebar-border); margin: 0 auto;
      }
      .sb-item { justify-content: center; padding: 12px; position: relative; }
      .sb-item .sb-label { display: none; }
      .sb-item.active::after {
        content: ""; position: absolute; left: 0; top: 8px; bottom: 8px;
        width: 3px; border-radius: 0 3px 3px 0; background: var(--accent);
      }
      .sb-user { justify-content: center; padding: 4px; }
      .sb-user-info, .sb-logout-label { display: none; }
      .sb-user .role-pill { display: none; }
      .sb-avatar { width: 36px; height: 36px; }
      .sb-foot { padding: 8px; gap: 6px; }
      .sb-logout { width: 36px; padding: 0; margin: 0 auto; }
      .topbar { padding: 0 22px; height: 56px; }
      .page { padding: 22px; }
      .topbar-crumbs { font-size: 12px; }
    }

    /* ============================================================
       Responsive: Mobile (≤ 767px) — offcanvas sidebar
       ============================================================ */
    @media (max-width: 767.98px) {
      .app-shell { grid-template-columns: 1fr; }
      .sidebar.desktop-sb { display: none; }
      .hamb { display: inline-flex; }
      .topbar { padding: 0 16px; height: 56px; gap: 8px; }
      .topbar-crumbs { font-size: 13px; }
      /* Освобождаем ширину для названия страницы: «Панель /» на телефоне
         не несёт информации, а плашка сокращается до «🗓 нечет». */
      .topbar-crumbs .crumb-root,
      .topbar-crumbs .sep,
      .week-chip-long { display: none; }
      .week-chip { padding: 0 10px; font-size: 11px; }
      .page { padding: 16px; gap: 16px; }
      .page-title h1, .page-title h2, .page-title h3 { font-size: 22px; }
      .stat-card .fs-2 { font-size: 24px !important; }
      .stat-card .fs-3 { font-size: 20px !important; }
      .card-header { padding: 12px 14px; }
      .card-body { padding: 16px; }
      .table { font-size: 12.5px; }
      .table > :not(caption) > * > * { padding: 10px 10px; }
      .btn { font-size: 13px; }
    }
    @media (min-width: 768px) {
      .offcanvas-sb { display: none !important; }
    }
    .offcanvas {
      background: var(--sidebar-bg) !important;
      color: var(--sidebar-fg) !important;
    }
    .offcanvas .sb-nav { padding: 12px; }
    .offcanvas-header {
      border-bottom: 1px solid var(--sidebar-border);
      padding: 16px 18px;
    }

    /* Focus outlines */
    :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }

    /* List items used in tables/lists from current content */
    .list-group-item {
      background: var(--surface) !important;
      border-color: var(--border) !important;
      color: var(--text) !important;
    }

    /* Modal */
    .modal-content { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md); }
    .modal-header, .modal-footer { border-color: var(--border); }

    /* ============================================================
       Sticky-scroll tables — горизонтальный скролл виден сразу
       (без долистывания страницы до конца таблицы)
       ============================================================ */
    .table-responsive {
      max-height: calc(100vh - 200px);
      overflow: auto;
      border-radius: var(--radius-md);
      border: 1px solid var(--border);
      background: var(--surface);
      scrollbar-width: thin;
    }
    .table-responsive .table {
      margin: 0;
      border: 0;
    }
    .table-responsive .table thead th {
      position: sticky; top: 0; z-index: 2;
      background: var(--surface-2);
      box-shadow: inset 0 -1px 0 var(--border);
    }
    .table-responsive::-webkit-scrollbar { height: 10px; width: 10px; }
    .table-responsive::-webkit-scrollbar-thumb {
      background: var(--border-strong); border-radius: 999px;
    }
    .table-responsive::-webkit-scrollbar-track { background: var(--surface-2); }

    /* ============================================================
       Schedule: segmented view-switcher
       ============================================================ */
    .seg {
      display: inline-flex;
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 3px; gap: 2px;
    }
    .seg button {
      appearance: none; border: 0;
      background: transparent;
      color: var(--text-3);
      height: 32px; padding: 0 14px;
      border-radius: 7px;
      font: inherit; font-size: 13px; font-weight: 500;
      cursor: pointer;
      transition: background 120ms, color 120ms;
    }
    .seg button:hover { color: var(--text); }
    .seg button.on {
      background: var(--surface);
      color: var(--text);
      box-shadow: var(--shadow-sm);
      font-weight: 600;
    }

    /* ============================================================
       Schedule: Cards view — день-колонки и pair-card
       ============================================================ */
    .day-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: var(--space-5);
    }
    @media (max-width: 1200px) { .day-grid { grid-template-columns: repeat(2, 1fr); } }
    @media (max-width: 720px)  { .day-grid { grid-template-columns: 1fr; } }
    .day-col {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      overflow: hidden;
      display: flex; flex-direction: column;
    }
    .day-head {
      display: flex; align-items: center; justify-content: space-between;
      padding: 14px 16px;
      border-bottom: 1px solid var(--border);
      background: var(--surface-2);
    }
    .day-name { font-weight: 700; font-size: 14px; letter-spacing: -.005em; }
    .day-count { font-size: 11.5px; color: var(--text-3); font-variant-numeric: tabular-nums; }
    .day-list { padding: 10px; display: flex; flex-direction: column; gap: 8px; max-height: 720px; overflow-y: auto; }
    .empty-mini { padding: 22px; text-align: center; color: var(--text-4); font-size: 12.5px; }

    .pair-card {
      padding: 10px 12px 10px 16px;
      border-radius: 10px;
      background: var(--surface);
      border: 1px solid var(--border);
      display: flex; flex-direction: column; gap: 6px;
      position: relative;
      transition: border-color 120ms, background 120ms;
    }
    .pair-card:hover { border-color: var(--border-strong); background: var(--surface-2); }
    .pair-card::before {
      content: ""; position: absolute;
      left: 0; top: 10px; bottom: 10px; width: 3px;
      border-radius: 0 4px 4px 0;
      background: var(--type-color, var(--accent));
    }
    .pair-time {
      font-family: var(--font-mono);
      font-size: 11.5px;
      color: var(--text-2);
      font-weight: 600;
      display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
    }
    .pair-time .week {
      font-family: var(--font); font-weight: 500;
      font-size: 10.5px; color: var(--text-3);
      padding: 1px 6px; border-radius: 4px;
      background: var(--surface-2);
      border: 1px solid var(--border);
    }
    .pair-subject {
      font-weight: 600; font-size: 13.5px;
      letter-spacing: -.005em; line-height: 1.3;
      color: var(--text);
    }
    .pair-meta {
      display: flex; align-items: center; gap: 8px;
      font-size: 11.5px; color: var(--text-3);
      flex-wrap: wrap;
    }
    .pair-meta .sep { color: var(--text-4); }
    .pair-meta .mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
    .pair-meta .course-chip {
      display: inline-flex; align-items: center;
      height: 18px; padding: 0 7px;
      border-radius: 999px;
      background: var(--surface-3); color: var(--text-2);
      font-size: 10.5px; font-weight: 600;
    }
    .type-badge {
      display: inline-flex; align-items: center;
      padding: 1px 7px; border-radius: 5px;
      font-size: 10.5px; font-weight: 700;
      letter-spacing: .03em; text-transform: uppercase;
      background: color-mix(in srgb, var(--type-color, var(--text-3)) 14%, transparent);
      color: var(--type-color, var(--text-3));
    }

    /* ============================================================
       Schedule: Calendar view — матрица время × день
       ============================================================ */
    .cal-wrap {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      overflow: auto;
      max-height: calc(100vh - 220px);
    }
    .cal {
      display: grid;
      grid-template-columns: 100px repeat(6, minmax(170px, 1fr));
      min-width: 1100px;
    }
    .cal-corner, .cal-day-head {
      padding: 12px 14px;
      font-size: 11px; font-weight: 600;
      color: var(--text-3);
      letter-spacing: .04em; text-transform: uppercase;
      border-bottom: 1px solid var(--border);
      border-right: 1px solid var(--border);
      background: var(--surface-2);
      position: sticky; top: 0; z-index: 3;
    }
    .cal-corner { background: var(--surface-3); z-index: 4; left: 0; }
    .cal-day-head:last-child { border-right: 0; }
    .cal-time {
      padding: 12px 10px;
      font-size: 11px;
      color: var(--text-3);
      font-family: var(--font-mono);
      border-bottom: 1px solid var(--border);
      border-right: 1px solid var(--border);
      background: var(--surface-2);
      display: flex; flex-direction: column; gap: 4px;
      position: sticky; left: 0; z-index: 2;
    }
    .cal-time-slot {
      width: 22px; height: 22px;
      border-radius: 6px;
      background: var(--surface); color: var(--text-2);
      border: 1px solid var(--border);
      display: grid; place-items: center;
      font-family: var(--font); font-weight: 700; font-size: 11px;
    }
    .cal-cell {
      padding: 6px;
      border-bottom: 1px solid var(--border);
      border-right: 1px solid var(--border);
      display: flex; flex-direction: column; gap: 4px;
      min-height: 90px;
      background: var(--surface);
    }
    .cal-cell:last-of-type { border-right: 0; }
    .cal-block {
      padding: 7px 9px;
      background: color-mix(in srgb, var(--type-color, var(--accent)) 9%, var(--surface));
      border-left: 3px solid var(--type-color, var(--accent));
      border-radius: 6px;
      font-size: 11.5px; line-height: 1.3;
      display: flex; flex-direction: column; gap: 2px;
    }
    .cal-block-sub { font-weight: 600; font-size: 12px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .cal-block-meta { font-size: 10.5px; color: var(--text-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .cal-block-foot { display: flex; align-items: center; gap: 6px; margin-top: 2px; flex-wrap: wrap; }
    .cal-more { font-size: 10.5px; color: var(--text-3); padding: 4px 6px; border-radius: 5px; background: var(--surface-2); text-align: center; }
  </style>
</head>
<body>

{% macro nav_links() %}
  <div class="sb-section"><span class="sb-section-label">Расписание</span></div>
  <a href="{{ url_for('dashboard') }}" class="sb-item {{ 'active' if ep == 'dashboard' }}">
    <span class="sb-icon">📊</span><span class="sb-label">Дашборд</span></a>
  <a href="{{ url_for('schedule_page') }}" class="sb-item {{ 'active' if ep == 'schedule_page' }}">
    <span class="sb-icon">📅</span><span class="sb-label">Все пары</span></a>
  {% if is_admin %}
    <a href="{{ url_for('upload_page') }}" class="sb-item {{ 'active' if ep == 'upload_page' }}">
      <span class="sb-icon">📤</span><span class="sb-label">Загрузить</span></a>
    <div class="sb-section"><span class="sb-section-label">Пользователи</span></div>
    <a href="{{ url_for('users_page') }}" class="sb-item {{ 'active' if ep == 'users_page' }}">
      <span class="sb-icon">👥</span><span class="sb-label">Все юзеры</span></a>
  {% endif %}
  {% if is_owner %}
    <div class="sb-section"><span class="sb-section-label">Управление</span></div>
    <a href="{{ url_for('admins_page') }}" class="sb-item {{ 'active' if ep == 'admins_page' }}">
      <span class="sb-icon">🛡️</span><span class="sb-label">Админы</span></a>
    <a href="{{ url_for('audit_page') }}" class="sb-item {{ 'active' if ep == 'audit_page' }}">
      <span class="sb-icon">📜</span><span class="sb-label">Аудит-лог</span></a>
  {% endif %}
  {% if is_admin %}
    <a href="{{ url_for('broadcast_page') }}" class="sb-item {{ 'active' if ep == 'broadcast_page' }}">
      <span class="sb-icon">📢</span><span class="sb-label">Рассылка</span></a>
    <a href="{{ url_for('conflicts_page') }}" class="sb-item {{ 'active' if ep == 'conflicts_page' }}">
      <span class="sb-icon">⚠️</span><span class="sb-label">Конфликты</span></a>
    <a href="{{ url_for('diff_page') }}" class="sb-item {{ 'active' if ep == 'diff_page' }}">
      <span class="sb-icon">🔀</span><span class="sb-label">Diff версий</span></a>
  {% endif %}
  <div class="sb-section"><span class="sb-section-label">Аккаунт</span></div>
  <a href="{{ url_for('me_page') }}" class="sb-item {{ 'active' if ep == 'me_page' }}">
    <span class="sb-icon">👤</span><span class="sb-label">Мой профиль</span></a>
{% endmacro %}

{% macro brand_block() %}
  <div class="sb-brand">
    <img class="sb-logo" src="/static/logo-mark.svg" alt="" width="36" height="36">
    <div class="sb-brand-text">
      <div class="sb-brand-name">Электронное расписание</div>
      <div class="sb-brand-sub">ФФМОИиТ · VK-бот</div>
    </div>
  </div>
{% endmacro %}

{% macro user_block() %}
  <div class="sb-foot">
    <div class="sb-user">
      <div class="sb-avatar">{{ (display_name or 'Г')[:1] | upper }}</div>
      <div class="sb-user-info">
        <div class="sb-user-name">{{ display_name or 'Гость' }}</div>
        {% if vk_id %}
          <div class="sb-user-id"><a href="https://vk.com/id{{ vk_id }}" target="_blank">vk.com/id{{ vk_id }}</a></div>
        {% endif %}
      </div>
      {% if is_owner %}<span class="role-pill owner">owner</span>
      {% elif is_admin %}<span class="role-pill admin">admin</span>
      {% else %}<span class="role-pill user">user</span>{% endif %}
    </div>
    <form method="post" action="{{ url_for('logout') }}" style="margin:0;">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <button type="submit" class="sb-logout" style="border:0;background:none;width:100%;cursor:pointer;font:inherit;">
        <span>🚪</span><span class="sb-logout-label">Выйти</span>
      </button>
    </form>
  </div>
{% endmacro %}

<div class="app-shell">
  <!-- Static sidebar (desktop / tablet rail) -->
  <aside class="sidebar desktop-sb">
    {{ brand_block() }}
    <nav class="sb-nav">{{ nav_links() }}</nav>
    {{ user_block() }}
  </aside>

  <!-- Offcanvas sidebar (mobile) -->
  <div class="offcanvas offcanvas-start offcanvas-sb" tabindex="-1"
       id="mobileSidebar" aria-labelledby="mobileSidebarLabel" style="width: 280px;">
    <div class="offcanvas-header" style="border-bottom:1px solid var(--sidebar-border);">
      <div style="display:flex;align-items:center;gap:10px;">
        <img class="sb-logo" src="/static/logo-mark.svg" alt="" width="34" height="34">
        <div>
          <div style="font-weight:700;font-size:13px;">Электронное расписание</div>
          <div style="font-size:11px;color:var(--sidebar-fg-muted);">ФФМОИиТ · VK-бот</div>
        </div>
      </div>
      <button type="button" class="btn-close" data-bs-dismiss="offcanvas" style="filter: var(--bs-btn-close-filter, none);"></button>
    </div>
    <div class="offcanvas-body p-0" style="display:flex;flex-direction:column;">
      <nav class="sb-nav" style="flex:1;">{{ nav_links() }}</nav>
      {{ user_block() }}
    </div>
  </div>

  <main class="main">
    <div class="topbar">
      <button class="hamb" type="button" data-bs-toggle="offcanvas" data-bs-target="#mobileSidebar" aria-label="Меню">☰</button>
      <div class="topbar-crumbs">
        <span class="crumb-root">Панель</span>
        <span class="sep">/</span>
        <strong>{{ page_title }}</strong>
      </div>
      {% if current_week %}
        {# На телефоне остаётся только «🗓 нечет»: полная формулировка не влезала
           и налезала на название страницы. #}
        <span class="chip week-chip">
          🗓<span class="week-chip-long"> Сейчас:</span> {{ current_week }}<span class="week-chip-long"> неделя</span>
        </span>
      {% endif %}
    </div>
    <div class="page">
      {{ content | safe }}
      {# Правила VK Mini Apps: п. 1.1.4 — документы должны быть доступны внутри
         приложения, п. 2.4.1 — как и способ связи. Внутри VK пользователь
         входит бесшовно и страницу входа с этими ссылками не видит вовсе. #}
      <footer class="app-foot">
        <a href="{{ url_for('privacy') }}">Политика конфиденциальности</a>
        <span aria-hidden="true">·</span>
        <a href="{{ url_for('terms') }}">Условия использования</a>
        <span aria-hidden="true">·</span>
        <a href="{{ support_url }}" target="_blank" rel="noopener">Написать в поддержку</a>
      </footer>
    </div>
  </main>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

_LOGIN_TPL = """
<!doctype html>
<html lang="ru" data-theme="light">
<head>
  <meta charset="utf-8">
  <script src="https://unpkg.com/@vkontakte/vk-bridge/dist/browser.min.js"></script>
  <script>try{if(window.vkBridge)vkBridge.send("VKWebAppInit").catch(function(){});}catch(e){}</script>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#C21E41">
  <meta name="robots" content="noindex, nofollow, noarchive">
  <title>Вход — Электронное расписание</title>
  <link rel="icon" href="/static/logo-mark.svg" type="image/svg+xml">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --accent: #C21E41;   /* фирменный цвет вуза, как в основном шаблоне */
      --accent-fg: #fff;
      --accent-soft: color-mix(in srgb, var(--accent) 14%, transparent);
      --accent-ring: color-mix(in srgb, var(--accent) 30%, transparent);
      --bg: #FAFAF7;
      --surface: #FFFFFF; --surface-2: #F8F7F4;
      --border: #E7E5DE; --border-strong: #D6D3CB;
      --text: #1A1A17; --text-2: #4B4A45; --text-3: #7A7872; --text-4: #A6A39B;
      --font: 'Manrope', ui-sans-serif, system-ui, sans-serif;
      --font-mono: 'JetBrains Mono', ui-monospace, monospace;
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; min-height: 100vh; min-height: 100dvh; }
    body {
      font-family: var(--font); color: var(--text);
      background:
        radial-gradient(circle at 18% 18%, color-mix(in srgb, var(--accent) 18%, transparent), transparent 55%),
        radial-gradient(circle at 82% 88%, color-mix(in srgb, #6366F1 14%, transparent), transparent 55%),
        var(--bg);
      display: flex; align-items: center; justify-content: center;
      /* Безопасные зоны: viewport-fit=cover отдаёт странице область под вырезом
         и полосой жестов, иначе карточка входа уезжает под системные элементы. */
      padding: max(16px, env(safe-area-inset-top, 0px))
               max(16px, env(safe-area-inset-right, 0px))
               max(16px, env(safe-area-inset-bottom, 0px))
               max(16px, env(safe-area-inset-left, 0px));
      -webkit-font-smoothing: antialiased;
    }
    .login-shell {
      width: 100%; max-width: 440px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 20px;
      box-shadow: 0 30px 80px -20px rgba(20,20,17,.18),
                  0 12px 32px -8px rgba(20,20,17,.08);
      padding: 36px 32px;
    }
    /* Вход — единственная страница, которую видит незнакомый человек:
       здесь уместен полный логотип с подписью, а не только знак. */
    .brand {
      display: flex; flex-direction: column; align-items: center;
      text-align: center; gap: 14px;
      margin-bottom: 22px;
    }
    .brand-logo-full { width: min(240px, 70%); height: auto; display: block; }
    .brand-logo {
      width: 44px; height: 44px; border-radius: 12px;
      background: var(--accent); color: var(--accent-fg);
      display: grid; place-items: center;
      font-weight: 800; font-size: 18px; letter-spacing: -.02em;
      box-shadow: 0 8px 20px -6px color-mix(in srgb, var(--accent) 60%, transparent);
    }
    .brand-text { line-height: 1.2; }
    .brand-name { font-size: 15px; font-weight: 700; }
    .brand-sub { font-size: 12px; color: var(--text-3); }
    h1 { font-size: 26px; font-weight: 700; letter-spacing: -.02em; margin: 0 0 6px; }
    .sub { font-size: 13.5px; color: var(--text-3); margin-bottom: 22px; }
    .steps {
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px 18px;
      margin-bottom: 18px;
    }
    .steps ol { margin: 0; padding-left: 18px; }
    .steps li { padding: 3px 0; font-size: 13.5px; color: var(--text-2); }
    .steps code {
      background: var(--surface);
      border: 1px solid var(--border);
      padding: 1px 7px; border-radius: 6px;
      font-family: var(--font-mono); font-size: 12.5px;
      color: var(--accent);
    }
    .err {
      background: color-mix(in srgb, #DC2626 10%, var(--surface));
      border: 1px solid color-mix(in srgb, #DC2626 25%, var(--border));
      color: #B91C1C;
      padding: 10px 14px; border-radius: 10px;
      font-size: 13px; margin-bottom: 14px;
    }
    .label {
      display: block; font-size: 12px; font-weight: 600;
      text-transform: uppercase; letter-spacing: .06em;
      color: var(--text-3); margin: 0 0 8px 4px;
    }
    .code-input {
      width: 100%;
      font-family: var(--font-mono);
      font-size: 38px; font-weight: 700;
      letter-spacing: .55em; text-align: center;
      padding: 18px 8px 18px 24px;
      background: var(--surface);
      border: 1.5px solid var(--border);
      border-radius: 14px;
      color: var(--text);
      outline: none;
      transition: border-color 140ms, box-shadow 140ms;
    }
    .code-input::placeholder { color: var(--text-4); letter-spacing: .55em; }
    .code-input:focus { border-color: var(--accent); box-shadow: 0 0 0 4px var(--accent-ring); }
    .submit {
      width: 100%; height: 50px;
      margin-top: 14px;
      background: var(--accent); color: var(--accent-fg);
      border: 0; border-radius: 14px;
      font-family: inherit; font-size: 15px; font-weight: 600;
      cursor: pointer;
      box-shadow: 0 8px 20px -8px color-mix(in srgb, var(--accent) 70%, transparent),
                  inset 0 1px 0 rgba(255,255,255,.2);
      transition: transform 80ms, background 140ms;
    }
    .submit:hover { background: color-mix(in srgb, var(--accent) 92%, black); }
    .submit:active { transform: translateY(1px); }
    .foot {
      margin-top: 22px; text-align: center;
      font-size: 12px; color: var(--text-4);
    }
    @media (max-width: 480px) {
      .login-shell { padding: 26px 22px; border-radius: 16px; }
      h1 { font-size: 22px; }
      .code-input { font-size: 30px; letter-spacing: .42em; padding-left: 16px; }
    }
  </style>
</head>
<body>
  <div class="login-shell">
    <div class="brand">
      <img class="brand-logo-full" src="/static/logo-full.svg"
           alt="Университет Яковлева" width="240" height="119">
      <div class="brand-text">
        <div class="brand-name">Электронное расписание</div>
        <div class="brand-sub">ФФМОИиТ · VK-бот</div>
      </div>
    </div>

    <h1>Вход в панель</h1>
    <p class="sub">Введи одноразовый код из VK-бота, чтобы продолжить.</p>

    {% if error %}<div class="err">{{ error }}</div>{% endif %}

    <div class="steps">
      <ol>
        <li>Открой VK-бота и напиши <code>/login</code></li>
        <li>Скопируй код из ответа</li>
        <li>Введи его ниже ↓</li>
      </ol>
    </div>

    <form method="post" action="{{ url_for('login_code') }}">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <label class="label" for="code">Код из бота</label>
      <input id="code" type="text" name="code" class="code-input"
             placeholder="······" maxlength="6" minlength="6"
             pattern="\\d{6}" inputmode="numeric" autocomplete="off"
             autofocus required>
      <label style="display:flex;align-items:center;gap:8px;margin:14px 4px 0;font-size:13.5px;color:var(--text-2);cursor:pointer;user-select:none;">
        <input type="checkbox" name="remember" value="1" checked
               style="width:16px;height:16px;accent-color:var(--accent);cursor:pointer;">
        Запомнить меня на этом устройстве (1 год)
      </label>
      <button class="submit" type="submit">Войти →</button>
    </form>

    <div class="foot">
      elschedule.ru · защищённое соединение<br>
      <a href="{{ url_for('privacy') }}" style="color:inherit;">Политика конфиденциальности</a>
      ·
      <a href="{{ url_for('terms') }}" style="color:inherit;">Условия использования</a>
    </div>
  </div>
</body>
</html>
"""

_DASHBOARD_CONTENT = """
{% set TYPE_COLORS = {
  'лк':'#6366F1','лекция':'#6366F1','лекц':'#6366F1',
  'пр':'#10B981','практика':'#10B981','практ':'#10B981',
  'лб':'#F59E0B','лаб':'#F59E0B','лабораторная':'#F59E0B',
  'сем':'#EC4899','семинар':'#EC4899',
  'кур':'#EAB308','курсовая':'#EAB308',
  'экз':'#DC2626','экзамен':'#DC2626',
  'зач':'#0EA5E9','зачёт':'#0EA5E9','зачет':'#0EA5E9',
} %}
{% macro tcolor(t) %}{{ TYPE_COLORS.get((t or '').strip().lower(), '#7A7872') }}{% endmacro %}

{% macro day_card(label, info, quick='today') %}
  <div class="card" style="overflow:hidden;">
    <div style="padding:14px 18px;border-bottom:1px solid var(--border);background:var(--surface-2);display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
      <span style="font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--text-3);">{{ label }}</span>
      <strong style="font-size:15px;color:var(--text);">{{ info.day or '—' }}</strong>
      {% if info.week %}<span class="chip" style="background:var(--accent-soft);color:var(--accent);border-color:transparent;height:22px;padding:0 10px;font-weight:600;border-radius:999px;display:inline-flex;align-items:center;font-size:11px;">{{ info.week }}</span>{% endif %}
      <span style="margin-left:auto;color:var(--text-3);font-size:12.5px;">
        {% if info.is_sunday %}выходной 🎉{% else %}{{ info.rows|length }} пар{% endif %}
      </span>
    </div>
    <div style="padding:10px 14px;">
      {% if info.is_sunday %}
        <div style="padding:30px;text-align:center;color:var(--text-3);">Воскресенье — занятий нет</div>
      {% elif not info.rows %}
        <div style="padding:24px;text-align:center;color:var(--text-3);">Нет занятий</div>
      {% else %}
        {% set last_group = namespace(course=None, direction=None) %}
        {# Карточка раньше прятала хвост в скрытый скролл и резала пару пополам.
           Показываем первые восемь и честно говорим, сколько осталось. #}
        {% set shown = info.rows[:8] %}
        {% for r in shown %}
          {% if r[0] != last_group.course or r[1] != last_group.direction %}
            {% if not loop.first %}<div style="height:8px;"></div>{% endif %}
            <div style="display:flex;align-items:center;gap:8px;padding:6px 4px 8px;font-size:11.5px;">
              <span style="background:var(--surface-3);color:var(--text-2);padding:2px 8px;border-radius:999px;font-weight:600;">{{ r[0] }}к</span>
              <strong style="color:var(--text-2);font-weight:600;">{{ r[1] }}</strong>
            </div>
            {% set last_group.course = r[0] %}
            {% set last_group.direction = r[1] %}
          {% endif %}
          <div class="pair-card" style="--type-color: {{ tcolor(r[8]) }};margin-bottom:6px;">
            <div class="pair-time">
              <span>{{ r[3] }}</span>
              {% if r[7] %}<span class="week">{{ r[7] }}</span>{% endif %}
              <span style="flex:1;"></span>
              {% if r[8] %}<span class="type-badge" style="--type-color: {{ tcolor(r[8]) }};">{{ type_short(r[8]) }}</span>{% endif %}
            </div>
            <div class="pair-subject">{{ r[4] }}</div>
            <div class="pair-meta">
              {% if r[5] %}<span>{{ r[5] }}</span><span class="sep">·</span>{% endif %}
              {% if r[6] %}<span class="mono">ауд. {{ r[6] }}</span>{% endif %}
            </div>
          </div>
        {% endfor %}
        {% if info.rows|length > shown|length %}
          <a href="{{ url_for('schedule_page', quick=quick) }}"
             style="display:block;margin-top:4px;padding:10px;text-align:center;border-radius:10px;
                    background:var(--surface-2);color:var(--text-2);text-decoration:none;
                    font-size:13px;font-weight:500;">
            Ещё {{ info.rows|length - shown|length }} пар за этот день →
          </a>
        {% endif %}
      {% endif %}
    </div>
  </div>
{% endmacro %}

<div class="d-flex align-items-center justify-content-between mb-4" style="flex-wrap:wrap;gap:10px;">
  <div>
    <h1 class="mb-0 h3">{% if is_admin %}📊 Дашборд{% else %}📅 Моё расписание{% endif %}</h1>
    <div style="margin-top:4px;color:var(--text-3);font-size:13px;">
      Привет, {{ display_name or 'друг' }}!
      {% if pref %}Твоя группа — <strong style="color:var(--accent);">{{ pref[0] }} курс · {{ pref[1] }}</strong>, ниже только её пары.
      {% else %}Сейчас видно пары всех курсов — отметь свою группу, чтобы остались только твои.
      {% endif %}
    </div>
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;">
    <a href="{{ url_for('schedule_page', quick='today') }}" class="btn btn-outline-secondary btn-sm">📅 Полное расписание</a>
    <a href="{{ url_for('calendar_ics') }}" class="btn btn-outline-secondary btn-sm" title="Скачать .ics для Google/Apple Calendar">📥 В календарь</a>
    {% if is_admin %}
      <a href="{{ url_for('upload_page') }}" class="btn btn-primary btn-sm">📤 Загрузить расписание</a>
      <a href="{{ url_for('download_current') }}" class="btn btn-outline-secondary btn-sm">⬇ Скачать Excel</a>
    {% endif %}
  </div>
</div>

<!-- ─── Подписка на курс/направление ─────────────────────── -->
<div class="card mb-4" style="padding:18px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
  <div style="font-size:28px;">{% if pref %}🎯{% else %}🔔{% endif %}</div>
  <div style="flex:1;min-width:200px;">
    <div style="font-weight:700;font-size:14.5px;color:var(--text);">
      {% if pref %}Твоя группа выбрана{% else %}Выбери свою группу — курс и направление{% endif %}
    </div>
    <div style="color:var(--text-3);font-size:12.5px;margin-top:2px;">
      {% if pref %}
        Твоя группа — <strong style="color:var(--text-2);">{{ pref[0] }} курс · {{ pref[1] }}</strong>.
        Ниже показаны её пары на сегодня и завтра, в календарь попадают они же,
        а бот присылает в VK напоминание за {{ notify_before_min }} минут до начала каждой пары.
        Группу можно сменить или отключить — расписание остальных курсов никуда не денется.
      {% else %}
        Это как выбрать свою группу один раз, чтобы дальше не искать её в общем расписании.
        Что изменится: ниже останутся только пары твоей группы вместо пар всех курсов,
        в календарь попадут они же, а бот начнёт присылать в VK напоминание
        за {{ notify_before_min }} минут до начала каждой пары. Отключить можно в любой момент.
      {% endif %}
    </div>
  </div>
  <button type="button" class="btn btn-primary btn-sm" onclick="document.getElementById('subscribeForm').style.display='block'; this.style.display='none';">
    {% if pref %}🔄 Сменить группу{% else %}🔔 Выбрать группу{% endif %}
  </button>
  {% if pref %}
    <form method="post" action="{{ url_for('me_unsubscribe') }}" style="margin:0;"
          onsubmit="return confirm('Отключить напоминания и снова видеть пары всех курсов? Вернуть выбор можно в любой момент.');">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <button class="btn btn-outline-secondary btn-sm" style="color:#DC2626;border-color:color-mix(in srgb,#DC2626 30%, var(--border));">✕ Отключить</button>
    </form>
  {% endif %}
</div>

<form id="subscribeForm" method="post" action="{{ url_for('me_subscribe') }}"
      class="card mb-4" style="padding:18px;display:none;flex-wrap:wrap;gap:12px;align-items:end;">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <div style="flex:1;min-width:140px;">
    <label class="form-label" style="font-size:11px;margin-bottom:4px;">Курс</label>
    <select aria-label="Курс" name="course" id="subCourse" class="form-select form-select-sm" required>
      <option value="">— выбери —</option>
      {% for c in all_courses %}
        <option value="{{ c }}" {% if pref and pref[0] == c %}selected{% endif %}>{{ c }} курс</option>
      {% endfor %}
    </select>
  </div>
  <div style="flex:2;min-width:200px;">
    <label class="form-label" style="font-size:11px;margin-bottom:4px;">Направление</label>
    <select aria-label="Направление" name="direction" id="subDirection" class="form-select form-select-sm" required>
      <option value="">— сначала выбери курс —</option>
      {% if pref %}
        {% for d in dirs_by_course.get(pref[0], []) %}
          <option value="{{ d }}" {% if d == pref[1] %}selected{% endif %}>{{ d }}</option>
        {% endfor %}
      {% endif %}
    </select>
  </div>
  <button class="btn btn-primary btn-sm" style="height:32px;">✓ Сохранить</button>
  <button type="button" class="btn btn-outline-secondary btn-sm" style="height:32px;"
          onclick="document.getElementById('subscribeForm').style.display='none';">Отмена</button>
</form>

<script>
(function(){
  // Динамическое обновление направлений при смене курса
  const dirsByCourse = {{ dirs_by_course | tojson }};
  const courseSel = document.getElementById('subCourse');
  const dirSel = document.getElementById('subDirection');
  if (courseSel && dirSel) {
    courseSel.addEventListener('change', function() {
      const c = parseInt(this.value);
      const dirs = dirsByCourse[c] || [];
      dirSel.innerHTML = dirs.length
        ? '<option value="">— выбери направление —</option>' + dirs.map(d => `<option value="${d.replace(/"/g,'&quot;')}">${d}</option>`).join('')
        : '<option value="">— нет данных для этого курса —</option>';
    });
  }
})();
</script>

<!-- ─── Расписание: сегодня и завтра (видно всем) ─────────── -->
<div class="row g-3 mb-4">
  <div class="col-lg-6">{{ day_card('📍 Сегодня' + (' · ' + pref[0]|string + 'к ' + pref[1] if pref else ''), preview.today) }}</div>
  <div class="col-lg-6">{{ day_card('→ Завтра' + (' · ' + pref[0]|string + 'к ' + pref[1] if pref else ''), preview.tomorrow, 'tomorrow') }}</div>
</div>

{% if not pref and not is_admin %}
  <div class="card" style="padding:18px;text-align:center;color:var(--text-3);font-size:13.5px;">
    💡 Выбор группы ничего не скрывает: расписание любого курса всегда открывается кнопкой «📅 Полное расписание» вверху страницы.
  </div>
{% endif %}

{% if is_admin and stats %}

<div class="row g-3 mb-4">
  <div class="col-sm-6 col-lg-3">
    <div class="card stat-card blue h-100">
      <div class="card-body">
        <div class="text-muted small">Строк расписания</div>
        <div class="fs-2 fw-bold">{{ stats.schedule_vk }}</div>
        <div class="text-muted small">TG-копия: {{ stats.schedule_s }}</div>
      </div>
    </div>
  </div>
  <div class="col-sm-6 col-lg-3">
    <div class="card stat-card green h-100">
      <div class="card-body">
        <div class="text-muted small">Уникальных пользователей</div>
        <div class="fs-2 fw-bold">{{ stats.users }}</div>
      </div>
    </div>
  </div>
  <div class="col-sm-6 col-lg-3">
    <div class="card stat-card purple h-100">
      <div class="card-body">
        <div class="text-muted small">Активных подписок</div>
        <div class="fs-2 fw-bold">{{ stats.subscriptions }}</div>
        {% if stats.subs_disabled %}
          <div class="text-muted small">отключено: {{ stats.subs_disabled }}</div>
        {% endif %}
      </div>
    </div>
  </div>
  <div class="col-sm-6 col-lg-3">
    <div class="card stat-card orange h-100">
      <div class="card-body">
        <div class="text-muted small">Напоминания · Дедлайны · Заметки</div>
        <div class="fs-3 fw-bold">
          {{ stats.reminders }} · {{ stats.deadlines }} · {{ stats.notes }}
        </div>
      </div>
    </div>
  </div>
</div>

<div class="row g-3 mb-4">

  <div class="col-lg-7">
    <div class="card h-100">
      <div class="card-header fw-semibold">🏆 Топ направлений по подпискам</div>
      <div class="card-body">
        {% if stats.top_directions %}
          {% set max_n = stats.top_directions[0][2] %}
          {% for direction, course, n in stats.top_directions %}
            <div class="d-flex justify-content-between align-items-center small mb-1">
              <span class="text-truncate me-2" title="{{ direction }}">
                <span class="badge bg-secondary me-1">{{ course }}к</span>{{ direction }}
              </span>
              <span class="fw-semibold ms-2">{{ n }}</span>
            </div>
            <div class="progress mb-2" style="height:6px">
              <div class="progress-bar bg-primary"
                   style="width: {{ (n / max_n * 100)|round(0) }}%"></div>
            </div>
          {% endfor %}
        {% else %}
          <p class="text-muted mb-0">Пока никто не подписался.</p>
        {% endif %}
      </div>
    </div>
  </div>

  <div class="col-lg-5">
    <div class="card h-100">
      <div class="card-header fw-semibold">📚 Распределение по курсам</div>
      <div class="card-body p-0">
        {% if stats.courses_distribution %}
          <table class="table table-sm mb-0">
            <thead class="table-light">
              <tr><th>Курс</th><th>Направлений</th><th>Записей</th><th>Подписок</th></tr>
            </thead>
            <tbody>
              {% for c, subs, dirs, rows in stats.courses_distribution %}
              <tr>
                <td><strong>{{ c }}</strong></td>
                <td>{{ dirs }}</td>
                <td>{{ rows }}</td>
                <td>
                  {% if subs > 0 %}
                    <span class="badge bg-primary">{{ subs }}</span>
                  {% else %}
                    <span class="text-muted">—</span>
                  {% endif %}
                </td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        {% else %}
          <p class="text-muted mb-0 p-3">Расписание не загружено.</p>
        {% endif %}
      </div>
    </div>
  </div>

</div>

<div class="row g-3">
  <div class="col-lg-7">
    <div class="card h-100">
      <div class="card-header fw-semibold d-flex justify-content-between">
        <span>📜 Последние загрузки расписания</span>
        <a href="{{ url_for('upload_page') }}" class="small">все →</a>
      </div>
      <div class="card-body p-0">
        {% if stats.recent_uploads %}
          <table class="table table-sm mb-0">
            <thead class="table-light">
              <tr><th>Когда</th><th>Файл</th><th>Записей</th><th>Кем</th></tr>
            </thead>
            <tbody>
              {% for ts, name, rows, who in stats.recent_uploads %}
              <tr>
                <td class="small text-nowrap">{{ ts }}</td>
                <td class="small">{{ name }}</td>
                <td>{{ rows }}</td>
                <td class="small">{{ who }}</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        {% else %}
          <p class="text-muted mb-0 p-3">Загрузок пока не было.</p>
        {% endif %}
      </div>
    </div>
  </div>

  <div class="col-lg-5">
    <div class="card h-100">
      <div class="card-header fw-semibold">👥 Недавняя активность</div>
      <div class="card-body p-0">
        {% if stats.recent_users %}
          <table class="table table-sm mb-0">
            <thead class="table-light">
              <tr><th>VK</th><th>Когда</th><th>Что</th></tr>
            </thead>
            <tbody>
              {% for uid, ts, kind in stats.recent_users %}
              <tr>
                <td><a href="https://vk.com/id{{ uid }}" target="_blank" class="small">id{{ uid }}</a></td>
                <td class="small text-nowrap">{{ ts }}</td>
                <td class="small">{{ kind }}</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        {% else %}
          <p class="text-muted mb-0 p-3">Активности нет.</p>
        {% endif %}
      </div>
    </div>
  </div>
</div>
{% endif %}{# end is_admin stats block #}
"""

_UPLOAD_CONTENT = """
<style>
  .dropzone {
    border: 2px dashed #adb5bd;
    border-radius: 10px;
    padding: 2rem;
    text-align: center;
    background: #fff;
    transition: all 0.2s;
    cursor: pointer;
  }
  .dropzone:hover, .dropzone.dragover {
    border-color: #0d6efd;
    background: #e7f1ff;
  }
  .dropzone .icon { font-size: 2.5rem; line-height: 1; }
  .dropzone input[type=file] { display: none; }
  .dropzone .filename { font-weight: 600; color: #0d6efd; }
  .upload-spinner { display: none; }
  form.uploading .upload-spinner { display: inline-block; }
  form.uploading button[type=submit] { pointer-events: none; opacity: .6; }
</style>

<h3 class="mb-4">📤 Загрузить расписание</h3>

{% if commit_result %}
<div class="alert alert-success">
  ✅ Расписание обновлено: <strong>{{ commit_result.row_count }}</strong> записей.
  {% if commit_result.broadcast %}
    {% if commit_result.broadcast.started %}
      📢 Рассылка уведомлений запущена для {{ commit_result.broadcast.total }} подписчиков —
      прогресс смотри на <a href="{{ url_for('broadcast_page') }}">странице рассылки</a>.
    {% else %}
      📢 Рассылка не запущена: другая рассылка ещё идёт. Повтори позже на
      <a href="{{ url_for('broadcast_page') }}">странице рассылки</a>.
    {% endif %}
  {% endif %}
  <br>Бот подхватит изменения в течение минуты (hot-reload).
</div>
{% if conflicts_count and conflicts_count > 0 %}
<div class="alert alert-warning d-flex align-items-center gap-3">
  <span style="font-size:24px;">⚠️</span>
  <div class="flex-grow-1">
    <strong>Найдено конфликтов: {{ conflicts_count }}</strong>
    <div class="small text-muted">
      {% if conflict_teachers %}Двойное бронирование преподавателей: {{ conflict_teachers }}.{% endif %}
      {% if conflict_rooms %} Двойное бронирование аудиторий: {{ conflict_rooms }}.{% endif %}
    </div>
  </div>
  <a href="{{ url_for('conflicts_page') }}" class="btn btn-sm btn-outline-warning">
    Посмотреть →
  </a>
</div>
{% elif commit_result %}
<div class="alert alert-info py-2 small">✅ Конфликтов не найдено — расписание чистое.</div>
{% endif %}
{% endif %}

{% if error %}
<div class="card mb-4 border-danger">
  <div class="card-header bg-danger text-white">Ошибка</div>
  <div class="card-body p-0"><pre class="m-0">{{ error }}</pre></div>
</div>
{% endif %}

{% if preview and pending_token %}
<div class="card mb-4 border-primary">
  <div class="card-header bg-primary text-white d-flex justify-content-between">
    <span>🔍 Превью изменений</span>
    <span class="small">подписчиков сейчас: <strong>{{ subscriber_count }}</strong></span>
  </div>
  <div class="card-body">
    <table class="table table-sm mb-3">
      <thead><tr><th></th><th>Сейчас</th><th>Будет</th><th>Δ</th></tr></thead>
      <tbody>
        <tr><td>Курсы</td>
            <td>{{ preview.courses_before }}</td>
            <td>{{ preview.courses_after }}</td>
            <td class="{% if preview.courses_after > preview.courses_before %}text-success{% elif preview.courses_after < preview.courses_before %}text-danger{% endif %}">
              {{ '%+d' % (preview.courses_after - preview.courses_before) }}
            </td></tr>
        <tr><td>Направления</td>
            <td>{{ preview.directions_before }}</td>
            <td>{{ preview.directions_after }}</td>
            <td class="{% if preview.directions_after > preview.directions_before %}text-success{% elif preview.directions_after < preview.directions_before %}text-danger{% endif %}">
              {{ '%+d' % (preview.directions_after - preview.directions_before) }}
            </td></tr>
        <tr><td>Записей</td>
            <td>{{ preview.records_before }}</td>
            <td>{{ preview.records_after }}</td>
            <td class="{% if preview.records_after > preview.records_before %}text-success{% elif preview.records_after < preview.records_before %}text-danger{% endif %}">
              {{ '%+d' % (preview.records_after - preview.records_before) }}
            </td></tr>
      </tbody>
    </table>
    {% if preview.new_directions %}
      <p class="mb-1 fw-semibold text-success">Появятся ({{ preview.new_directions|length }}):</p>
      <ul class="small">
        {% for d in preview.new_directions %}<li>{{ d }}</li>{% endfor %}
      </ul>
    {% endif %}
    {% if preview.removed_directions %}
      <p class="mb-1 fw-semibold text-danger">Исчезнут ({{ preview.removed_directions|length }}):</p>
      <ul class="small">
        {% for d in preview.removed_directions %}<li>{{ d }}</li>{% endfor %}
      </ul>
    {% endif %}
    {% if not preview.new_directions and not preview.removed_directions
          and preview.records_before == preview.records_after %}
      <div class="alert alert-warning small mb-3">
        ⚠ Состав направлений не изменился и количество записей такое же.
        Возможно, файл идентичен текущему расписанию.
      </div>
    {% endif %}

    <form method="post" action="{{ url_for('upload_commit') }}" class="mt-3">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <input type="hidden" name="token" value="{{ pending_token }}">
      <div class="form-check mb-3">
        <input type="checkbox" class="form-check-input" id="notify_check"
               name="notify" value="1" {% if subscriber_count > 0 %}checked{% endif %}>
        <label for="notify_check" class="form-check-label">
          📢 Уведомить подписчиков ({{ subscriber_count }} чел.) в VK о новом расписании
        </label>
      </div>
      <div class="d-flex gap-2">
        <button class="btn btn-success">✅ Применить</button>
        <button class="btn btn-outline-secondary" type="button"
                onclick="document.getElementById('cancel-form').submit()">Отмена</button>
      </div>
    </form>
    <form id="cancel-form" method="post" action="{{ url_for('upload_cancel') }}"
          class="d-none">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <input type="hidden" name="token" value="{{ pending_token }}">
    </form>
  </div>
</div>
{% else %}
<div class="card mb-4">
  <div class="card-body">
    <p class="text-muted mb-3">
      Загрузи Excel-файл расписания (<code>.xlsx</code>). Покажу, что изменится —
      ничего не запишется, пока не подтвердишь.
    </p>
    <form method="post" enctype="multipart/form-data" id="upload-form">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <label class="dropzone d-block mb-3" id="dropzone">
        <div class="icon">📂</div>
        <div class="mt-2" id="drop-text">
          Перетащи файл сюда или нажми, чтобы выбрать
        </div>
        <div class="text-muted small">.xlsx · максимум один файл</div>
        <input type="file" name="excel_file" accept=".xlsx,.xls" required id="file-input">
      </label>
      <button class="btn btn-primary">
        <span class="spinner-border spinner-border-sm me-1 upload-spinner"></span>
        🔍 Загрузить и показать дифф
      </button>
    </form>
    <script>
      (function () {
        var dz = document.getElementById('dropzone');
        var fi = document.getElementById('file-input');
        var txt = document.getElementById('drop-text');
        var form = document.getElementById('upload-form');
        function setName(name) {
          txt.innerHTML = '<span class="filename">' + name + '</span>';
        }
        fi.addEventListener('change', function () {
          if (fi.files && fi.files[0]) setName(fi.files[0].name);
        });
        ['dragenter', 'dragover'].forEach(function (ev) {
          dz.addEventListener(ev, function (e) {
            e.preventDefault(); e.stopPropagation(); dz.classList.add('dragover');
          });
        });
        ['dragleave', 'drop'].forEach(function (ev) {
          dz.addEventListener(ev, function (e) {
            e.preventDefault(); e.stopPropagation(); dz.classList.remove('dragover');
          });
        });
        dz.addEventListener('drop', function (e) {
          var files = e.dataTransfer.files;
          if (files.length) { fi.files = files; setName(files[0].name); }
        });
        form.addEventListener('submit', function () { form.classList.add('uploading'); });
      })();
    </script>
  </div>
</div>
{% endif %}

<div class="card">
  <div class="card-header fw-semibold d-flex justify-content-between align-items-center">
    <span>📜 История загрузок</span>
    <a href="{{ url_for('download_current') }}" class="btn btn-sm btn-outline-primary">
      ⬇ Скачать текущий Excel
    </a>
  </div>
  <div class="card-body p-0">
    {% if versions %}
    <table class="table table-sm m-0">
      <thead><tr>
        <th>#</th><th>Когда</th><th>Файл</th><th>Записей</th><th>Кем</th><th></th>
      </tr></thead>
      <tbody>
        {% for v in versions %}
        <tr>
          <td>{{ v.id }}</td>
          <td class="text-nowrap small">{{ v.uploaded_at }}</td>
          <td class="small">{{ v.original_filename }}</td>
          <td>{{ v.row_count }}</td>
          <td class="small">{{ v.uploaded_by }}</td>
          <td class="d-flex gap-1">
            <a href="{{ url_for('download_version', version_id=v.id) }}"
               class="btn btn-sm btn-outline-secondary" title="Скачать">⬇</a>
            {% if not loop.first %}
            <form method="post" action="{{ url_for('upload_rollback', version_id=v.id) }}"
                  onsubmit="return confirm('Откатить расписание к версии #{{ v.id }}?')">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
              <button class="btn btn-sm btn-outline-warning">↩ Откат</button>
            </form>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
      <p class="text-muted mb-0 p-3">Загрузок пока не было.</p>
    {% endif %}
  </div>
</div>
"""

_SCHEDULE_CONTENT = """
{% set DAYS = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота'] %}
{% set TYPE_COLORS = {
  'лк': '#6366F1', 'лекция': '#6366F1', 'лекц': '#6366F1',
  'пр': '#10B981', 'практика': '#10B981', 'практ': '#10B981',
  'лб': '#F59E0B', 'лаб': '#F59E0B', 'лабораторная': '#F59E0B',
  'сем': '#EC4899', 'семинар': '#EC4899',
  'кур': '#EAB308', 'курсовая': '#EAB308',
  'экз': '#DC2626', 'экзамен': '#DC2626',
  'зач': '#0EA5E9', 'зачёт': '#0EA5E9', 'зачет': '#0EA5E9',
} %}
{% macro type_color(t) %}{{ TYPE_COLORS.get((t or '').strip().lower(), '#7A7872') }}{% endmacro %}

<div class="page-title" style="display:flex;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;gap:14px;">
  <div>
    <h1 style="font-size:28px;font-weight:700;letter-spacing:-.02em;margin:0;">Расписание · VK-база</h1>
    <div style="margin-top:6px;font-size:14px;color:var(--text-3);">
      Показано <strong style="color:var(--text);">{{ rows|length }}</strong> из {{ total }} записей{% if rows|length != total %} · фильтры активны{% endif %}
    </div>
  </div>
  <div class="seg" role="tablist" id="schedView">
    <button type="button" data-view="table">📋 Таблица</button>
    <button type="button" data-view="cards" class="on">🗂 Карточки</button>
  </div>
</div>

<!-- Quick-фильтры: Сегодня/Завтра/Вся неделя + чёт/нечёт -->
<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
  <div class="seg">
    <a href="{{ url_for('schedule_page', quick='today') }}"
       class="{{ 'on' if quick == 'today' }}" style="text-decoration:none;display:inline-flex;align-items:center;padding:0 14px;height:32px;border-radius:7px;font-size:13px;font-weight:500;color:var(--text-3);">📍 Сегодня</a>
    <a href="{{ url_for('schedule_page', quick='tomorrow') }}"
       class="{{ 'on' if quick == 'tomorrow' }}" style="text-decoration:none;display:inline-flex;align-items:center;padding:0 14px;height:32px;border-radius:7px;font-size:13px;font-weight:500;color:var(--text-3);">→ Завтра</a>
    <a href="{{ url_for('schedule_page', quick='all') }}"
       class="{{ 'on' if quick == 'all' }}" style="text-decoration:none;display:inline-flex;align-items:center;padding:0 14px;height:32px;border-radius:7px;font-size:13px;font-weight:500;color:var(--text-3);">🗓 Вся неделя</a>
  </div>
  {% if quick == 'today' or quick == 'tomorrow' %}
    <span class="chip" style="background:var(--accent-soft);color:var(--accent);border-color:transparent;height:26px;padding:0 12px;font-weight:600;border-radius:999px;display:inline-flex;align-items:center;">
      {{ day_filter or 'выходной 🎉' }}{% if week_filter %} · {{ week_filter }} неделя{% endif %}
    </span>
  {% endif %}
</div>

<form class="card" method="get" style="padding:14px 18px;display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;align-items:end;">
  <div>
    <label class="form-label" style="font-size:11px;margin-bottom:4px;">Курс</label>
    <select aria-label="Фильтр по курсу" name="course" class="form-select form-select-sm">
      <option value="">Все курсы</option>
      {% for c in courses %}
        <option value="{{ c }}" {% if c|string == course_filter %}selected{% endif %}>{{ c }} курс</option>
      {% endfor %}
    </select>
  </div>
  <div>
    <label class="form-label" style="font-size:11px;margin-bottom:4px;">Направление</label>
    <select aria-label="Фильтр по направлению" name="direction" class="form-select form-select-sm">
      <option value="">Все направления</option>
      {% for d in directions %}
        <option value="{{ d }}" {% if d == direction_filter %}selected{% endif %}>{{ d }}</option>
      {% endfor %}
    </select>
  </div>
  <div>
    <label class="form-label" style="font-size:11px;margin-bottom:4px;">День недели</label>
    <select aria-label="Фильтр по дню недели" name="day" class="form-select form-select-sm">
      <option value="">Все дни</option>
      {% for d in DAYS %}
        <option value="{{ d }}" {% if d == day_filter %}selected{% endif %}>{{ d }}</option>
      {% endfor %}
    </select>
  </div>
  <div>
    <label class="form-label" style="font-size:11px;margin-bottom:4px;">Неделя</label>
    <select aria-label="Фильтр по чётности недели" name="week" class="form-select form-select-sm">
      <option value="">Любая</option>
      <option value="чёт" {% if week_filter == 'чёт' %}selected{% endif %}>Чётная</option>
      <option value="нечет" {% if week_filter == 'нечет' %}selected{% endif %}>Нечётная</option>
    </select>
  </div>
  <div>
    <label class="form-label" style="font-size:11px;margin-bottom:4px;">Тип занятия</label>
    <select aria-label="Фильтр по типу занятия" name="type" class="form-select form-select-sm">
      <option value="">Все типы</option>
      {% for t in types %}
        <option value="{{ t }}" {% if t == type_filter %}selected{% endif %}>{{ t }}</option>
      {% endfor %}
    </select>
  </div>
  <div>
    <label class="form-label" style="font-size:11px;margin-bottom:4px;">Преподаватель</label>
    <select aria-label="Фильтр по преподавателю" name="teacher" class="form-select form-select-sm">
      <option value="">Все преподаватели</option>
      {% for t in teachers %}
        <option value="{{ t }}" {% if t == teacher_filter %}selected{% endif %}>{{ t }}</option>
      {% endfor %}
    </select>
  </div>
  <div>
    <label class="form-label" style="font-size:11px;margin-bottom:4px;">Аудитория</label>
    <select aria-label="Фильтр по аудитории" name="room" class="form-select form-select-sm">
      <option value="">Любая</option>
      {% for r in rooms %}
        <option value="{{ r }}" {% if r == room_filter %}selected{% endif %}>{{ r }}</option>
      {% endfor %}
    </select>
  </div>
  <div style="grid-column: 1 / -1;display:flex;gap:10px;align-items:end;flex-wrap:wrap;">
    <div style="flex:1;min-width:200px;">
      <label class="form-label" style="font-size:11px;margin-bottom:4px;">Поиск</label>
      <input type="text" name="q" value="{{ q }}" class="form-control form-control-sm"
             placeholder="🔍 Предмет, направление, преподаватель, аудитория…">
    </div>
    <button class="btn btn-primary btn-sm" style="height:32px;">Найти</button>
    {% if course_filter or q or day_filter or week_filter or quick or direction_filter or teacher_filter or type_filter or room_filter %}
      <a href="{{ url_for('schedule_page') }}" class="btn btn-outline-secondary btn-sm" style="height:32px;display:inline-flex;align-items:center;">Сбросить</a>
    {% endif %}
  </div>
</form>

<!-- Активные фильтры — чипы для быстрого снятия -->
{% set active = [] %}
{% if course_filter %}{% set _ = active.append(('course', course_filter + ' курс')) %}{% endif %}
{% if direction_filter %}{% set _ = active.append(('direction', direction_filter)) %}{% endif %}
{% if day_filter and day_filter != '__none__' %}{% set _ = active.append(('day', day_filter)) %}{% endif %}
{% if week_filter %}{% set _ = active.append(('week', week_filter + ' неделя')) %}{% endif %}
{% if type_filter %}{% set _ = active.append(('type', 'тип: ' + type_filter)) %}{% endif %}
{% if teacher_filter %}{% set _ = active.append(('teacher', teacher_filter)) %}{% endif %}
{% if room_filter %}{% set _ = active.append(('room', 'ауд. ' + room_filter)) %}{% endif %}
{% if q %}{% set _ = active.append(('q', '«' + q + '»')) %}{% endif %}
{% if active %}
<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
  <span style="font-size:12px;color:var(--text-3);">Активные фильтры:</span>
  {% for key, label in active %}
    {% set new_args = {} %}
    {% if course_filter and key != 'course' %}{% set _ = new_args.update({'course': course_filter}) %}{% endif %}
    {% if direction_filter and key != 'direction' %}{% set _ = new_args.update({'direction': direction_filter}) %}{% endif %}
    {% if day_filter and day_filter != '__none__' and key != 'day' %}{% set _ = new_args.update({'day': day_filter}) %}{% endif %}
    {% if week_filter and key != 'week' %}{% set _ = new_args.update({'week': week_filter}) %}{% endif %}
    {% if type_filter and key != 'type' %}{% set _ = new_args.update({'type': type_filter}) %}{% endif %}
    {% if teacher_filter and key != 'teacher' %}{% set _ = new_args.update({'teacher': teacher_filter}) %}{% endif %}
    {% if room_filter and key != 'room' %}{% set _ = new_args.update({'room': room_filter}) %}{% endif %}
    {% if q and key != 'q' %}{% set _ = new_args.update({'q': q}) %}{% endif %}
    <a href="{{ url_for('schedule_page', **new_args) }}"
       class="chip" style="background:var(--accent-soft);color:var(--accent);border-color:transparent;height:24px;padding:0 10px;font-weight:600;border-radius:999px;display:inline-flex;align-items:center;gap:6px;font-size:11.5px;text-decoration:none;">
      {{ label }} <span style="opacity:.6;">×</span>
    </a>
  {% endfor %}
</div>
{% endif %}

<!-- Группировка курс → направление, дальше дни (для обоих видов) -->
{% set groups = [] %}
{% set seen = {} %}
{% for r in rows %}
  {% set key = (r[0], r[1]) %}
  {% if key not in seen %}
    {% set _ = seen.update({key: groups|length}) %}
    {% set _ = groups.append({'course': r[0], 'direction': r[1], 'rows': []}) %}
  {% endif %}
  {% set _ = groups[seen[key]]['rows'].append(r) %}
{% endfor %}

<!-- ─── 1. ТАБЛИЦА (с подзаголовками курс/направление) ───── -->
<div data-view-pane="table" style="display:none;">
  <div class="table-responsive">
    <table class="table table-hover align-middle">
      <thead>
        <tr>
          <th style="width:140px;">День</th>
          <th style="width:120px;">Время</th>
          <th style="width:70px;">Нед.</th>
          <th>Предмет</th>
          <th style="width:80px;">Тип</th>
          <th>Преподаватель</th>
          <th style="width:80px;">Ауд.</th>
          <th style="width:140px;">Период</th>
        </tr>
      </thead>
      <tbody>
        {% if not groups %}
          <tr><td colspan="8" style="text-align:center;padding:40px;color:var(--text-3);">Нет записей по фильтру</td></tr>
        {% endif %}
        {% for g in groups %}
          <tr style="background:var(--surface-2);">
            <td colspan="8" style="padding:10px 14px;border-bottom:1px solid var(--border);">
              <div style="display:flex;align-items:center;gap:10px;font-size:13px;">
                <span class="course-chip" style="background:var(--accent-soft);color:var(--accent);padding:3px 10px;border-radius:999px;font-size:11.5px;font-weight:700;">{{ g.course }} курс</span>
                <strong style="color:var(--text);">{{ g.direction }}</strong>
                <span style="margin-left:auto;color:var(--text-3);font-size:12px;">{{ g.rows|length }} пар</span>
              </div>
            </td>
          </tr>
          {% for r in g.rows %}
          <tr>
            <td>{{ r[2] }}</td>
            <td style="white-space:nowrap;font-family:var(--font-mono);font-size:12.5px;">{{ r[3] }}</td>
            <td style="color:var(--text-3);">{{ r[7] or '—' }}</td>
            <td style="font-weight:500;">{{ r[4] }}</td>
            <td><span class="type-badge" style="--type-color: {{ type_color(r[8]) }};">{{ type_short(r[8]) or '—' }}</span></td>
            <td style="color:var(--text-3);">{{ r[5] }}</td>
            <td style="font-family:var(--font-mono);font-size:12.5px;">{{ r[6] }}</td>
            <td style="color:var(--text-3);font-size:12.5px;">{{ r[9] or '—' }}</td>
          </tr>
          {% endfor %}
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>

<!-- ─── 2. КАРТОЧКИ (курс → направление → день-колонки) ────── -->
<div data-view-pane="cards">
  {% if not groups %}
    <div class="card" style="padding:40px;text-align:center;color:var(--text-3);">Нет записей по фильтру</div>
  {% endif %}
  {% for g in groups %}
    <div class="card" style="margin-bottom:18px;overflow:hidden;">
      <div style="padding:14px 18px;border-bottom:1px solid var(--border);background:var(--surface-2);display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
        <span class="course-chip" style="background:var(--accent-soft);color:var(--accent);padding:4px 12px;border-radius:999px;font-size:12px;font-weight:700;">{{ g.course }} курс</span>
        <strong style="font-size:15px;color:var(--text);letter-spacing:-.005em;">{{ g.direction }}</strong>
        <span style="margin-left:auto;color:var(--text-3);font-size:12.5px;">{{ g.rows|length }} пар</span>
      </div>
      <div style="padding:14px;">
        <div class="day-grid">
          {% for d in DAYS %}
            {% set day_rows = g.rows|selectattr('2', 'equalto', d)|list %}
            {% if day_rows %}
              <div class="day-col">
                <div class="day-head">
                  <span class="day-name">{{ d }}</span>
                  <span class="day-count">{{ day_rows|length }} пар</span>
                </div>
                <div class="day-list">
                  {% for r in day_rows %}
                  <div class="pair-card" style="--type-color: {{ type_color(r[8]) }};">
                    <div class="pair-time">
                      <span>{{ r[3] }}</span>
                      {% if r[7] %}<span class="week">{{ r[7] }}</span>{% endif %}
                      <span style="flex:1;"></span>
                      {% if r[8] %}<span class="type-badge" style="--type-color: {{ type_color(r[8]) }};">{{ type_short(r[8]) }}</span>{% endif %}
                    </div>
                    <div class="pair-subject">{{ r[4] }}</div>
                    <div class="pair-meta">
                      {% if r[5] %}<a href="{{ url_for('teacher_page', name=r[5]) }}" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--border-strong);">{{ r[5] }}</a>{% endif %}
                      {% if r[5] and r[6] %}<span class="sep">·</span>{% endif %}
                      {% if r[6] %}<a href="{{ url_for('room_page', name=r[6]) }}" class="mono" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--border-strong);">ауд. {{ r[6] }}</a>{% endif %}
                      {% if r[9] %}<span class="sep">·</span><span>{{ r[9] }}</span>{% endif %}
                    </div>
                  </div>
                  {% endfor %}
                </div>
              </div>
            {% endif %}
          {% endfor %}
        </div>
      </div>
    </div>
  {% endfor %}
</div>

<script>
(function(){
  const seg = document.getElementById('schedView');
  if (!seg) return;
  const panes = document.querySelectorAll('[data-view-pane]');
  let initial = localStorage.getItem('schedView') || 'cards';
  if (initial !== 'table' && initial !== 'cards') initial = 'cards';
  function showView(v) {
    panes.forEach(p => p.style.display = (p.dataset.viewPane === v ? '' : 'none'));
    seg.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.view === v));
    localStorage.setItem('schedView', v);
  }
  seg.addEventListener('click', e => {
    const b = e.target.closest('button[data-view]');
    if (b) showView(b.dataset.view);
  });
  showView(initial);
})();
</script>
"""

_ME_CONTENT = """
{% macro del_btn(kind, id, label='Удалить') %}
  <form method="post" action="{{ url_for('me_delete') }}" class="d-inline"
        onsubmit="return confirm('{{ label }}?');">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
    <input type="hidden" name="kind" value="{{ kind }}">
    <input type="hidden" name="id" value="{{ id }}">
    <button class="btn btn-sm btn-link text-danger p-0" style="font-size:18px;line-height:1;"
            title="{{ label }}">✕</button>
  </form>
{% endmacro %}

<div class="d-flex align-items-center mb-4 gap-3">
  <div>
    <h1 class="mb-0 h3">👤 Мой профиль</h1>
    <span class="text-muted small">
      VK: <a href="https://vk.com/id{{ vk_id }}" target="_blank">vk.com/id{{ vk_id }}</a>
    </span>
  </div>
</div>

{% if pref %}
{# Тот же смысл и то же название, что на дашборде: «твоя группа».
   Было «Сохранённое расписание» синей плашкой — другой термин и чужой цвет. #}
<div class="sub-chip d-inline-flex mb-4">
  <span class="sub-chip-text">
    📌 Твоя группа: <strong>{{ pref[0] }} курс · {{ pref[1] }}</strong><br>
    <span style="color:var(--text-3);">Ниже — напоминания, которые бот шлёт по её парам.</span>
  </span>
</div>
{% endif %}

<div class="row g-3">

  {# Заметки #}
  <div class="col-12">
    <div class="card">
      <div class="card-header fw-semibold">📝 Заметки
        <span class="badge bg-secondary ms-1">{{ notes|length }}</span>
      </div>
      <div class="card-body p-0">
        {% if notes %}
          <ul class="list-group list-group-flush">
            {% for row in notes %}
            <li class="list-group-item d-flex justify-content-between gap-3">
              <div class="flex-grow-1">
                <div class="text-muted small mb-1">{{ row[2] }}</div>
                <div style="white-space:pre-wrap">{{ row[1] }}</div>
              </div>
              <div>{{ del_btn('note', row[0], 'Удалить заметку') }}</div>
            </li>
            {% endfor %}
          </ul>
        {% else %}
          <p class="text-muted mb-0 p-3">Нет заметок.</p>
        {% endif %}
      </div>
    </div>
  </div>

  {# Активные напоминания #}
  <div class="col-md-6">
    <div class="card h-100">
      <div class="card-header fw-semibold">⏰ Активные напоминания
        <span class="badge bg-secondary ms-1">{{ reminders|length }}</span>
      </div>
      <div class="card-body p-0">
        {% if reminders %}
          <ul class="list-group list-group-flush">
            {% for row in reminders %}
            <li class="list-group-item d-flex justify-content-between gap-3">
              <div class="flex-grow-1">
                <strong>{{ row[1] }}</strong>
                <div class="text-muted small">📅 {{ row[2] }}</div>
              </div>
              <div>{{ del_btn('reminder', row[0], 'Удалить напоминание') }}</div>
            </li>
            {% endfor %}
          </ul>
        {% else %}
          <p class="text-muted mb-0 p-3">Нет активных напоминаний.</p>
        {% endif %}
      </div>

      {# История сработавших — свёрнутая #}
      {% if reminders_past %}
      <div class="card-footer p-0" style="background:transparent;border-top:1px solid var(--border);">
        <details>
          <summary style="padding:10px 14px;cursor:pointer;font-size:13px;color:var(--text-3);">
            История сработавших ({{ reminders_past|length }})
          </summary>
          <ul class="list-group list-group-flush" style="border-top:1px solid var(--border);">
            {% for row in reminders_past %}
            <li class="list-group-item d-flex justify-content-between gap-3" style="opacity:.7;">
              <div class="flex-grow-1">
                <span style="text-decoration:line-through;">{{ row[1] }}</span>
                <div class="text-muted small">📅 {{ row[2] }}</div>
              </div>
              <div>{{ del_btn('reminder', row[0], 'Удалить из истории') }}</div>
            </li>
            {% endfor %}
          </ul>
        </details>
      </div>
      {% endif %}
    </div>
  </div>

  {# Дедлайны #}
  <div class="col-md-6">
    <div class="card h-100">
      <div class="card-header fw-semibold">📌 Дедлайны
        <span class="badge bg-secondary ms-1">{{ deadlines|length }}</span>
      </div>
      <div class="card-body p-0">
        {% if deadlines %}
          <ul class="list-group list-group-flush">
            {% for row in deadlines %}
            <li class="list-group-item d-flex justify-content-between gap-3">
              <div class="flex-grow-1">
                <strong>{{ row[1] }}</strong>
                {% if row[2] %}<span class="text-muted"> — {{ row[2] }}</span>{% endif %}
                <div class="text-muted small">📅 {{ row[3] }}</div>
              </div>
              <div>{{ del_btn('deadline', row[0], 'Удалить дедлайн') }}</div>
            </li>
            {% endfor %}
          </ul>
        {% else %}
          <p class="text-muted mb-0 p-3">Нет дедлайнов.</p>
        {% endif %}
      </div>
    </div>
  </div>

  {# Подписки #}
  <div class="col-12">
    <div class="card">
      <div class="card-header fw-semibold">🔔 Напоминания о парах
        <span class="badge bg-secondary ms-1">{{ subscriptions|length }}</span>
      </div>
      <div class="card-body">
        <p class="text-muted" style="font-size:13px;margin-bottom:12px;">
          {% if subscriptions %}
            Бот присылает в VK напоминание за {{ notify_before_min }} минут до начала каждой пары
            этих групп. Крестик отключает напоминания — расписание останется доступным.
          {% else %}
            Здесь появятся группы, за парами которых следит бот: он присылает в VK
            напоминание за {{ notify_before_min }} минут до начала занятия.
          {% endif %}
        </p>
        {% if subscriptions %}
          <div class="d-flex flex-column gap-2">
            {% for row in subscriptions %}
              {# Раньше это был bootstrap-badge: он не переносит текст, и длинное
                 название направления уезжало за край экрана на телефоне. #}
              <div class="sub-chip">
                <span class="sub-chip-text">
                  <strong>{{ row[1] }} курс</strong> · {{ row[2] }}
                </span>
                <form method="post" action="{{ url_for('me_delete') }}" class="m-0"
                      onsubmit="return confirm('Отписаться от {{ row[1] }} курс — {{ row[2] }}?');">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                  <input type="hidden" name="kind" value="subscription">
                  <input type="hidden" name="id" value="{{ row[0] }}">
                  <button class="sub-chip-x" type="submit" title="Отписаться"
                          aria-label="Отписаться от {{ row[1] }} курс {{ row[2] }}">✕</button>
                </form>
              </div>
            {% endfor %}
          </div>
        {% else %}
          <p class="mb-0" style="font-size:13px;">
            Пока ни одной. Выбрать группу можно на
            <a href="{{ url_for('dashboard') }}">главной странице</a>.
          </p>
        {% endif %}
      </div>
    </div>
  </div>

  {# Удаление всех данных — обещание из политики конфиденциальности, которое
     до сих пор выполнялось вручную через переписку с администратором. #}
  <div class="col-12">
    <div class="card" style="border-color:color-mix(in srgb,#DC2626 30%,var(--border));">
      <div class="card-header fw-semibold" style="color:#DC2626;">🗑 Удалить мои данные</div>
      <div class="card-body">
        {% if data_counts %}
          <p style="font-size:13px;margin-bottom:10px;">
            Сейчас о тебе хранится:
            {% for table, n in data_counts.items() %}<span class="chip" style="margin:2px 4px 2px 0;">{{ data_labels[table] }} — {{ n }}</span>{% endfor %}
          </p>
        {% else %}
          <p style="font-size:13px;margin-bottom:10px;">Кроме записи о входе, о тебе ничего не хранится.</p>
        {% endif %}
        <p style="font-size:13px;color:var(--text-3);">
          Кнопка удаляет заметки, напоминания, дедлайны, подписки, выбранную группу
          и сессии входа — сразу и без возможности восстановить. Бот про тебя забудет:
          уведомления о парах перестанут приходить, диалог начнётся с нуля.
          В журнале безопасности останется запись о самом факте удаления — она
          нужна, чтобы можно было разобраться в спорной ситуации, и пропадёт
          вместе с остальным журналом через {{ audit_keep_days }} дней.
        </p>
        {% if is_env_owner %}
          <p style="font-size:13px;color:var(--text-3);margin-bottom:0;">
            Ты владелец из <code>.env</code> — панель не может удалить твою роль.
            Данные удалятся, роль останется до правки файла на сервере.
          </p>
        {% endif %}
        <form method="post" action="{{ url_for('me_delete_all') }}" class="mt-2"
              onsubmit="return confirm('Удалить все данные без возможности восстановить?');">
          <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
          <label style="display:flex;align-items:center;gap:8px;font-size:13px;margin-bottom:10px;">
            <input type="checkbox" name="confirm" value="yes" required>
            Понимаю, что данные не восстановить
          </label>
          <button class="btn btn-sm" type="submit"
                  style="background:#DC2626;color:#fff;border:0;">Удалить всё обо мне</button>
        </form>
      </div>
    </div>
  </div>

</div>
"""


# ── Вспомогательный рендеринг ─────────────────────────────────────────────────

def _render_page(title: str, content_tpl: str, **ctx):
    """Рендерит страницу. Передаёт в шаблон is_admin/is_owner/vk_id/display_name
    из g.user (актуальные роли, без кеша в session)."""
    content_html = render_template_string(content_tpl, **ctx)
    user = getattr(g, "user", None) or {}
    return render_template_string(
        _BASE_TPL,
        page_title=title,
        content=content_html,
        ep=request.endpoint,
        is_admin=bool(user.get("is_admin")),
        is_owner=bool(user.get("is_owner")),
        vk_id=user.get("vk_id"),
        display_name=user.get("name"),
        current_week=_current_week_label(),
    )


# ── Маршруты: авторизация ─────────────────────────────────────────────────────

@app.route("/login", methods=["GET"])
def login():
    # Из VK Mini App пользователь уже опознан по подписанным launch-параметрам
    # (правила VK Mini Apps, п. 1.1.2): показывать ему форму с кодом нельзя.
    if getattr(g, "user", None):
        return redirect(url_for("dashboard"))
    error = request.args.get("error")
    return render_template_string(_LOGIN_TPL, error=error)


# ── Юридические страницы (публичные, нужны для модерации VK) ───────────────────

_LEGAL_TPL = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#C21E41">
  <title>{{ title }} — Электронное расписание</title>
  <link rel="icon" href="/static/logo-mark.svg" type="image/svg+xml">
  <style>
    :root { --accent:#C21E41; --bg:#0f1110; --surface:#161a18; --text:#e8eae6; --muted:#9aa39c; --border:#262b27; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--text);
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
      line-height:1.65; padding:24px 16px; }
    .wrap { max-width:760px; margin:0 auto; }
    .card { background:var(--surface); border:1px solid var(--border);
      border-radius:16px; padding:28px 26px; }
    h1 { font-size:24px; margin:0 0 4px; }
    h2 { font-size:17px; margin:26px 0 8px; color:var(--accent); }
    .upd { color:var(--muted); font-size:13px; margin-bottom:18px; }
    p, li { font-size:15px; color:var(--text); }
    ul { padding-left:20px; }
    a { color:var(--accent); }
    .back { display:inline-block; margin-top:24px; color:var(--muted); text-decoration:none; font-size:14px; }
    .back:hover { color:var(--accent); }
    code { background:#0c0e0d; padding:1px 6px; border-radius:5px; font-size:13px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>{{ title }}</h1>
      <div class="upd">Последнее обновление: {{ updated }}</div>
      {{ body | safe }}
      <a class="back" href="{{ url_for('login') }}">← Назад ко входу</a>
    </div>
  </div>
</body>
</html>"""

_PRIVACY_BODY = """
<p>Сервис «Электронное расписание» (далее — «Сервис», сайт <code>elschedule.ru</code>
и VK Mini App) предоставляет студентам ФФМОИиТ ЧГПУ доступ к расписанию занятий,
напоминаниям и заметкам. Настоящая Политика описывает, какие данные мы
обрабатываем и зачем.</p>

<h2>1. Кто обрабатывает данные</h2>
<p>Сервис ведёт {{ developer_name }} — оператор персональных данных в смысле
152-ФЗ. Связаться можно через <a href="{{ support_url }}">сообщество бота</a>
ВКонтакте; там же принимаются вопросы об обработке данных и запросы на их
удаление.</p>

<h2>3. Какие данные мы собираем</h2>
<ul>
  <li><b>Идентификатор VK (VK ID)</b> — для привязки ваших настроек, заметок и
      напоминаний к аккаунту.</li>
  <li><b>Имя и фамилия</b> — отображаемое имя, получаемое через VK API
      исключительно для показа в интерфейсе.</li>
  <li><b>Учебные настройки</b> — выбранные курс и направление.</li>
  <li><b>Пользовательский контент</b> — заметки, напоминания, дедлайны и
      подписки на рассылку расписания, которые вы создаёте сами.</li>
  <li><b>Технические данные</b> — IP-адрес (для защиты от перебора), время
      входа, журнал действий администраторов.</li>
  <li><b>Файлы cookie</b> — сессионная кука и токен «запомнить меня» для входа
      без повторного ввода кода.</li>
</ul>

<h2>2. Зачем мы используем данные</h2>
<ul>
  <li>Чтобы показывать ваше персональное расписание и напоминания.</li>
  <li>Чтобы сохранять ваши заметки и настройки между сессиями.</li>
  <li>Чтобы отправлять уведомления об изменениях расписания (если вы подписаны).</li>
  <li>Чтобы защищать Сервис от злоупотреблений (ограничение частоты входов).</li>
</ul>

<h2>4. Передача третьим лицам</h2>
<p>Мы <b>не продаём и не передаём</b> ваши персональные данные третьим лицам.
Имя запрашивается у VK API только для отображения. Данные хранятся на нашем
сервере и не используются в рекламных целях.</p>

<h2>5. Хранение и удаление</h2>
<p>Заметки, напоминания, дедлайны и подписки хранятся, пока вы пользуетесь
Сервисом, и удаляются по вашей команде. Служебные записи живут ограниченный
срок: одноразовый код входа — {{ code_ttl_min }} минут (запись о нём удаляется
через {{ codes_days }} сутки), журнал отправленных уведомлений о парах —
{{ notifs_days }} дня, просроченные дедлайны — {{ deadline_days }} дней,
токен «запомнить меня» — {{ remember_days }} дней, журнал действий
администраторов — {{ audit_days }} дней.</p>
<p><b>Удалить всё сразу</b> можно самостоятельно: «Мой профиль» → «Удалить мои
данные». Кнопка стирает заметки, напоминания, дедлайны, подписки, выбранную
группу и сессии входа без возможности восстановления; в журнале безопасности
остаётся только запись о самом факте удаления. Отдельные записи удаляются там же
по одной.</p>

<h2>6. Безопасность</h2>
<p>Соединение защищено HTTPS. Вход выполняется по одноразовому коду из VK-бота.
Применяются защита от перебора (rate-limit), CSRF-токены и политика Content
Security Policy.</p>

<h2>7. Контакты</h2>
<p>По вопросам обработки данных пишите администратору через VK-бота Сервиса.</p>
"""

_TERMS_BODY = """
<p>Используя Сервис «Электронное расписание», вы соглашаетесь с настоящими
условиями.</p>

<h2>1. Назначение</h2>
<p>Сервис предоставляет справочную информацию о расписании занятий, а также
инструменты для личных заметок, напоминаний и дедлайнов. Сервис носит
вспомогательный характер; официальным источником расписания остаётся ваше
учебное заведение.</p>

<h2>2. Учётная запись</h2>
<p>Вход выполняется по одноразовому коду, выдаваемому VK-ботом. Вы отвечаете за
сохранность доступа к своему аккаунту VK.</p>

<h2>3. Допустимое использование</h2>
<ul>
  <li>Не пытайтесь получить несанкционированный доступ к чужим данным или
      административным функциям.</li>
  <li>Не используйте Сервис для рассылки спама или вредоносного контента.</li>
  <li>Не нарушайте работу Сервиса автоматизированными запросами.</li>
</ul>

<h2>4. Ответственность</h2>
<p>Сервис предоставляется «как есть». Администрация прилагает усилия для
точности расписания, но не гарантирует отсутствие ошибок и не несёт
ответственности за решения, принятые на основе данных Сервиса.</p>

<h2>5. Изменения</h2>
<p>Условия могут обновляться. Продолжая пользоваться Сервисом, вы принимаете
актуальную редакцию.</p>
"""


@app.route("/privacy", methods=["GET"])
def privacy():
    # Тело политики — тоже шаблон: сроки хранения берём из конфига, чтобы
    # документ не расходился с тем, что реально делает код.
    from vkbot.models import panel_codes as _pc
    from vkbot.models import panel_remember as _pr

    body = render_template_string(
        _PRIVACY_BODY,
        developer_name=DEVELOPER_NAME,
        code_ttl_min=_pc.CODE_TTL_MIN,
        codes_days=_bot_config.PANEL_CODES_CLEANUP_DAYS,
        notifs_days=_bot_config.SENT_NOTIFS_CLEANUP_DAYS,
        deadline_days=_bot_config.DEADLINE_CLEANUP_DAYS,
        remember_days=_pr.TOKEN_TTL_DAYS,
        audit_days=_bot_config.AUDIT_KEEP_DAYS,
    )
    return render_template_string(
        _LEGAL_TPL, title="Политика конфиденциальности",
        updated="21 августа 2026", body=body,
    )


@app.route("/terms", methods=["GET"])
def terms():
    return render_template_string(
        _LEGAL_TPL, title="Пользовательское соглашение",
        updated="28 мая 2026", body=_TERMS_BODY,
    )


def _client_ip() -> str:
    """IP клиента для rate-limit и аудита.

    Заголовкам X-Real-IP / X-Forwarded-For верим ТОЛЬКО если объявлено, что
    перед приложением стоит прокси (PANEL_TRUSTED_PROXIES > 0). Иначе любой
    запрос мог бы подменить свой IP и обнулить per-IP лимит на /login/code.
    ProxyFix уже подставил доверенное значение в remote_addr.
    """
    return request.remote_addr or ""


@app.route("/login/code", methods=["POST"])
def login_code():
    """Вход по одноразовому коду из бота.

    Выдаём RM-токен в куку. Flask-сессия — как fallback для ПЕРВОГО редиректа,
    пока браузер не вернул куку обратно. На всех последующих запросах
    источник правды — кука vkbot_rm.
    """
    code = _normalize_code(request.form.get("code"))
    ip = _client_ip()
    # Сначала пробуем валидировать код — валидный код ВСЕГДА пускает,
    # даже при global/per-IP lock. Иначе ботнет может надолго забанить
    # реального админа, заполнив счётчик неудач.
    uid = panel_codes.verify(code)
    if not uid:
        # Только теперь проверяем лимиты, чтобы не подсказывать злоумышленнику,
        # что код был правильным до бана.
        if panel_codes.is_globally_locked():
            audit.log(None, "auth.global_lock", ip, "")
            return redirect(url_for("login", error="Система временно заблокирована. Подожди 10 минут."))
        if panel_codes.is_rate_limited(ip):
            audit.log(None, "auth.rate_limited", ip, "")
            return redirect(url_for("login", error="Слишком много неудачных попыток. Подожди 10 минут."))
        panel_codes.record_failure(ip)
        return redirect(url_for("login", error="Неверный или истёкший код."))
    seen_users.touch(uid)
    audit.log(uid, "auth.login", f"id{uid}", "via OTP code")
    remember = (request.form.get("remember", "1") == "1")
    resp = redirect(url_for("dashboard"))
    # Flask-сессия — для первого редиректа (кука ещё не вернётся обратно).
    # permanent только при «запомнить меня»: иначе сессия живёт до закрытия
    # браузера, как пользователь и просил.
    session.permanent = bool(remember)
    session["logged_in"] = True
    session["vk_id"] = uid
    if remember:
        token = panel_remember.issue(uid)
        _set_remember_cookie(resp, token)
    return resp


@app.route("/logout", methods=["POST"])
def logout():
    """Отзывает RM-токен этого устройства, чистит куку и Flask-сессию."""
    token = request.cookies.get(_REMEMBER_COOKIE)
    if token:
        try:
            panel_remember.revoke(token)
        except Exception:
            logging.exception("Не удалось отозвать remember-token при logout")
    session.clear()
    resp = redirect(url_for("login"))
    _clear_remember_cookie(resp)
    return resp


@app.route("/logout/all", methods=["POST"])
@login_required
def logout_all():
    """Отзывает ВСЕ RM-токены пользователя — выход со всех устройств."""
    uid = _current_vk_id()
    if uid:
        try:
            panel_remember.revoke_all(uid)
            audit.log(uid, "auth.logout_all", f"id{uid}", "")
        except Exception:
            pass
    session.clear()
    resp = redirect(url_for("login"))
    _clear_remember_cookie(resp)
    return resp


# ── Маршруты: страницы ────────────────────────────────────────────────────────

def _today_tomorrow_preview(course: int | None = None, direction: str | None = None) -> dict:
    """Сводка на дашборд: расписание на сегодня и завтра.

    Если переданы course/direction — отфильтровано по подписке пользователя.
    """
    from vkbot.config import now_msk
    from vkbot.schedule.week import week_type_for
    today_d = now_msk().date()
    tomorrow_d = today_d + _dt.timedelta(days=1)
    out: dict = {}
    extra_conds = ""
    extra_params: list = []
    if course is not None:
        extra_conds += " AND course = ?"
        extra_params.append(int(course))
    if direction:
        extra_conds += " AND direction = ?"
        extra_params.append(direction)
    conn = None
    try:
        conn = _sched_conn(vk=True)
        for key, d in (("today", today_d), ("tomorrow", tomorrow_d)):
            wd = d.weekday()
            if wd == 6:
                out[key] = {"day": "Воскресенье", "week": "", "rows": [], "is_sunday": True, "date": d.isoformat()}
                continue
            day_name = _WEEKDAY_RU[wd]
            wk = week_type_for(d)
            rows = conn.execute(
                "SELECT course, direction, day, time, subject, teacher, room, "
                "week, class_type, date_range FROM schedule "
                "WHERE day = ? AND (week IS NULL OR week = '' OR week = ?)"
                + extra_conds
                + f" ORDER BY course, direction, {_TIME_ORDER_SQL}",
                (day_name, wk, *extra_params),
            ).fetchall()
            out[key] = {"day": day_name, "week": wk, "rows": rows, "is_sunday": False, "date": d.isoformat()}
    except Exception:
        out = {
            "today": {"day": "", "week": "", "rows": [], "is_sunday": False, "date": ""},
            "tomorrow": {"day": "", "week": "", "rows": [], "is_sunday": False, "date": ""},
        }
    finally:
        if conn is not None:
            conn.close()
    return out


def _get_pref(uid: int) -> tuple[int, str] | None:
    """Возвращает (course, direction) если у юзера есть подписка, иначе None."""
    if not uid:
        return None
    try:
        with _notes_conn() as conn:
            row = conn.execute(
                "SELECT course, direction FROM user_prefs WHERE user_id=?",
                (uid,),
            ).fetchone()
        if row and row[0] and row[1]:
            return (int(row[0]), str(row[1]))
    except Exception:
        pass
    return None


def _set_pref(uid: int, course: int, direction: str) -> None:
    with _notes_conn() as conn:
        conn.execute(
            "INSERT INTO user_prefs (user_id, course, direction) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET course=excluded.course, direction=excluded.direction",
            (uid, int(course), direction),
        )
        conn.commit()


def _clear_pref(uid: int) -> None:
    with _notes_conn() as conn:
        conn.execute("DELETE FROM user_prefs WHERE user_id=?", (uid,))
        conn.commit()


def _all_courses_directions() -> tuple[list[int], dict[int, list[str]]]:
    """Список курсов и {course: [directions]} — для селекторов подписки."""
    courses: list[int] = []
    by_course: dict[int, list[str]] = {}
    try:
        with _sched_conn(vk=True) as conn:
            courses = [r[0] for r in conn.execute(
                "SELECT DISTINCT course FROM schedule ORDER BY course"
            ).fetchall()]
            for c in courses:
                by_course[c] = [r[0] for r in conn.execute(
                    "SELECT DISTINCT direction FROM schedule "
                    "WHERE course=? AND direction IS NOT NULL AND direction != '' "
                    "ORDER BY direction",
                    (c,),
                ).fetchall()]
    except Exception:
        pass
    return courses, by_course


def _current_week_label() -> str:
    """Возвращает 'чёт' или 'нечет' для текущей недели (МСК)."""
    try:
        from vkbot.config import now_msk
        from vkbot.schedule.week import week_type_for
        return week_type_for(now_msk().date())
    except Exception:
        return ""


@app.route("/")
@login_required
def dashboard():
    """Доступен всем: админам — полный, юзерам — расписание (с подпиской если есть)."""
    uid = _current_vk_id()
    pref = _get_pref(uid) if uid else None
    if pref:
        preview = _today_tomorrow_preview(course=pref[0], direction=pref[1])
    else:
        preview = _today_tomorrow_preview()
    courses, dirs_by_course = _all_courses_directions()
    return _render_page(
        "Дашборд", _DASHBOARD_CONTENT,
        stats=_get_stats() if g.user["is_admin"] else None,
        preview=preview,
        pref=pref,
        all_courses=courses,
        dirs_by_course=dirs_by_course,
        current_week=_current_week_label(),
    )


@app.route("/me/subscribe", methods=["POST"])
@login_required
def me_subscribe():
    uid = _current_vk_id()
    course = (request.form.get("course") or "").strip()
    direction = (request.form.get("direction") or "").strip()
    next_url = _safe_next(request.form.get("next"), url_for("dashboard"))
    if uid and course.isdigit() and direction:
        try:
            # Панель хранит выбранную группу в user_prefs, а напоминания бот
            # шлёт по таблице subscriptions. Раньше панель писала только первое
            # и обещала уведомления, которых не было. Держим обе стороны вместе.
            previous = _get_pref(uid)
            _set_pref(uid, int(course), direction)
            if previous and (previous[0], previous[1].lower()) != (int(course), direction.lower()):
                subscriptions.delete_by_group(uid, previous[0], previous[1])
            if not subscriptions.exists(uid, int(course), direction):
                subscriptions.add(uid, int(course), direction)
        except Exception:
            logging.exception("me_subscribe failed uid=%s", uid)
    return redirect(next_url)


@app.route("/me/unsubscribe", methods=["POST"])
@login_required
def me_unsubscribe():
    uid = _current_vk_id()
    next_url = _safe_next(request.form.get("next"), url_for("dashboard"))
    if uid:
        try:
            previous = _get_pref(uid)
            _clear_pref(uid)
            if previous:
                subscriptions.delete_by_group(uid, previous[0], previous[1])
        except Exception:
            logging.exception("me_unsubscribe failed uid=%s", uid)
    return redirect(next_url)


# Хранилище отложенных загрузок: токен → (excel_path, original_filename, created_ts).
# Очищаем устаревшие записи через _gc_pending_uploads() при каждом обращении,
# чтобы tmp-файлы не накапливались, если админ закрыл вкладку.
_PENDING_UPLOADS: dict[str, tuple[str, str, float]] = {}
_PENDING_TTL_SEC = 30 * 60  # 30 минут


def _gc_pending_uploads() -> int:
    """Удаляет просроченные загрузки + их tmp-файлы. Возвращает кол-во удалённых."""
    import time as _time
    now = _time.time()
    expired = [
        t for t, item in _PENDING_UPLOADS.items()
        if now - item[2] > _PENDING_TTL_SEC
    ]
    for t in expired:
        _drop_pending(t)
    return len(expired)


def _drop_pending(token: str) -> None:
    item = _PENDING_UPLOADS.pop(token, None)
    if item:
        try:
            os.unlink(item[0])
        except Exception:
            pass


def _add_pending(token: str, tmp_path: str, original: str) -> None:
    """Регистрирует отложенную загрузку + триггерит GC старых."""
    import time as _time
    _PENDING_UPLOADS[token] = (tmp_path, original, _time.time())
    _gc_pending_uploads()


import atexit as _atexit


@_atexit.register
def _cleanup_pending_on_exit() -> None:
    """При остановке сервиса — удалить все tmp-файлы pending загрузок."""
    for token in list(_PENDING_UPLOADS.keys()):
        _drop_pending(token)


def _subscriber_count() -> int:
    try:
        return len(notifier.subscribed_uids())
    except Exception:
        return 0


_BROADCAST_TEXT = (
    "📅 Расписание обновлено!\n"
    "Открой бота и проверь свой курс — могли поменяться пары, аудитории или преподаватели."
)



# Excel upload validation: ext + magic bytes (защита от подмены .xlsx произвольным файлом).
_XLSX_MAGIC = bytes.fromhex("504b0304")          # zip-контейнер (xlsx/xlsm)
_XLS_MAGIC  = bytes.fromhex("d0cf11e0")  # OLE2-контейнер (старый xls)
_ALLOWED_EXTS = {".xlsx", ".xls", ".xlsm"}

def _validate_excel_upload(f) -> str:
    """Возвращает '' если ок, иначе текст ошибки. Проверяет:
    1) расширение в whitelist; 2) magic bytes; 3) для xlsx — наличие
    [Content_Types].xml внутри zip (отсекает произвольные zip-payload'ы)."""
    if not f or not f.filename:
        return "Файл не выбран."
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in _ALLOWED_EXTS:
        return f"Неподдерживаемое расширение {ext or '?'}. Нужен .xlsx/.xls."
    head = f.stream.read(8)
    f.stream.seek(0)
    if head.startswith(_XLS_MAGIC):
        return ""  # старый OLE2 формат, magic совпал — ок
    if not head.startswith(_XLSX_MAGIC):
        return "Файл не похож на Excel (неверный заголовок)."
    # Для zip-формата проверяем, что это реально Office Open XML, а не
    # произвольный zip-архив с .xlsx-расширением.
    import zipfile
    import io
    try:
        data = f.stream.read()
        f.stream.seek(0)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            if "[Content_Types].xml" not in names:
                return "Файл — zip-архив, но не Excel (нет [Content_Types].xml)."
            # Поверхностный sanity-check на размер — отсекает zip-bombs
            if any(zi.file_size > 50_000_000 for zi in z.infolist()):
                return "Подозрительно крупная запись внутри архива."
    except zipfile.BadZipFile:
        return "Файл не является валидным zip/xlsx."
    except Exception:
        return "Не удалось проверить структуру файла."
    return ""


@app.route("/upload", methods=["GET", "POST"])
@admin_required
def upload_page():
    """Шаг 1: загружаем Excel, считаем превью, показываем дифф. Шаг 2: /upload/commit."""
    preview = None
    error = None
    pending_token = None
    if request.method == "POST":
        f = request.files.get("excel_file")
        v_err = _validate_excel_upload(f)
        if v_err:
            error = v_err
            f = None
        else:
            suffix = os.path.splitext(f.filename)[1] or ".xlsx"
            try:
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                    tmp_path = tmp.name
                    f.save(tmp_path)
                preview = schedule_loader.preview(tmp_path)
                pending_token = secrets.token_urlsafe(16)
                _add_pending(pending_token, tmp_path, f.filename)
            except Exception:
                logging.exception("upload preview failed")
                error = "Не удалось обработать файл (см. логи сервиса)."
    return _render_page(
        "Загрузить расписание",
        _UPLOAD_CONTENT,
        preview=preview,
        error=error,
        pending_token=pending_token,
        versions=schedule_loader.list_versions()[:10],
        subscriber_count=_subscriber_count(),
    )


def _sync_legacy_schedule_db(excel_path: str, context: str) -> None:
    """Переливает то же расписание в s.db — базу легаси Telegram-бота.

    Ошибки только логируем: VK-бот и панель читают свою базу, расхождение с
    легаси-ботом не повод валить загрузку. Вызывать нужно из ВСЕХ путей, где
    расписание меняется (панель и API, загрузка и откат), иначе базы разъедутся.
    """
    try:
        from import_excel import import_schedule

        import_schedule(excel_path, SCHEDULE_DB_S)
    except Exception:
        logging.exception("Не удалось обновить s.db (%s)", context)


@app.route("/upload/commit", methods=["POST"])
@admin_required
def upload_commit():
    token = request.form.get("token", "")
    notify = request.form.get("notify") == "1"
    item = _PENDING_UPLOADS.get(token)
    if not item:
        return redirect(url_for("upload_page"))
    excel_path, original, _ts = item
    who = f"admin:{_current_vk_id() or 'password'}"
    try:
        result = schedule_loader.commit(excel_path, who, original)
        _ics_cache_clear()
        audit.log(
            _current_vk_id(), "schedule.upload",
            original or "—",
            f"rows={result.get('row_count', '?')}, notify={'yes' if notify else 'no'}",
        )
        _sync_legacy_schedule_db(excel_path, "загрузка из панели")
        # Рассылка подписчикам (опционально) — в фоне, чтобы не держать запрос.
        if notify:
            uids = notifier.subscribed_uids()
            if _start_broadcast(_BROADCAST_TEXT, uids, _current_vk_id()):
                result["broadcast"] = {"started": True, "total": len(uids)}
            else:
                result["broadcast"] = {"started": False, "total": len(uids)}
    except Exception:
        logging.exception("schedule commit failed")
        return _render_page(
            "Загрузить расписание",
            _UPLOAD_CONTENT,
            preview=None,
            error="Не удалось применить расписание — подробности в логах сервиса.",
            pending_token=None,
            versions=schedule_loader.list_versions()[:10],
            subscriber_count=_subscriber_count(),
        )
    finally:
        _drop_pending(token)
    # Подсчёт конфликтов в свежезалитом расписании — показываем сразу на странице
    conflicts = _find_conflicts()
    return _render_page(
        "Загрузить расписание",
        _UPLOAD_CONTENT,
        preview=None,
        error=None,
        pending_token=None,
        commit_result=result,
        conflicts_count=len(conflicts["teachers"]) + len(conflicts["rooms"]),
        conflict_teachers=len(conflicts["teachers"]),
        conflict_rooms=len(conflicts["rooms"]),
        versions=schedule_loader.list_versions()[:10],
        subscriber_count=_subscriber_count(),
    )


# ── Скачивание Excel ──────────────────────────────────────────────────────────

@app.route("/download/current")
@admin_required
def download_current():
    """Отдаёт самый свежий сохранённый Excel из истории версий."""
    versions = schedule_loader.list_versions()
    if not versions:
        return redirect(url_for("upload_page"))
    path = Path(versions[0]["file_path"])
    if not path.exists():
        return redirect(url_for("upload_page"))
    return send_file(
        str(path),
        as_attachment=True,
        download_name=versions[0]["original_filename"] or path.name,
    )


@app.route("/download/version/<int:version_id>")
@admin_required
def download_version(version_id: int):
    for v in schedule_loader.list_versions():
        if v["id"] == version_id:
            path = Path(v["file_path"])
            if not path.exists():
                return ("File missing on disk", 404)
            return send_file(
                str(path),
                as_attachment=True,
                download_name=v["original_filename"] or path.name,
            )
    return ("Version not found", 404)


@app.route("/upload/cancel", methods=["POST"])
@admin_required
def upload_cancel():
    _drop_pending(request.form.get("token", ""))
    return redirect(url_for("upload_page"))


@app.route("/upload/rollback/<int:version_id>", methods=["POST"])
@admin_required
def upload_rollback(version_id: int):
    try:
        result = schedule_loader.rollback(
            version_id, uploaded_by=f"admin:{_current_vk_id() or 'password'}"
        )
        _ics_cache_clear()
        audit.log(_current_vk_id(), "schedule.rollback", f"version={version_id}", "")
        # rollback() возвращает свежую копию того же Excel — из неё и обновляем
        # s.db (легаси Telegram-бот), иначе базы разъедутся.
        _sync_legacy_schedule_db(result["saved_path"], "откат из панели")
    except Exception:
        logging.exception("schedule rollback failed")
        return _render_page(
            "Загрузить расписание",
            _UPLOAD_CONTENT,
            preview=None,
            error="Не удалось откатить расписание — подробности в логах сервиса.",
            pending_token=None,
            versions=schedule_loader.list_versions()[:10],
            subscriber_count=_subscriber_count(),
        )
    return redirect(url_for("upload_page"))


# ── REST API ──────────────────────────────────────────────────────────────────


def _check_api_token() -> bool:
    if not UPLOAD_API_TOKEN:
        return False
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    provided = auth[7:]
    return hmac.compare_digest(provided, UPLOAD_API_TOKEN)


@app.route("/api/schedule/upload", methods=["POST"])
@csrf.exempt
def api_schedule_upload():
    """Загрузить Excel, получить превью (но НЕ применять).

    Заголовок: Authorization: Bearer <UPLOAD_API_TOKEN>
    Тело: multipart/form-data с полем `file`
    Ответ: {"token": "...", "preview": {...}}
    """
    if not _check_api_token():
        return jsonify({"error": "unauthorized"}), 401
    f = request.files.get("file") or request.files.get("excel_file")
    v_err = _validate_excel_upload(f)
    if v_err:
        return jsonify({"error": v_err}), 400
    suffix = os.path.splitext(f.filename)[1] or ".xlsx"
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
            f.save(tmp_path)
        pv = schedule_loader.preview(tmp_path)
        token = secrets.token_urlsafe(16)
        _add_pending(token, tmp_path, f.filename)
        return jsonify({"token": token, "preview": pv.__dict__})
    except Exception:
        logging.exception("api upload failed")
        return jsonify({"error": "internal error"}), 500


@app.route("/api/schedule/commit", methods=["POST"])
@csrf.exempt
def api_schedule_commit():
    if not _check_api_token():
        return jsonify({"error": "unauthorized"}), 401
    token = (request.json or {}).get("token") if request.is_json else request.form.get("token")
    item = _PENDING_UPLOADS.get(token or "")
    if not item:
        return jsonify({"error": "unknown or expired token"}), 404
    excel_path, original, _ts = item
    try:
        result = schedule_loader.commit(excel_path, "api", original)
        _ics_cache_clear()
        # Аудит нужен и здесь: иначе загрузка через API — единственный способ
        # подменить расписание, не оставив следа в /admin/audit.
        audit.log(
            None, "schedule.upload", original or "—",
            f"rows={result.get('row_count', '?')}, via=api",
        )
        _sync_legacy_schedule_db(excel_path, "загрузка через API")
        return jsonify(result)
    except Exception:
        # Текст исключения наружу не отдаём: в нём бывают пути и SQL.
        logging.exception("api schedule commit failed")
        return jsonify({"error": "internal error"}), 500
    finally:
        _drop_pending(token or "")


@app.route("/api/schedule/versions", methods=["GET"])
def api_schedule_versions():
    if not _check_api_token():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(schedule_loader.list_versions())


@app.route("/api/schedule/rollback/<int:version_id>", methods=["POST"])
@csrf.exempt
def api_schedule_rollback(version_id: int):
    if not _check_api_token():
        return jsonify({"error": "unauthorized"}), 401
    # Проверяем существование версии отдельно: если ловить ValueError вокруг
    # всего rollback(), под 404 «version not found» уедет и ошибка разбора
    # Excel внутри commit() — да ещё и без записи в лог.
    version = next(
        (v for v in schedule_loader.list_versions() if v["id"] == version_id), None
    )
    if version is None or not Path(version["file_path"]).exists():
        return jsonify({"error": "version not found"}), 404
    try:
        result = schedule_loader.rollback(version_id, uploaded_by="api")
    except Exception:
        logging.exception("api schedule rollback failed")
        return jsonify({"error": "internal error"}), 500
    _ics_cache_clear()
    audit.log(None, "schedule.rollback", f"version={version_id}", "via=api")
    _sync_legacy_schedule_db(result["saved_path"], "откат через API")
    return jsonify(result)


_WEEKDAY_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def _today_tomorrow_filter(quick: str) -> tuple[str, str]:
    """Возвращает (day_filter, week_filter) для quick=today|tomorrow.

    week — 'чёт' или 'нечет' для соответствующей даты.
    Если воскресенье — возвращаем '' (выходной).
    """
    from vkbot.config import now_msk
    from vkbot.schedule.week import week_type_for
    today = now_msk().date()
    if quick == "tomorrow":
        target = today + _dt.timedelta(days=1)
    else:
        target = today
    weekday_idx = target.weekday()
    if weekday_idx == 6:  # воскресенье
        return ("__none__", "")
    return (_WEEKDAY_RU[weekday_idx], week_type_for(target))


# SQL-фрагмент для правильной сортировки дней (Пн → Сб) — стабильно для SQLite
# Сортировать по колонке `time` как по тексту нельзя: в базах вуза время
# записано тремя способами — «8:00-9:30», «8.15 - 9.45» и «1 пара 08:00-09:30».
# CAST в SQLite берёт числовой префикс и останавливается на первом нечисловом
# символе: 8, 8 и 1 соответственно. Выражение через INSTR(time,' ') здесь не
# годится — на формате без пробела оно давало 0 для всех строк, и порядок
# сваливался обратно в текстовый (11:20, 13:20, 8:00, 9:40).
# То же выражение использует бот (vkbot/schedule/repo.py::get_day).
_TIME_ORDER_SQL = "CAST(time AS INTEGER), time"

# Типы занятий в исходниках пишут по-разному: «лк», «лек», «лекция». Плашка в
# интерфейсе узкая, и «ЛЕКЦИЯ» ломала ряд карточек — приводим к короткой форме
# на отображении, не трогая данные (в старых строках лежит длинный вариант).
_TYPE_SHORT = {
    "лекция": "лк", "лекц": "лк", "лек": "лк",
    "практика": "пр", "практ": "пр",
    "лабораторная": "лб", "лаб": "лб",
    "семинар": "сем", "курсовая": "кур",
    "экзамен": "экз", "зачёт": "зач", "зачет": "зач",
}


def _type_short(value: str | None) -> str:
    v = (value or "").strip().lower()
    return _TYPE_SHORT.get(v, v)


app.jinja_env.globals["type_short"] = _type_short
app.jinja_env.globals["support_url"] = SUPPORT_URL
# Сколько минут до пары приходит уведомление — в текстах про подписку
# должно стоять то же число, что реально использует воркер.
app.jinja_env.globals["notify_before_min"] = _bot_config.CLASS_NOTIFY_BEFORE_MIN

_DAY_ORDER_SQL = (
    "CASE day "
    "WHEN 'Понедельник' THEN 1 WHEN 'Вторник' THEN 2 WHEN 'Среда' THEN 3 "
    "WHEN 'Четверг' THEN 4 WHEN 'Пятница' THEN 5 WHEN 'Суббота' THEN 6 "
    "WHEN 'Воскресенье' THEN 7 ELSE 9 END"
)


@app.route("/schedule")
@login_required
def schedule_page():
    course_filter = request.args.get("course", "").strip()
    day_filter = request.args.get("day", "").strip()
    week_filter = request.args.get("week", "").strip()
    direction_filter = request.args.get("direction", "").strip()
    teacher_filter = request.args.get("teacher", "").strip()
    type_filter = request.args.get("type", "").strip()
    room_filter = request.args.get("room", "").strip()
    quick = request.args.get("quick", "").strip()
    q = request.args.get("q", "").strip()

    # Без фильтров страница отдавала всё расписание разом: почти мегабайт HTML
    # и 28 000 пикселей высоты. Открываем на сегодняшнем дне — «Вся неделя»
    # рядом, явной ссылкой quick=all.
    if not any((course_filter, day_filter, week_filter, direction_filter,
                teacher_filter, type_filter, room_filter, quick, q)):
        quick = "today"

    # Quick-фильтры (Сегодня/Завтра) — переопределяют day/week
    if quick in ("today", "tomorrow"):
        day_filter, week_filter = _today_tomorrow_filter(quick)

    courses, rows, total = [], [], 0
    directions: list[str] = []
    teachers: list[str] = []
    types: list[str] = []
    rooms: list[str] = []
    conn = None
    try:
        conn = _sched_conn(vk=True)
        courses = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT course FROM schedule ORDER BY course"
            ).fetchall()
        ]
        # Направления зависят от выбранного курса (если есть)
        if course_filter:
            directions = [
                r[0] for r in conn.execute(
                    "SELECT DISTINCT direction FROM schedule WHERE course=? "
                    "AND direction IS NOT NULL AND direction != '' "
                    "ORDER BY direction",
                    (int(course_filter),),
                ).fetchall()
            ]
        else:
            directions = [
                r[0] for r in conn.execute(
                    "SELECT DISTINCT direction FROM schedule "
                    "WHERE direction IS NOT NULL AND direction != '' "
                    "ORDER BY direction"
                ).fetchall()
            ]
        teachers = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT teacher FROM schedule "
                "WHERE teacher IS NOT NULL AND teacher != '' "
                "ORDER BY teacher"
            ).fetchall()
        ]
        types = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT class_type FROM schedule "
                "WHERE class_type IS NOT NULL AND class_type != '' "
                "ORDER BY class_type"
            ).fetchall()
        ]
        rooms = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT room FROM schedule "
                "WHERE room IS NOT NULL AND room != '' "
                "ORDER BY room"
            ).fetchall()
        ]
        total = conn.execute("SELECT COUNT(*) FROM schedule").fetchone()[0]
        sql = (
            "SELECT course, direction, day, time, subject, teacher, room, "
            "week, class_type, date_range FROM schedule"
        )
        params: list = []
        conds: list[str] = []
        if course_filter:
            conds.append("course = ?")
            params.append(int(course_filter))
        if day_filter == "__none__":
            conds.append("1 = 0")
        elif day_filter:
            conds.append("day = ?")
            params.append(day_filter)
        if week_filter:
            conds.append("(week IS NULL OR week = '' OR week = ?)")
            params.append(week_filter)
        if direction_filter:
            conds.append("direction = ?")
            params.append(direction_filter)
        if teacher_filter:
            conds.append("teacher = ?")
            params.append(teacher_filter)
        if type_filter:
            conds.append("LOWER(TRIM(COALESCE(class_type, ''))) = LOWER(?)")
            params.append(type_filter)
        if room_filter:
            conds.append("room = ?")
            params.append(room_filter)
        if q:
            conds.append("(subject LIKE ? OR direction LIKE ? OR teacher LIKE ? OR room LIKE ?)")
            params += [f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"]
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        # Стабильная сортировка: курс → направление → день (Пн-Сб) → время
        sql += f" ORDER BY course, direction, {_DAY_ORDER_SQL}, {_TIME_ORDER_SQL}"
        rows = conn.execute(sql, params).fetchall()
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()
    return _render_page(
        "Расписание", _SCHEDULE_CONTENT,
        courses=courses, rows=rows, total=total,
        course_filter=course_filter, day_filter=day_filter, q=q,
        week_filter=week_filter, quick=quick,
        direction_filter=direction_filter, teacher_filter=teacher_filter,
        type_filter=type_filter, room_filter=room_filter,
        directions=directions, teachers=teachers, types=types, rooms=rooms,
    )


@app.route("/me")
@login_required
def me_page():
    vk_id = _current_vk_id()
    if not vk_id:
        # Fallback на первый owner VK ID (на практике сюда не зайдём — пароля больше нет)
        vk_id = next(iter(OWNER_VK_IDS), 0)
    data = _get_user_data(vk_id) if vk_id else {}
    return _render_page(
        "Мой профиль", _ME_CONTENT,
        vk_id=vk_id,
        notes=data.get("notes", []),
        reminders=data.get("reminders", []),
        reminders_past=data.get("reminders_past", []),
        deadlines=data.get("deadlines", []),
        subscriptions=data.get("subscriptions", []),
        pref=data.get("pref"),
        data_counts=user_data.count_all(vk_id) if vk_id else {},
        data_labels=user_data.LABELS,
        audit_keep_days=_bot_config.AUDIT_KEEP_DAYS,
        is_env_owner=vk_id in OWNER_VK_IDS,
    )


@app.route("/me/delete-all", methods=["POST"])
@login_required
def me_delete_all():
    """Удаляет все данные пользователя по его собственному запросу.

    Политика конфиденциальности обещает такое удаление; до сих пор оно шло
    перепиской с администратором. Сессию гасим здесь же: токены «запомнить
    меня» удалены, продолжать сессию было бы нечестно.
    """
    uid = _current_vk_id()
    if not uid:
        return redirect(url_for("login"))
    if (request.form.get("confirm") or "") != "yes":
        return redirect(_with_flash(url_for("me_page"), "Нужно подтвердить удаление", "danger"))

    try:
        removed = user_data.purge(uid)
    except Exception:
        logging.exception("me: не удалось удалить данные пользователя")
        return redirect(_with_flash(
            url_for("me_page"), "Не удалось удалить данные, попробуй ещё раз", "danger"))

    audit.log(uid, "me.delete_all", f"id={uid}",
              ", ".join(f"{k}={v}" for k, v in removed.items()) or "нечего было удалять")
    session.clear()
    resp = redirect(_with_flash(url_for("login"),
                                "Все твои данные удалены. Бот про тебя забыл.", "success"))
    resp.delete_cookie(_REMEMBER_COOKIE)
    return resp


_ME_DELETABLE_TABLES = {
    "note": "notes",
    "reminder": "reminders",
    "deadline": "deadlines",
    "subscription": "subscriptions",
}


@app.route("/me/delete", methods=["POST"])
@login_required
def me_delete():
    """Удаляет одну запись пользователя. Проверка владельца обязательна."""
    uid = _current_vk_id()
    kind = (request.form.get("kind") or "").strip()
    try:
        item_id = int(request.form.get("id") or 0)
    except (TypeError, ValueError):
        item_id = 0
    table = _ME_DELETABLE_TABLES.get(kind)
    if not uid or not item_id or not table:
        return redirect(url_for("me_page"))
    try:
        with _notes_conn() as conn:
            # Если снимают напоминания по выбранной группе, надо убрать и саму
            # отметку группы: иначе дашборд продолжит обещать уведомления,
            # которых уже нет.
            clears_pref = False
            if kind == "subscription":
                row = conn.execute(
                    "SELECT course, direction FROM subscriptions WHERE id=? AND user_id=?",
                    (item_id, uid),
                ).fetchone()
                pref = _get_pref(uid)
                clears_pref = bool(
                    row and pref and (pref[0], pref[1].lower()) == (row[0], (row[1] or "").lower())
                )
            cur = conn.execute(
                f"DELETE FROM {table} WHERE id=? AND user_id=?",
                (item_id, uid),
            )
            conn.commit()
            audit.log(uid, f"me.delete_{kind}", f"id={item_id}", f"rows={cur.rowcount}")
        if clears_pref:
            _clear_pref(uid)
    except Exception:
        logging.exception("me_delete failed uid=%s kind=%s", uid, kind)
    return redirect(url_for("me_page"))


# ── Пользователи бота: агрегация и страницы ───────────────────────────────────


def _aggregate_users(search: str = "") -> list[dict]:
    """Собирает всех пользователей бота из всех таблиц + статистика."""
    users: dict[int, dict] = {}
    conn = None
    try:
        conn = _notes_conn()
        # notes
        for uid, n, last in conn.execute(
            "SELECT user_id, COUNT(*), MAX(timestamp) FROM notes GROUP BY user_id"
        ).fetchall():
            u = users.setdefault(uid, {"vk_id": uid, "last_seen": "", "counts": {}})
            u["counts"]["notes"] = n
            u["last_seen"] = max(u["last_seen"], last or "")
        # reminders
        for uid, n, last in conn.execute(
            "SELECT user_id, COUNT(*), MAX(remind_at) FROM reminders GROUP BY user_id"
        ).fetchall():
            u = users.setdefault(uid, {"vk_id": uid, "last_seen": "", "counts": {}})
            u["counts"]["reminders"] = n
            u["last_seen"] = max(u["last_seen"], last or "")
        # deadlines
        try:
            for uid, n, last in conn.execute(
                "SELECT user_id, COUNT(*), MAX(deadline_at) FROM deadlines GROUP BY user_id"
            ).fetchall():
                u = users.setdefault(uid, {"vk_id": uid, "last_seen": "", "counts": {}})
                u["counts"]["deadlines"] = n
                u["last_seen"] = max(u["last_seen"], last or "")
        except Exception:
            pass
        # subscriptions
        for uid, n in conn.execute(
            "SELECT user_id, COUNT(*) FROM subscriptions "
            "WHERE COALESCE(disabled,0)=0 GROUP BY user_id"
        ).fetchall():
            u = users.setdefault(uid, {"vk_id": uid, "last_seen": "", "counts": {}})
            u["counts"]["subscriptions"] = n
        # user_prefs (только наличие)
        for (uid,) in conn.execute("SELECT user_id FROM user_prefs").fetchall():
            users.setdefault(uid, {"vk_id": uid, "last_seen": "", "counts": {}})
        # seen_users — все, кого видел бот или панель (даже без данных в других таблицах)
        try:
            for uid, first_seen, last_seen in conn.execute(
                "SELECT vk_id, first_seen, last_seen FROM seen_users"
            ).fetchall():
                u = users.setdefault(uid, {"vk_id": uid, "last_seen": "", "counts": {}})
                u["last_seen"] = max(u.get("last_seen") or "", last_seen or "")
                u.setdefault("first_seen", first_seen)
        except Exception:
            pass
    except Exception:
        pass
    finally:
        if conn is not None:
            conn.close()

    if not users:
        return []

    # Резолвим имена пачкой
    names = vk_names.resolve(users.keys())
    # admin-role lookup в одном SQL
    admin_ids: set[int] = set()
    try:
        admin_ids = {u["vk_id"] for u in panel_users.list_all()}
    except Exception:
        pass
    owner_ids = _all_owner_ids()

    out: list[dict] = []
    s = search.strip().lower()
    for uid, u in users.items():
        name = names.get(uid, f"id{uid}")
        if s:
            # ищем по ID или по имени
            if s not in str(uid) and s not in name.lower():
                continue
        u["name"] = name
        if uid in owner_ids:
            u["role"] = "owner"
        elif uid in admin_ids:
            u["role"] = "admin"
        else:
            u["role"] = "user"
        out.append(u)
    # Сортируем по last_seen DESC
    out.sort(key=lambda x: x.get("last_seen") or "", reverse=True)
    return out


_USERS_CONTENT = """
{% set self_url = url_for('users_page', q=q) if q else url_for('users_page') %}
<div class="page-title">
  <h1>👥 Пользователи бота</h1>
  <form method="get" class="d-flex gap-2" style="flex: 1; max-width: 360px;">
    <input type="text" name="q" value="{{ q }}" class="form-control form-control-sm"
           placeholder="Поиск по ID или имени">
    <button class="btn btn-sm btn-outline-primary">Найти</button>
  </form>
</div>

{% if flash %}
  <div class="alert alert-{{ flash_kind or 'success' }} py-2 small">{{ flash }}</div>
{% endif %}

{% if is_owner %}
<div class="alert alert-info py-2 small" style="display:flex;align-items:center;gap:10px;">
  <span style="font-size:18px;">🛡️</span>
  <span>Кнопки <strong>«Дать админа»</strong> / <strong>«Убрать»</strong> работают прямо отсюда — менять в <code>/admin/admins</code> не обязательно.</span>
</div>
{% endif %}

<div class="card">
  <div class="card-body p-0">
    {% if users %}
    <div class="table-responsive">
      <table class="table table-hover align-middle mb-0">
        <thead class="table-light">
          <tr>
            <th>Имя</th><th class="d-none d-md-table-cell">VK ID</th>
            <th class="text-center">Заметок</th>
            <th class="text-center d-none d-sm-table-cell">Напомин.</th>
            <th class="text-center d-none d-sm-table-cell">Дедл.</th>
            <th class="text-center">Подписок</th>
            <th>Роль</th>
            {% if is_owner %}<th class="text-end">Действия</th>{% endif %}
          </tr>
        </thead>
        <tbody>
          {% for u in users %}
          <tr>
            <td>
              <a href="https://vk.com/id{{ u.vk_id }}" target="_blank"
                 class="text-decoration-none">{{ u.name }}</a>
              <div class="text-muted small d-md-none">id{{ u.vk_id }}</div>
            </td>
            <td class="d-none d-md-table-cell text-muted small">{{ u.vk_id }}</td>
            <td class="text-center">{{ u.counts.notes or 0 }}</td>
            <td class="text-center d-none d-sm-table-cell">{{ u.counts.reminders or 0 }}</td>
            <td class="text-center d-none d-sm-table-cell">{{ u.counts.deadlines or 0 }}</td>
            <td class="text-center">{{ u.counts.subscriptions or 0 }}</td>
            <td>
              {% if u.role == 'owner' %}
                <span class="role-pill owner">owner</span>
              {% elif u.role == 'admin' %}
                <span class="badge bg-success">admin</span>
              {% else %}
                <span class="badge bg-secondary">user</span>
              {% endif %}
            </td>
            {% if is_owner %}
            <td class="text-end" style="white-space:nowrap;">
              {% if u.role == 'user' %}
                <form method="post" action="{{ url_for('admin_grant') }}" class="d-inline"
                      onsubmit="return confirm('Дать админа {{ u.name }} (id{{ u.vk_id }})?');">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                  <input type="hidden" name="vk_id" value="{{ u.vk_id }}">
                  <input type="hidden" name="name" value="{{ u.name }}">
                  <input type="hidden" name="next" value="{{ self_url }}">
                  <button class="btn btn-sm btn-success">🛡️ Дать админа</button>
                </form>
              {% elif u.role == 'admin' %}
                <form method="post" action="{{ url_for('admin_revoke') }}" class="d-inline"
                      onsubmit="return confirm('Убрать админа у {{ u.name }}?');">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                  <input type="hidden" name="vk_id" value="{{ u.vk_id }}">
                  <input type="hidden" name="next" value="{{ self_url }}">
                  <button class="btn btn-sm btn-outline-secondary" style="color:#DC2626;border-color:color-mix(in srgb,#DC2626 30%, var(--border));">✖ Убрать</button>
                </form>
              {% elif u.role == 'owner' %}
                <span class="text-muted small">не удаляется</span>
              {% endif %}
            </td>
            {% endif %}
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    {% else %}
      <p class="text-muted mb-0 p-3">Никого не нашли{% if q %} по запросу «{{ q }}»{% endif %}.</p>
    {% endif %}
  </div>
</div>
"""


@app.route("/admin/users")
@admin_required
def users_page():
    q = request.args.get("q", "").strip()
    users = _aggregate_users(search=q)
    flash = request.args.get("flash")
    flash_kind = request.args.get("kind", "success")
    return _render_page(
        "Пользователи", _USERS_CONTENT,
        users=users, q=q,
        flash=flash, flash_kind=flash_kind,
    )


# ── Управление админами (owner-only) ──────────────────────────────────────────

_ADMINS_CONTENT = """
<div class="page-title">
  <h1>🛡️ Управление админами</h1>
</div>

{% if flash %}
  <div class="alert alert-{{ flash_kind or 'info' }} py-2 small">{{ flash }}</div>
{% endif %}

<div class="card mb-4">
  <div class="card-header fw-semibold">Выдать права админа</div>
  <div class="card-body">
    <form method="post" action="{{ url_for('admin_grant') }}" class="row g-2 align-items-center">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="col-sm-8 col-md-6">
        <input aria-label="VK ID или имя" type="text" name="query" class="form-control" required autocomplete="off"
               list="grantUsers"
               placeholder="VK ID, vk.com/id…, или имя из бота">
        <datalist id="grantUsers">
          {% for u in known_users %}
            <option value="{{ u.name }}" label="id{{ u.vk_id }} · {{ u.role }}"></option>
          {% endfor %}
        </datalist>
      </div>
      <div class="col-auto">
        <button class="btn btn-success">🛡️ Выдать админа</button>
      </div>
    </form>
    <div class="form-text small mt-2">
      Можно вписать VK ID (<code>123456</code>), ссылку <code>vk.com/id123456</code> или
      имя пользователя из бота — поиск с автодополнением.
    </div>
  </div>
</div>

<div class="card mb-4 border-danger-subtle">
  <div class="card-header fw-semibold">👑 Передать владение</div>
  <div class="card-body">
    <form method="post" action="{{ url_for('owner_grant') }}" class="row g-2 align-items-center">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
      <div class="col-sm-7 col-md-5">
        <input aria-label="VK ID или имя" type="text" name="query" class="form-control" required autocomplete="off"
               list="grantUsers" placeholder="VK ID, vk.com/id…, или имя из бота">
      </div>
      <div class="col-auto">
        <input aria-label="Одноразовый код из бота" type="text" name="code" class="form-control" required
               inputmode="numeric" pattern="[0-9]*" maxlength="6" minlength="6"
               autocomplete="off" placeholder="код из бота" style="max-width:11rem;">
      </div>
      <div class="col-auto">
        <button class="btn btn-danger">👑 Передать владение</button>
      </div>
    </form>
    <div class="form-text small mt-2">
      Владелец может всё, включая выдачу и снятие админов и владельцев.
      Операция подтверждается одноразовым кодом: запросите его в боте
      («🔑 Войти в панель») и введите сюда — украденной сессии кода не хватит.
      <br>
      Ваше собственное владение задано в <code>.env</code> и через панель не снимается:
      что бы ни произошло здесь, доступ у вас остаётся. Полностью уйти можно, выдав
      владение преемнику и убрав себя из <code>.env</code> на сервере.
    </div>
  </div>
</div>

<div class="card">
  <div class="card-header fw-semibold d-flex justify-content-between">
    <span>Текущие админы</span>
    <span class="badge bg-secondary">{{ admins|length + owners|length }}</span>
  </div>
  <div class="card-body p-0">
    <div class="table-responsive">
      <table class="table mb-0 align-middle">
        <thead class="table-light">
          <tr>
            <th>Имя</th>
            <th class="d-none d-sm-table-cell">VK ID</th>
            <th>Роль</th>
            <th class="d-none d-md-table-cell">Добавлен</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {% for o in owners %}
          <tr>
            <td>
              <a href="https://vk.com/id{{ o.vk_id }}" target="_blank"
                 class="text-decoration-none">{{ o.name }}</a>
            </td>
            <td class="d-none d-sm-table-cell text-muted small">{{ o.vk_id }}</td>
            <td><span class="badge bg-danger">owner</span></td>
            <td class="d-none d-md-table-cell text-muted small">
              {% if o.source == 'env' %}из .env{% else %}выдан в панели{% endif %}
            </td>
            <td class="text-end">
              {% if o.source == 'env' %}
                <span class="text-muted small">снимается на сервере</span>
              {% else %}
                <form method="post" action="{{ url_for('owner_revoke') }}"
                      class="d-inline-flex gap-1 align-items-center justify-content-end">
                  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                  <input type="hidden" name="vk_id" value="{{ o.vk_id }}">
                  <input aria-label="Код подтверждения" type="text" name="code" required class="form-control form-control-sm"
                         inputmode="numeric" pattern="[0-9]*" maxlength="6" minlength="6"
                         autocomplete="off" placeholder="код" style="max-width:6.5rem;">
                  <button class="btn btn-sm btn-outline-danger">👑 Снять владение</button>
                </form>
              {% endif %}
            </td>
          </tr>
          {% endfor %}
          {% for a in admins %}
          <tr>
            <td>
              <a href="https://vk.com/id{{ a.vk_id }}" target="_blank"
                 class="text-decoration-none">{{ a.name or ('id' ~ a.vk_id) }}</a>
            </td>
            <td class="d-none d-sm-table-cell text-muted small">{{ a.vk_id }}</td>
            <td><span class="badge bg-success">admin</span></td>
            <td class="d-none d-md-table-cell text-muted small">{{ a.added_at }}</td>
            <td class="text-end">
              <form method="post" action="{{ url_for('admin_revoke') }}" class="d-inline"
                    onsubmit="return confirm('Убрать админа у {{ a.name or a.vk_id }}?');">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
                <input type="hidden" name="vk_id" value="{{ a.vk_id }}">
                <button class="btn btn-sm btn-outline-warning">✖ Убрать</button>
              </form>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
</div>
"""


@app.route("/admin/admins")
@owner_required
def admins_page():
    raw = panel_users.list_all()
    db_owners = _db_owner_ids()
    all_ids = {a["vk_id"] for a in raw} | OWNER_VK_IDS
    names = vk_names.resolve(all_ids)
    admins = []
    for a in raw:
        # Владельцы (и из env, и выданные в панели) идут отдельным блоком выше.
        if a["vk_id"] in OWNER_VK_IDS or a["vk_id"] in db_owners:
            continue
        admins.append({**a, "name": names.get(a["vk_id"], a.get("name") or f"id{a['vk_id']}")})
    owners = [
        {"vk_id": uid, "name": names.get(uid, f"id{uid}"), "source": "env"}
        for uid in sorted(OWNER_VK_IDS)
    ] + [
        {"vk_id": uid, "name": names.get(uid, f"id{uid}"), "source": "panel"}
        for uid in sorted(db_owners - OWNER_VK_IDS)
    ]
    # Для datalist автодополнения — все юзеры бота с ролями
    try:
        known_users = _aggregate_users()
    except Exception:
        known_users = []
    flash = request.args.get("flash")
    flash_kind = request.args.get("kind", "success")
    return _render_page(
        "Управление админами",
        _ADMINS_CONTENT,
        admins=admins, owners=owners, flash=flash, flash_kind=flash_kind,
        known_users=known_users,
    )


def _resolve_grant_target(raw: str) -> tuple[int | None, str, str | None]:
    """Разбирает строку из формы и возвращает (vk_id, display_name, error).

    Принимает:
      - чистый VK ID: "12345"
      - URL: "vk.com/id12345" или "https://vk.com/id12345"
      - имя из бота: ищем в _aggregate_users, точное совпадение
      - частичное имя: если 1 точное → берём, если несколько → ошибка
    """
    raw = (raw or "").strip()
    if not raw:
        return None, "", "Введи VK ID или имя"

    # 1) URL → VK ID
    import re
    m = re.search(r"(?:vk\.com/(?:id)?|^)(\d+)$", raw)
    if m:
        vk_id = int(m.group(1))
        name = vk_names.resolve_one(vk_id)
        return vk_id, name, None

    # 2) Чистый int
    if raw.isdigit():
        vk_id = int(raw)
        name = vk_names.resolve_one(vk_id)
        return vk_id, name, None

    # 3) Поиск по имени среди пользователей бота
    users = _aggregate_users(search=raw)
    # Точное совпадение (case-insensitive)
    raw_lc = raw.lower()
    exact = [u for u in users if u.get("name", "").lower() == raw_lc]
    if len(exact) == 1:
        return int(exact[0]["vk_id"]), exact[0]["name"], None
    if len(exact) > 1:
        ids = ", ".join(f"id{u['vk_id']}" for u in exact[:5])
        return None, "", f"Несколько точных совпадений: {ids}. Уточни VK ID."

    # Если точных нет — берём единственного частичного, иначе ошибка
    if len(users) == 1:
        return int(users[0]["vk_id"]), users[0]["name"], None
    if len(users) > 1:
        ids = ", ".join(f"{u['name']} (id{u['vk_id']})" for u in users[:5])
        return None, "", f"Найдено несколько: {ids}. Уточни."
    return None, "", f"Юзер «{raw}» не найден среди пользователей бота. Введи VK ID напрямую."


@app.route("/admin/grant", methods=["POST"])
@owner_required
def admin_grant():
    next_url = _safe_next(request.form.get("next"), url_for("admins_page"))
    raw_vk = (request.form.get("vk_id") or "").strip()
    raw_query = (request.form.get("query") or "").strip()  # объединённое поле «ID или имя»

    # Если пришло объединённое поле — резолвим
    if raw_query and not raw_vk.isdigit():
        vk_id, name, err = _resolve_grant_target(raw_query)
        if err or vk_id is None:
            return redirect(_with_flash(next_url, err or "Не удалось распознать получателя", "danger"))
    else:
        try:
            vk_id = int(raw_vk)
        except (TypeError, ValueError):
            return redirect(_with_flash(next_url, "Некорректный VK ID", "danger"))
        if vk_id <= 0:
            return redirect(_with_flash(next_url, "Некорректный VK ID", "danger"))
        name = (request.form.get("name") or vk_names.resolve_one(vk_id)).strip()

    if vk_id in _all_owner_ids():
        return redirect(_with_flash(next_url, "Владелец уже имеет все права.", "info"))
    panel_users.grant(vk_id, granted_by=_current_vk_id() or 0, name=name)
    audit.log(_current_vk_id(), "admin.grant", f"id{vk_id}", name)
    return redirect(_with_flash(next_url, f"✅ {name} (id{vk_id}) теперь админ", "success"))


@app.route("/admin/revoke", methods=["POST"])
@owner_required
def admin_revoke():
    next_url = _safe_next(request.form.get("next"), url_for("admins_page"))
    try:
        vk_id = int(request.form.get("vk_id") or 0)
    except (TypeError, ValueError):
        return redirect(_with_flash(next_url, "Некорректный VK ID", "danger"))
    if vk_id in OWNER_VK_IDS:
        return redirect(
            _with_flash(next_url, "Владелец из .env снимается только на сервере.", "danger")
        )
    if vk_id in _db_owner_ids():
        return redirect(
            _with_flash(next_url, "Сначала снимите владение, потом права админа.", "danger")
        )
    panel_users.revoke(vk_id)
    name = vk_names.resolve_one(vk_id)
    audit.log(_current_vk_id(), "admin.revoke", f"id{vk_id}", name)
    return redirect(_with_flash(next_url, f"✖ {name} (id{vk_id}) снят с админов", "success"))


# ── Владение панелью ──────────────────────────────────────────────────────────
#
# Владельцы бывают двух видов и это принципиально:
#   * из env — несменяемый якорь, снимается только на сервере;
#   * из panel_users (role='owner') — выдаётся и снимается здесь.
# Угнанная сессия поэтому не может разжаловать владельца из .env: максимум —
# добавить совладельца, что видно в аудите, уходит уведомлением в VK всем
# владельцам и снимается одной кнопкой.
#
# Любая операция с владением требует step-up: свежего одноразового кода из бота.
# Кука без доступа к VK-аккаунту такую операцию не проведёт.


def _step_up_error(actor_uid: int) -> str | None:
    """Проверяет код подтверждения. Возвращает текст ошибки или None, если всё чисто."""
    code = _normalize_code(request.form.get("code"))
    ip = _client_ip()
    if not code:
        return "Нужен код из бота: операции с владением подтверждаются отдельно."
    if panel_codes.is_globally_locked() or panel_codes.is_rate_limited(ip):
        audit.log(actor_uid, "auth.step_up_rate_limited", ip, "")
        return "Слишком много неудачных попыток. Подождите 10 минут."
    verified = panel_codes.verify(code)
    if verified is None:
        panel_codes.record_failure(ip)
        return "Код неверен или истёк. Запросите новый в боте: «🔑 Войти в панель»."
    if verified != actor_uid:
        # Код чужого аккаунта — либо ошибка, либо попытка обойти подтверждение.
        panel_codes.record_failure(ip)
        audit.log(actor_uid, "auth.step_up_foreign_code", f"id{verified}", "")
        return "Этот код выдан другому аккаунту."
    return None


def _notify_owners(text: str) -> None:
    """Сообщает всем владельцам об изменении состава. Тихо не передаём владение."""
    if not _bot_config.VK_TOKEN:
        return
    try:
        import itertools as _it

        counter = _it.count()
        for uid in sorted(_all_owner_ids()):
            notifier.send_one(uid, text, counter)
    except Exception:
        logging.exception("Не удалось уведомить владельцев об изменении прав")


@app.route("/admin/owner/grant", methods=["POST"])
@owner_required
def owner_grant():
    """Выдаёт владение: получатель получает всё, включая управление админами."""
    next_url = _safe_next(request.form.get("next"), url_for("admins_page"))
    actor = _current_vk_id() or 0

    # Получателя разбираем ДО проверки кода: код одноразовый, и опечатка в имени
    # не должна его сжигать — иначе за каждую опечатку идёшь в бота за новым.
    vk_id, name, resolve_err = _resolve_grant_target(request.form.get("query") or "")
    if resolve_err or vk_id is None:
        return redirect(
            _with_flash(next_url, resolve_err or "Не удалось распознать получателя", "danger")
        )
    if vk_id in OWNER_VK_IDS:
        return redirect(_with_flash(next_url, "Это владелец из .env, у него уже всё есть.", "info"))
    if vk_id in _db_owner_ids():
        return redirect(_with_flash(next_url, f"{name} уже владелец.", "info"))

    err = _step_up_error(actor)
    if err:
        return redirect(_with_flash(next_url, err, "danger"))

    panel_users.grant(vk_id, granted_by=actor, name=name, role=panel_users.ROLE_OWNER)
    audit.log(actor, "admin.owner_grant", f"id{vk_id}", name)
    _notify_owners(
        f"🔐 Панель: id{actor} передал владение пользователю {name} (id{vk_id}).\n"
        f"Если это не вы — снимите владение на странице «Управление админами»."
    )
    return redirect(_with_flash(next_url, f"👑 {name} (id{vk_id}) теперь владелец", "success"))


@app.route("/admin/owner/revoke", methods=["POST"])
@owner_required
def owner_revoke():
    """Снимает владение, оставляя админку: разжалование не должно запирать людей."""
    next_url = _safe_next(request.form.get("next"), url_for("admins_page"))
    actor = _current_vk_id() or 0

    try:
        vk_id = int(request.form.get("vk_id") or 0)
    except (TypeError, ValueError):
        return redirect(_with_flash(next_url, "Некорректный VK ID", "danger"))

    if vk_id in OWNER_VK_IDS:
        return redirect(
            _with_flash(
                next_url,
                "Владелец из .env снимается только на сервере: правка .env и рестарт панели.",
                "danger",
            )
        )
    if vk_id not in _db_owner_ids():
        return redirect(_with_flash(next_url, "Этот пользователь не владелец.", "info"))
    if len(_all_owner_ids()) <= 1:
        return redirect(
            _with_flash(next_url, "Нельзя снять последнего владельца панели.", "danger")
        )

    err = _step_up_error(actor)
    if err:
        return redirect(_with_flash(next_url, err, "danger"))

    panel_users.set_role(vk_id, panel_users.ROLE_ADMIN)
    name = vk_names.resolve_one(vk_id)
    audit.log(actor, "admin.owner_revoke", f"id{vk_id}", name)
    _notify_owners(f"🔐 Панель: id{actor} снял владение с {name} (id{vk_id}). Права админа сохранены.")
    return redirect(
        _with_flash(next_url, f"👑→🛡️ {name} (id{vk_id}) снова просто админ", "success")
    )


def _safe_next(raw: str | None, fallback: str) -> str:
    """Разрешает только локальные пути — защита от открытого редиректа.

    Значение приходит из формы (поле `next`), поэтому абсолютный URL, схему и
    protocol-relative (`//evil.com`) отбрасываем. Обратный слэш режем тоже:
    браузеры нормализуют его в прямой, поэтому `/\\evil.com` уезжает на чужой
    домен, хотя по виду это локальный путь.
    """
    candidate = (raw or "").strip()
    if "\\" in candidate:
        return fallback
    if not candidate.startswith("/") or candidate.startswith("//"):
        return fallback
    from urllib.parse import urlparse

    parts = urlparse(candidate)
    if parts.scheme or parts.netloc:
        return fallback
    return candidate


def _with_flash(url: str, msg: str, kind: str = "success") -> str:
    """Добавляет ?flash=...&kind=... к URL, сохраняя существующие параметры."""
    from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse
    parts = urlparse(url)
    params = dict(parse_qsl(parts.query))
    params["flash"] = msg
    params["kind"] = kind
    return urlunparse(parts._replace(query=urlencode(params)))


# ── Детектор конфликтов в расписании (admin) ─────────────────────────────────


def _find_conflicts() -> dict:
    """Ищет пересечения: один препод/одна аудитория в разных парах одновременно.

    Возвращает {'teachers': [...], 'rooms': [...]}.
    Каждый элемент — словарь с резоном и списком пересекающихся записей.
    """
    teachers: list[dict] = []
    rooms: list[dict] = []
    try:
        with _sched_conn(vk=True) as conn:
            # Конфликты преподавателей: одинаковый (teacher, day, time, week-or-empty)
            for row in conn.execute(
                """
                SELECT teacher, day, time, COALESCE(NULLIF(week,''), '*') AS w,
                       GROUP_CONCAT(course || ' курс · ' || direction
                                    || ' · ' || subject
                                    || ' · ауд.' || COALESCE(room,'?'), ' || ') AS pairs_str,
                       COUNT(*) AS n
                FROM schedule
                WHERE teacher IS NOT NULL AND teacher != ''
                GROUP BY teacher, day, time, w
                HAVING n > 1
                ORDER BY teacher, """ + _DAY_ORDER_SQL + """, """ + _TIME_ORDER_SQL + """
                """
            ).fetchall():
                teachers.append({
                    "teacher": row[0], "day": row[1], "time": row[2],
                    "week": "" if row[3] == "*" else row[3],
                    "pairs": row[4], "count": row[5],
                })
            # Конфликты аудиторий: одинаковая (room, day, time, week)
            for row in conn.execute(
                """
                SELECT room, day, time, COALESCE(NULLIF(week,''), '*') AS w,
                       GROUP_CONCAT(course || ' курс · ' || direction
                                    || ' · ' || subject
                                    || ' · ' || COALESCE(teacher,'?'), ' || ') AS pairs_str,
                       COUNT(*) AS n
                FROM schedule
                WHERE room IS NOT NULL AND room != ''
                GROUP BY room, day, time, w
                HAVING n > 1
                ORDER BY room, """ + _DAY_ORDER_SQL + """, """ + _TIME_ORDER_SQL + """
                """
            ).fetchall():
                rooms.append({
                    "room": row[0], "day": row[1], "time": row[2],
                    "week": "" if row[3] == "*" else row[3],
                    "pairs": row[4], "count": row[5],
                })
    except Exception:
        pass
    return {"teachers": teachers, "rooms": rooms}


_CONFLICTS_CONTENT = """
<div class="page-title">
  <h1>⚠️ Конфликты в расписании</h1>
  <div style="color:var(--text-3);font-size:13px;">
    Преподаватель/аудитория с двумя парами в одно время. Проверяй после каждой загрузки.
  </div>
</div>

{% if not c.teachers and not c.rooms %}
  <div class="alert alert-success">✅ Конфликтов нет — расписание чистое.</div>
{% endif %}

{% if c.teachers %}
<div class="card mb-4">
  <div class="card-header" style="display:flex;align-items:center;gap:10px;">
    <span style="font-size:18px;">👨‍🏫</span>
    <strong>Двойное бронирование преподавателей</strong>
    <span style="margin-left:auto;color:var(--text-3);font-size:12px;">{{ c.teachers|length }}</span>
  </div>
  <div class="table-responsive">
    <table class="table mb-0">
      <thead><tr>
        <th>Преподаватель</th><th>День</th><th>Время</th><th>Нед.</th><th>Конфликтующие пары</th>
      </tr></thead>
      <tbody>
        {% for t in c.teachers %}
        <tr>
          <td><a href="{{ url_for('teacher_page', name=t.teacher) }}" style="color:var(--accent);text-decoration:none;font-weight:600;">{{ t.teacher }}</a></td>
          <td>{{ t.day }}</td>
          <td class="mono">{{ t.time }}</td>
          <td>{{ t.week or '—' }}</td>
          <td style="font-size:12.5px;color:var(--text-2);">{{ t.pairs|replace(' || ', ' ⚔️ ') }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% endif %}

{% if c.rooms %}
<div class="card mb-4">
  <div class="card-header" style="display:flex;align-items:center;gap:10px;">
    <span style="font-size:18px;">🚪</span>
    <strong>Двойное бронирование аудиторий</strong>
    <span style="margin-left:auto;color:var(--text-3);font-size:12px;">{{ c.rooms|length }}</span>
  </div>
  <div class="table-responsive">
    <table class="table mb-0">
      <thead><tr>
        <th>Аудитория</th><th>День</th><th>Время</th><th>Нед.</th><th>Конфликтующие пары</th>
      </tr></thead>
      <tbody>
        {% for r in c.rooms %}
        <tr>
          <td><a href="{{ url_for('room_page', name=r.room) }}" style="color:var(--accent);text-decoration:none;font-weight:600;">{{ r.room }}</a></td>
          <td>{{ r.day }}</td>
          <td class="mono">{{ r.time }}</td>
          <td>{{ r.week or '—' }}</td>
          <td style="font-size:12.5px;color:var(--text-2);">{{ r.pairs|replace(' || ', ' ⚔️ ') }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% endif %}
"""


@app.route("/admin/conflicts")
@admin_required
def conflicts_page():
    return _render_page("Конфликты расписания", _CONFLICTS_CONTENT, c=_find_conflicts())


# ── Diff между версиями расписания (admin) ───────────────────────────────────


def _versions_for_diff() -> list[dict]:
    try:
        return schedule_loader.list_versions()
    except Exception:
        return []


def _read_schedule_from_excel(path: str) -> set[tuple]:
    """Превращает Excel-файл версии в множество кортежей-«ключей пары» для сравнения."""
    try:
        from contextlib import closing
        from import_excel import import_schedule
        import tempfile
        import sqlite3 as _sql
        # Парсим в tmp-БД, читаем оттуда
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_db = tmp.name
        try:
            import_schedule(path, tmp_db)
            # closing() обязателен: sqlite3.Connection.__exit__ закрывает только
            # транзакцию, а с живым дескриптором os.unlink ниже на Windows молча
            # не срабатывает и временные БД копятся.
            with closing(_sql.connect(tmp_db)) as conn:
                return set(conn.execute(
                    "SELECT course, direction, day, time, subject, teacher, "
                    "room, week, class_type FROM schedule"
                ).fetchall())
        finally:
            try:
                os.unlink(tmp_db)
            except Exception:
                logging.warning("Не удалось удалить временную БД диффа %s", tmp_db)
    except Exception:
        return set()


def _diff_pairs(a: set[tuple], b: set[tuple]) -> dict:
    """Возвращает {'added': [...], 'removed': [...]} между двумя множествами пар."""
    return {
        "added": sorted(b - a),
        "removed": sorted(a - b),
    }


_DIFF_CONTENT = """
<div class="page-title">
  <h1>🔀 Diff между версиями расписания</h1>
  <div style="color:var(--text-3);font-size:13px;">Что появилось / исчезло между двумя загрузками</div>
</div>

<form method="get" class="card mb-4" style="padding:14px 18px;display:flex;gap:12px;align-items:end;flex-wrap:wrap;">
  <div>
    <label class="form-label" style="font-size:11px;">База (старая версия)</label>
    <select aria-label="Версия слева" name="a" class="form-select form-select-sm">
      {% for v in versions %}
        <option value="{{ v.id }}" {% if v.id == a_id %}selected{% endif %}>
          #{{ v.id }} · {{ v.uploaded_at }} · {{ v.original_filename }}
        </option>
      {% endfor %}
    </select>
  </div>
  <div>
    <label class="form-label" style="font-size:11px;">Сравнить с (новая версия)</label>
    <select aria-label="Версия справа" name="b" class="form-select form-select-sm">
      {% for v in versions %}
        <option value="{{ v.id }}" {% if v.id == b_id %}selected{% endif %}>
          #{{ v.id }} · {{ v.uploaded_at }} · {{ v.original_filename }}
        </option>
      {% endfor %}
    </select>
  </div>
  <button class="btn btn-primary btn-sm" style="height:32px;">Сравнить</button>
</form>

{% if not versions or versions|length < 2 %}
  <div class="alert alert-info">
    Нужны хотя бы две сохранённые версии. Загрузи новый Excel в <a href="{{ url_for('upload_page') }}">Загрузке</a>.
  </div>
{% elif diff is none %}
  <div class="alert alert-warning">Выбери две разные версии и нажми «Сравнить».</div>
{% else %}
  <div class="row g-3">
    <div class="col-md-6">
      <div class="card">
        <div class="card-header" style="background:color-mix(in srgb, #10B981 8%, var(--surface));">
          ➕ <strong>Добавлено</strong>
          <span style="margin-left:auto;font-size:12px;color:var(--text-3);">{{ diff.added|length }}</span>
        </div>
        <div class="card-body p-0" style="max-height:500px;overflow:auto;">
          <table class="table mb-0">
            <thead><tr><th>Курс</th><th>День</th><th>Время</th><th>Предмет</th><th>Препод.</th><th>Ауд.</th></tr></thead>
            <tbody>
              {% for r in diff.added %}
                <tr>
                  <td>{{ r[0] }}</td><td>{{ r[2] }}</td><td class="mono">{{ r[3] }}</td>
                  <td>{{ r[4] }}</td><td>{{ r[5] }}</td><td class="mono">{{ r[6] }}</td>
                </tr>
              {% endfor %}
              {% if not diff.added %}<tr><td colspan="6" style="text-align:center;color:var(--text-3);padding:20px;">Ничего нового</td></tr>{% endif %}
            </tbody>
          </table>
        </div>
      </div>
    </div>
    <div class="col-md-6">
      <div class="card">
        <div class="card-header" style="background:color-mix(in srgb, #DC2626 8%, var(--surface));">
          ➖ <strong>Удалено</strong>
          <span style="margin-left:auto;font-size:12px;color:var(--text-3);">{{ diff.removed|length }}</span>
        </div>
        <div class="card-body p-0" style="max-height:500px;overflow:auto;">
          <table class="table mb-0">
            <thead><tr><th>Курс</th><th>День</th><th>Время</th><th>Предмет</th><th>Препод.</th><th>Ауд.</th></tr></thead>
            <tbody>
              {% for r in diff.removed %}
                <tr>
                  <td>{{ r[0] }}</td><td>{{ r[2] }}</td><td class="mono">{{ r[3] }}</td>
                  <td>{{ r[4] }}</td><td>{{ r[5] }}</td><td class="mono">{{ r[6] }}</td>
                </tr>
              {% endfor %}
              {% if not diff.removed %}<tr><td colspan="6" style="text-align:center;color:var(--text-3);padding:20px;">Ничего не удалено</td></tr>{% endif %}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
{% endif %}
"""


@app.route("/admin/diff")
@admin_required
def diff_page():
    versions = _versions_for_diff()
    try:
        a_id = int(request.args.get("a") or 0)
        b_id = int(request.args.get("b") or 0)
    except ValueError:
        a_id = b_id = 0
    diff = None
    if a_id and b_id and a_id != b_id:
        # Дефолт: a = более старая, b = более новая (но не принципиально)
        version_map = {v["id"]: v for v in versions}
        va, vb = version_map.get(a_id), version_map.get(b_id)
        if va and vb:
            set_a = _read_schedule_from_excel(va["file_path"])
            set_b = _read_schedule_from_excel(vb["file_path"])
            diff = _diff_pairs(set_a, set_b)
    return _render_page(
        "Diff расписания", _DIFF_CONTENT,
        versions=versions, a_id=a_id, b_id=b_id, diff=diff,
    )


# ── Страницы преподавателя / аудитории ────────────────────────────────────────

_ENTITY_CONTENT = """
<div class="page-title">
  <h1>{{ icon }} {{ title }}</h1>
  <div style="color:var(--text-3);font-size:13px;">{{ rows|length }} пар(ы) в расписании</div>
</div>

{% if not rows %}
  <div class="card" style="padding:30px;text-align:center;color:var(--text-3);">
    Не нашли пар для «{{ name }}». Возможно, такого {{ kind_label }} нет в расписании.
  </div>
{% else %}
  {% set DAYS = ['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота'] %}
  {% set TYPE_COLORS = {
    'лк':'#6366F1','лекция':'#6366F1','лекц':'#6366F1',
    'пр':'#10B981','практика':'#10B981','практ':'#10B981',
    'лб':'#F59E0B','лаб':'#F59E0B','лабораторная':'#F59E0B',
    'сем':'#EC4899','семинар':'#EC4899',
    'кур':'#EAB308','курсовая':'#EAB308',
    'экз':'#DC2626','экзамен':'#DC2626',
    'зач':'#0EA5E9','зачёт':'#0EA5E9','зачет':'#0EA5E9',
  } %}
  {% macro tc(t) %}{{ TYPE_COLORS.get((t or '').strip().lower(), '#7A7872') }}{% endmacro %}

  <div class="day-grid">
    {% for d in DAYS %}
      {% set day_rows = rows|selectattr('2', 'equalto', d)|list %}
      {% if day_rows %}
        <div class="day-col">
          <div class="day-head">
            <span class="day-name">{{ d }}</span>
            <span class="day-count">{{ day_rows|length }} пар</span>
          </div>
          <div class="day-list">
            {% for r in day_rows %}
            <div class="pair-card" style="--type-color: {{ tc(r[8]) }};">
              <div class="pair-time">
                <span>{{ r[3] }}</span>
                {% if r[7] %}<span class="week">{{ r[7] }}</span>{% endif %}
                <span style="flex:1;"></span>
                {% if r[8] %}<span class="type-badge" style="--type-color: {{ tc(r[8]) }};">{{ type_short(r[8]) }}</span>{% endif %}
              </div>
              <div class="pair-subject">{{ r[4] }}</div>
              <div class="pair-meta">
                {% if kind == 'teacher' %}
                  {% if r[6] %}<span class="mono">ауд. {{ r[6] }}</span><span class="sep">·</span>{% endif %}
                  <span class="course-chip">{{ r[0] }}к</span>
                  <span>{{ r[1] }}</span>
                {% else %}
                  {% if r[5] %}<span>{{ r[5] }}</span><span class="sep">·</span>{% endif %}
                  <span class="course-chip">{{ r[0] }}к</span>
                  <span>{{ r[1] }}</span>
                {% endif %}
              </div>
            </div>
            {% endfor %}
          </div>
        </div>
      {% endif %}
    {% endfor %}
  </div>
{% endif %}
"""


@app.route("/teacher/<path:name>")
@login_required
def teacher_page(name: str):
    rows = []
    try:
        with _sched_conn(vk=True) as conn:
            rows = conn.execute(
                "SELECT course, direction, day, time, subject, teacher, room, "
                "week, class_type, date_range FROM schedule WHERE teacher=? "
                f"ORDER BY {_DAY_ORDER_SQL}, {_TIME_ORDER_SQL}",
                (name,),
            ).fetchall()
    except Exception:
        pass
    return _render_page(
        f"Преподаватель · {name}", _ENTITY_CONTENT,
        icon="👨‍🏫", title=name, name=name, kind="teacher",
        kind_label="преподавателя", rows=rows,
    )


@app.route("/room/<path:name>")
@login_required
def room_page(name: str):
    rows = []
    try:
        with _sched_conn(vk=True) as conn:
            rows = conn.execute(
                "SELECT course, direction, day, time, subject, teacher, room, "
                "week, class_type, date_range FROM schedule WHERE room=? "
                f"ORDER BY {_DAY_ORDER_SQL}, {_TIME_ORDER_SQL}",
                (name,),
            ).fetchall()
    except Exception:
        pass
    return _render_page(
        f"Аудитория {name}", _ENTITY_CONTENT,
        icon="🚪", title=f"Аудитория {name}", name=name, kind="room",
        kind_label="аудитории", rows=rows,
    )


# ── Экспорт расписания в .ics (iCalendar) ────────────────────────────────────


def _parse_time_range(time_str: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """'08:30-10:00' → ((8,30),(10,0)). Терпим к разделителям ':', '.', '-', '–'."""
    import re
    s = (time_str or "").strip()
    if not s:
        return None
    parts = re.split(r"\s*[-–—]\s*", s)
    if len(parts) != 2:
        return None
    out = []
    for p in parts:
        m = re.match(r"^(\d{1,2})[:\.](\d{2})$", p.strip())
        if not m:
            return None
        out.append((int(m.group(1)), int(m.group(2))))
    return tuple(out)  # type: ignore[return-value]


def _ics_escape(text: str) -> str:
    """Экранирование по RFC 5545: backslash, comma, semicolon, newline."""
    return (text or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def _build_ics(pairs: list, *, weeks_ahead: int = 8, calendar_name: str = "Расписание") -> str:
    """Собирает iCal-фид из выборки пар на ближайшие N недель.

    `pairs` — список кортежей в формате schedule_page rows:
      (course, direction, day, time, subject, teacher, room, week, class_type, date_range)
    Генерируем по событию на каждый день, где пара есть, на weeks_ahead недель вперёд.
    """
    from vkbot.config import now_msk
    from vkbot.schedule.week import week_type_for

    today = now_msk().date()
    # Понедельник текущей недели
    monday = today - _dt.timedelta(days=today.weekday())

    day_to_offset = {
        "Понедельник": 0, "Вторник": 1, "Среда": 2,
        "Четверг": 3, "Пятница": 4, "Суббота": 5,
    }

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//VKBot//ElSchedule//RU",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_ics_escape(calendar_name)}",
        "X-WR-TIMEZONE:Europe/Moscow",
        # Минимальный VTIMEZONE для МСК (UTC+3, без DST)
        "BEGIN:VTIMEZONE",
        "TZID:Europe/Moscow",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0300",
        "TZOFFSETTO:+0300",
        "TZNAME:MSK",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]

    seq = 0
    for week_no in range(weeks_ahead):
        for r in pairs:
            day_name = r[2]
            time_str = r[3]
            week_pref = (r[7] or "").strip()
            offset = day_to_offset.get(day_name)
            if offset is None:
                continue
            event_date = monday + _dt.timedelta(weeks=week_no, days=offset)
            # Фильтр чёт/нечёт: пары с week_pref видны только в соответствующую неделю
            if week_pref:
                actual_week = week_type_for(event_date)
                if week_pref != actual_week:
                    continue
            tr = _parse_time_range(time_str)
            if not tr:
                continue
            (h1, m1), (h2, m2) = tr
            dtstart = f"{event_date:%Y%m%d}T{h1:02d}{m1:02d}00"
            dtend   = f"{event_date:%Y%m%d}T{h2:02d}{m2:02d}00"
            summary_parts = [r[4] or "Пара"]
            if r[8]:
                summary_parts.append(f"({r[8]})")
            summary = " ".join(summary_parts)
            location = (r[6] or "").strip()
            description_parts = []
            if r[5]:
                description_parts.append(f"Преподаватель: {r[5]}")
            if r[0] and r[1]:
                description_parts.append(f"{r[0]} курс · {r[1]}")
            if r[9]:
                description_parts.append(f"Период: {r[9]}")
            description = "\n".join(description_parts)
            uid = f"{event_date:%Y%m%d}-{h1:02d}{m1:02d}-{abs(hash((r[4], r[6], r[5]))) % 10**9}@elschedule.ru"
            seq += 1
            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{today:%Y%m%d}T000000Z",
                f"DTSTART;TZID=Europe/Moscow:{dtstart}",
                f"DTEND;TZID=Europe/Moscow:{dtend}",
                f"SUMMARY:{_ics_escape(summary)}",
            ]
            if location:
                lines.append(f"LOCATION:{_ics_escape(location)}")
            if description:
                lines.append(f"DESCRIPTION:{_ics_escape(description)}")
            lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    # RFC требует \r\n
    return "\r\n".join(lines) + "\r\n"


# ── Кэш для /calendar.ics ────────────────────────────────────────────────────
# Календарные клиенты (Google/Apple) дёргают .ics каждые 1-3 часа. Без кэша
# каждый запрос = SQL + цикл по 8 неделям × все пары. Инвалидируется при
# успешном commit нового расписания (см. upload_commit).
_ICS_CACHE: dict[tuple, tuple[str, str, float]] = {}  # key → (ics, cal_name, ts)
_ICS_TTL_SEC = 3600


def _ics_cache_clear() -> None:
    _ICS_CACHE.clear()


@app.route("/calendar.ics")
@login_required
def calendar_ics():
    """Генерирует .ics для подписанного пользователя (по user_prefs).

    Можно подписаться в Google/Apple Calendar:
      https://elschedule.ru/calendar.ics
    но т.к. это требует auth, нужны параметры. Делаем download через cookies.
    """
    import time as _time
    uid = _current_vk_id()
    pref = _get_pref(uid) if uid else None
    key = (pref[0], pref[1]) if pref else ("ALL", "")

    hit = _ICS_CACHE.get(key)
    if hit and _time.time() - hit[2] < _ICS_TTL_SEC:
        ics, cal_name, _ = hit
    else:
        pairs = []
        cal_name = "ЧГПУ · Расписание"
        try:
            with _sched_conn(vk=True) as conn:
                if pref:
                    cal_name = f"ЧГПУ · {pref[0]} курс · {pref[1]}"
                    pairs = conn.execute(
                        "SELECT course, direction, day, time, subject, teacher, room, "
                        "week, class_type, date_range FROM schedule "
                        "WHERE course=? AND direction=? "
                        f"ORDER BY {_DAY_ORDER_SQL}, {_TIME_ORDER_SQL}",
                        (pref[0], pref[1]),
                    ).fetchall()
                else:
                    pairs = conn.execute(
                        "SELECT course, direction, day, time, subject, teacher, room, "
                        "week, class_type, date_range FROM schedule "
                        f"ORDER BY course, direction, {_DAY_ORDER_SQL}, {_TIME_ORDER_SQL}"
                    ).fetchall()
        except Exception:
            pass
        ics = _build_ics(list(pairs), weeks_ahead=8, calendar_name=cal_name)
        _ICS_CACHE[key] = (ics, cal_name, _time.time())

    filename = "elschedule.ics"
    return (
        ics, 200,
        {
            "Content-Type": "text/calendar; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Календарным клиентам разрешаем кэшировать 1 час; браузеру — нет.
            "Cache-Control": "private, max-age=3600",
        },
    )


# ── Аудит-лог (owner) ─────────────────────────────────────────────────────────

_AUDIT_CONTENT = """
<div class="page-title">
  <h1>📜 Аудит-лог</h1>
  <div style="color:var(--text-3);font-size:12.5px;">Найдено {{ rows|length }} событий{% if any_filter %} (с фильтрами){% endif %}</div>
</div>

<form method="get" class="card mb-3" style="padding:14px 18px;display:flex;gap:10px;align-items:end;flex-wrap:wrap;">
  <div>
    <label class="form-label" style="font-size:11px;margin-bottom:3px;">Группа действий</label>
    <select aria-label="Фильтр по действию" name="action" class="form-select form-select-sm" style="min-width:160px;">
      <option value="">— все —</option>
      {% for p in action_prefixes %}
        <option value="{{ p }}" {% if p == action_filter %}selected{% endif %}>{{ p }}.*</option>
      {% endfor %}
    </select>
  </div>
  <div>
    <label class="form-label" style="font-size:11px;margin-bottom:3px;">Actor</label>
    <select aria-label="Фильтр по пользователю" name="actor" class="form-select form-select-sm" style="min-width:200px;">
      <option value="">— все —</option>
      {% for uid in actor_ids %}
        <option value="{{ uid }}" {% if uid == actor_filter %}selected{% endif %}>
          {{ names.get(uid, 'id' ~ uid) }} (id{{ uid }})
        </option>
      {% endfor %}
    </select>
  </div>
  <div>
    <label class="form-label" style="font-size:11px;margin-bottom:3px;">С даты</label>
    <input aria-label="Показывать события с даты" type="date" name="since" value="{{ since_filter }}"
           class="form-control form-control-sm" style="min-width:160px;">
  </div>
  <div>
    <label class="form-label" style="font-size:11px;margin-bottom:3px;">Лимит</label>
    <select aria-label="Сколько записей показывать" name="limit" class="form-select form-select-sm">
      {% for n in [100, 300, 1000, 5000] %}
        <option value="{{ n }}" {% if n == limit_value %}selected{% endif %}>{{ n }}</option>
      {% endfor %}
    </select>
  </div>
  <button class="btn btn-primary btn-sm" style="height:31px;">Применить</button>
  {% if any_filter %}
    <a href="{{ url_for('audit_page') }}" class="btn btn-outline-secondary btn-sm" style="height:31px;">Сбросить</a>
  {% endif %}
</form>

<div class="card">
  <div class="table-responsive">
    <table class="table mb-0 align-middle">
      <thead>
        <tr>
          <th style="width:170px;">Когда</th>
          <th style="width:200px;">Кто</th>
          <th style="width:160px;">Действие</th>
          <th style="width:180px;">Объект</th>
          <th>Подробности</th>
        </tr>
      </thead>
      <tbody>
        {% if not rows %}
          <tr><td colspan="5" style="text-align:center;padding:30px;color:var(--text-3);">Пусто</td></tr>
        {% endif %}
        {% for r in rows %}
        <tr>
          <td class="mono" style="font-size:12px;color:var(--text-3);">{{ r.created_at }}</td>
          <td>
            {% if r.actor_vk_id %}
              <a href="https://vk.com/id{{ r.actor_vk_id }}" target="_blank" style="text-decoration:none;color:inherit;">
                {{ names.get(r.actor_vk_id, 'id' ~ r.actor_vk_id) }}
              </a>
              <div style="color:var(--text-3);font-size:11px;">id{{ r.actor_vk_id }}</div>
            {% else %}
              <span style="color:var(--text-4);">—</span>
            {% endif %}
          </td>
          <td>
            <span class="badge"
                  style="background:{{ action_color(r.action) }};color:white;font-size:11px;font-weight:600;padding:3px 8px;border-radius:6px;">
              {{ r.action }}
            </span>
          </td>
          <td style="font-size:12.5px;color:var(--text-2);">{{ r.target or '—' }}</td>
          <td style="font-size:12.5px;color:var(--text-3);">{{ r.details or '' }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
"""


def _action_color(action: str) -> str:
    if action.startswith("admin."):
        return "#6366F1"
    if action.startswith("auth."):
        return "#10B981"
    if action.startswith("schedule."):
        return "#F59E0B"
    if action.startswith("broadcast"):
        return "#EC4899"
    return "#7A7872"


@app.route("/admin/audit")
@owner_required
def audit_page():
    action_filter = (request.args.get("action") or "").strip()
    try:
        actor_filter = int(request.args.get("actor") or 0)
    except (TypeError, ValueError):
        actor_filter = 0
    since_filter = (request.args.get("since") or "").strip()
    try:
        limit_value = int(request.args.get("limit") or 300)
    except (TypeError, ValueError):
        limit_value = 300
    # Clamp лимит чтобы не словить OOM на огромном логе
    limit_value = max(10, min(limit_value, 10000))

    rows = audit.list_recent(
        limit=limit_value,
        action_prefix=(action_filter + ".") if action_filter else "",
        actor_vk_id=actor_filter if actor_filter > 0 else None,
        since=since_filter,
    )
    # Имена нужны и для строк лога, и для селектора Actor
    all_actor_ids = sorted(set(audit.distinct_actors()) | OWNER_VK_IDS)
    names = vk_names.resolve(set(all_actor_ids) | {r["actor_vk_id"] for r in rows if r["actor_vk_id"]})
    return _render_page(
        "Аудит-лог", _AUDIT_CONTENT,
        rows=rows, names=names, action_color=_action_color,
        action_prefixes=audit.distinct_action_prefixes(),
        actor_ids=all_actor_ids,
        action_filter=action_filter,
        actor_filter=actor_filter,
        since_filter=since_filter,
        limit_value=limit_value,
        any_filter=bool(action_filter or actor_filter or since_filter),
    )


# ── Рассылка подписчикам (admin) ─────────────────────────────────────────────

_BROADCAST_CONTENT = """
<div class="page-title">
  <h1>📢 Рассылка подписчикам</h1>
</div>

{% if flash %}
  <div class="alert alert-{{ flash_kind or 'success' }} py-2 small">{{ flash }}</div>
{% endif %}

<div class="card mb-4" style="padding:18px;">
  <div style="display:flex;gap:14px;flex-wrap:wrap;align-items:center;">
    <div style="font-size:28px;">📢</div>
    <div style="flex:1;min-width:200px;">
      <div style="font-weight:700;font-size:14.5px;">Сообщение уйдёт всем подписчикам на «Пары»</div>
      <div style="color:var(--text-3);font-size:12.5px;margin-top:2px;">
        Сейчас активных подписчиков: <strong style="color:var(--text);">{{ subscriber_count }}</strong> ·
        Скорость рассылки ~17 в секунду
      </div>
    </div>
  </div>
</div>

<form method="post" action="{{ url_for('broadcast_send') }}" class="card mb-4"
      style="padding:18px;display:flex;flex-direction:column;gap:14px;"
      onsubmit="return confirm('Отправить сообщение {{ subscriber_count }} подписчикам?');">
      <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <div>
    <label class="form-label">Текст сообщения</label>
    <textarea name="text" rows="8" class="form-control" required minlength="3" maxlength="4096"
              placeholder="Пиши понятно и кратко. VK уведомит пользователей этим текстом."
              style="font-family:var(--font);resize:vertical;">{{ default_text }}</textarea>
    <div class="form-text small">Максимум 4096 символов. Эмодзи и переносы строк работают.</div>
  </div>
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
    <button class="btn btn-primary" style="height:38px;">📤 Отправить всем подписчикам</button>
    <a href="{{ url_for('dashboard') }}" class="btn btn-outline-secondary" style="height:38px;display:inline-flex;align-items:center;">Отмена</a>
    <span style="color:var(--text-3);font-size:12.5px;margin-left:auto;">
      ⚠️ Отменить рассылку после старта нельзя
    </span>
  </div>
</form>

<div id="bc-live" class="card mb-4" style="padding:18px;{% if not (bc and bc.status in ['running','done','error']) %}display:none;{% endif %}">
  <div style="font-weight:600;margin-bottom:10px;">
    Текущая рассылка: <span id="bc-status">{{ bc.status if bc else '' }}</span>
  </div>
  <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:13.5px;">
    <div>👥 Всего: <strong id="bc-total">{{ bc.total or 0 }}</strong></div>
    <div>📨 Отправлено: <strong id="bc-sent">{{ bc.sent or 0 }}</strong></div>
    <div>⚠️ Ошибок: <strong id="bc-failed">{{ bc.failed or 0 }}</strong></div>
    <div>🔕 Отписалось: <strong id="bc-disabled">{{ bc.disabled or 0 }}</strong></div>
  </div>
</div>
<script>
(function(){
  var box=document.getElementById('bc-live');
  if(!box) return;
  function ru(s){return {running:'идёт…',done:'завершена ✅',error:'ошибка ⚠️',idle:''}[s]||s||'';}
  function paint(d){
    box.style.display = (d.status && d.status!=='idle') ? '' : 'none';
    document.getElementById('bc-status').textContent = ru(d.status);
    document.getElementById('bc-total').textContent = d.total||0;
    document.getElementById('bc-sent').textContent = d.sent||0;
    document.getElementById('bc-failed').textContent = d.failed||0;
    document.getElementById('bc-disabled').textContent = d.disabled||0;
  }
  function poll(){
    fetch('{{ url_for("broadcast_status") }}',{headers:{'X-Requested-With':'fetch'}})
      .then(function(r){return r.json();})
      .then(function(d){paint(d); if(d.status==='running'){setTimeout(poll,1500);}})
      .catch(function(){});
  }
  {% if bc and bc.status=='running' %}poll();{% endif %}
})();
</script>

{% if last_result %}
  <div class="card" style="padding:18px;">
    <div style="font-weight:600;margin-bottom:10px;">Результат прошлой рассылки</div>
    <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:13.5px;">
      <div>📨 Отправлено: <strong>{{ last_result.sent or 0 }}</strong></div>
      <div>⚠️ Ошибок: <strong>{{ last_result.failed or 0 }}</strong></div>
      <div>🔕 Отписалось: <strong>{{ last_result.disabled or 0 }}</strong></div>
    </div>
  </div>
{% endif %}
"""


# Состояние рассылки (в памяти процесса — панель запускается одним воркером).
# Рассылка идёт в фоновом потоке, чтобы не блокировать HTTP-запрос на минуты.
_LAST_BROADCAST: dict = {"status": "idle"}
_BROADCAST_LOCK = threading.Lock()


def _broadcast_snapshot() -> dict:
    with _BROADCAST_LOCK:
        return dict(_LAST_BROADCAST)


def _start_broadcast(text: str, uids: list[int], actor) -> bool:
    """Запускает рассылку в фоне. Возвращает False, если рассылка уже идёт."""
    now_hms = _bot_config.now_msk().strftime("%H:%M:%S")
    with _BROADCAST_LOCK:
        if _LAST_BROADCAST.get("status") == "running":
            return False
        _LAST_BROADCAST.clear()
        _LAST_BROADCAST.update(
            status="running", total=len(uids), sent=0, failed=0, disabled=0,
            started_at=now_hms, finished_at=None,
        )

    def _progress(sent: int, failed: int, disabled: int) -> None:
        with _BROADCAST_LOCK:
            _LAST_BROADCAST.update(sent=sent, failed=failed, disabled=disabled)

    def _worker() -> None:
        try:
            br = notifier.broadcast(text, uids, progress=_progress)
            with _BROADCAST_LOCK:
                _LAST_BROADCAST.update(
                    status="done", sent=br.sent, failed=br.failed,
                    disabled=len(br.disabled_uids),
                    finished_at=_bot_config.now_msk().strftime("%H:%M:%S"),
                    result=br.as_dict(),
                )
            audit.log(
                actor, "broadcast.send", f"{len(uids)} подписчиков",
                f"sent={br.sent}, failed={br.failed}",
            )
        except Exception:
            logging.exception("broadcast worker failed")
            with _BROADCAST_LOCK:
                _LAST_BROADCAST.update(
                    status="error",
                    finished_at=_bot_config.now_msk().strftime("%H:%M:%S"),
                )

    threading.Thread(target=_worker, name="broadcast", daemon=True).start()
    return True


@app.route("/admin/broadcast", methods=["GET"])
@admin_required
def broadcast_page():
    flash = request.args.get("flash")
    flash_kind = request.args.get("kind", "success")
    snap = _broadcast_snapshot()
    return _render_page(
        "Рассылка", _BROADCAST_CONTENT,
        subscriber_count=_subscriber_count(),
        default_text="",
        flash=flash, flash_kind=flash_kind,
        bc=snap,
        last_result=snap.get("result"),
    )


@app.route("/admin/broadcast/status")
@admin_required
def broadcast_status():
    """JSON-снимок текущей рассылки — для live-прогресса на странице."""
    return jsonify(_broadcast_snapshot())


@app.route("/admin/broadcast/send", methods=["POST"])
@admin_required
def broadcast_send():
    text = (request.form.get("text") or "").strip()
    if len(text) < 3:
        return redirect(_with_flash(url_for("broadcast_page"), "Слишком короткое сообщение", "danger"))
    uids = notifier.subscribed_uids()
    if not uids:
        return redirect(_with_flash(url_for("broadcast_page"), "Нет активных подписчиков", "danger"))
    if not _start_broadcast(text, uids, _current_vk_id()):
        return redirect(_with_flash(
            url_for("broadcast_page"),
            "Рассылка уже идёт — дождись её завершения", "danger",
        ))
    return redirect(_with_flash(
        url_for("broadcast_page"),
        f"📤 Рассылка запущена для {len(uids)} подписчиков. Прогресс — ниже.",
        "success",
    ))


# ── Запуск ────────────────────────────────────────────────────────────────────

# ВАЖНО про масштабирование: _PENDING_UPLOADS, _LAST_BROADCAST и _ICS_CACHE —
# состояние в памяти процесса. Панель обязана работать РОВНО В ОДНОМ воркере,
# иначе загрузка расписания будет падать с «unknown or expired token», а
# прогресс рассылки — прыгать. Нужно масштабировать — сначала вынести это
# состояние в БД/Redis.

def _run_server(host: str, port: int, *, dev: bool = False) -> None:
    if dev:
        print(f"[dev] Панель запущена: http://{host}:{port}/")
        app.run(host=host, port=port, debug=False)
        return
    try:
        from waitress import serve
    except ImportError:
        print(
            "waitress не установлен (pip install waitress) — "
            "поднимаю dev-сервер Werkzeug, для прода так нельзя.",
            file=sys.stderr,
        )
        app.run(host=host, port=port, debug=False)
        return
    print(f"Панель запущена: http://{host}:{port}/ (waitress, 1 процесс)")
    serve(app, host=host, port=port, threads=8, ident="panel")


if __name__ == "__main__":
    port = _bot_config.int_env("PANEL_PORT", 5000)
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (ValueError, IndexError):
            pass
    host = os.getenv("PANEL_HOST", "127.0.0.1")
    _run_server(host, port, dev="--dev" in sys.argv)
