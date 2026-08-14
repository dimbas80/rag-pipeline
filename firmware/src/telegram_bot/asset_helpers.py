#!/usr/bin/env python3
"""
Чистые helper'ы для точечного выбора изображений таблиц/рисунков по caption.

Используются bot.py в режиме fallback, когда cited_chunk_ids пуст:
вместо отправки ВСЕХ изображений из search_results (старое поведение)
из ответа LLM извлекаются явные ссылки вида «Таблица N» / «Рисунок N»
и по ним ищется ОДНОЗНАЧНЫЙ asset (по asset_type + номеру подписи).

Модуль не зависит от Telegram и qa_graph — только от stdlib,
поэтому все функции тестируются напрямую (firmware/tests/test_bot_assets.py).
"""

import os
import re

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


def find_assets_by_reference(
    search_results: list[dict], asset_type: str, ref_num: str
) -> list[dict]:
    """Все ассеты из search_results нужного типа с совпадающим номером подписи.

    Каждый элемент результата — словарь вида:
        {asset_type, caption, image_path, doc_dir, full_path}
    full_path — резолв относительного image_path через doc_dir результата.
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
            image_path = asset.get("image_path") or ""
            if not image_path:
                continue
            full = os.path.join(doc_dir, image_path) if doc_dir else image_path
            matches.append(
                {
                    "asset_type": a_type,
                    "caption": caption,
                    "image_path": image_path,
                    "doc_dir": doc_dir,
                    "full_path": full,
                }
            )
    return matches


def resolve_images_to_send(
    search_results: list[dict], answer: str, query: str = ""
) -> tuple[list[str], list[str]]:
    """Решает, какие изображения отправить, когда cited_chunk_ids пуст.

    Для каждой явной ссылки «Таблица N» / «Рисунок N» (сначала из answer,
    при отсутствии ссылок — из query) ищет однозначный ассет по asset_type
    и номеру подписи среди search_results.

    Возвращает (paths_to_send, warnings):
    - ссылок нет или номер не найден → paths пуст (картинки не отправляются);
    - на один номер нашлись ассеты с РАЗНЫМИ путями (разные документы) →
      warning, paths пуст (пачка не отправляется);
    - однозначные ссылки → пути файлов для отправки (без дубликатов).
    """
    warnings: list[str] = []

    refs = extract_asset_references(answer)
    if not refs and query:
        refs = extract_asset_references(query)

    if not refs:
        return [], warnings

    selected: list[str] = []
    for asset_type, ref_num in refs:
        matches = find_assets_by_reference(search_results, asset_type, ref_num)
        unique_paths = list(dict.fromkeys(m["full_path"] for m in matches))
        if not unique_paths:
            warnings.append(f"Не найден ассет для ссылки: {asset_type} {ref_num}")
            continue
        if len(unique_paths) > 1:
            warnings.append(
                f"Неоднозначная ссылка {asset_type} {ref_num}: найдено путей "
                f"{len(unique_paths)} ({unique_paths[:3]}) — пачка не отправлена"
            )
            return [], warnings
        selected.append(unique_paths[0])

    return list(dict.fromkeys(selected)), warnings
