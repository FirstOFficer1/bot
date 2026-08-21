"""Проверка живого Mini App: открывается ли фрейм внутри VK и уходит ли init.

    python tools/check_miniapp.py [--base-url URL] [--headed]

Отвечает на вопрос, который не виден ни из тестов, ни из curl: VK встраивает
панель в iframe и ждёт от неё сообщение `VKWebAppInit`. Не дождался — показывает
пустой экран и «Приложение не инициализировано». Сломать это может что угодно
на пути: заголовок CSP (в том числе второй, добавленный nginx — браузер применяет
обе политики сразу), недоступная библиотека, ошибка в разметке.

Скрипт поднимает поддельную родительскую страницу на домене VK, встраивает в неё
настоящую панель и слушает postMessage. Логин не нужен — до входа страница уже
обязана инициализироваться.

Проверяются оба домена: VK отдаёт веб и с vk.com, и с vk.ru, и именно забытый
vk.ru однажды блокировал приложение при живом и здоровом vk.com.

Только чтение: ни одного запроса, меняющего данные.
"""

from __future__ import annotations

import argparse
import sys

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ap = argparse.ArgumentParser(description="Приёмка VK Mini App (только чтение)")
_ap.add_argument("--base-url", default="https://elschedule.ru")
_ap.add_argument("--headed", action="store_true", help="показать браузер")
_ap.add_argument("--timeout", type=int, default=8000, help="сколько ждать init, мс")
_args = _ap.parse_args()

BASE = _args.base_url.rstrip("/")
PARENTS = ("https://vk.ru/__miniapp_probe", "https://vk.com/__miniapp_probe")

# Chromium режет запросы из документа, отданного перехватом, в «публичную» сеть:
# без этого iframe не загрузится вообще и проверка ничего не скажет о панели.
LAUNCH_ARGS = ["--disable-features=LocalNetworkAccessChecks,PrivateNetworkAccessChecks"]

PARENT_HTML = """<!doctype html>
<meta charset="utf-8"><title>проверка Mini App</title>
<script>
  window.__msgs = [];
  addEventListener("message", function (e) { window.__msgs.push(e.data); });
</script>
<iframe src="{src}" style="width:100%;height:96vh;border:0"></iframe>
"""

failures: list[str] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}{(' — ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(name)


def probe(browser, parent_url: str, target: str) -> None:
    page = browser.new_page()
    console_errors: list[str] = []
    load_failures: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("requestfailed",
            lambda r: load_failures.append(f"{r.url} — {r.failure}"))
    page.route(parent_url, lambda route: route.fulfill(
        status=200, content_type="text/html; charset=utf-8",
        body=PARENT_HTML.replace("{src}", target)))

    page.goto(parent_url, wait_until="load")
    try:
        page.wait_for_function(
            "window.__msgs.some(m => m && m.handler === 'VKWebAppInit')",
            timeout=_args.timeout,
        )
        got_init = True
    except Exception:
        got_init = False

    framed = any(f.url.startswith(BASE) for f in page.frames)
    csp_error = next((e for e in console_errors if "Content Security Policy" in e), "")

    check(framed, f"{parent_url.split('/')[2]}: фрейм открылся",
          csp_error[:120] or (load_failures[0][:120] if load_failures else "фрейм не загрузился"))
    check(got_init, f"{parent_url.split('/')[2]}: VKWebAppInit дошёл до VK",
          "VK покажет «Приложение не инициализировано»")
    page.close()


def main() -> int:
    target = f"{BASE}/login"
    print(f"Mini App: {target}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not _args.headed, args=LAUNCH_ARGS)
        try:
            for parent in PARENTS:
                probe(browser, parent, target)
        finally:
            browser.close()

    if failures:
        print(f"\nПровалено: {len(failures)} — {', '.join(failures)}")
        return 1
    print("\nВсё хорошо: внутри VK приложение встраивается и инициализируется.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
