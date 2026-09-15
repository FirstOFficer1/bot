"""Данные из внешних источников не попадают в места, где они исполняются.

Два таких места уже находили в бою. Имя выбранного файла подставлялось в
`innerHTML` — в Linux/macOS файл можно назвать так, что имя станет разметкой.
Названия направлений (они приходят из загруженного Excel) тоже уходили в
`innerHTML` и срабатывали у каждого, кто открыл дашборд и переключил курс.

Отдельная ловушка — inline-обработчики вида `onsubmit="confirm('…{{ x }}…')"`.
Там экранирования Jinja недостаточно: браузер декодирует сущности ДО того, как
значение компилируется как JS, поэтому `&#39;` снова становится кавычкой и
разрывает строку. Подтверждения переведены на `data-confirm`, который читается
через getAttribute и как код не разбирается.

Тест сторожевой, как `test_timestamps.py`: он не проверяет поведение, он не даёт
вернуть шаблон обратно.
"""

from __future__ import annotations

import re

import web_panel

# Шаблоны страниц панели живут в модуле строками.
_TEMPLATES = {
    name: value
    for name, value in vars(web_panel).items()
    if name.isupper() and isinstance(value, str) and "<" in value and len(value) > 200
}

# Граница слева обязательна: без неё `on\w+=` радостно находит `on` внутри
# `data-confirm=` и `content=`. На это тест уже попадался.
_INLINE_HANDLER_RE = re.compile(r"(?<![\w-])(on\w+)\s*=\s*\"([^\"]*)\"")
# (?<!:) — чтобы не съесть «https://…» вместе с остатком строки.
_JS_LINE_COMMENT_RE = re.compile(r"(?<!:)//[^\n]*")


def _without_js_comments(text: str) -> str:
    """Комментарии сами объясняют опасные приёмы — считать их не надо."""
    return _JS_LINE_COMMENT_RE.sub("", text)


def test_templates_were_found():
    """Если шаблоны переименуют, тест обязан упасть, а не молча опустеть."""
    assert len(_TEMPLATES) >= 5, f"шаблоны панели не найдены: {sorted(_TEMPLATES)}"


def test_no_inline_confirm_handlers():
    """Подтверждения — только через data-confirm."""
    offenders = [
        f"{name}: {m.group(0)[:70]}"
        for name, tpl in _TEMPLATES.items()
        for m in _INLINE_HANDLER_RE.finditer(_without_js_comments(tpl))
        if "confirm(" in m.group(2)
    ]

    assert not offenders, (
        "inline confirm() вернулся — в on*-обработчике HTML-экранирования "
        "недостаточно, используй data-confirm:\n" + "\n".join(offenders)
    )


def test_no_template_variables_inside_inline_handlers():
    """В on*-обработчик не должно подставляться ничего из шаблона."""
    offenders = [
        f"{name}: {m.group(0)[:70]}"
        for name, tpl in _TEMPLATES.items()
        for m in _INLINE_HANDLER_RE.finditer(_without_js_comments(tpl))
        if "{{" in m.group(2)
    ]

    assert not offenders, (
        "шаблонная переменная внутри inline-обработчика — браузер раскодирует "
        "сущности до разбора JS:\n" + "\n".join(offenders)
    )


def test_innerhtml_is_not_fed_with_data():
    """innerHTML допустим только с константой, но не со значением из данных."""
    offenders: list[str] = []
    for name, tpl in _TEMPLATES.items():
        for match in re.finditer(r"innerHTML\s*=\s*([^;\n]+)", _without_js_comments(tpl)):
            expression = match.group(1).strip()
            if re.fullmatch(r"'[^']*'|\"[^\"]*\"", expression):
                continue  # константа в кавычках без склейки
            offenders.append(f"{name}: innerHTML = {expression[:70]}")

    assert not offenders, (
        "в innerHTML попадает не константа — собирай узлы через "
        "createElement/textContent:\n" + "\n".join(offenders)
    )


def test_upload_page_builds_the_filename_through_the_dom():
    """Точечно закрепляем уже исправленное место: имя файла — не разметка."""
    body = _without_js_comments(web_panel._UPLOAD_CONTENT)
    set_name = body.split("function setName")[1][:500]

    assert "textContent = name" in set_name, "имя файла перестало идти через textContent"
    assert "innerHTML" not in set_name, "innerHTML вернулся в показ имени файла"


def test_direction_selector_builds_options_through_the_dom():
    """Названия направлений приходят из Excel — только createElement."""
    dashboard = _without_js_comments(web_panel._DASHBOARD_CONTENT)

    assert "createElement('option')" in dashboard
    assert "innerHTML" not in dashboard, "селектор направлений снова собирается строкой"
