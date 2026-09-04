#!/usr/bin/env python3
"""
LangGraph QA-система для нормативных документов (ГОСТ, СП, СНиП).

Юнит 1: базовая инфраструктура — состояние графа (QAGraphState),
конфигурация (QAGraphConfig) и вызов LLM через Chat Completions.

Юнит 2: узлы графа — analyze_query, search_node, evaluate_results,
reformulate_query, ask_clarification, generate_answer (разделы 5.1–5.6
архитектуры langgraph-rag-architecture.md).

Юнит 3: сборка StateGraph (build_graph, раздел 4) и класс-обёртка
QAGraph (run/stream/resume, разделы 2.2, 6.4).

Юнит 4: CLI — argparse (--query/--interactive/--qdrant-path/--api-key/
--verbose), интерактивный режим «запрос → ответ/уточнение → ответ
пользователя → …» и __main__-блок (раздел 10 архитектуры).

Юнит 5: конфигурируемые LLM-провайдеры — llm_config.yaml
(load_llm_config/get_chat_url/get_api_key/llm_chat), CLI-аргументы
--llm-config/--llm-provider/--llm-model/--api-key (раздел 6 архитектуры).
Любой OpenAI-совместимый провайдер подключается правкой YAML без
изменения кода.

Расчёты по формулам: если ответ LLM содержит Python-код в блоке
```python ... ```, он автоматически исполняется execute_calculation()
(только стандартная библиотека, timeout 5 с, изоляция PYTHONPATH и cwd);
результат вставляется в ответ после блока кода, чтобы пользователь мог
проверить и формулу, и вычисление.

Установка:
    pip install -r requirements.txt

Провайдер/модель по умолчанию: deepseek/deepseek-v4-flash
(настраивается в firmware/src/llm_config.yaml)
"""
import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Sequence, TypedDict

import requests
import yaml
from dotenv import load_dotenv
from fastembed import SparseTextEmbedding
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from qdrant_client import QdrantClient, models
# Прямой импорт функций поиска (раздел 2.2 архитектуры — не subprocess):
# переиспользуются QdrantClient и SparseTextEmbedding между вызовами графа.
from search import (
    SPARSE_MODEL,
    filter_by_rrf_score,
    hybrid_search,
    rerank_siliconflow,
    result_to_dict,
)
# Чистые helper'ы для явных ссылок «Таблица N»/«Рисунок N» (раздел 3.3):
# extract_asset_references — ссылки из текста запроса/ответа,
# asset_caption_number — номер из подписи ассета. Модуль зависит только
# от stdlib, поэтому безопасен для импорта из core-графа.
from telegram_bot.asset_helpers import asset_caption_number, extract_asset_references
from llm_providers import (
    ProviderUnavailableError,
    _get_providers,
    get_api_key,
    get_endpoint,
    resolve_subrole,
    run_with_fallback,
)

logger = logging.getLogger("qa_graph")

# ─── Константы LLM-чата ───────────────────────────────────────────────
# Провайдеры, модели и параметры узлов — в llm_config.yaml (раздел 6
# архитектуры). Здесь только сетевые константы retry/timeout.

API_TIMEOUT = 120
API_RETRIES = 3
API_RETRY_BACKOFF = 2.0

# ─── Пост-валидация цитат (раздел 5.6 архитектуры) ────────────────────

CITATION_PATTERN = re.compile(
    r"\[([А-ЯЁа-яёA-Za-z0-9\s.,/\-—–]+?)\s*[,;]\s*(?:п\.|пп\.|табл\.|таблица|ст\.|разд\.|прил\.)\s*[\d.]+\]"
)

# Гибкий вариант для матчинга цитат в _match_citations_to_chunks
# (LLM не всегда ставит квадратные скобки, но формат «Документ, п. X.X.X» сохраняет)
# Группа 1: document_id, группа 2: тип (п./табл./ст.), группа 3: номер
# Номер: цифры + опционально кириллическая буква приложения (А-Я) + .цифры
CITATION_MATCH_PATTERN = re.compile(
    r"\[?([А-ЯЁа-яёA-Za-z0-9\s.,/\-—–]{3,}?)\s*[,;]\s*"
    r"(п\.|пп\.|табл\.|таблица|таблицей|ст\.|разд\.|прил\.)\s*"
    r"([А-ЯA-Z]?\.?\d+(?:\.\d+)*)\]?"
)


def _norm_dashes(s: str) -> str:
    """«—»/«–» → «-»: LLM ставит длинное тире в номерах документов
    («ГОСТ 31996—2012»), тогда как document_id в базе содержит дефис.
    Без нормализации has_citations не видит цитату (лишняя регенерация
    ответа) и _match_citations_to_chunks не находит chunk_id (пустой
    cited_chunk_ids → бот не отправляет картинки по цитатам)."""
    return (s or "").replace("—", "-").replace("–", "-")


# ─── Состояние графа (раздел 3 архитектуры) ───────────────────────────

class QAGraphState(TypedDict):
    """Состояние QA-графа."""

    # ── Входные данные ──
    query: str
    """Исходный запрос пользователя (неизменен на протяжении всего графа)."""

    user_query: str
    """Новый вопрос пользователя БЕЗ контекста истории (None → равен query).
    Нужен целенаправленному поиску: ссылки «Таблица N»/«Рисунок N» извлекаются
    только из нового вопроса, чтобы «табл. 19» в цитатах прошлых ответов
    (переданных в query для LLM-контекста) не уводила поиск к чужому чанку."""

    # ── Диалог ──
    messages: Annotated[Sequence[BaseMessage], add_messages]
    """История диалога: system, user, assistant — для контекста уточнений."""

    # ── Результаты поиска ──
    search_results: list[dict]
    """Результаты поиска после retrieve + rerank.
    Каждый элемент — словарь из result_to_dict() (search.py):
    {rank, score, rrf_score, quality, chunk_id, document_id,
     document_type, domain, title, status, section_path,
     heading_texts, text, references, assets}
    """

    # ── Управление потоком ──
    reformulate_count: int
    """Счётчик попыток переформулирования запроса (0..2)."""

    # ── Классификация запроса ──
    query_analysis: dict | None
    """Результат analyze_query: {is_concrete: bool, key_terms: list[str],
    suggested_clarification: str | None}"""

    # ── Промежуточные данные ──
    active_query: str
    """Текущий поисковый запрос (может отличаться от исходного после
    reformulate_query или уточнения)."""

    # ── Выход ──
    final_answer: str | None
    """Итоговый ответ пользователю (с цитатами) или None если ещё не готов."""

    needs_clarification: str | None
    """Если не None — текст уточняющего вопроса для пользователя.
    Устанавливается узлом ask_clarification, очищается после ответа."""

    cited_chunk_ids: list[str]
    """Chunk ID, реально процитированные LLM в final_answer.
    Вычисляются матчингом CITATION_PATTERN с search_results.
    Используется bot.py для показа ТОЛЬКО релевантных картинок."""

    # ── Обработка ошибок ──
    error: str | None
    """Сообщение об ошибке, если что-то пошло не так."""


# ─── Конфигурация (раздел 8 архитектуры) ──────────────────────────────

# Путь к llm_config.yaml по умолчанию — рядом с модулем (firmware/src/).
DEFAULT_SEARCH_CONFIG_PATH = str(Path(__file__).resolve().parent / "search_config.yaml")
DEFAULT_PROVIDERS_PATH = str(Path(__file__).resolve().parent / "providers.yaml")



@dataclass
class QAGraphConfig:
    """Конфигурация QA-графа: параметры по умолчанию из раздела 8.

    Провайдеры/модели/параметры LLM-узлов — в отдельном файле
    llm_config.yaml (раздел 6 архитектуры); здесь только путь к нему
    и опциональные переопределения (CLI --llm-provider/--llm-model/
    --api-key).
    """

    # Qdrant
    qdrant_path: str = "./qdrant_data"
    collection: str = "technical_standard"

    # Поиск
    retrieve_k: int = 30
    final_k: int = 6
    rrf_threshold: float = 0.15

    # LLM (конфигурируемые провайдеры, раздел 6)
    search_config_path: str = DEFAULT_SEARCH_CONFIG_PATH
    providers_path: str = DEFAULT_PROVIDERS_PATH

    llm_provider: str | None = None
    llm_model: str | None = None
    # Переопределение API-ключа провайдера (CLI --api-key); при None —
    # чтение api_key_env из окружения/.env.
    llm_api_key: str | None = None


    # Пороги оценки
    score_good_threshold: float = 0.7
    score_medium_threshold: float = 0.4
    max_reformulate_attempts: int = 2


# ─── Search/provider configuration ─────────────────────────────────────
_search_config_cache: dict[str, dict] = {}


def load_search_config(path: str | None = None) -> dict:
    path = path or DEFAULT_SEARCH_CONFIG_PATH
    if not os.path.exists(path):
        raise ValueError(f"Файл конфигурации поиска не найден: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"Ошибка парсинга YAML в {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("nodes"), dict) or not data["nodes"]:
        raise ValueError(f"{path}: секция nodes отсутствует или пуста")
    for name, params in data["nodes"].items():
        if not isinstance(params, dict):
            raise ValueError(f"{path}: узел {name!r} должен быть словарём")
        for field in ("temperature", "max_tokens"):
            value = params.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{path}: у узла {name!r} параметр {field!r} должен быть числом")
    return data


def _get_search_config(config: QAGraphConfig) -> dict:
    path = config.search_config_path
    if path not in _search_config_cache:
        _search_config_cache[path] = load_search_config(path)
    return _search_config_cache[path]



def llm_chat(messages: list[dict], config: QAGraphConfig | None = None,
             node_name: str = "generate_answer", provider_override: str | None = None,
             model_override: str | None = None, timeout: int = API_TIMEOUT) -> str:
    cfg = config or QAGraphConfig()
    search_cfg = _get_search_config(cfg)
    node_params = search_cfg["nodes"].get(node_name)
    if not isinstance(node_params, dict):
        raise ValueError(f"Узел {node_name!r} не найден в nodes")
    providers = _get_providers(cfg.providers_path)
    spec = resolve_subrole(providers, "build_search_index", "query_processing")
    if provider_override or cfg.llm_provider:
        spec["provider"] = provider_override or cfg.llm_provider
    if model_override or cfg.llm_model:
        spec["model"] = model_override or cfg.llm_model
    # Validate CLI overrides while retaining configured fallback.
    spec = {**spec, "fallback": spec.get("fallback")}
    payload = {"model": spec["model"], "messages": messages,
               "temperature": node_params["temperature"], "max_tokens": node_params["max_tokens"]}
    def attempt(target):
        try:
            key = get_api_key(providers, target["provider"], cfg.llm_api_key)
            response = requests.post(get_endpoint(providers, target["provider"], "chat"),
                                     json={**payload, "model": target["model"]},
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                     timeout=timeout)
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except ProviderUnavailableError:
            raise
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderUnavailableError(str(exc)) from exc
    started = time.monotonic()
    result = run_with_fallback(spec, attempt, "chat")
    logger.info("[timing] llm_chat %s: %.1fs (provider=%s model=%s)",
                node_name, time.monotonic() - started, spec["provider"], spec["model"])
    return result


# ─── Форматирование результатов поиска (раздел 5.6) ───────────────────

def _heading_to_str(heading_texts) -> str:
    """heading_texts может быть dict ({chapter, section, clause}) или str."""
    if not heading_texts:
        return ""
    if isinstance(heading_texts, dict):
        return " → ".join(
            str(v).strip() for v in heading_texts.values() if str(v).strip()
        )
    return str(heading_texts).strip()


def _format_search_results(results: list[dict]) -> str:
    """Форматирование результатов поиска для промпта генерации ответа.

    Формат (раздел 5.6 архитектуры):

        [1] {document_id} | {section_path} | score: {score}
           {heading_texts}
           {text}
           (источник: {title})

    Отсутствующие поля пропускаются/заменяются заглушками — функция не
    падает на частично заполненных словарях.
    """
    blocks = []
    for i, r in enumerate(results, 1):
        doc_id = r.get("document_id") or "—"
        section_path = r.get("section_path") or ""
        score = r.get("score", 0.0)
        title = r.get("title") or doc_id
        text = str(r.get("text") or "").strip()
        chunk_id = r.get("chunk_id", "")

        header = f"[{i}] {doc_id}"
        if section_path:
            header += f" | {section_path}"
        header += f" | score: {score}"

        lines = [header]
        heading = _heading_to_str(r.get("heading_texts"))
        if heading:
            lines.append(f"   {heading}")
        if text:
            lines.append(f"   {text}")

        lines.append(f"   (источник: {title})")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)

# ─── Кэш ассетов для изображений ───────────────────────────────────────

_assets_cache: dict[str, dict] = {}
_doc_dirs: dict[str, str] = {}

# Корень документов: BASE_DIR из общего .env (systemd EnvironmentFile); дефолт — прод.
BASE_MARKDOWN = os.path.join(os.environ.get("BASE_DIR", "/mnt/sdb/!База_ГОСТ"), "Markdown")


def _load_assets(doc_id: str) -> dict | None:
    """Загружает _assets.json для документа (с кэшированием)."""
    if doc_id in _assets_cache:
        return _assets_cache[doc_id]
    for entry in os.listdir(BASE_MARKDOWN):
        doc_dir = os.path.join(BASE_MARKDOWN, entry)
        assets_path = os.path.join(doc_dir, f"{entry}_assets.json")
        if not os.path.exists(assets_path):
            continue
        try:
            with open(assets_path) as f:
                assets = json.load(f)
        except Exception:
            continue
        if assets.get("document_id") == doc_id:
            _assets_cache[doc_id] = assets
            _doc_dirs[doc_id] = doc_dir
            return assets
    _assets_cache[doc_id] = None
    return None


def _enrich_results_with_dirs(results: list[dict]) -> list[dict]:
    """Добавляет поле doc_dir в каждый результат через кэш _load_assets.

    doc_dir нужен для резолва относительных image_path из поля assets
    в абсолютные пути файлов. Модифицирует список на месте.
    """
    for r in (results or []):
        doc_id = r.get("document_id", "")
        if not doc_id:
            continue
        _load_assets(doc_id)  # прогревает кэш
        r["doc_dir"] = _doc_dirs.get(doc_id, "")
    return results


def _match_citations_to_chunks(answer: str, search_results: list[dict]) -> list[str]:
    """Извлекает цитаты из ответа LLM и матчит с search_results.

    Для «п./ст./разд.» — матчинг по document_id + номеру пункта.
    Для «табл./таблица» — поиск чанка, содержащего таблицу с таким номером
    (caption «Таблица X.Y» в _assets.json), а не матчинг по section_path.
    Использует CITATION_MATCH_PATTERN — гибкий (скобки опциональны).
    """
    if not answer or not search_results:
        return []

    cited: set[str] = set()

    for m in CITATION_MATCH_PATTERN.finditer(answer):
        doc_ref = _norm_dashes(m.group(1).strip())  # "СО 153-34.21.122-2003"
        ref_type = m.group(2)          # "п." или "табл." или "ст."
        ref_num = m.group(3)           # "3.3.2.2" или "3.1"

        # ── Таблицы: ищем чанк по caption в _assets.json ──
        if ref_type.startswith("табл"):
            assets = _load_assets(doc_ref)  # попробуем doc_ref как doc_id
            if not assets:
                # doc_ref мог быть не точным document_id — ищем по search_results
                for r in search_results:
                    if doc_ref.lower() in (r.get("document_id") or "").lower():
                        assets = _load_assets(r["document_id"])
                        break
            if assets:
                table_caption = f"Таблица {ref_num}"
                for item in assets.get("assets", {}).get("tables", []):
                    if item.get("caption") == table_caption:
                        for cid in item.get("chunk_ids", []):
                            cited.add(cid)
                        break
            continue

        # ── Пункты/статьи/разделы: матчинг по section_path ──
        for r in search_results:
            r_doc_id = (r.get("document_id") or "").strip()
            if doc_ref.lower() not in r_doc_id.lower() and r_doc_id.lower() not in doc_ref.lower():
                continue
            if ref_num:
                r_clause = r.get("clause") or ""
                r_section_path = r.get("section_path") or ""
                if ref_num == r_clause or r_section_path.rstrip().endswith(ref_num):
                    cited.add(r.get("chunk_id", ""))
                    break
            else:
                cited.add(r.get("chunk_id", ""))
                break

    return [c for c in cited if c]


# ─── Парсинг JSON из ответа LLM ───────────────────────────────────────

def _try_json_loads(text: str) -> dict | None:
    """json.loads, но возвращает None вместо исключения и только для dict."""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_first_json_object(text: str) -> dict | None:
    """Извлечь первый валидный JSON-объект {...} из произвольного текста.

    Использует JSONDecoder.raw_decode — корректно обрабатывает строки,
    экранирование и вложенность без ручного подсчёта скобок.
    """
    decoder = json.JSONDecoder()
    start = 0
    while True:
        idx = text.find("{", start)
        if idx == -1:
            return None
        try:
            obj, _ = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            start = idx + 1
            continue
        return obj if isinstance(obj, dict) else None


def _parse_json_response(text: str | None) -> dict | None:
    """Безопасный парсинг JSON из ответа LLM.

    Стратегии (по порядку):
    1. json.loads на весь текст (после strip).
    2. Снять markdown-обёртку ```json ... ``` / ``` ... ``` и повторить.
    3. Извлечь первый сбалансированный JSON-объект {...} из текста
       (LLM часто оборачивает JSON в прозу).

    Если ничего не вышло — возвращает None; fallback на значения по
    умолчанию выполняет вызывающий код (например, is_concrete=True
    в узле analyze_query, раздел 5.1).
    """
    if not text:
        return None
    cleaned = text.strip()

    # 1. Прямой парсинг
    parsed = _try_json_loads(cleaned)
    if parsed is not None:
        return parsed

    # 2. Markdown-обёртка
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.DOTALL)
    if fence:
        parsed = _try_json_loads(fence.group(1).strip())
        if parsed is not None:
            return parsed

    # 3. Первый JSON-объект в тексте
    return _extract_first_json_object(cleaned)


# ─── Пост-валидация цитат (раздел 5.6) ────────────────────────────────

def has_citations(answer: str) -> bool:
    """Проверка наличия цитат формата [Документ, пункт/таблица] в ответе.

    Регэксп из раздела 5.6 архитектуры:
        [СП 89.13330.2016, п. 16.1], [ГОСТ 31996-2012, табл. 19]
    """
    return bool(CITATION_PATTERN.search(answer))


# ══════════════════════════════════════════════════════════════════════
# Юнит 2: узлы графа (разделы 5.1–5.6 архитектуры)
# ══════════════════════════════════════════════════════════════════════

# Параметры LLM по узлам (temperature/max_tokens) — в llm_config.yaml,
# секция nodes (раздел 6.2 архитектуры). Здесь — только промпты.

# ─── Общие вспомогательные функции узлов ──────────────────────────────


def _get_qa_config(config=None) -> QAGraphConfig:
    """Разрешение конфигурации узла.

    Принимает либо сам QAGraphConfig (удобно для прямых вызовов и тестов),
    либо LangGraph RunnableConfig вида {"configurable": {"qa_config": cfg}}
    (как будет передавать сборка графа в юните 3). По умолчанию —
    QAGraphConfig() со значениями из раздела 8.
    """
    if isinstance(config, QAGraphConfig):
        return config
    if isinstance(config, dict):
        configurable = config.get("configurable") or {}
        qa = configurable.get("qa_config")
        if isinstance(qa, QAGraphConfig):
            return qa
    return QAGraphConfig()


def _message_to_dict(msg) -> dict | None:
    """BaseMessage → {"role": ..., "content": ...} для llm_chat.

    Типы LangGraph: human → user, ai → assistant, system → system.
    Не-текстовые сообщения (tool и т.п.) пропускаются.
    """
    role = getattr(msg, "type", None)
    if role == "human":
        role = "user"
    elif role == "ai":
        role = "assistant"
    elif role != "system":
        return None
    content = getattr(msg, "content", None)
    if content is None:
        return None
    return {"role": role, "content": str(content)}


def _build_llm_messages(system_prompt: str, query: str, history=None) -> list[dict]:
    """Собрать сообщения для llm_chat: system-промпт + история + текущий запрос."""
    messages = [{"role": "system", "content": system_prompt}]
    for msg in history or []:
        d = _message_to_dict(msg)
        if d is not None:
            messages.append(d)
    messages.append({"role": "user", "content": query})
    return messages


def _fill_template(template: str, **kwargs) -> str:
    """Подстановка плейсхолдеров {name} без str.format.

    Промпты архитектуры содержат фигурные скобки в примерах JSON
    (разделы 5.1/5.5) — str.format их бы не пережил. Замена идёт
    последовательно по именам плейсхолдеров.
    """
    out = template
    for key, value in kwargs.items():
        out = out.replace("{" + key + "}", str(value))
    return out


# ─── Клиенты Qdrant (раздел 2.2, фикс 3.1) ────────────────────────────
# Локальный Qdrant держит ЭКСКЛЮЗИВНЫЙ файловый замок на папку базы,
# пока клиент открыт. Поэтому клиент создаётся на время ОДНОГО запроса
# (search_node) и закрывается в finally через _close_qdrant_client() —
# между запросами открытый клиент не держится, и create_index.py может
# открыть базу при работающем боте без pkill. Sparse-эмбеддер (fastembed)
# НЕ держит замок базы — он остаётся закэшированным.

_qdrant_clients: dict[str, QdrantClient] = {}
_sparse_model: SparseTextEmbedding | None = None


def _get_qdrant_client(path: str) -> QdrantClient:
    """Возвращает QdrantClient для *path* (создаёт при первом обращении).

    Клиент кэшируется в модуле до конца запроса; после использования
    ОБЯЗАТЕЛЬНО закрывается через _close_qdrant_client(path, client) —
    иначе локальная база остаётся заблокированной для других процессов.
    """
    client = _qdrant_clients.get(path)
    if client is None:
        client = QdrantClient(path=path)
        _qdrant_clients[path] = client
    return client


def _close_qdrant_client(path: str, client: QdrantClient | None = None) -> None:
    """Закрывает и забывает локальный QdrantClient (идемпотентно).

    Локальный Qdrant использует файловый замок: если держать клиент в
    модульном кэше после запроса, другой процесс (например, create_index.py)
    не сможет открыть базу. ``client`` опционален — можно закрыть объект,
    созданный вне кэша. ``close`` определяется feature-detected, чтобы
    тестовые дубли и старые версии клиента без этого метода не падали.
    Повторный вызов с тем же путём безопасен: кэш уже пуст, а close()
    у QdrantClient тоже идемпотентен.
    """
    cached = _qdrant_clients.pop(path, None)
    target = client or cached
    if target is None:
        return
    close = getattr(target, "close", None)
    if callable(close):
        close()


def _get_sparse_model() -> SparseTextEmbedding:
    """Ленивый синглтон sparse-эмбеддера (Qdrant/bm25) — разделяемый
    между вызовами графа (раздел 2.2)."""
    global _sparse_model
    if _sparse_model is None:
        _sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL)
    return _sparse_model


# ─── Узел 1: analyze_query (раздел 5.1) ───────────────────────────────

# Rule-based предфильтр конкретности (оптимизация 2026-09-03: узел стоит
# 5–17 с на каждый вопрос, а его вердикт влияет только на выбор маршрута
# search/ask_clarification — см. _route_after_analyze).
_DOC_HINT_RE = re.compile(r"\b(?:ГОСТ|СП|СНиП|СанПиН|ПУЭ|ПТЭ|РД|ВСН|СН)\b", re.I)
_DIGIT_RE = re.compile(r"\d")
_SIGNIFICANT_WORD_RE = re.compile(r"[А-Яа-яЁёA-Za-z]{4,}")


def _looks_concrete(query: str) -> bool | None:
    """Локальная оценка «конкретности» запроса без LLM.

    True  — запрос явно конкретен (номер документа, любая цифра или
            ≥3 значимых слова): analyze_query можно пропустить, маршрут
            и так будет search;
    None  — уверенности нет, решает LLM как раньше (короткие запросы,
            продолжения диалога «а в воздухе?»).

    False никогда не возвращается: локально нельзя надёжно распознать
    абстрактный запрос, ложное уточнение хуже лишнего поиска.
    """
    q = query or ""
    if _DOC_HINT_RE.search(q) or _DIGIT_RE.search(q):
        return True
    if len(_SIGNIFICANT_WORD_RE.findall(q)) >= 3:
        return True
    return None


ANALYZE_QUERY_PROMPT = """Ты — анализатор поисковых запросов к базе нормативных документов
(ГОСТ, СП, СНиП, СанПиН).

Твоя задача: определить, достаточно ли конкретен запрос для эффективного
поиска в векторной базе нормативных документов.

Конкретный запрос содержит:
- Название или номер документа (ГОСТ 31996, СП 89.13330)
- Предмет поиска (высота молниеотвода, сечение кабеля)
- Технические термины

Абстрактный запрос:
- "расскажи про нормативы"
- "какие есть требования"
- "что нужно знать про..."

Верни JSON:
{
  "is_concrete": true/false,
  "key_terms": ["список", "ключевых", "терминов"],
  "suggested_clarification": "уточняющий вопрос или null"
}"""


def analyze_query(state: QAGraphState, config=None) -> dict:
    """Узел 1: классификация запроса (раздел 5.1).

    Вызывает LLM с промптом анализа, парсит is_concrete/key_terms/
    suggested_clarification, устанавливает active_query = query.
    Если LLM вернул не-JSON или упал — fallback is_concrete=True
    (пропускаем в search, раздел 5.1 «Валидация»).
    """
    cfg = _get_qa_config(config)
    query = state.get("query", "")
    history = state.get("messages") or []

    # Rule-based пропуск LLM-классификации (оптимизация скорости):
    # явно конкретный запрос → маршрут search без вызова LLM.
    if _looks_concrete(query) is True:
        logger.info("[timing] analyze_query: 0.0s (rule-based, LLM пропущен)")
        return {
            "query_analysis": {
                "is_concrete": True,
                "key_terms": [],
                "suggested_clarification": None,
            },
            "active_query": query,
        }

    messages = _build_llm_messages(ANALYZE_QUERY_PROMPT, query, history)
    try:
        raw = llm_chat(messages, cfg, "analyze_query")
    except RuntimeError:
        raw = None

    parsed = _parse_json_response(raw) or {}

    is_concrete = parsed.get("is_concrete")
    if not isinstance(is_concrete, bool):
        is_concrete = True  # fallback из 5.1: пропускаем в search

    key_terms = parsed.get("key_terms")
    if not isinstance(key_terms, list):
        key_terms = []

    suggested = parsed.get("suggested_clarification")
    if not isinstance(suggested, str) or not suggested.strip():
        suggested = None

    return {
        "query_analysis": {
            "is_concrete": is_concrete,
            "key_terms": [str(t).strip() for t in key_terms if str(t).strip()],
            "suggested_clarification": suggested,
        },
        # Раздел 5.1, шаг 3: active_query = query (needs_clarification
        # здесь НЕ устанавливается — это делает ask_clarification).
        "active_query": query,
    }


# ─── Целенаправленный поиск таблиц/рисунков (фикс 3.3) ───────────────
# Для явных запросов вида «покажи таблицу Б.1 СП 89.13330» обычный
# семантический топ может не найти нужный чанк (или найти соседний).
# Поэтому при явной ссылке «Таблица N»/«Рисунок N» ищем чанк по caption
# в _assets.json документов и вычитываем его из Qdrant по payload.chunk_id.


def _iter_all_assets():
    """Итератор по (doc_dir, document_id, assets) для всех документов базы.

    Читает {entry}_assets.json из BASE_MARKDOWN, заполняет _assets_cache/
    _doc_dirs (общий кэш с _load_assets) — повторные вызовы не читают JSON.
    """
    seen: set[str] = set()
    for entry in os.listdir(BASE_MARKDOWN):
        doc_dir = os.path.join(BASE_MARKDOWN, entry)
        assets_path = os.path.join(doc_dir, f"{entry}_assets.json")
        if not os.path.exists(assets_path):
            continue
        try:
            with open(assets_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        doc_id = data.get("document_id")
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        _assets_cache[doc_id] = data
        _doc_dirs[doc_id] = doc_dir
        yield doc_dir, doc_id, data


# Номер документа в запросе: «ГОСТ 31996», «СП 89.13330», «СНиП 2.04.05»,
# «СанПиН 2.1», «СО 153-34.21.122» (для уточнения документа при поиске).
_DOC_NUM_RE = re.compile(
    r"(?:гост|сп|снип|санпин|со)\s*[\d.\-]+", re.IGNORECASE
)


def _query_matches_document(query: str, doc_id: str) -> bool:
    """Совпадает ли имя документа из запроса с document_id.

    «СП 89.13330» матчится с document_id «СП 89.13330.2016» (префикс/подстрока
    номера), «ГОСТ 31996» — с «ГОСТ 31996-2012», «СП 89» — с «СП 89.13330.2016».
    Сравнение без учёта регистра; номер документа извлекается из запроса
    регуляркой (ГОСТ/СП/СНиП/СанПиН/СО + номер).
    """

    q = (query or "").lower()
    doc = (doc_id or "").lower()
    if not q or not doc:
        return False
    # 1. document_id целиком содержится в запросе или наоборот
    if doc in q or q in doc:
        return True
    # 2. номер документа из запроса — подстрока document_id (без пробелов)
    for m in _DOC_NUM_RE.finditer(q):
        hint = re.sub(r"\s+", "", m.group(0))
        if hint and hint in re.sub(r"\s+", "", doc):
            return True
    return False


def _find_asset_chunks_by_caption(
    asset_type: str, ref_num: str
) -> list[tuple[str, str]]:
    """Ищет (document_id, chunk_id) для ассетов с номером подписи ref_num.

    Проходит по _assets.json всех документов базы (с кэшированием) и
    возвращает пары (document_id, chunk_id) для caption, номер которой
    совпадает с ref_num. Таблицы — caption «Таблица Б.1», рисунки —
    «fig_1»/«Рисунок 3 — ...» (см. asset_caption_number в asset_helpers).
    """
    matches: list[tuple[str, str]] = []
    group = "tables" if asset_type == "table" else "images"
    for _doc_dir, doc_id, data in _iter_all_assets():
        for item in data.get("assets", {}).get(group, []):
            caption = item.get("caption") or ""
            if asset_caption_number(asset_type, caption) != ref_num:
                continue
            for cid in item.get("chunk_ids") or []:
                matches.append((doc_id, cid))
    return matches


def _scroll_chunks_by_ids(client, collection: str, chunk_ids: list[str]) -> list:
    """Вычитывает точки Qdrant по payload.chunk_id (scroll с фильтром).

    scroll() возвращает Record без score-поля — оборачиваем в объект,
    похожий на ScoredPoint (payload + score), чтобы переиспользовать
    result_to_dict() без изменений.
    """
    ids = list(dict.fromkeys(chunk_ids))
    flt = models.Filter(
        must=[
            models.FieldCondition(
                key="chunk_id", match=models.MatchAny(any=ids)
            )
        ]
    )
    points: list = []
    offset = None
    while True:
        page, next_offset = client.scroll(
            collection_name=collection,
            scroll_filter=flt,
            limit=100,
            offset=offset,
        )
        for rec in page:
            points.append(
                types.SimpleNamespace(payload=rec.payload, score=1.0)
            )
        if next_offset is None:
            break
        offset = next_offset
    return points


def _targeted_search_results(
    query: str, client, cfg: QAGraphConfig, ref_query: str | None = None
) -> list[dict] | None:
    """Целенаправленный поиск чанка с явно запрошенной таблицей/рисунком.

    Если в запросе есть явная ссылка «Таблица N»/«Рисунок N» — ищем чанк
    по caption в _assets.json (не полагаясь на семантический топ) и
    вычитываем его из Qdrant по payload.chunk_id. Возвращает список
    result-словарей со score=1.0 (→ evaluate_results направит в
    generate_answer), либо None, если явной ссылки нет, чанк не найден,
    документ неоднозначен или возникла ошибка (вызывающий продолжает
    обычный семантический поиск).

    ref_query — текст для извлечения ссылок (по умолчанию query). Вызывающий
    передаёт user_query (новый вопрос без истории): «табл. N» в цитатах
    прошлых ответов не должна включать целенаправленный поиск. Сужение по
    документу ниже использует полный query — документ может быть назван
    в контексте диалога.
    """
    try:
        refs = extract_asset_references(ref_query or query)
        if not refs:
            return None
        asset_type, ref_num = refs[0]

        found = _find_asset_chunks_by_caption(asset_type, ref_num)
        if not found:
            return None

        # Если запрос называет документ — оставляем только его чанки.
        doc_ids = {doc_id for doc_id, _ in found}
        if len(doc_ids) > 1:
            named = [d for d in doc_ids if _query_matches_document(query, d)]
            if named:
                named_set = set(named)
                found = [(d, c) for d, c in found if d in named_set]
                doc_ids = {d for d, _ in found}

        if not found:
            return None
        if len(doc_ids) > 1:
            logger.warning(
                f"Целенаправленный поиск {asset_type} {ref_num}: "
                f"неоднозначный документ ({sorted(doc_ids)}) — "
                f"продолжаю семантический поиск"
            )
            return None

        chunk_ids = [cid for _, cid in found]
        points = _scroll_chunks_by_ids(client, cfg.collection, chunk_ids)
        if not points:
            return None

        results = [result_to_dict(p, 1.0, i) for i, p in enumerate(points, 1)]
        _enrich_results_with_dirs(results)
        logger.info(
            f"Целенаправленный поиск: {asset_type} {ref_num} → "
            f"{len(results)} чанк(ов) {chunk_ids[:5]}"
        )
        return results
    except Exception as exc:
        logger.warning(
            f"Целенаправленный поиск не удался, продолжаю семантический: {exc}"
        )
        return None


# ─── Узел 2: search_node (раздел 5.2) ─────────────────────────────────

def search_node(state: QAGraphState, config=None) -> dict:
    """Узел 2: гибридный поиск + RRF-фильтр + реранк (раздел 5.2).

    Последовательность:
      0. (фикс 3.3) если в запросе явная ссылка «Таблица N»/«Рисунок N» —
         целенаправленный поиск чанка по caption (_targeted_search_results);
      1. hybrid_search(query=active_query, top_k=retrieve_k) — retrieve
      2. filter_by_rrf_score(threshold=rrf_threshold) — отсев кандидатов
      3. rerank_siliconflow(top_n=final_k) — реранк
      4. result_to_dict() — преобразование в список словарей

    Обработка ошибок (раздел 5.2):
      - hybrid_search не нашёл результатов → search_results = []
      - rerank_siliconflow упал → fallback на RRF-порядок без реранка
      - hybrid_search упал (Qdrant недоступен) → error + пустые результаты

    Жизненный цикл клиента (фикс 3.1): локальный Qdrant держит файловый
    замок на папку базы — клиент создаётся на время одного вызова и
    закрывается в finally через _close_qdrant_client().
    """
    cfg = _get_qa_config(config)
    query = (state.get("active_query") or "").strip() or state.get("query", "")
    client = _get_qdrant_client(cfg.qdrant_path)
    sparse_model = _get_sparse_model()
    api_key = cfg.llm_api_key

    # Целенаправленный поиск для явных запросов «покажи таблицу/рисунок N»
    # (фикс 3.3): ищем чанк по caption, не полагаясь на семантический топ.
    # Ссылки извлекаем ТОЛЬКО из нового вопроса (user_query) — «табл. N»
    # в истории диалога (цитаты прошлых ответов) не должна уводить поиск
    # к чужому документу (инцидент СП 52.13330, 2026-09-03).
    targeted = _targeted_search_results(
        query, client, cfg, ref_query=state.get("user_query") or query
    )
    if targeted is not None:
        _close_qdrant_client(cfg.qdrant_path, client)
        return {"search_results": targeted}

    try:
        candidates = hybrid_search(
            client,
            cfg.collection,
            sparse_model,
            query,
            api_key,
            top_k=cfg.retrieve_k,
        )
    except Exception as exc:
        # Qdrant/эмбеддинг недоступен (раздел 7.1): error → END с fallback-ответом
        return {
            "search_results": [],
            "error": f"Поиск временно недоступен: {exc}",
        }
    finally:
        # Замок базы освобождается сразу после retrieve — фильтр/реранк
        # не используют клиент, а create_index.py может открыть базу при
        # работающем боте без pkill (фикс 3.1).
        _close_qdrant_client(cfg.qdrant_path, client)

    if not candidates:
        return {"search_results": []}

    filtered = filter_by_rrf_score(candidates, threshold=cfg.rrf_threshold)

    docs = [c.payload.get("text", "") for c in filtered]
    try:
        scores = rerank_siliconflow(
            query, docs, api_key, top_n=min(cfg.final_k, len(docs))
        )
        ranked = sorted(zip(filtered, scores), key=lambda x: x[1], reverse=True)[
            : cfg.final_k
        ]
    except Exception:
        # Rerank API недоступен (раздел 5.2): fallback на RRF-порядок
        # (сортировка по RRF-score, убывание — как вернул бы Qdrant)
        ranked = [
            (c, c.score)
            for c in sorted(filtered, key=lambda c: c.score, reverse=True)
        ][: cfg.final_k]

    results = [
        result_to_dict(point, score, i)
        for i, (point, score) in enumerate(ranked, 1)
    ]
    return {"search_results": results}


# ─── Узел 3: evaluate_results (раздел 5.3) ────────────────────────────

def evaluate_results(state: QAGraphState, config=None) -> str:
    """Узел 3: роутер — оценка качества результатов (раздел 5.3).

    Чистая функция без LLM. Возвращает строку-маршрут:
      "generate_answer"     — max_score > score_good_threshold (0.7)
      "reformulate_query"   — 0.4 <= max_score <= 0.7 и попыток < 2
      "ask_clarification"   — попыток >= 2 или max_score < 0.4 или пусто
    """
    cfg = _get_qa_config(config)
    results = state.get("search_results") or []
    count = state.get("reformulate_count", 0)

    if not results:
        return "ask_clarification"

    max_score = max(r.get("score", 0.0) for r in results)

    if max_score > cfg.score_good_threshold:
        return "generate_answer"
    if max_score >= cfg.score_medium_threshold:
        if count < cfg.max_reformulate_attempts:
            return "reformulate_query"
        return "ask_clarification"
    return "ask_clarification"  # max_score < score_medium_threshold


# ─── Узел 4: reformulate_query (раздел 5.4) ───────────────────────────

REFORMULATE_QUERY_PROMPT = """Ты — помощник для переформулирования поисковых запросов к базе
нормативных документов (ГОСТ, СП, СНиП).

Исходный запрос: {query}

Результаты поиска показали недостаточную релевантность.
Лучшие найденные документы:
{top_documents_summary}

Переформулируй запрос, используя:
- Синонимы технических терминов
- Альтернативные формулировки
- Номера связанных нормативов (если уместно)

Верни ТОЛЬКО переформулированный запрос, одной строкой, без кавычек."""


def _format_top_documents_summary(results: list[dict], limit: int = 3) -> str:
    """Краткая сводка лучших найденных документов для промпта 5.4."""
    lines = []
    for r in results[:limit]:
        doc_id = r.get("document_id") or "—"
        title = r.get("title") or doc_id
        score = r.get("score", 0.0)
        text = " ".join(str(r.get("text") or "").split())[:200]
        lines.append(f"- {title} ({doc_id}) | score: {score} | {text}")
    return "\n".join(lines) if lines else "(результаты поиска отсутствуют)"


def reformulate_query(state: QAGraphState, config=None) -> dict:
    """Узел 4: LLM-переформулировка запроса (раздел 5.4).

    Переформулируется активный запрос (тот, по которому искали и получили
    плохие результаты). При пустом ответе LLM или ошибке — reformulate_count
    всё равно инкрементируется, active_query не меняется (раздел 5.4).
    """
    cfg = _get_qa_config(config)
    query = state.get("query", "")
    active_query = state.get("active_query") or query
    results = state.get("search_results") or []

    prompt = _fill_template(
        REFORMULATE_QUERY_PROMPT,
        query=active_query,
        top_documents_summary=_format_top_documents_summary(results),
    )
    count = state.get("reformulate_count", 0)

    try:
        raw = llm_chat(
            [{"role": "system", "content": prompt},
             {"role": "user", "content": active_query}],
            cfg,
            "reformulate_query",
        )
    except RuntimeError:
        raw = ""

    new_query = raw.strip().strip('"').strip()
    if not new_query:
        return {"reformulate_count": count + 1}

    return {"active_query": new_query, "reformulate_count": count + 1}


# ─── Узел 5: ask_clarification (раздел 5.5) ───────────────────────────

ASK_CLARIFICATION_PROMPT = """Ты — ассистент для уточнения поисковых запросов к нормативным документам.

Пользователь спросил: {query}

Этот запрос недостаточно конкретен / результаты поиска неудовлетворительны.

Сформулируй 1–2 КОНКРЕТНЫХ уточняющих вопроса, которые помогут сузить поиск.
Вопросы должны:
- Уточнять номер/название документа (СП, ГОСТ, СанПиН)
- Уточнять конкретный аспект (проектирование, монтаж, испытания)
- Уточнять технические параметры

Верни JSON:
{
  "questions": ["вопрос 1", "вопрос 2"]
}"""

DEFAULT_CLARIFICATION_QUESTION = (
    "Уточните, пожалуйста, номер или название документа по вашему запросу"
)


def _generate_clarification_questions(state: QAGraphState, cfg: QAGraphConfig) -> list[str]:
    """Шаги 1–2 раздела 5.5: промпт + LLM → 1–2 уточняющих вопроса.

    При не-JSON ответе или ошибке LLM возвращается один дефолтный вопрос.
    """
    query = state.get("query", "")
    history = state.get("messages") or []
    system_prompt = _fill_template(ASK_CLARIFICATION_PROMPT, query=query)
    messages = _build_llm_messages(system_prompt, query, history)

    try:
        raw = llm_chat(messages, cfg, "ask_clarification")
    except RuntimeError:
        raw = None

    parsed = _parse_json_response(raw)
    questions: list[str] = []
    if parsed and isinstance(parsed.get("questions"), list):
        questions = [str(q).strip() for q in parsed["questions"] if str(q).strip()]
    if not questions:
        questions = [f"{DEFAULT_CLARIFICATION_QUESTION}: {query}"]
    return questions[:2]


def ask_clarification(state: QAGraphState, config=None) -> dict:
    """Узел 5: уточняющие вопросы + interrupt (раздел 5.5).

    Генерирует 1–2 вопроса, передаёт их в interrupt() — граф
    останавливается до ответа пользователя. При возобновлении:
      - ответ пользователя добавляется в messages
      - active_query = f"{query}. Уточнение: {user_response}"
      - needs_clarification сбрасывается, reformulate_count = 0
    """
    cfg = _get_qa_config(config)
    questions = _generate_clarification_questions(state, cfg)
    clarification_text = "\n".join(f"• {q}" for q in questions)

    # LangGraph interrupt — граф останавливается здесь (раздел 5.5, шаг 4)
    user_response = interrupt(clarification_text)

    # Шаг 5: возобновление после ответа пользователя
    new_query = f"{state.get('query', '')}. Уточнение: {user_response}"
    update: dict = {
        "needs_clarification": None,
        "active_query": new_query,
        "reformulate_count": 0,  # сброс счётчика при новом уточнении
    }
    if user_response:
        update["messages"] = [HumanMessage(content=str(user_response))]
    return update


# ─── Расчёты по формулам (execute_calculation) ────────────────────────
# ТЗ: generate_answer может вернуть Python-код в блоке ```python ... ```.
# execute_calculation() исполняет такие блоки (timeout 5 с, запрещены
# input()/while True, изоляция PYTHONPATH и cwd) и вставляет результат
# в ответ сразу после блока кода — пользователь видит и формулу, и код,
# и результат и может проверить вычисление.

PYTHON_FENCE_PATTERN = re.compile(r"```python\n(.*?)\n```", re.DOTALL)

CALC_BANNED_MESSAGE = (
    "[Ошибка: интерактивный ввод или бесконечный цикл запрещён]"
)
CALC_TIMEOUT_MESSAGE = "[Ошибка: превышено время исполнения]"
CALC_NO_OUTPUT_MESSAGE = "[выполнено без вывода]"


def execute_calculation(answer: str, timeout: int = 5) -> str:
    """Находит ```python-блоки в ответе, исполняет, вставляет результат.

    Каждый блок ```python ... ``` исполняется в отдельном subprocess:
      - только стандартная библиотека (PYTHONPATH очищен — нет доступа
        к модулям проекта);
      - cwd — свежая временная директория (нет доступа к файлам проекта);
      - timeout (по умолчанию 5 c) — защита от зависаний;
      - `input()` и `while True` запрещены на этапе разбора.

    Результат исполнения вставляется после блока кода:

        ```python
        print(2 + 3)
        ```
        **Результат:**
        ```
        5
        ```

    Если блок не найден — ответ возвращается без изменений.
    Если код завершился с ошибкой/таймаутом/без вывода — вместо результата
    вставляется соответствующее сообщение в квадратных скобках.
    """
    def run_block(code: str) -> str:
        if "input(" in code or "while True" in code:
            return CALC_BANNED_MESSAGE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                r = subprocess.run(
                    ["python3", "-c", code],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=tmp,
                    env={**os.environ, "PYTHONPATH": ""},  # изоляция
                )
        except subprocess.TimeoutExpired:
            return CALC_TIMEOUT_MESSAGE
        if r.returncode == 0:
            return r.stdout.strip() or CALC_NO_OUTPUT_MESSAGE
        return f"[Ошибка: {r.stderr.strip()[:200]}]"

    def replace_block(m):
        result = run_block(m.group(1))
        return f"{m.group(0)}\n**Результат:**\n```\n{result}\n```"

    return PYTHON_FENCE_PATTERN.sub(replace_block, answer)


# ─── Узел 6: generate_answer (раздел 5.6) ─────────────────────────────

GENERATE_ANSWER_PROMPT = """Ты — эксперт по нормативным документам (ГОСТ, СП, СНиП, СанПиН).
Твоя задача — ответить на вопрос пользователя, основываясь ТОЛЬКО
на предоставленных фрагментах документов.

ПРАВИЛА:
1. Отвечай на русском языке.
2. ВСЕГДА указывай источник в КВАДРАТНЫХ СКОБКАХ в формате:
   [Документ, п. НОМЕР] или [Документ, табл. НОМЕР]
   Примеры: [СП 89.13330.2016, п. 16.1], [ГОСТ 31996-2012, табл. 19]
   НЕ пиши «таблицей», «согласно пункту», «в соответствии с» — только [Документ, п./табл. N].
3. Каждое утверждение должно заканчиваться ссылкой в скобках.
   Правильно: «...расстояние не менее 100 мм [СП 89.13330.2016, п. 10.1.11].»
   Неправильно: «...согласно СП 89.13330.2016, таблицей Д.1 установлено...»
4. Если в результатах поиска нет ответа — честно скажи об этом
   и предложи переформулировать запрос.
5. Не выдумывай информацию, которой нет в предоставленных фрагментах.
6. НЕ используй LaTeX-разметку ($...$, $$...$$, \\text, \\frac и т.д.).
   Пиши формулы обычным текстом: h = r₀ / 1.2, мм², ≥, √.
   Для индексов используй Unicode: x₁, r₀, U₀, h₀.
   Для единиц: 30 Н/мм², а не $30\\text{ Н}/\\text{мм}^2$.
7. Будь краток, но точен.
8. НЕ добавляй в ответ теги [image: ...] — изображения прикрепляются
   автоматически. Ты можешь сослаться на рисунок в тексте:
   «...как показано на рис. 3.2», но путь к файлу не указывай.
9. Если пользователь явно просит показать таблицу или рисунок
   (например, «покажи таблицу Б.1»), а фрагмент с этой таблицей/рисунком
   есть среди НАЙДЕННЫХ ФРАГМЕНТОВ — ОБЯЗАТЕЛЬНО подтверди, что она
   существует: укажи номер и название, приведи её содержимое (строки
   таблицы) или кратко опиши. НИКОГДА не утверждай, что таблица/рисунок
   отсутствует, если в предоставленных фрагментах есть таблица с таким
   номером.

ВОПРОС: {query}

НАЙДЕННЫЕ ФРАГМЕНТЫ ДОКУМЕНТОВ:
{search_results_formatted}

ОТВЕТ:"""

FALLBACK_EMPTY_RESULTS = (
    "К сожалению, по вашему запросу не найдено релевантных "
    "нормативных документов. Попробуйте уточнить запрос: укажите номер ГОСТ/СП "
    "или конкретный технический аспект."
)

FALLBACK_LLM_ERROR = (
    "К сожалению, сервис генерации ответа временно недоступен. "
    "Попробуйте повторить запрос позже."
)

FALLBACK_EMPTY_ANSWER = (
    "К сожалению, не удалось сформулировать ответ по вашему запросу. "
    "Попробуйте переформулировать вопрос или уточнить номер документа."
)

CITATION_REGEN_INSTRUCTION = (
    "В ответе ОБЯЗАТЕЛЬНО укажи источник для каждого утверждения "
    "в формате [Документ, пункт]."
)


def generate_answer(state: QAGraphState, config=None) -> dict:
    """Узел 6: генерация ответа + расчёты + пост-валидация цитат (5.6).

    - Пустые search_results → fallback-ответ без вызова LLM.
    - После генерации ответ LLM проходит через execute_calculation():
      ```python-блоки исполняются, результат вставляется после блока кода.
    - Затем проверяется наличие цитат (has_citations); при отсутствии —
      однократная перегенерация с доп. инструкцией (max 1 доп. попытка).
    - После успешной генерации: enrich doc_dir + матчинг цитат→cited_chunk_ids.
    - LLM недоступен → fallback-ответ + error (раздел 7.1).
    """
    cfg = _get_qa_config(config)
    query = state.get("query", "")
    results = state.get("search_results") or []

    # Обработка пустых результатов (раздел 5.6): LLM не вызывается
    if not results:
        return {"final_answer": FALLBACK_EMPTY_RESULTS, "cited_chunk_ids": []}

    # Обогащаем doc_dir до вызова LLM (кэш прогревается один раз)
    _enrich_results_with_dirs(results)

    prompt = _fill_template(
        GENERATE_ANSWER_PROMPT,
        query=query,
        search_results_formatted=_format_search_results(results),
    )

    answer = ""
    last_exc: Exception | None = None
    for attempt in range(2):  # max 1 доп. попытка (раздел 5.6)
        try:
            answer = llm_chat(
                [{"role": "system", "content": prompt},
                 {"role": "user", "content": query}],
                cfg,
                "generate_answer",
            )
        except RuntimeError as exc:
            last_exc = exc
            break
        if has_citations(answer):
            break
        if attempt == 0:
            prompt += f"\n\n{CITATION_REGEN_INSTRUCTION}"

    if last_exc is not None:
        return {
            "final_answer": FALLBACK_LLM_ERROR,
            "error": f"LLM API недоступен: {last_exc}",
            "cited_chunk_ids": [],
        }

    # Модель вернула пустой/пробельный ответ (решение 33): не пустой пузырь и
    # не error — честный fallback с просьбой переформулировать запрос.
    if not answer.strip():
        return {"final_answer": FALLBACK_EMPTY_ANSWER, "cited_chunk_ids": []}

    # Матчинг цитат → конкретные чанки
    cited = _match_citations_to_chunks(answer, results)

    return {"final_answer": answer, "cited_chunk_ids": cited}


# ══════════════════════════════════════════════════════════════════════
# Юнит 3: сборка графа и класс-обёртка QAGraph (разделы 4, 2.2, 6.4)
# ══════════════════════════════════════════════════════════════════════

# ─── Условные рёбра (таблица 4.2) ─────────────────────────────────────


def _route_after_analyze(state: QAGraphState, config=None) -> str:
    """Conditional-функция после analyze_query (таблица 4.2).

        | analyze_query | is_concrete == True  | search            |
        | analyze_query | is_concrete == False | ask_clarification |

    Если query_analysis отсутствует или is_concrete не равен False —
    маршрут в search (fallback из раздела 5.1: не-JSON → is_concrete=True).
    """
    analysis = state.get("query_analysis") or {}
    if analysis.get("is_concrete") is False:
        return "ask_clarification"
    return "search"


def _evaluate_results_node(state: QAGraphState, config=None) -> dict:
    """Узел evaluate_results (раздел 4.1).

    Сам evaluate_results — чистая функция-роутер (раздел 5.3): она не
    изменяет состояние, а возвращает строку-маршрут. Поэтому как узел
    графа она представлена no-op-обёрткой, а маршрутизация выполняется
    в conditional-рёбрах через evaluate_results() (таблица 4.2).
    """
    return {}


# ─── Сборка графа (раздел 4) ──────────────────────────────────────────


def build_graph(checkpointer=None):
    """Сборка StateGraph QA-системы (раздел 4.1) и компиляция.

    Узлы: analyze_query, search, evaluate_results, reformulate_query,
    ask_clarification, generate_answer (разделы 5.1–5.6).

    Рёбра — строго по таблице 4.2:

        START ──▶ analyze_query
        analyze_query ──(is_concrete)──▶ search | ask_clarification
        ask_clarification ──▶ search
        search ──▶ evaluate_results
        evaluate_results ──(max_score, reformulate_count)──▶
            generate_answer | reformulate_query | ask_clarification
        reformulate_query ──▶ search
        generate_answer ──▶ END

    Компилируется с checkpointer (по умолчанию MemorySaver) — без него
    interrupt() в узле ask_clarification не работает (раздел 5.5,
    открытый вопрос 12.3 про персистентный checkpointer).
    """
    graph = StateGraph(QAGraphState)

    graph.add_node("analyze_query", analyze_query)
    graph.add_node("search", search_node)
    graph.add_node("evaluate_results", _evaluate_results_node)
    graph.add_node("reformulate_query", reformulate_query)
    graph.add_node("ask_clarification", ask_clarification)
    graph.add_node("generate_answer", generate_answer)

    graph.add_edge(START, "analyze_query")
    graph.add_conditional_edges(
        "analyze_query",
        _route_after_analyze,
        {"search": "search", "ask_clarification": "ask_clarification"},
    )
    graph.add_edge("ask_clarification", "search")
    graph.add_edge("search", "evaluate_results")
    graph.add_conditional_edges(
        "evaluate_results",
        evaluate_results,
        {
            "generate_answer": "generate_answer",
            "reformulate_query": "reformulate_query",
            "ask_clarification": "ask_clarification",
        },
    )
    graph.add_edge("reformulate_query", "search")
    graph.add_edge("generate_answer", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())


# ─── Класс-обёртка QAGraph (юнит 3) ───────────────────────────────────


class QAGraph:
    """Класс-обёртка над скомпилированным QA-графом.

    Инициализация (раздел 2.2, фикс 3.1): QdrantClient при инициализации
    НЕ открывается — локальный Qdrant держит эксклюзивный файловый замок
    на папку базы, поэтому клиент создаётся на время каждого поиска в
    search_node и закрывается в finally. Sparse-эмбеддер (fastembed) и
    API-ключ SiliconFlow (embedding/rerank) разделяются между вызовами
    графа. LLM-чат конфигурируется через llm_config.yaml
    (путь/провайдер/модель/ключ — в QAGraphConfig.llm_*), ключ
    провайдера резолвится лениво в llm_chat()/get_api_key(). Граф
    компилируется с MemorySaver-checkpointer для interrupt/resume.

    Использование:
        qa = QAGraph(QAGraphConfig(siliconflow_api_key=...))
        result = qa.run("вопрос")
        if "__interrupt__" in result:      # запрошено уточнение
            result = qa.resume("ответ пользователя")
    """

    def __init__(self, config: QAGraphConfig | None = None, checkpointer=None):
        self.config = config or QAGraphConfig()
        # Разрешение API-ключа SiliconFlow один раз (раздел 6.4:
        # аргумент → env → .env); ValueError если ключ не найден
        # (решение юнита 1 — без sys.exit). Ключ LLM-провайдера НЕ
        # резолвится здесь — он читается из llm_config.yaml через
        # get_api_key() при первом вызове llm_chat.
        self.api_key = None
        # QdrantClient НЕ открывается при инициализации (фикс 3.1): локальный
        # Qdrant держит эксклюзивный файловый замок на папку базы, поэтому
        # клиент создаётся на время каждого поиска в search_node и закрывается
        # в finally. self.client оставлен как None для совместимости.
        self.client = None
        self.sparse_model = _get_sparse_model()
        self.checkpointer = checkpointer or MemorySaver()
        self.graph = build_graph(checkpointer=self.checkpointer)
        # thread_id текущего прогона — для resume() после interrupt()
        self._thread_id: str | None = None

    def _initial_state(self, query: str, user_query: str | None = None) -> dict:
        """Начальное состояние графа (раздел 3): query + пустые поля.

        user_query — новый вопрос пользователя без контекста истории
        (None → равен query); используется целенаправленным поиском.
        """
        return {
            "query": query,
            "user_query": user_query or query,
            "messages": [],
            "search_results": [],
            "reformulate_count": 0,
            "query_analysis": None,
            "active_query": query,
            "final_answer": None,
            "needs_clarification": None,
            "cited_chunk_ids": [],
            "error": None,
        }

    def list_documents(self) -> list[dict]:
        """Return unique document IDs and titles from the configured collection."""
        client = None
        try:
            client = _get_qdrant_client(self.config.qdrant_path)
            offset = None
            documents: list[dict] = []
            seen: set[str] = set()
            while True:
                records, next_offset = client.scroll(
                    collection_name=self.config.collection,
                    offset=offset,
                    limit=256,
                    with_payload=["document_id", "title"],
                    with_vectors=False,
                )
                for record in records:
                    payload = record.payload or {}
                    document_id = payload.get("document_id")
                    if document_id is None:
                        continue
                    document_id = str(document_id)
                    if document_id in seen:
                        continue
                    seen.add(document_id)
                    documents.append({
                        "document_id": document_id,
                        "title": str(payload.get("title") or document_id),
                    })
                if next_offset is None:
                    return documents
                offset = next_offset
        except Exception:
            logger.exception("Не удалось получить список документов из Qdrant")
            return []
        finally:
            if client is not None:
                _close_qdrant_client(self.config.qdrant_path, client)

    def _thread_config(self) -> dict:
        """RunnableConfig: qa_config для узлов + thread_id для checkpointer."""
        return {
            "configurable": {
                "qa_config": self.config,
                "thread_id": self._thread_id,
            }
        }

    def run(self, query: str, user_query: str | None = None) -> dict:
        """graph.invoke() с начальным состоянием.

        user_query — новый вопрос пользователя без контекста истории
        (None → равен query); см. QAGraphState.user_query.

        Возвращает финальное состояние графа. Если граф остановился на
        interrupt() (запрошено уточнение), в результате присутствует ключ
        `__interrupt__` (список Interrupt), а final_answer отсутствует —
        тогда продолжайте через resume().
        """
        self._thread_id = uuid.uuid4().hex
        return self.graph.invoke(
            self._initial_state(query, user_query),
            config=self._thread_config(),
        )

    def stream(self, query: str, user_query: str | None = None):
        """Streaming-режим для отслеживания прогресса.

        stream_mode="updates": каждый чанк — словарь {имя_узла: обновление}.
        На interrupt() чанк будет {"__interrupt__": (Interrupt, ...)}.
        Возвращает генератор; thread_id фиксируется сразу (до итерации),
        чтобы resume() можно было вызвать и после частичного чтения.
        user_query — новый вопрос без контекста истории (см. run()).
        """
        self._thread_id = uuid.uuid4().hex
        return self._stream_impl(query, user_query)

    def _stream_impl(self, query: str, user_query: str | None = None):
        yield from self.graph.stream(
            self._initial_state(query, user_query),
            config=self._thread_config(),
            stream_mode="updates",
        )

    def resume(self, user_response: str) -> dict:
        """Возобновление после interrupt() (раздел 5.5).

        Отправляет ответ пользователя через Command(resume=...) в тот же
        поток (thread_id), где остановился run()/stream(). Граф проходит
        ask_clarification → search → evaluate_results → ... до END.
        """
        if self._thread_id is None:
            raise RuntimeError(
                "resume() можно вызывать только после run()/stream(), "
                "который остановился на interrupt()."
            )
        return self.graph.invoke(
            Command(resume=user_response),
            config=self._thread_config(),
        )

    def resume_stream(self, user_response: str):
        """Streaming-возобновление после interrupt() (юнит 4, CLI).

        Аналог resume(), но через graph.stream(Command(resume=...)) —
        позволяет CLI логировать промежуточные узлы и после уточнения.
        Возвращает генератор чанков {имя_узла: обновление}, как stream().
        """
        return self.graph.stream(
            Command(resume=user_response),
            config=self._thread_config(),
            stream_mode="updates",
        )


# ══════════════════════════════════════════════════════════════════════
# Юнит 4: CLI (раздел 10 архитектуры)
# ══════════════════════════════════════════════════════════════════════

CLI_PROMPT = "Вы: "
CLI_ANSWER_PROMPT = "Ваш ответ: "
CLI_EXIT_COMMANDS = {"exit", "quit", "выход", "q"}


def _log_node_step(node: str, update, verbose: bool, print_fn) -> None:
    """Вывод одного шага графа (логирование узлов, раздел 10).

    update — словарь-обновление состояния от узла (stream updates).
    В не-verbose режиме печатается только имя узла и счётчики;
    в verbose — детали (анализ запроса, результаты поиска).
    """
    if node == "analyze_query":
        analysis = (update or {}).get("query_analysis") or {}
        print_fn(f"  [analyze_query] is_concrete={analysis.get('is_concrete')}")
    elif node == "search":
        results = (update or {}).get("search_results") or []
        print_fn(f"  [search] результатов: {len(results)}")
        if verbose:
            for r in results:
                print_fn(
                    f"      {r.get('rank')}. {r.get('title')} "
                    f"| score={r.get('score', 0.0):.3f}"
                )
    elif node == "evaluate_results":
        print_fn("  [evaluate_results] оценка результатов")
    elif node == "reformulate_query":
        if update:
            print_fn(f"  [reformulate_query] активный запрос: {update.get('active_query')}")
    elif node == "ask_clarification":
        print_fn("  [ask_clarification] уточнение")
    elif node == "generate_answer":
        print_fn("  [generate_answer] ответ сформирован")
    else:
        print_fn(f"  [{node}]")


def _print_interrupt_value(interrupts, print_fn) -> str | None:
    """Извлечь текст уточняющего вопроса из __interrupt__-чанка.

    Возвращает текст вопроса (str) или None, если чанк пустой.
    """
    if not interrupts:
        return None
    intr = interrupts[0]
    return getattr(intr, "value", str(intr))


def run_single_query(qa, query: str, verbose: bool = False,
                     input_fn=input, print_fn=print) -> str | None:
    """Прогнать один запрос через граф с логированием шагов.

    Цикл «запрос → ответ/уточнение → ответ пользователя → …» (раздел 10):
      1. qa.stream(query) — логирование узлов по мере выполнения;
      2. на __interrupt__ — печать уточняющих вопросов, ввод ответа
         пользователя (input_fn), продолжение через qa.resume_stream();
      3. до получения final_answer (или явного завершения потока).

    Возвращает итоговый ответ (str) или None.
    """
    print_fn(f"Запрос: {query}")
    final_answer: str | None = None
    first_pass = True

    while True:
        if first_pass:
            chunks = qa.stream(query)
            first_pass = False
        else:
            answer = input_fn(CLI_ANSWER_PROMPT).strip()
            chunks = qa.resume_stream(answer)

        interrupted = False
        for chunk in chunks:
            node = next(iter(chunk))
            if node == "__interrupt__":
                value = _print_interrupt_value(chunk[node], print_fn)
                print_fn(f"\n[УТОЧНЕНИЕ] {value}")
                interrupted = True
                break
            _log_node_step(node, chunk[node], verbose, print_fn)
            if node == "generate_answer":
                final_answer = (chunk[node] or {}).get("final_answer")

        if not interrupted:
            break

    if final_answer:
        print_fn(f"\nОтвет:\n{final_answer}")
    else:
        print_fn("\nОтвет не получен.")
    return final_answer


def run_interactive(qa, verbose: bool = False,
                    input_fn=input, print_fn=print) -> None:
    """Диалоговый режим CLI (раздел 10).

    Цикл «запрос → ответ/уточнение → …». Выход по команде exit/quit/
    выход/q, Ctrl+C или EOF (Ctrl+D).
    """
    print_fn("Интерактивный режим QA-ассистента по нормативным документам.")
    print_fn("Введите запрос или 'exit' для выхода.\n")

    while True:
        try:
            query = input_fn(CLI_PROMPT).strip()
        except (EOFError, KeyboardInterrupt):
            print_fn("\nДо свидания!")
            return
        if not query:
            continue
        if query.lower() in CLI_EXIT_COMMANDS:
            print_fn("До свидания!")
            return
        try:
            run_single_query(qa, query, verbose=verbose,
                             input_fn=input_fn, print_fn=print_fn)
        except KeyboardInterrupt:
            print_fn("\nПрервано (Ctrl+C). До свидания!")
            return
        print_fn()


def build_parser() -> argparse.ArgumentParser:
    """Парсер аргументов CLI (раздел 10)."""
    ap = argparse.ArgumentParser(
        prog="qa_graph.py",
        description="QA-система по нормативным документам "
                    "(ГОСТ, СП, СНиП): LangGraph + Qdrant + SiliconFlow "
                    "(embedding/rerank) + конфигурируемый LLM-чат "
                    "(llm_config.yaml).",
    )
    ap.add_argument("--query", help="Один запрос (без --interactive)")
    ap.add_argument("--interactive", action="store_true",
                    help="Диалоговый режим: цикл запрос → ответ/уточнение → …")
    ap.add_argument("--qdrant-path", default="./qdrant_data",
                    help="Путь к локальному хранилищу Qdrant "
                         "(по умолчанию ./qdrant_data)")
    ap.add_argument("--config", default=DEFAULT_SEARCH_CONFIG_PATH,
                    help="Путь к search_config.yaml")
    ap.add_argument("--providers_config", default=DEFAULT_PROVIDERS_PATH,
                    help="Путь к providers.yaml")
    ap.add_argument("--llm-provider",
                    help="Переопределить провайдера LLM-чата "
                         "(например, deepseek, siliconflow)")
    ap.add_argument("--llm-model",
                    help="Переопределить модель LLM-чата")
    ap.add_argument("--api-key",
                    help="API-ключ: переопределяет ключ LLM-провайдера из "
                         "конфигурации providers.yaml и ключи embedding/rerank (или ключи в .env)")
    ap.add_argument("--verbose", action="store_true",
                    help="Подробный вывод шагов графа (узлы, результаты)")
    return ap


def main(argv=None, input_fn=input, print_fn=print) -> int:
    """Точка входа CLI (раздел 10).

    input_fn/print_fn — инъекция ввода/вывода для тестов и интерактивного
    режима (по умолчанию — input()/print()).

    Коды возврата:
      0 — успех / корректный выход (exit, Ctrl+C в интерактивном режиме);
      1 — ошибка выполнения;
      2 — ошибка конфигурации (нет/некорректен llm_config.yaml, нет
          API-ключа) / нет аргументов.
    """
    ap = build_parser()
    args = ap.parse_args(argv)

    if not args.query and not args.interactive:
        ap.print_help()
        return 0

    try:
        # Предварительная проверка конфигурации: плохой путь/структура
        # YAML и отсутствующий ключ провайдера → код 2 до запуска графа.
        load_search_config(args.config)
        provider_cfg = _get_providers(args.providers_config)
        spec = resolve_subrole(provider_cfg, "build_search_index", "query_processing")
        provider = args.llm_provider or spec["provider"]
        get_api_key(provider_cfg, provider, override=args.api_key)

        cfg = QAGraphConfig(
            qdrant_path=args.qdrant_path,
            search_config_path=args.config,
            providers_path=args.providers_config,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
            llm_api_key=args.api_key,
        )
        qa = QAGraph(cfg)
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    try:
        if args.interactive:
            run_interactive(qa, verbose=args.verbose,
                            input_fn=input_fn, print_fn=print_fn)
        else:
            run_single_query(qa, args.query, verbose=args.verbose,
                             input_fn=input_fn, print_fn=print_fn)
    except KeyboardInterrupt:
        print("\nПрервано (Ctrl+C). До свидания!", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
