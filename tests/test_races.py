"""Гонки при четырёх потоках панели.

Панель работает под gunicorn с `-w 1 --threads 4`, то есть параллельные запросы
внутри одного процесса — норма, а не экзотика. Три места этого не учитывали:

* одноразовый код входа проверялся `SELECT`-ом, а гасился отдельным `UPDATE`;
* токен отложенной загрузки читался через `get()` и удалялся лишь в `finally`,
  так что всё время импорта окно было открыто;
* имя архива версии складывалось из секунды и имени файла и потому повторялось.

Тесты бьют по ним настоящей конкуренцией: без реальных потоков такая проверка
проходит всегда и ничего не доказывает.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from vkbot.models import panel_codes
from vkbot.schedule import loader

UID = 8008


# ── Одноразовый код входа ────────────────────────────────────────────────────

def test_code_is_redeemed_by_exactly_one_thread():
    """Раньше два потока успевали прочитать один непогашенный код и оба входили."""
    code, _ttl = panel_codes.issue(UID)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: panel_codes.verify(code), range(8)))

    winners = [r for r in results if r is not None]
    assert winners == [UID], f"код сработал {len(winners)} раз вместо одного"


def test_code_still_works_in_the_simple_case():
    """Починка не должна ломать обычный вход: первый раз пускает, второй — нет."""
    code, _ttl = panel_codes.issue(UID)

    assert panel_codes.verify(code) == UID
    assert panel_codes.verify(code) is None


def test_wrong_code_is_rejected():
    panel_codes.issue(UID)
    assert panel_codes.verify("00000000") is None


# ── Токен отложенной загрузки ────────────────────────────────────────────────

def test_pending_upload_is_claimed_by_exactly_one_thread(tmp_path):
    """Второй одновременный коммит не должен применить ту же загрузку повторно."""
    import web_panel

    tmp_file = tmp_path / "schedule.xlsx"
    tmp_file.write_bytes(b"x")
    web_panel._add_pending("tok", str(tmp_file), "schedule.xlsx")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: web_panel._claim_pending("tok"), range(8)))

    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1, f"загрузку забрали {len(claimed)} раз вместо одного"


def test_claiming_does_not_delete_the_file_of_the_winner(tmp_path):
    """Файл нужен победителю — удаляет его вызывающий, когда закончит."""
    import web_panel

    tmp_file = tmp_path / "schedule.xlsx"
    tmp_file.write_bytes(b"x")
    web_panel._add_pending("tok2", str(tmp_file), "schedule.xlsx")

    item = web_panel._claim_pending("tok2")

    assert item is not None
    assert tmp_file.exists(), "файл удалили из-под того, кто выиграл гонку"
    web_panel._discard_tmp(str(tmp_file))
    assert not tmp_file.exists()


# ── Имя архива версии ────────────────────────────────────────────────────────

def test_version_archive_never_reuses_a_name():
    """Одинаковые секунда и имя файла обязаны дать разные архивы."""
    paths = [loader._reserve_version_file("20260915_120000", "rasp.xlsx") for _ in range(5)]

    assert len(set(paths)) == 5, "имя архива повторилось — версия затёрла бы чужую"
    assert all(p.exists() for p in paths)
    for p in paths:
        p.unlink()


def test_version_archive_keeps_extension_and_timestamp():
    """Имя остаётся читаемым: время впереди, расширение на месте."""
    first = loader._reserve_version_file("20260915_120000", "rasp.xlsx")
    second = loader._reserve_version_file("20260915_120000", "rasp.xlsx")

    assert first.name == "20260915_120000_rasp.xlsx"
    assert second.name.startswith("20260915_120000_rasp-")
    assert second.suffix == ".xlsx"
    first.unlink()
    second.unlink()


def test_version_archive_survives_concurrent_reservations():
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(
            pool.map(
                lambda _: loader._reserve_version_file("20260915_130000", "same.xlsx"),
                range(8),
            )
        )

    assert len(set(paths)) == 8
    for p in paths:
        p.unlink()
