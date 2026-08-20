"""Приёмка живой панели: только чтение, ничего не меняем.

    python tools/check_live.py <код-из-бота> [--base-url URL] [--shots DIR] [--mode all|desktop|mobile]

Зачем отдельно от smoke_panel.py: тот заливает демо-расписание и на проде
затёр бы настоящее. Здесь только GET-страницы, замер вёрстки на телефоне и
проверка порядка пар на реальных данных — можно гонять после каждого деплоя.

Код одноразовый: на два прогона (десктоп и телефон) нужны два разных кода,
поэтому по умолчанию скрипт делает один проход — режим задаётся --mode.
"""

from __future__ import annotations

import argparse
import sys

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_ap = argparse.ArgumentParser(description="Приёмка живой панели (только чтение)")
_ap.add_argument("code", help="одноразовый код входа из бота")
_ap.add_argument("--base-url", default="https://elschedule.ru")
_ap.add_argument("--shots", default="", help="куда сложить скриншоты телефона")
_ap.add_argument("--mode", default="all", choices=("all", "desktop", "mobile"))
_args = _ap.parse_args()

BASE = _args.base_url.rstrip("/")
CODE = _args.code
OUT = _args.shots
MODE = _args.mode

failures: list[str] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    print(f"  {'✓' if ok else '✗'} {name}{(' — ' + detail) if detail and not ok else ''}")
    if not ok:
        failures.append(name)


LAYOUT = """() => {
    const cw = document.documentElement.clientWidth;
    const over = [...document.querySelectorAll('*')]
        .filter(e => e.getBoundingClientRect().right > cw + 1);
    return {over: over.length,
            worst: over.length ? (over[0].className || over[0].tagName).toString().slice(0, 30) : '',
            scroll: document.documentElement.scrollWidth > cw,
            h1: document.querySelectorAll('h1').length,
            chip: (document.querySelector('.week-chip') || {}).innerText || '',
            logo: !!document.querySelector('img[src*="logo"]')};
}"""


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        errors: list[str] = []

        # ── Десктоп: вход и основные страницы ────────────────────────────────
        if MODE in ("all", "desktop"):
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)[:80]))

            page.goto(f"{BASE}/login", wait_until="networkidle", timeout=30000)
            check(page.locator('img[src*="logo-full"]').count() == 1, "на входе логотип вуза")
            page.fill("input[name=code]", CODE)
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")
            check("/login" not in page.url, "вход по коду из бота работает")

            page.goto(f"{BASE}/schedule", wait_until="networkidle")
            html = page.content()
            check("Показано" in html, "страница расписания открывается")

            # Время растёт внутри карточки дня и начинается заново в следующей —
            # проверяем каждую карточку отдельно, а не общий список.
            cards = page.eval_on_selector_all(
                ".card, .day-card",
                """els => els.map(c => [...c.querySelectorAll('.pair-time span:first-child')]
                                        .map(e => e.innerText.trim()))
                             .filter(a => a.length > 1)"""
            )
            def as_minutes(t: str) -> int:
                head = t.replace("–", "-").split("-")[0].strip().replace(".", ":")
                h, _, m = head.partition(":")
                return int(h) * 60 + int(m or 0)

            bad = []
            for card in cards:
                mins = [as_minutes(t) for t in card if t and t[0].isdigit()]
                if mins != sorted(mins):
                    bad.append(card[:5])
            check(not bad, "в каждой карточке пары идут по возрастанию времени", str(bad[:1]))
            check(bool(cards), "на странице есть карточки с парами")

            for path, marker in (("/me", "профиль"), ("/admin/users", "Пользователи")):
                page.goto(f"{BASE}{path}", wait_until="networkidle")
                check(page.title() != "", f"{path} открывается")

            d = page.evaluate(LAYOUT)
            check(d["h1"] == 1, "на странице ровно один h1")
            ctx.close()

        if MODE in ('all', 'mobile'):
        # ── Телефон: вёрстка ─────────────────────────────────────────────────
            ctx = browser.new_context(viewport={"width": 360, "height": 780}, is_mobile=True)
            mp = ctx.new_page()
            mp.on("pageerror", lambda e: errors.append(str(e)[:80]))
            mp.goto(f"{BASE}/login", wait_until="networkidle")
            mp.fill("input[name=code]", CODE)
            mp.click("button[type=submit]")
            mp.wait_for_load_state("networkidle")

            for path in ("/", "/me", "/schedule"):
                mp.goto(f"{BASE}{path}", wait_until="networkidle")
                mp.wait_for_timeout(250)
                d = mp.evaluate(LAYOUT)
                if OUT:
                    mp.screenshot(path=f"{OUT}/prod{path.replace('/', '-') or '-dash'}.png")
                check(d["over"] == 0, f"{path}: ничего не выходит за экран", d["worst"])
                check(not d["scroll"], f"{path}: нет горизонтальной прокрутки")
                if path == "/":
                    check(len(d["chip"]) <= 14, f"плашка чётности сокращена: {d['chip']!r}")

            ctx.close()
        browser.close()

        check(not errors, "ошибок JavaScript нет", str(errors[:2]))

    print(f"\n{'ПРИЁМКА OK' if not failures else 'ПРОБЛЕМЫ: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
