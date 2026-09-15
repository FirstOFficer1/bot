"""Данные из внешних источников не попадают в места, где они исполняются.

Два таких места уже находили в бою. Имя выбранного файла подставлялось в
`innerHTML` — в Linux/macOS файл можно назвать `<img src=x onerror=...>.xlsx`.
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


def test_templates_were_found():
    """Если шаблоны переименуют, тест обязан упасть, а не молча опустеть."""
    assert len(_TEMPLATES) >= 5, f"шаблоны панели не найдены: {sorted(_TEMPLATES)}"


def test_no_inline_confirm_handlers():
    """Подтверждения — только через data-confirm."""
    offenders = [
        name for name, tpl in _TEMPLATES.items()
        if re.search(r"on\w+\s*=\s*\"[^\"]*confirm\(", tpl)
    ]

    assert not offenders, (
        f"inline confirm() вернулся в {offenders}: в on*-обработчике "
        "HTML-экранирования недостаточно, используй data-confirm"
    )


def test_no_template_variables_inside_inline_handlers():
    """В on*-обработчик не должно подставляться вообще ничего из шаблона."""
    offenders: list[str] = []
    for name, tpl in _TEMPLATES.items():
        for match in re.finditer(r"on\w+\s*=\s*\"([^\"]*)\"", tpl):
            if "{{" in match.group(1):
                offenders.append(f"{name}: {match.group(0)[:70]}")

    assert not offenders, (
        "шаблонная переменная внутри inline-обработчика — "
        f"браузер раскодирует сущности до разбора JS:\n" + "\n".join(offenders)
    )


def test_innerhtml_is_not_fed_with_data():
    """innerHTML допустим только с константой, но не со значением из данных."""
    offenders: list[str] = []
    for name, tpl in _TEMPLATES.items():
        for match in re.finditer(r"innerHTML\s*=\s*([^;\n]+)", tpl):
            expression = match.group(1)
            # Константа в кавычках без склейки — безопасно.
            if re.fullmatch(r"\s*'[^']*'\s*|\s*\"[^\"]*\"\s*", expression):
                continue
            offenders.append(f"{name}: innerHTML = {expression.strip()[:70]}")

    assert not offenders, (
        "в innerHTML попадает не константа — собирай узлы через "
        f"createElement/textContent:\n" + "\n".join(offenders)
    )


def test_upload_page_shows_filename_through_text_content():
    """Точечно закрепляем уже исправленное место: имя файла — через textContent."""
    upload = web_panel._UPLOAD_CONTENT

    assert "textContent = name" in upload.replace("badge.textContent = name", "textContent = name")
    assert "innerHTML" not in upload.split("function setName")[1][:400]
