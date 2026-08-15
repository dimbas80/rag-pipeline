"""
Тесты для firmware/src/telegram_bot/asset_helpers.py и bot._send_images.

Покрывают точечный fallback изображений по caption:
  - «Таблица 19 …» → только ГОСТ 31996 asset image/table_22.png
  - «Таблица Д.1» (приложение) → однозначный asset
  - отсутствие номера → картинки не отправляются
  - неоднозначная ссылка (несколько разных путей) → warning, пачка не отправляется
  - cited_chunk_ids не пуст → старое поведение сохраняется
  - многостраничные таблицы: image_paths из N путей в одном документе →
    отправляются все N страниц; query с номером документа сужает выбор

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


# ─── Удаление Markdown-таблиц из текста ────────────────────────────────

def test_strip_markdown_table_without_caption():
    text = "До\n| A | B |\n|---|:---:|\n| 1 | 2 |\nПосле"
    assert helpers.strip_markdown_tables(text) == (
        "До\n(таблица — см. прикреплённое изображение)\nПосле"
    )


def test_strip_markdown_table_keeps_caption_number():
    text = "Таблица 19 — Токи\n| A | B |\n|---|---|\n| 1 | 2 |"
    assert helpers.strip_markdown_tables(text) == (
        "Таблица 19 — см. прикреплённое изображение"
    )


def test_strip_markdown_tables_multiple_and_preserves_citations():
    text = (
        "[Документ, табл. 1]\n| A | B |\n|---|---|\n| 1 | 2 |\n\n"
        "Текст [Документ, п. 2]\n| X | Y |\n|:---|---:|\n| x | y |"
    )
    result = helpers.strip_markdown_tables(text)
    assert "[Документ, табл. 1]" in result
    assert "[Документ, п. 2]" in result
    assert result.count("см. прикреплённое изображение") == 2


def test_strip_markdown_tables_leaves_plain_text_unchanged():
    text = "Обычный текст [Документ, табл. 1]"
    assert helpers.strip_markdown_tables(text) == text


# ─── Intent detection for document listing ─────────────────────────────

def test_is_document_list_request_positive_and_negative():
    positives = [
        "Какие документы в базе?",
        "Перечень документов",
        "Список документов",
        "Что загружено?",
        "Какие ГОСТ/СП есть в базе",
        "Перечисли документы",
        "Какие нормативы в базе",
    ]
    negatives = ["Какой допустимый ток кабеля?", "Что требует СП 89.13330?", "Покажи таблицу 19"]
    assert all(helpers.is_document_list_request(value) for value in positives)
    assert not any(helpers.is_document_list_request(value) for value in negatives)


def test_is_document_list_request_content_query_not_false_positive():
    """Контент-запросы «какие документы/нормативы регламентируют …» не должны
    распознаваться как запрос списка документов (фикс ложного позитива)."""
    content_queries = [
        "Какие документы регламентируют молниезащиту?",
        "Какие нормативные документы регламентируют молниезащиту?",
        "Какие нормативы регламентируют молниезащиту?",
        "Какие документы требуют заземление оборудования?",
        "Какие документы определяют категорию надёжности электроснабжения?",
    ]
    for value in content_queries:
        assert helpers.is_document_list_request(value) is False, value


# ─── Форматирование перечня документов (format_document_list) ─────────

def test_format_document_list_empty_and_none():
    """Пустой список и None → «База пуста / не удалось получить список»."""
    empty_msg = "База пуста / не удалось получить список"
    assert helpers.format_document_list([]) == empty_msg
    assert helpers.format_document_list(None) == empty_msg


def test_format_document_list_multiple_documents():
    """Несколько документов → заголовок с верным N и строки «• id — title»."""
    documents = [
        {"document_id": "gost_31996", "title": "ГОСТ 31996-2012"},
        {"document_id": "sp_89", "title": "СП 89.13330.2016"},
        {"document_id": "so_153", "title": "СО 153-34.21.122-2003"},
    ]
    result = helpers.format_document_list(documents)
    lines = result.splitlines()
    assert lines[0] == "📚 Документы в базе (3):"
    assert lines[1] == "• gost_31996 — ГОСТ 31996-2012"
    assert lines[2] == "• sp_89 — СП 89.13330.2016"
    assert lines[3] == "• so_153 — СО 153-34.21.122-2003"


def test_format_document_list_single_document():
    """Один документ → заголовок «(1)» и ровно одна строка перечня."""
    result = helpers.format_document_list(
        [{"document_id": "gost_31996", "title": "ГОСТ 31996-2012"}]
    )
    lines = result.splitlines()
    assert lines[0] == "📚 Документы в базе (1):"
    assert lines[1] == "• gost_31996 — ГОСТ 31996-2012"


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


def _write_assets(tmp_path, files, name="doc"):
    """Создаёт файлы вида {'image/table_22.png': b'...'} в tmp_path/<name>."""
    doc = tmp_path / name
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
    asyncio.run(bot._send_images(
        upd, results, "покажи рисунок",
        cited_chunk_ids=["c2"], answer="см. рисунок.",
    ))
    assert [c for _, c in upd.message.sent] == ["fig_1.png"]


# ─── Фикс 3.2: приоритет явной ссылки в QUERY над cited_chunk_ids ─────

def _mk_sp89_chunk(doc_dir, chunk_id="sp89_kotelnye/_h62"):
    """Чанк СП 89 с двумя соседними таблицами (Е.1 и Д.1) — как в _h62."""
    return _mk_result(doc_dir, [
        {"asset_type": "table", "caption": "Таблица Д.1", "image_path": "image/table_11.png"},
        {"asset_type": "table", "caption": "Таблица Е.1", "image_path": "image/table_12.png"},
    ], chunk_id=chunk_id)


def test_query_ref_priority_over_cited_sends_only_one(tmp_path):
    """«покажи таблицу Е.1» при cited_chunk_ids=[_h62] (в чанке Е.1 И Д.1)
    → отправляется ТОЛЬКО Е.1 (table_12.png), соседняя Д.1 — нет."""
    doc = _write_assets(tmp_path, {
        "image/table_11.png": b"PNGD1",
        "image/table_12.png": b"PNGE1",
    })
    results = [_mk_sp89_chunk(str(doc))]
    upd = FakeUpdate()
    asyncio.run(bot._send_images(
        upd, results, "покажи таблицу Е.1 СП 89.13330",
        cited_chunk_ids=["sp89_kotelnye/_h62"], answer="Вот таблица.",
    ))
    assert [c for _, c in upd.message.sent] == ["table_12.png"]


def test_query_ref_priority_d1_sends_only_d1(tmp_path):
    """«покажи таблицу Д.1» из того же чанка → только Д.1 (table_11.png)."""
    doc = _write_assets(tmp_path, {
        "image/table_11.png": b"PNGD1",
        "image/table_12.png": b"PNGE1",
    })
    results = [_mk_sp89_chunk(str(doc))]
    upd = FakeUpdate()
    asyncio.run(bot._send_images(
        upd, results, "покажи таблицу Д.1 СП 89",
        cited_chunk_ids=["sp89_kotelnye/_h62"], answer="Вот таблица.",
    ))
    assert [c for _, c in upd.message.sent] == ["table_11.png"]


def test_query_ref_ambiguous_fail_closed(tmp_path):
    """Один номер → ассеты из РАЗНЫХ документов: картинки НЕ отправляются
    (fail closed), даже когда cited_chunk_ids не пуст."""
    doc1 = _write_assets(tmp_path, {"image/table_19.png": b"PNG19"}, name="doc1")
    doc2 = _write_assets(tmp_path, {"image/table_19b.png": b"PNG19B"}, name="doc2")
    results = [
        _mk_result(str(doc1), [
            {"asset_type": "table", "caption": "Таблица 19 — ГОСТ", "image_path": "image/table_19.png"},
        ], chunk_id="c1"),
        _mk_result(str(doc2), [
            {"asset_type": "table", "caption": "Таблица 19 — СП", "image_path": "image/table_19b.png"},
        ], chunk_id="c2"),
    ]
    upd = FakeUpdate()
    asyncio.run(bot._send_images(
        upd, results, "покажи таблицу 19",
        cited_chunk_ids=["c1", "c2"], answer="Данные в таблице 19.",
    ))
    assert upd.message.sent == []


def test_query_ref_not_found_fail_closed(tmp_path):
    """Ссылка с номером, которого нет среди ассетов → картинки НЕ отправляются."""
    doc = _write_assets(tmp_path, {"image/table_11.png": b"PNGD1"})
    results = [_mk_sp89_chunk(str(doc))]
    upd = FakeUpdate()
    asyncio.run(bot._send_images(
        upd, results, "покажи таблицу Ж.5 СП 89",
        cited_chunk_ids=["sp89_kotelnye/_h62"], answer="Вот таблица.",
    ))
    assert upd.message.sent == []


def test_no_refs_keeps_cited_behavior(tmp_path):
    """Явных ссылок нет ни в query, ни в answer → старое cited-поведение
    (все asset'ы процитированных чанков по интенту)."""
    doc = _write_assets(tmp_path, {
        "image/table_11.png": b"PNGD1",
        "image/table_12.png": b"PNGE1",
    })
    results = [_mk_sp89_chunk(str(doc))]
    upd = FakeUpdate()
    # «таблица» в запросе есть, но БЕЗ номера → явная ссылка не извлекается
    asyncio.run(bot._send_images(
        upd, results, "какие таблицы в приложении Д и Е?",
        cited_chunk_ids=["sp89_kotelnye/_h62"], answer="См. приложение.",
    ))
    assert [c for _, c in upd.message.sent] == ["table_11.png", "table_12.png"]


# ─── Фикс 3.3: обработка __interrupt__ (уточняющий вопрос) ────────────

class _FakeInterrupt:
    def __init__(self, value):
        self.value = value


def test_interrupt_reply_text_returns_question():
    result = {"__interrupt__": [_FakeInterrupt("Уточните номер документа")]}
    assert bot._interrupt_reply_text(result) == "Уточните номер документа"


def test_interrupt_reply_text_none_without_interrupt():
    assert bot._interrupt_reply_text({"final_answer": "ответ"}) is None
    assert bot._interrupt_reply_text({}) is None
    assert bot._interrupt_reply_text(None) is None
    assert bot._interrupt_reply_text({"__interrupt__": []}) is None


def test_interrupt_reply_text_falls_back_to_str():
    class Weird:
        def __str__(self):
            return "текст вопроса"

    assert bot._interrupt_reply_text({"__interrupt__": [Weird()]}) == "текст вопроса"


def test_resolve_prefer_query_uses_query_refs_first():
    """prefer='query': ссылка из query приоритетнее ссылки из answer."""
    sp_doc = "/mnt/docs/СП 89"
    results = [
        _mk_result(sp_doc, [
            {"asset_type": "table", "caption": "Таблица Д.1", "image_path": "image/table_11.png"},
            {"asset_type": "table", "caption": "Таблица Е.1", "image_path": "image/table_12.png"},
        ]),
    ]
    paths, warnings = helpers.resolve_images_to_send(
        results,
        answer="В таблице Д.1 приведены данные.",
        query="покажи таблицу Е.1",
        prefer="query",
    )
    assert warnings == []
    assert paths == [f"{sp_doc}/image/table_12.png"]


# ─── Фикс 3.4: многостраничные таблицы (image_paths) ───────────────────

def test_multipage_table_b1_sends_all_pages():
    """«Таблица Б.1» с image_paths из 3 png → все 3 страницы, НЕ ambiguous."""
    sp_results = [
        _mk_result(SP_DOC, [
            {"asset_type": "table", "caption": "Таблица Б.1",
             "image_path": "image/table_5.png",
             "image_paths": ["image/table_3.png", "image/table_4.png", "image/table_5.png"]},
        ]),
    ]
    paths, warnings = helpers.resolve_images_to_send(
        sp_results, "Параметры приведены в таблице Б.1."
    )
    assert warnings == []
    assert paths == [
        f"{SP_DOC}/image/table_3.png",
        f"{SP_DOC}/image/table_4.png",
        f"{SP_DOC}/image/table_5.png",
    ]


def test_single_page_table_e1_sends_one():
    """«Таблица Е.1» (image_paths из 1 png) → ровно 1 путь."""
    sp_results = [
        _mk_result(SP_DOC, [
            {"asset_type": "table", "caption": "Таблица Е.1",
             "image_path": "image/table_12.png",
             "image_paths": ["image/table_12.png"]},
        ]),
    ]
    paths, warnings = helpers.resolve_images_to_send(
        sp_results, "Данные в таблице Е.1."
    )
    assert warnings == []
    assert paths == [f"{SP_DOC}/image/table_12.png"]


def test_multipage_table_missing_image_paths_fallback():
    """Нет image_paths (старые чанки/тесты) → fallback на image_path."""
    sp_results = [
        _mk_result(SP_DOC, [
            {"asset_type": "table", "caption": "Таблица Д.1",
             "image_path": "image/table_11.png"},
        ]),
    ]
    paths, warnings = helpers.resolve_images_to_send(
        sp_results, "Данные в таблице Д.1."
    )
    assert warnings == []
    assert paths == [f"{SP_DOC}/image/table_11.png"]


def test_ambiguous_same_number_different_docs_fail_closed():
    """Один номер «Таблица 19» в РАЗНЫХ документах → warning, пачка НЕ
    отправляется, даже если у одного из документов несколько страниц."""
    mixed_results = GOST_RESULTS + [
        _mk_result(SP_DOC, [
            {"asset_type": "table", "caption": "Таблица 19 — Другие данные",
             "image_path": "image/table_9.png",
             "image_paths": ["image/table_9.png", "image/table_10.png"]},
        ], chunk_id="c2"),
    ]
    paths, warnings = helpers.resolve_images_to_send(
        mixed_results, "По ГОСТ и СП данные в таблице 19."
    )
    assert paths == []
    assert len(warnings) == 1
    assert "Неоднозначная ссылка" in warnings[0]


def test_query_doc_narrows_ambiguous_choice():
    """«…таблицу 19 ГОСТ 31996» при совпадениях в двух документах →
    сужение по doc_dir до ГОСТ, отправляется только путь ГОСТ."""
    mixed_results = GOST_RESULTS + [
        _mk_result(SP_DOC, [
            {"asset_type": "table", "caption": "Таблица 19 — Другие данные",
             "image_path": "image/table_9.png"},
        ], chunk_id="c2"),
    ]
    paths, warnings = helpers.resolve_images_to_send(
        mixed_results,
        answer="Данные в таблице 19.",
        query="покажи таблицу 19 ГОСТ 31996",
        prefer="query",
    )
    assert warnings == []
    assert paths == [f"{GOST_DOC}/image/table_22.png"]


def test_query_doc_narrows_multipage_b1():
    """«покажи таблицу Б.1 СП 89.13330» при совпадении в двух документах →
    сужение по doc_dir до СП → все 3 страницы таблицы Б.1."""
    other_doc = "/mnt/docs/Другой документ"
    mixed = [
        _mk_result(SP_DOC, [
            {"asset_type": "table", "caption": "Таблица Б.1",
             "image_path": "image/table_5.png",
             "image_paths": ["image/table_3.png", "image/table_4.png", "image/table_5.png"]},
        ], chunk_id="sp"),
        _mk_result(other_doc, [
            {"asset_type": "table", "caption": "Таблица Б.1",
             "image_path": "image/table_1.png"},
        ], chunk_id="other"),
    ]
    paths, warnings = helpers.resolve_images_to_send(
        mixed, answer="Вот таблица.",
        query="покажи таблицу Б.1 СП 89.13330",
        prefer="query",
    )
    assert warnings == []
    assert paths == [
        f"{SP_DOC}/image/table_3.png",
        f"{SP_DOC}/image/table_4.png",
        f"{SP_DOC}/image/table_5.png",
    ]


def test_send_images_multipage_table_sends_all_pages(tmp_path):
    """bot._send_images: «покажи таблицу Б.1 СП 89.13330» →
    отправляются все 3 страницы (table_3/4/5.png)."""
    doc = _write_assets(tmp_path, {
        "image/table_3.png": b"PNGT3",
        "image/table_4.png": b"PNGT4",
        "image/table_5.png": b"PNGT5",
    })
    results = [
        _mk_result(str(doc), [
            {"asset_type": "table", "caption": "Таблица Б.1",
             "image_path": "image/table_5.png",
             "image_paths": ["image/table_3.png", "image/table_4.png", "image/table_5.png"]},
        ]),
    ]
    upd = FakeUpdate()
    asyncio.run(bot._send_images(
        upd, results, "покажи таблицу Б.1 СП 89.13330",
        cited_chunk_ids=[], answer="Вот таблица.",
    ))
    assert [c for _, c in upd.message.sent] == ["table_3.png", "table_4.png", "table_5.png"]
