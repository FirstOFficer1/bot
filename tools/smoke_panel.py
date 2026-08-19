"""Сквозная smoke-проверка веб-панели в реальном браузере (Playwright).

Проходит путь администратора целиком: вход по одноразовому коду → дашборд →
загрузка Excel → превью → применение → расписание → служебные страницы.
Падает с непустым кодом возврата, если что-то на пути сломалось.

Подготовка (один раз):
    pip install playwright && playwright install chromium

Запуск (панель должна быть уже поднята):
    python web_panel.py &
    python tools/smoke_panel.py --base-url http://127.0.0.1:5000

    --headed      показать браузер
    --shots DIR   куда складывать скриншоты шагов

Шаг «неверный код» намеренно тратит одну попытку из per-IP лимита панели
(10 за 10 минут), а счётчик живёт в памяти процесса панели и снаружи не
сбрасывается. Больше десяти прогонов подряд с одного адреса за десять минут —
и smoke провалит собственный вход; подождите окно или перезапустите панель.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Консоль Windows по умолчанию cp1251 и падает на «✓»/«✗» в выводе.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import Page, sync_playwright  # noqa: E402

from vkbot.models import panel_codes  # noqa: E402

FAILURES: list[str] = []
STEPS: list[str] = []


def step(name: str) -> None:
    STEPS.append(name)
    print(f"  ✓ {name}")


def check(condition: bool, name: str) -> None:
    if condition:
        step(name)
    else:
        FAILURES.append(name)
        print(f"  ✗ {name}")


def shot(page: Page, shots_dir: Path | None, name: str) -> None:
    if shots_dir:
        shots_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(shots_dir / f"{name}.png"), full_page=True)


def run(base_url: str, headed: bool, shots_dir: Path | None, excel: Path) -> int:
    admin_id = int(os.getenv("ADMIN_ID", "0"))
    if not admin_id:
        print("ADMIN_ID не задан в .env — не с кем логиниться", file=sys.stderr)
        return 2

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        page.set_default_timeout(15_000)

        # ── 1. Аноним не пускается дальше логина ─────────────────────────────
        page.goto(f"{base_url}/")
        check("/login" in page.url, "аноним редиректится на /login")
        shot(page, shots_dir, "01-login")

        # ── 2. Неверный код отклоняется ──────────────────────────────────────
        page.fill("input[name=code]", "000000")
        page.click("button[type=submit]")
        page.wait_for_load_state()
        check("error" in page.url or "Неверный" in page.content(),
              "неверный код отклонён с сообщением")

        # ── 3. Вход по настоящему одноразовому коду от бота ──────────────────
        code, _ttl = panel_codes.issue(admin_id)
        page.goto(f"{base_url}/login")
        page.fill("input[name=code]", code)
        page.click("button[type=submit]")
        page.wait_for_load_state()
        check(page.url.rstrip("/") == base_url.rstrip("/"), "вход по коду → дашборд")
        shot(page, shots_dir, "02-dashboard")

        # ── 4. Тот же код повторно не работает ───────────────────────────────
        ctx2 = browser.new_context()
        p2 = ctx2.new_page()
        p2.goto(f"{base_url}/login")
        p2.fill("input[name=code]", code)
        p2.click("button[type=submit]")
        p2.wait_for_load_state()
        check("/login" in p2.url, "код одноразовый: повторный вход отклонён")
        ctx2.close()

        # ── 5. Загрузка расписания: превью ───────────────────────────────────
        page.goto(f"{base_url}/upload")
        check("Загрузить расписание" in page.content(), "страница загрузки открылась")
        page.set_input_files("input[name=excel_file]", str(excel))
        page.click("#upload-form button[type=submit], #upload-form button")
        page.wait_for_load_state()
        body = page.content()
        check("Применить" in body, "превью посчитано, есть кнопка «Применить»")
        check("name=\"token\"" in body or 'name="token"' in body,
              "выдан токен отложенной загрузки")
        shot(page, shots_dir, "03-upload-preview")

        # ── 6. Применение (без рассылки в VK) ────────────────────────────────
        notify = page.locator("#notify_check")
        if notify.count() and notify.is_checked():
            notify.uncheck()
        page.click("button:has-text('Применить')")
        page.wait_for_load_state()
        committed = page.content()
        check(
            "Не удалось применить" not in committed and "Traceback" not in committed,
            "применение прошло без ошибки",
        )
        shot(page, shots_dir, "04-upload-committed")

        # ── 7. Расписание видно на странице ──────────────────────────────────
        page.goto(f"{base_url}/schedule")
        content = page.content()
        check("Математический анализ" in content or "Базы данных" in content,
              "импортированные пары показываются в /schedule")
        shot(page, shots_dir, "05-schedule")

        # ── 8. Служебные страницы владельца ──────────────────────────────────
        for path, marker, label in (
            ("/admin/users", "Пользователи", "/admin/users открывается"),
            ("/admin/admins", "админ", "/admin/admins открывается"),
            ("/admin/audit", "удит", "/admin/audit открывается"),
            ("/admin/conflicts", "онфликт", "/admin/conflicts открывается"),
            ("/me", "рофиль", "/me открывается"),
        ):
            page.goto(f"{base_url}{path}")
            check(page.status if False else marker in page.content(), label)
        shot(page, shots_dir, "06-admin-users")

        # ── 9. Аудит записал вход и загрузку ────────────────────────────────
        page.goto(f"{base_url}/admin/audit")
        audit_html = page.content()
        check("auth.login" in audit_html, "аудит: зафиксирован вход")
        check("schedule.upload" in audit_html, "аудит: зафиксирована загрузка расписания")
        shot(page, shots_dir, "07-audit")

        # ── 10. Календарь .ics отдаётся ─────────────────────────────────────
        resp = page.request.get(f"{base_url}/calendar.ics")
        check(resp.ok and "BEGIN:VCALENDAR" in resp.text(), "/calendar.ics валиден")

        # ── 11. Выход ───────────────────────────────────────────────────────
        page.goto(f"{base_url}/")
        page.click("button:has-text('Выйти')")
        page.wait_for_load_state()
        check("/login" in page.url, "выход работает")

        browser.close()

    print()
    if FAILURES:
        print(f"SMOKE FAILED: {len(FAILURES)} из {len(STEPS) + len(FAILURES)} проверок")
        for f in FAILURES:
            print(f"  ✗ {f}")
        return 1
    print(f"SMOKE OK: все {len(STEPS)} проверок прошли")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:5000")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--shots")
    ap.add_argument("--excel", default="demo_schedule.xlsx")
    args = ap.parse_args()

    excel_path = Path(args.excel)
    if not excel_path.exists():
        print(f"Нет файла расписания {excel_path}. Сгенерируй: "
              f"python tools/make_demo_schedule.py {excel_path}", file=sys.stderr)
        sys.exit(2)

    sys.exit(run(
        args.base_url,
        args.headed,
        Path(args.shots) if args.shots else None,
        excel_path.resolve(),
    ))
