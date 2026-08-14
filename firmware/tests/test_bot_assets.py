"""
Тесты для firmware/src/telegram_bot/asset_helpers.py и bot._send_images.

Покрывают точечный fallback изображений по caption:
  - «Таблица 19 …» → только ГОСТ 31996 asset image/table_22.png
  - «Таблица Д.1» (приложение) → однозначный asset
  - отсутствие номера → картинки не отправляются
  - неоднозначная ссылка (несколько разных путей) → warning, пачка не отправляется
  - cited_chunk_ids не пуст → старое поведение сохраняется

Запуск (из корня репозитория):
    python -m pytest firmware/tests/test_bot_assets.py -v
"""
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest

# bot.py на этапе импорта проверяет TELEGRAM_BOT_TOKEN и делает sys.exit(1),
# если он не задан — для тестов подставляем фиктивный токен.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

SRC = Path(__file__).resolve().parents[1] / "src"
BOT_DIR = SRC / "telegram_bot"

# Каталог src нужен для импорта qa_graph (лениво, из get_qa), а telegram_bot —
# для `from asset_helpers import ...` внутри bot.py.
for p in (str(SRC), str(BOT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, BOT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None, f"cannot load {name}.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


helpers = _load("asset_helpers")
bot = _load("bot")


# ─── Извлечение ссылок из ответа ───────────────────────────────────────

def test_extract_references_table_variants():
    answer = (
        "В таблице 19 приведены допустимые токи. "
        "См. также табл. Д.1 и Таблицу 3."
    )
    assert helpers.extract_asset_references(answer) == [
        ("table", "19"),
        ("table", "д.1"),
        ("table", "3"),
    ]


def test_extract_references_figure_variants():
    answer = "На рис. 2 показана схема, рисунок 3 — детали, fig. 4 и figure 5."
    assert helpers.extract_asset_references(answer) == [
        ("image", "2"),
        ("image", "3"),
        ("image", "4"),
        ("image", "5"),
    ]


def test_extract_references_english_table():
    assert helpers.extract_asset_references("see Table 19 in GOST") == [
        ("table", "19"),
    ]


def test_extract_references_no_number_no_match():
    # «таблица» без номера — ссылка не извлекается
    assert helpers.extract_asset_references("В таблице приведены данные.") == []
    assert helpers.extract_asset_references("") == []
    assert helpers.extract_asset_references(None) == []


def test_extract_references_dedup():
    answer = "таблица 19 ... таблице 19 ... рис. 2 ... рис 2"
    assert helpers.extract_asset_references(answer) == [
        ("table", "19"),
        ("image", "2"),
    ]


# ─── Номер из подписи ассета ───────────────────────────────────────────

def test_caption_number_table():
    assert helpers.asset_caption_number("table", "Таблица 19 — Допустимые токовые нагрузки кабелей") == "19"
    assert helpers.asset_caption_number("table", "Таблица 1") == "1"
    assert helpers.asset_caption_number("table", "Таблица Д.1") == "д.1"
    assert helpers.asset_caption_number("table", "Таблица 13.1") == "13.1"
    assert helpers.asset_caption_number("table", "") is None
    assert helpers.asset_caption_number("table", "Просто подпись") is None


def test_caption_number_image():
    assert helpers.asset_caption_number("image", "fig_1") == "1"
    assert helpers.asset_caption_number("image", "Рисунок 3 — Схема") == "3"
    assert helpers.asset_caption_number("image", "") is None


# ─── Точечный fallback: resolve_images_to_send ─────────────────────────

def _mk_result(doc_dir, assets, chunk_id="c1"):
    return {
        "chunk_id": chunk_id,
        "document_id": "doc",
        "doc_dir": doc_dir,
        "assets": assets,
    }


GOST_DOC = "/mnt/docs/ГОСТ 31996-2012 Кабели"
SP_DOC = "/mnt/docs/СП 89.13330.2016 Котельные установки"

GOST_RESULTS = [
    _mk_result(GOST_DOC, [
        {"asset_type": "table", "caption": "Таблица 18 — Допустимые температуры нагрева токопроводящих жил", "image_path": "image/table_23.png"},
        {"asset_type": "table", "caption": "Таблица 19 — Допустимые токовые нагрузки кабелей с медными жилами", "image_path": "image/table_22.png"},
        {"asset_type": "table", "caption": "Таблица 20 — Допустимые токовые нагрузки кабелей со сшитым полиэтиленом", "image_path": "image/table_24.png"},
        {"asset_type": "image", "caption": "fig_1", "image_path": "image/fig_1.png"},
    ]),
]


def test_table_19_selects_only_gost_table_22():
    """Запрос «Покажи таблицу 19 допустимых длительных токов кабелей»
    должен выбрать ТОЛЬКО ГОСТ 31996 asset image/table_22.png."""
    answer = "В таблице 19 приведены допустимые длительные токи кабелей по ГОСТ 31996."
    paths, warnings = helpers.resolve_images_to_send(GOST_RESULTS, answer)
    assert warnings == []
    assert paths == [f"{GOST_DOC}/image/table_22.png"]


def test_table_19_query_fallback_when_answer_has_no_refs():
    """Если в ответе нет явной ссылки, но она есть в запросе — используем запрос."""
    answer = "Допустимые токи приведены в соответствующей таблице документа."
    query = "Покажи таблицу 19 допустимых длительных токов кабелей"
    paths, warnings = helpers.resolve_images_to_send(GOST_RESULTS, answer, query)
    assert warnings == []
    assert paths == [f"{GOST_DOC}/image/table_22.png"]


def test_appendix_table_d1():
    """«Таблица Д.1» (приложение) — однозначный asset по caption."""
    sp_results = [
        _mk_result(SP_DOC, [
            {"asset_type": "table", "caption": "Таблица Г.2", "image_path": "image/table_7.png"},
            {"asset_type": "table", "caption": "Таблица Д.1", "image_path": "image/table_11.png"},
            {"asset_type": "table", "caption": "Таблица Ж.1", "image_path": "image/table_14.png"},
        ]),
    ]
    paths, warnings = helpers.resolve_images_to_send(
        sp_results, "Минимальные расстояния указаны в таблице Д.1."
    )
    assert warnings == []
    assert paths == [f"{SP_DOC}/image/table_11.png"]


def test_missing_number_no_images():
    """Номер не найден (ни в ответе, ни в запросе) — картинки не отправляются."""
    paths, warnings = helpers.resolve_images_to_send(
        GOST_RESULTS, "В таблице приведены допустимые токи."
    )
    assert paths == []
    assert warnings == []


def test_number_not_found_no_images():
    """Ссылка с номером, которого нет среди ассетов — warning, картинки нет."""
    paths, warnings = helpers.resolve_images_to_send(
        GOST_RESULTS, "Данные приведены в таблице 99."
    )
    assert paths == []
    assert len(warnings) == 1
    assert "99" in warnings[0]


def test_ambiguous_two_different_paths_no_batch():
    """Один номер → ассеты из РАЗНЫХ документов: warning, пачка не отправляется."""
    mixed_results = GOST_RESULTS + [
        _mk_result(SP_DOC, [
            {"asset_type": "table", "caption": "Таблица 19 — Другие данные", "image_path": "image/table_9.png"},
        ], chunk_id="c2"),
    ]
    paths, warnings = helpers.resolve_images_to_send(
        mixed_results, "По ГОСТ и СП данные в таблице 19."
    )
    assert paths == []
    assert len(warnings) == 1
    assert "Неоднозначная ссылка" in warnings[0]


def test_same_path_duplicates_not_ambiguous():
    """Один и тот же путь из нескольких чанков — не неоднозначность."""
    results = GOST_RESULTS + [
        _mk_result(GOST_DOC, [
            {"asset_type": "table", "caption": "Таблица 19 — Допустимые токовые нагрузки кабелей с медными жилами", "image_path": "image/table_22.png"},
        ], chunk_id="c2"),
    ]
    paths, warnings = helpers.resolve_images_to_send(
        results, "Смотри таблицу 19."
    )
    assert warnings == []
    assert paths == [f"{GOST_DOC}/image/table_22.png"]


def test_multiple_unique_refs_send_all():
    answer = "Таблица 19 и рисунок 1 даны ниже."
    paths, warnings = helpers.resolve_images_to_send(GOST_RESULTS, answer)
    assert warnings == []
    assert paths == [
        f"{GOST_DOC}/image/table_22.png",
        f"{GOST_DOC}/image/fig_1.png",
    ]


# ─── Поведение bot._send_images ────────────────────────────────────────

class FakeMessage:
    def __init__(self):
        self.sent = []  # [(bytes, caption)]

    async def reply_photo(self, photo, caption=None):
        self.sent.append((photo.read(), caption))


class FakeUpdate:
    def __init__(self):
        self.message = FakeMessage()


def _write_assets(tmp_path, files):
    """Создаёт файлы вида {'image/table_22.png': b'...'} в tmp_path/doc."""
    doc = tmp_path / "doc"
    for rel, content in files.items():
        p = doc / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return doc


def test_send_images_empty_cited_sends_only_resolved(tmp_path):
    doc = _write_assets(tmp_path, {
        "image/table_22.png": b"PNG22",
        "image/table_24.png": b"PNG24",
        "image/fig_1.png": b"PNGFIG1",
    })
    results = [
        _mk_result(str(doc), [
            {"asset_type": "table", "caption": "Таблица 19 — Допустимые токовые нагрузки кабелей с медными жилами", "image_path": "image/table_22.png"},
            {"asset_type": "table", "caption": "Таблица 20 — Допустимые токовые нагрузки кабелей", "image_path": "image/table_24.png"},
            {"asset_type": "image", "caption": "fig_1", "image_path": "image/fig_1.png"},
        ]),
    ]
    upd = FakeUpdate()
    asyncio.run(bot._send_images(
        upd, results, "Покажи таблицу 19",
        cited_chunk_ids=[], answer="В таблице 19 приведены допустимые длительные токи кабелей.",
    ))
    assert [c for _, c in upd.message.sent] == ["table_22.png"]


def test_send_images_empty_cited_no_refs_sends_nothing(tmp_path):
    doc = _write_assets(tmp_path, {"image/table_22.png": b"PNG22"})
    results = [
        _mk_result(str(doc), [
            {"asset_type": "table", "caption": "Таблица 19 — Допустимые токовые нагрузки", "image_path": "image/table_22.png"},
        ]),
    ]
    upd = FakeUpdate()
    asyncio.run(bot._send_images(
        upd, results, "Какие токи допустимы?",
        cited_chunk_ids=[], answer="Допустимые токи приведены в таблице документа.",
    ))
    assert upd.message.sent == []


def test_send_images_cited_keeps_old_behavior(tmp_path):
    """cited_chunk_ids не пуст → старое поведение: только процитированные чанки."""
    doc = _write_assets(tmp_path, {
        "image/table_1.png": b"PNGT1",
        "image/fig_1.png": b"PNGF1",
    })
    results = [
        _mk_result(str(doc), [
            {"asset_type": "table", "caption": "Таблица 1", "image_path": "image/table_1.png"},
        ], chunk_id="c1"),
        _mk_result(str(doc), [
            {"asset_type": "image", "caption": "fig_1", "image_path": "image/fig_1.png"},
        ], chunk_id="c2"),
    ]
    upd = FakeUpdate()
    # процитирован только c2, запрос про рисунок → отправляется fig_1.png
    asyncio.run(bot._send_images(
        upd, results, "покажи рисунок",
        cited_chunk_ids=["c2"], answer="см. рисунок.",
    ))
    assert [c for _, c in upd.message.sent] == ["fig_1.png"]
