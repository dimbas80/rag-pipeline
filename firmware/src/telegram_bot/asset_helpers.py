#!/usr/bin/env python3
"""
Чистые helper'ы для точечного выбора изображений таблиц/рисунков по caption.

Используются bot.py в режиме fallback, когда cited_chunk_ids пуст:
вместо отправки ВСЕХ изображений из search_results (старое поведение)
из ответа LLM извлекаются явные ссылки вида «Таблица N» / «Рисунок N»
и по ним ищется ОДНОЗНАЧНЫЙ asset (по asset_type + номеру подписи).

Многостраничные таблицы (фикс 3.4): у таблицы, разбитой на несколько
страниц, в базе поле image_paths с несколькими путями. Это ОДНА таблица —
бот отправляет ВСЕ её страницы. Неоднозначность только когда один и тот же
номер встречается в РАЗНЫХ документах (разные doc_dir); если в запросе
указан документ (номер ГОСТ/СП), выбор сужается до него перед проверкой.

Модуль не зависит от Telegram и qa_graph — только от stdlib,
поэтому все функции тестируются напрямую (firmware/tests/test_bot_assets.py).
"""

import os
import re


_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$"
)
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_CAPTION_RE = re.compile(
    r"^\s*(?:таблиц\w*|табл\.?)\s+((?:[А-Яа-яЁёA-Za-z]\.)?\d+(?:\.\d+)*)"
    r"(?:\s*[—–-].*)?\s*$",
    re.IGNORECASE,
)


def strip_markdown_tables(text: str) -> str:
    """Replace Markdown table blocks with short image indicators.

    Only pipe-delimited blocks containing a Markdown separator row are
    replaced.  Ordinary prose (including citation brackets) is preserved.
    """
    if not text or "|" not in text:
        return text

    lines = text.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        if not _TABLE_ROW_RE.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue

        start = i
        while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
            i += 1
        block = lines[start:i]
        separator_index = next(
            (j for j, line in enumerate(block) if _TABLE_SEPARATOR_RE.match(line)),
            None,
        )
        if separator_index is None:
            out.extend(block)
            continue

        # Include preceding table rows, but never consume a citation/prose line.
        caption = ""
        caption_line_index = None
        if out:
            match = _TABLE_CAPTION_RE.match(out[-1])
            if match:
                caption = f"Таблица {match.group(1)}"
                caption_line_index = len(out) - 1
        if caption and caption_line_index is not None:
            out.pop(caption_line_index)
            indicator = f"{caption} — см. прикреплённое изображение"
        else:
            indicator = "(таблица — см. прикреплённое изображение)"
        out.append(indicator)

    return "\n".join(out)


_DOCUMENT_LIST_PATTERNS = (
    # «какие» НЕ входит сюда намеренно: «какие документы в базе» покрывается
    # паттерном `документ\w*\s+в\s+базе`, а «перечень/список документов» —
    # своим паттерном ниже. Иначе «Какие документы регламентируют …» даёт
    # ложный позитив и уводит контент-запрос в голый список документов.
    re.compile(r"\b(?:перечисли|перечислите)\s+документ\w*", re.IGNORECASE),
    re.compile(r"\b(?:перечень|список)\s+документ\w*", re.IGNORECASE),
    re.compile(r"\bдокумент\w*\s+в\s+базе\b", re.IGNORECASE),
    re.compile(r"\bчто\s+(?:есть\s+)?в\s+базе\b", re.IGNORECASE),
    re.compile(r"\bчто\s+загружено\b", re.IGNORECASE),
    re.compile(r"\bкакие\s+(?:гост|сп)\s*/\s*(?:гост|сп)\s+есть\s+в\s+базе\b", re.IGNORECASE),
    # «какие нормативы» — только список, а не контент-вопрос: отсекаем
    # продолжение глаголами-предикатами («регламентируют», «требуют», …).
    # \b после \w* обязателен, иначе \w* откатывается до «норматив» и
    # negative lookahead ложно проходит (не видит \s+ перед «ы»).
    # (?:\w+\s+){0,2} пропускает прилагательное/существительное между
    # «норматив…» и глаголом: «нормативные документы регламентируют …».
    re.compile(
        r"\bкакие\s+норматив\w*\b"
        r"(?!\s+(?:\w+\s+){0,2}(?:регламентир|содерж|треб|описыв|устанавл|определ|применя)\w*)",
        re.IGNORECASE,
    ),
)


def is_document_list_request(query: str) -> bool:
    """Return whether *query* asks for the documents stored in the database."""
    if not query:
        return False
    return any(pattern.search(query) for pattern in _DOCUMENT_LIST_PATTERNS)


# ─── Номера ссылок/подписей ────────────────────────────────────────────
# Форматы номеров: «19», «3.1», «Д.1», «А.2» (буква приложения опциональна).
_REF_NUM = r"(?:[A-Za-zА-Яа-яЁё]\.)?\d+(?:\.\d+)*"

# Явные ссылки в тексте ответа: «Таблица 19», «табл. Д.1», «рис. 2»,
# «Рисунок 3», «fig. 1», «figure 4», «Table 5».
REFERENCE_PATTERNS: dict[str, re.Pattern] = {
    "table": re.compile(
        rf"(?:таблиц\w*|табл\.?|table)\s*({_REF_NUM})",
        re.IGNORECASE,
    ),
    "image": re.compile(
        rf"(?:рисун\w*|рис\.?|fig(?:ure)?\.?|изображени\w*)\s*({_REF_NUM})",
        re.IGNORECASE,
    ),
}

# Номер в подписи ассета. Примеры подписей:
#   table: «Таблица 19 — Допустимые токовые нагрузки ...», «Таблица Д.1», «Таблица 1»
#   image: «fig_1», «Рисунок 3 — ...»
_ASSET_TABLE_NUM = re.compile(rf"^(?:таблиц\w*|табл\.?|table)\s+({_REF_NUM})", re.IGNORECASE)
_ASSET_IMAGE_NUM = re.compile(rf"(?:fig|рис|рисун\w*)[_\s]+({_REF_NUM})", re.IGNORECASE)


def _normalize_ref_num(num: str) -> str:
    """Нормализация номера для сравнения: '19' → '19', 'Д.1' → 'д.1'."""
    return re.sub(r"\s+", "", num or "").lower()


def extract_asset_references(answer: str) -> list[tuple[str, str]]:
    """Извлекает явные ссылки на таблицы/рисунки из текста ответа.

    Возвращает список кортежей (asset_type, нормализованный номер) в порядке
    появления, без дубликатов. Например:
        «В таблице 19 приведены токи, см. также рис. 2» →
        [("table", "19"), ("image", "2")]
    """
    if not answer:
        return []
    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for asset_type, pattern in REFERENCE_PATTERNS.items():
        for m in pattern.finditer(answer):
            key = (asset_type, _normalize_ref_num(m.group(1)))
            if key not in seen:
                seen.add(key)
                refs.append(key)
    return refs


def asset_caption_number(asset_type: str, caption: str) -> str | None:
    """Номер из подписи ассета (нормализованный).

    ('table', 'Таблица 19 — ...') → '19'
    ('table', 'Таблица Д.1')     → 'д.1'
    ('image', 'fig_1')           → '1'
    Возвращает None, если номер не распознан.
    """
    if not caption:
        return None
    pattern = _ASSET_TABLE_NUM if asset_type == "table" else _ASSET_IMAGE_NUM
    m = pattern.search(caption.strip())
    if not m:
        return None
    return _normalize_ref_num(m.group(1))


# Обозначение норматива в запросе: «ГОСТ 31996», «ГОСТ Р 50571»,
# «СП 89.13330», «СНиП 2.04.07-86», «СанПиН 2.1.4.1074», «ТУ 16-705».
_NORM_REF_RE = re.compile(
    r"\b(?:гост\s*р\s*|гост\s*|сп\s*|снип\s*|санпин\s*|ту\s*)\d[\d.\-]*",
    re.IGNORECASE,
)


def _query_norm_refs(query: str) -> list[str]:
    """Обозначения нормативов из запроса (нормализованные).

    «СП 89.13330» → ['сп89.13330']; «ГОСТ Р 50571» → ['гостр50571'].
    Нормализация совпадает с _norm_doc_dir(): без пробелов, lowercase.
    """
    if not query:
        return []
    refs: list[str] = []
    for m in _NORM_REF_RE.finditer(query):
        ref = re.sub(r"\s+", "", m.group(0)).lower()
        if ref not in refs:
            refs.append(ref)
    return refs


def _norm_doc_dir(doc_dir: str) -> str:
    """Нормализация doc_dir для сравнения с обозначением из запроса."""
    return re.sub(r"\s+", "", doc_dir or "").lower()


def _narrow_by_query_doc(matches: list[dict], query: str) -> list[dict]:
    """Если в query указан документ, оставляет совпадения только из него.

    doc_dir результата (например «/mnt/docs/СП 89.13330.2016 …») сравнивается
    с обозначением норматива из запроса после нормализации: если в doc_dir
    встречается «сп89.13330» — результат относится к запрошенному документу.

    Если ни один doc_dir не содержит обозначение (сужение не сработало),
    возвращает matches без изменений — дальше решает проверка неоднозначности.
    """
    norm_refs = _query_norm_refs(query)
    if not norm_refs:
        return matches
    narrowed = [
        m for m in matches
        if any(ref in _norm_doc_dir(m["doc_dir"]) for ref in norm_refs)
    ]
    return narrowed or matches


def find_assets_by_reference(
    search_results: list[dict], asset_type: str, ref_num: str
) -> list[dict]:
    """Все ассеты из search_results нужного типа с совпадающим номером подписи.

    Каждый элемент результата — словарь вида:
        {asset_type, caption, image_paths, doc_dir, full_paths}
    full_paths — резолв ВСЕХ относительных путей ассета через doc_dir:
    из image_paths (страницы многостраничной таблицы), при отсутствии —
    из [image_path]. Один ассет (одна таблица) = один элемент списка.
    """
    matches: list[dict] = []
    for r in search_results or []:
        doc_dir = r.get("doc_dir") or ""
        for asset in r.get("assets") or []:
            a_type = (asset.get("asset_type") or "").lower()
            if a_type != asset_type:
                continue
            caption = asset.get("caption") or ""
            if asset_caption_number(asset_type, caption) != ref_num:
                continue
            # Все страницы ассета: image_paths (несколько путей), fallback
            # на одиночный image_path (старые чанки/тесты).
            rel_paths = asset.get("image_paths") or []
            if not rel_paths:
                single = asset.get("image_path") or ""
                rel_paths = [single] if single else []
            if not rel_paths:
                continue
            full_paths = [
                os.path.join(doc_dir, p) if doc_dir else p
                for p in rel_paths
            ]
            matches.append(
                {
                    "asset_type": a_type,
                    "caption": caption,
                    "image_paths": rel_paths,
                    "doc_dir": doc_dir,
                    "full_paths": full_paths,
                    # Совместимость со старым форматом: первый путь.
                    "image_path": rel_paths[0],
                    "full_path": full_paths[0],
                }
            )
    return matches


def resolve_images_to_send(
    search_results: list[dict], answer: str, query: str = "",
    prefer: str = "answer",
) -> tuple[list[str], list[str]]:
    """Решает, какие изображения отправить, когда cited_chunk_ids пуст.

    Для каждой явной ссылки «Таблица N» / «Рисунок N» ищет однозначный
    ассет по asset_type и номеру подписи среди search_results. Источник
    ссылок выбирается параметром ``prefer``:
      - prefer='answer' (по умолчанию): сначала ссылки из answer, при их
        отсутствии — из query (старое поведение);
      - prefer='query': сначала ссылки из query, при их отсутствии — из
        answer (фикс 3.2: явная ссылка в запросе приоритетна, даже когда
        cited_chunk_ids не пуст).

    Возвращает (paths_to_send, warnings):
    - ссылок нет или номер не найден → paths пуст (картинки не отправляются);
    - на один номер нашлись ассеты из РАЗНЫХ документов (разные doc_dir) →
      warning, paths пуст (пачка не отправляется, fail closed);
    - несколько путей с одним номером в пределах ОДНОГО документа →
      страницы одной многостраничной таблицы → paths содержит ВСЕ пути;
    - если в query указан документ (номер ГОСТ/СП, например «СП 89.13330») —
      выбор сужается по doc_dir до этого документа перед проверкой;
    - однозначные ссылки → пути файлов для отправки (без дубликатов).
    """
    warnings: list[str] = []

    answer_refs = extract_asset_references(answer)
    query_refs = extract_asset_references(query) if query else []
    if prefer == "query":
        refs = query_refs or answer_refs
    else:
        refs = answer_refs or query_refs

    if not refs:
        return [], warnings

    selected: list[str] = []
    for asset_type, ref_num in refs:
        matches = find_assets_by_reference(search_results, asset_type, ref_num)
        # Если в запросе указан документ (например «СП 89.13330») —
        # сужаем выбор до этого документа перед проверкой неоднозначности.
        matches = _narrow_by_query_doc(matches, query)
        # Плоский список (doc_dir, полный путь) всех страниц ассетов.
        pairs: list[tuple[str, str]] = []
        for m in matches:
            for fp in m["full_paths"]:
                pairs.append((m["doc_dir"], fp))
        if not pairs:
            warnings.append(f"Не найден ассет для ссылки: {asset_type} {ref_num}")
            continue
        # Один и тот же номер в пределах ОДНОГО документа — страницы одной
        # таблицы (разные image_paths) → отправляем все пути. Тот же номер
        # в РАЗНЫХ документах — неоднозначность, пачка не отправляется.
        doc_dirs = {d for d, _ in pairs}
        if len(doc_dirs) > 1:
            warnings.append(
                f"Неоднозначная ссылка {asset_type} {ref_num}: совпадения в "
                f"{len(doc_dirs)} документах ({sorted(doc_dirs)[:3]}) — "
                f"пачка не отправлена"
            )
            return [], warnings
        for _, fp in pairs:
            if fp not in selected:
                selected.append(fp)

    return list(dict.fromkeys(selected)), warnings
