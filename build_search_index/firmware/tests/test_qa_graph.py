"""
Тесты для firmware/src/qa_graph.py — юниты 1–3 (инфраструктура,
узлы графа, сборка StateGraph + класс QAGraph).

Запуск (из корня репозитория):
    python -m pytest firmware/tests/test_qa_graph.py -v

Не требуют сети: API-вызовы мокаются через monkeypatch,
чистые функции тестируются напрямую.
"""
import importlib.util
import os
import sys
import typing
from pathlib import Path

import pytest

# Тесты не должны зависеть от окружения запуска: фиксируем базовый URL
# ДО импорта скрипта, чтобы модульные константы указывали на публичный API.
os.environ.setdefault("SILICONFLOW_BASE_URL", "https://api.siliconflow.com")

SRC = Path(__file__).resolve().parents[1] / "src"

# qa_graph.py импортирует функции поиска из search.py (раздел 2.2
# архитектуры) — каталог src должен быть доступен для импорта.
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SRC / f"{name}.py")
    assert spec is not None and spec.loader is not None, f"cannot load {name}.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


qa_graph = _load("qa_graph")


# ─── Вспомогательные моки ─────────────────────────────────────────────

class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


def _chat_payload(content="Привет!"):
    return {
        "choices": [
            {"message": {"role": "assistant", "content": content}}
        ]
    }


# ─── Helpers for the split search/provider configuration ───────────────
SEARCH_CONFIG_TMPL = """\
nodes:
  analyze_query: {temperature: 0.0, max_tokens: 256}
  reformulate_query: {temperature: 0.3, max_tokens: 256}
  ask_clarification: {temperature: 0.3, max_tokens: 512}
  generate_answer: {temperature: 0.0, max_tokens: 2048}
"""
PROVIDERS_TMPL = """\
providers:
  deepseek:
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
    models: {deepseek-v4-flash: chat, deepseek-v4-pro: chat}
  siliconflow:
    base_url: https://api.siliconflow.com/v1
    api_key_env: SILICONFLOW_API_KEY
    models: {Qwen/Qwen3-32B: chat, Qwen/Qwen3-Embedding-8B: embedding, Qwen/Qwen3-Reranker-8B: rerank}
  provod:
    base_url: https://api.provod.ai/v1
    api_key_env: PROVOD_API_KEY
    models: {deepseek-v4-pro: chat}
roles:
  build_search_index:
    query_processing: {provider: deepseek, model: deepseek-v4-flash, fallback: {provider: provod, model: deepseek-v4-pro}}
    embedding: {provider: siliconflow, model: Qwen/Qwen3-Embedding-8B, fallback: {}}
    rerank: {provider: siliconflow, model: Qwen/Qwen3-Reranker-8B, fallback: {}}
"""

def _write_search_config(tmp_path, content=SEARCH_CONFIG_TMPL):
    path=tmp_path/'search_config.yaml'; path.write_text(content, encoding='utf-8'); return str(path)
def _write_providers(tmp_path, content=PROVIDERS_TMPL):
    path=tmp_path/'providers.yaml'; path.write_text(content, encoding='utf-8'); return str(path)
def _cfg_llm(tmp_path, **overrides):
    params={'search_config_path': _write_search_config(tmp_path), 'providers_path': _write_providers(tmp_path), 'llm_api_key': 'k-test'}
    params.update(overrides); return qa_graph.QAGraphConfig(**params)

# ─── QAGraphState ─────────────────────────────────────────────────────

def test_qa_graph_state_constructible():
    state = qa_graph.QAGraphState(
        query="вопрос",
        messages=[],
        search_results=[],
        reformulate_count=0,
        query_analysis=None,
        active_query="вопрос",
        final_answer=None,
        needs_clarification=None,
        error=None,
    )
    assert isinstance(state, dict)
    assert state["query"] == "вопрос"
    assert state["reformulate_count"] == 0


def test_qa_graph_state_schema():
    """Все поля из раздела 3 архитектуры присутствуют."""
    hints = typing.get_type_hints(qa_graph.QAGraphState, include_extras=True)
    expected = {
        "query", "user_query", "messages", "search_results", "reformulate_count",
        "query_analysis", "active_query", "final_answer",
        "needs_clarification", "error", "cited_chunk_ids",
    }
    assert set(hints) == expected


def test_qa_graph_state_messages_uses_add_messages():
    """messages аннотирован Annotated[Sequence[BaseMessage], add_messages]."""
    hints = typing.get_type_hints(qa_graph.QAGraphState, include_extras=True)
    args = typing.get_args(hints["messages"])
    assert qa_graph.add_messages in args  # LangGraph-редьюсер


# ─── QAGraphConfig ────────────────────────────────────────────────────

def test_qa_graph_config_defaults():
    """Значения по умолчанию из раздела 8 архитектуры."""
    cfg = qa_graph.QAGraphConfig()
    assert cfg.qdrant_path == "./qdrant_data"
    assert cfg.collection == "technical_standard"
    assert cfg.retrieve_k == 30
    assert cfg.final_k == 6
    assert cfg.rrf_threshold == 0.15
    # LLM-параметры вынесены в llm_config.yaml: в конфиге только путь
    # к файлу и опциональные переопределения.
    assert cfg.search_config_path == qa_graph.DEFAULT_SEARCH_CONFIG_PATH
    assert cfg.llm_provider is None
    assert cfg.llm_model is None
    assert cfg.llm_api_key is None
    assert cfg.providers_path == qa_graph.DEFAULT_PROVIDERS_PATH
    assert cfg.score_good_threshold == 0.7
    assert cfg.score_medium_threshold == 0.4
    assert cfg.max_reformulate_attempts == 2


def test_qa_graph_config_override():
    cfg = qa_graph.QAGraphConfig(
        llm_provider="siliconflow",
        llm_model="Qwen/Qwen3-32B",
        llm_api_key="k-cli",
        final_k=4,
    )
    assert cfg.llm_provider == "siliconflow"
    assert cfg.llm_model == "Qwen/Qwen3-32B"
    assert cfg.llm_api_key == "k-cli"
    assert cfg.final_k == 4


# ─── llm_chat (mock requests, конфиг из tmp llm_config.yaml) ──────────

def test_llm_chat_request_and_parse(monkeypatch, tmp_path):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeResponse(_chat_payload("Ответ модели"))

    monkeypatch.setattr(qa_graph.requests, "post", fake_post)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k-123")
    cfg = _cfg_llm(tmp_path, llm_api_key=None)
    out = qa_graph.llm_chat(
        [{"role": "user", "content": "вопрос"}],
        cfg,
        "analyze_query",
        timeout=30,
    )

    assert captured["url"] == "https://api.deepseek.com/v1/chat/completions"
    assert captured["json"]["model"] == "deepseek-v4-flash"
    assert captured["json"]["messages"] == [{"role": "user", "content": "вопрос"}]
    assert captured["json"]["temperature"] == 0.0   # из nodes.analyze_query
    assert captured["json"]["max_tokens"] == 256
    assert captured["headers"]["Authorization"] == "Bearer k-123"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["timeout"] == 30
    assert out == "Ответ модели"


def test_llm_chat_default_params(monkeypatch, tmp_path):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse(_chat_payload())

    monkeypatch.setattr(qa_graph.requests, "post", fake_post)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    cfg = _cfg_llm(tmp_path)
    qa_graph.llm_chat([{"role": "user", "content": "x"}], cfg, "generate_answer")
    assert captured["json"]["model"] == "deepseek-v4-flash"
    assert captured["json"]["temperature"] == 0.0
    assert captured["json"]["max_tokens"] == 2048
    assert captured["timeout"] == qa_graph.API_TIMEOUT


def test_llm_chat_node_params_from_config(monkeypatch, tmp_path):
    """temperature/max_tokens берутся из секции nodes конфига по узлу."""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return FakeResponse(_chat_payload())

    monkeypatch.setattr(qa_graph.requests, "post", fake_post)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    cfg = _cfg_llm(tmp_path)

    qa_graph.llm_chat([{"role": "user", "content": "x"}], cfg, "reformulate_query")
    assert captured["json"]["temperature"] == 0.3
    assert captured["json"]["max_tokens"] == 256

    qa_graph.llm_chat([{"role": "user", "content": "x"}], cfg, "ask_clarification")
    assert captured["json"]["temperature"] == 0.3
    assert captured["json"]["max_tokens"] == 512


def test_llm_chat_provider_override(monkeypatch, tmp_path):
    """provider_override/model_override переопределяют default из конфига."""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse(_chat_payload())

    monkeypatch.setattr(qa_graph.requests, "post", fake_post)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "k-sf")
    cfg = _cfg_llm(tmp_path)
    qa_graph.llm_chat(
        [{"role": "user", "content": "x"}], cfg, "generate_answer",
        provider_override="siliconflow",
        model_override="Qwen/Qwen3-32B",
    )
    assert captured["url"] == "https://api.siliconflow.com/v1/chat/completions"
    assert captured["json"]["model"] == "Qwen/Qwen3-32B"


def test_llm_chat_config_provider_model_override(monkeypatch, tmp_path):
    """llm_provider/llm_model из QAGraphConfig (CLI --llm-provider/--llm-model)."""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return FakeResponse(_chat_payload())

    monkeypatch.setattr(qa_graph.requests, "post", fake_post)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "k-sf")
    cfg = _cfg_llm(tmp_path, llm_provider="siliconflow", llm_model="Qwen/Qwen3-32B")
    qa_graph.llm_chat([{"role": "user", "content": "x"}], cfg, "generate_answer")
    assert captured["url"] == "https://api.siliconflow.com/v1/chat/completions"
    assert captured["json"]["model"] == "Qwen/Qwen3-32B"


def test_llm_chat_api_key_override_wins(monkeypatch, tmp_path):
    """--api-key (llm_api_key) переопределяет ключ провайдера из env."""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        return FakeResponse(_chat_payload())

    monkeypatch.setattr(qa_graph.requests, "post", fake_post)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k-env")
    cfg = _cfg_llm(tmp_path, llm_api_key="k-cli")
    qa_graph.llm_chat([{"role": "user", "content": "x"}], cfg, "generate_answer")
    assert captured["headers"]["Authorization"] == "Bearer k-cli"


def test_llm_chat_unknown_node_raises(tmp_path):
    cfg = _cfg_llm(tmp_path)
    with pytest.raises(ValueError, match="не найден в nodes"):
        qa_graph.llm_chat([{"role": "user", "content": "x"}], cfg, "no_such_node")


# ─── _format_search_results ───────────────────────────────────────────

def _result(**overrides):
    base = {
        "document_id": "doc-1",
        "section_path": "Раздел 1",
        "score": 0.82,
        "title": "ГОСТ 31996-2012",
        "heading_texts": {"chapter": "Глава 1", "section": "Зоны", "clause": "1.2"},
        "text": "Текст пункта",
    }
    base.update(overrides)
    return base


def test_format_search_results_basic():
    formatted = qa_graph._format_search_results([_result()])
    assert "[1] doc-1 | Раздел 1 | score: 0.82" in formatted
    assert "Глава 1 → Зоны → 1.2" in formatted
    assert "Текст пункта" in formatted
    assert "(источник: ГОСТ 31996-2012)" in formatted


def test_format_search_results_multiple_results_numbered():
    formatted = qa_graph._format_search_results(
        [_result(), _result(document_id="doc-2", score=0.5)]
    )
    assert "[1] doc-1" in formatted
    assert "[2] doc-2" in formatted


def test_format_search_results_empty_list():
    assert qa_graph._format_search_results([]) == ""


def test_format_search_results_missing_fields_no_crash():
    formatted = qa_graph._format_search_results([{}])
    assert "[1]" in formatted
    assert "(источник: —)" in formatted  # заглушка для document_id/title


def test_format_search_results_heading_texts_str():
    formatted = qa_graph._format_search_results(
        [_result(heading_texts="Прямая строка заголовка")]
    )
    assert "Прямая строка заголовка" in formatted


def test_format_search_results_heading_texts_none():
    formatted = qa_graph._format_search_results([_result(heading_texts=None)])
    assert "[1] doc-1" in formatted
    assert "источник" in formatted


# ─── _parse_json_response ─────────────────────────────────────────────

def test_parse_json_plain():
    assert qa_graph._parse_json_response('{"is_concrete": true}') == {"is_concrete": True}


def test_parse_json_with_whitespace():
    assert qa_graph._parse_json_response('  \n {"a": 1}  \t ') == {"a": 1}


def test_parse_json_markdown_fence_json():
    raw = '```json\n{"is_concrete": false, "key_terms": ["гост"]}\n```'
    parsed = qa_graph._parse_json_response(raw)
    assert parsed == {"is_concrete": False, "key_terms": ["гост"]}


def test_parse_json_markdown_fence_plain():
    raw = '```\n{"a": 1}\n```'
    assert qa_graph._parse_json_response(raw) == {"a": 1}


def test_parse_json_wrapped_in_prose():
    raw = 'Вот результат анализа:\n{"is_concrete": true, "key_terms": ["кабель"]}\nНадеюсь, поможет.'
    parsed = qa_graph._parse_json_response(raw)
    assert parsed["is_concrete"] is True
    assert parsed["key_terms"] == ["кабель"]


def test_parse_json_with_leading_prose():
    raw = 'Ответ: {"q": ["а", "б"]}'
    assert qa_graph._parse_json_response(raw) == {"q": ["а", "б"]}


def test_parse_json_nested_braces_in_string():
    """Скобки внутри строки не должны ломать извлечение объекта."""
    raw = 'текст {"a": "значение {с фигурной скобкой}"} хвост'
    assert qa_graph._parse_json_response(raw) == {"a": "значение {с фигурной скобкой}"}


def test_parse_json_invalid_returns_none():
    assert qa_graph._parse_json_response("это не json") is None


def test_parse_json_empty_returns_none():
    assert qa_graph._parse_json_response("") is None
    assert qa_graph._parse_json_response("   ") is None
    assert qa_graph._parse_json_response(None) is None


def test_parse_json_list_not_dict_returns_none():
    assert qa_graph._parse_json_response("[1, 2, 3]") is None


def test_parse_json_broken_then_valid():
    """Первый найденный объект битый — берётся следующий валидный."""
    raw = '{"a": } потом {"ok": true}'
    assert qa_graph._parse_json_response(raw) == {"ok": True}


# ─── has_citations ────────────────────────────────────────────────────

def test_has_citations_point():
    assert qa_graph.has_citations("Согласно [СП 89.13330.2016, п. 16.1] ...")


def test_has_citations_table():
    assert qa_graph.has_citations("по [ГОСТ 31996-2012, табл. 19]")


def test_has_citations_em_dash():
    """LLM любит длинное тире в номерах — цитата должна признаваться
    (иначе лишняя регенерация ответа)."""
    assert qa_graph.has_citations("нагрузка 187 А [ГОСТ 31996—2012, табл. 19].")
    assert qa_graph.has_citations("см. [ГОСТ 31996–2012, п. 10.1]")


def test_has_citations_table_word():
    assert qa_graph.has_citations("по [ГОСТ 31996-2012, таблица 19]")


def test_has_citations_semicolon_separator():
    assert qa_graph.has_citations("[СП 20.13330.2016; п. 5.3] — требование")


def test_has_citations_no_citation():
    assert not qa_graph.has_citations("Просто текст без ссылок на документы.")


def test_has_citations_wrong_format():
    """Без разделителя [,;] перед 'п.' — это не цитата по нашему формату."""
    assert not qa_graph.has_citations("См. СП 89 п. 16.1 без скобок")
    assert not qa_graph.has_citations("[ГОСТ 31996 п 19]")


def test_has_citations_empty():
    assert not qa_graph.has_citations("")


def test_match_citations_em_dash_matches_hyphen_doc_id():
    """«ГОСТ 31996—2012» в ответе LLM матчится с «ГОСТ 31996-2012» в базе
    (нормализация тире), иначе cited_chunk_ids пуст и картинки не уходят."""
    answer = "Нагрузка 187 А [ГОСТ 31996—2012, п. 10.1]."
    results = [{"document_id": "ГОСТ 31996-2012", "clause": "10.1", "chunk_id": "c1"}]
    assert qa_graph._match_citations_to_chunks(answer, results) == ["c1"]


# ══════════════════════════════════════════════════════════════════════
# Юнит 2: узлы графа
# ══════════════════════════════════════════════════════════════════════

# ─── Вспомогательные моки узлов ───────────────────────────────────────


class FakePoint:
    """Имитация qdrant ScoredPoint: payload + score (RRF)."""

    def __init__(self, payload, score):
        self.payload = payload
        self.score = score


def _point(score=0.8, **payload_overrides):
    payload = {
        "chunk_id": "c1",
        "document_id": "doc-1",
        "document_type": "ГОСТ",
        "domain": "строительство",
        "title": "ГОСТ 31996-2012",
        "status": "действующий",
        "section_path": "Раздел 1",
        "heading_texts": {"chapter": "Глава 1", "section": "Зоны", "clause": "1.2"},
        "text": "Текст пункта документа",
        "references": [],
        "assets": [],
    }
    payload.update(payload_overrides)
    return FakePoint(payload, score)


def _state(**overrides):
    base = {
        "query": "вопрос",
        "messages": [],
        "search_results": [],
        "reformulate_count": 0,
        "query_analysis": None,
        "active_query": "вопрос",
        "final_answer": None,
        "needs_clarification": None,
        "error": None,
    }
    base.update(overrides)
    return base


def _cfg(**overrides):
    params = {"llm_api_key": "k-sf"}
    params.update(overrides)
    return qa_graph.QAGraphConfig(**params)


def _patch_search_deps(monkeypatch):
    """Замокать тяжёлые зависимости search_node (клиенты/эмбеддер)."""
    monkeypatch.setattr(qa_graph, "_get_qdrant_client", lambda path: object())
    monkeypatch.setattr(qa_graph, "_get_sparse_model", lambda: object())


# ─── _get_qa_config / _message_to_dict / _fill_template ────────────────

def test_get_qa_config_variants():
    cfg = _cfg(final_k=3)
    assert qa_graph._get_qa_config(cfg) is cfg
    assert qa_graph._get_qa_config({"configurable": {"qa_config": cfg}}).final_k == 3
    assert qa_graph._get_qa_config(None).final_k == 6
    assert qa_graph._get_qa_config({}).final_k == 6


def test_message_to_dict_roles():
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    assert qa_graph._message_to_dict(HumanMessage(content="hi")) == {"role": "user", "content": "hi"}
    assert qa_graph._message_to_dict(AIMessage(content="yo")) == {"role": "assistant", "content": "yo"}
    assert qa_graph._message_to_dict(SystemMessage(content="sys")) == {"role": "system", "content": "sys"}
    assert qa_graph._message_to_dict(object()) is None


def test_fill_template_preserves_json_braces():
    out = qa_graph._fill_template("Верни JSON:\n{\n  \"questions\": []\n}\n{q}", q="вопрос")
    assert "вопрос" in out
    assert '{ "questions": [] }' not in out  # фигурные скобки сохранились
    assert '{\n  "questions": []\n}' in out


# ─── Узел 1: analyze_query (раздел 5.1) ───────────────────────────────

def test_analyze_query_concrete(monkeypatch):
    monkeypatch.setattr(
        qa_graph, "llm_chat",
        lambda messages, config, node_name, **kw: (
            '{"is_concrete": true, "key_terms": ["молниеотвод"], "suggested_clarification": null}'
        ),
    )
    # Запрос без цифр и номеров документов (2 значимых слова) — уходит в LLM.
    out = qa_graph.analyze_query(_state(query="высота молниеотвода"), _cfg())
    assert out["query_analysis"]["is_concrete"] is True
    assert out["query_analysis"]["key_terms"] == ["молниеотвод"]
    assert out["query_analysis"]["suggested_clarification"] is None
    assert out["active_query"] == "высота молниеотвода"


def test_analyze_query_rule_based_skips_llm(monkeypatch):
    """Явно конкретный запрос (номер документа/цифры) — LLM не вызывается."""
    def fail(*a, **kw):
        raise AssertionError("llm_chat не должен вызываться для конкретного запроса")
    monkeypatch.setattr(qa_graph, "llm_chat", fail)
    out = qa_graph.analyze_query(
        _state(query="Допустимый ток кабеля ВВГ 4х50 в земле"), _cfg())
    assert out["query_analysis"]["is_concrete"] is True
    assert out["active_query"] == "Допустимый ток кабеля ВВГ 4х50 в земле"


def test_analyze_query_abstract_does_not_set_needs_clarification(monkeypatch):
    monkeypatch.setattr(
        qa_graph, "llm_chat",
        lambda messages, config, node_name, **kw: (
            '{"is_concrete": false, "key_terms": [], '
            '"suggested_clarification": "Укажите номер документа"}'
        ),
    )
    out = qa_graph.analyze_query(_state(query="расскажи про нормативы"), _cfg())
    assert out["query_analysis"]["is_concrete"] is False
    assert out["query_analysis"]["suggested_clarification"] == "Укажите номер документа"
    # Раздел 5.1, шаг 4: needs_clarification здесь НЕ устанавливается
    assert "needs_clarification" not in out


def test_analyze_query_non_json_fallback_concrete(monkeypatch):
    monkeypatch.setattr(qa_graph, "llm_chat",
                        lambda messages, config, node_name, **kw: "просто текст")
    out = qa_graph.analyze_query(_state(query="вопрос"), _cfg())
    assert out["query_analysis"]["is_concrete"] is True
    assert out["query_analysis"]["key_terms"] == []
    assert out["query_analysis"]["suggested_clarification"] is None


def test_analyze_query_llm_error_fallback_concrete(monkeypatch):
    def boom(messages, config, node_name, **kw):
        raise RuntimeError("llm down")
    monkeypatch.setattr(qa_graph, "llm_chat", boom)
    out = qa_graph.analyze_query(_state(query="вопрос"), _cfg())
    assert out["query_analysis"]["is_concrete"] is True
    assert out["active_query"] == "вопрос"


def test_analyze_query_prompt_and_llm_params(monkeypatch):
    captured = {}

    def fake_llm(messages, config, node_name, provider_override=None,
                 model_override=None):
        captured.update(messages=messages, config=config, node_name=node_name,
                        provider_override=provider_override,
                        model_override=model_override)
        return '{"is_concrete": true, "key_terms": [], "suggested_clarification": null}'

    monkeypatch.setattr(qa_graph, "llm_chat", fake_llm)
    cfg = _cfg()
    qa_graph.analyze_query(_state(query="вопрос"), cfg)

    assert captured["messages"][0]["role"] == "system"
    assert "Ты — анализатор поисковых запросов" in captured["messages"][0]["content"]
    assert "is_concrete" in captured["messages"][0]["content"]
    assert captured["messages"][-1] == {"role": "user", "content": "вопрос"}
    # Параметры узла теперь резолвит llm_chat по node_name (раздел 6.2):
    # узел передаёт только messages/config/node_name.
    assert captured["node_name"] == "analyze_query"
    assert captured["config"] is cfg
    assert captured["provider_override"] is None
    assert captured["model_override"] is None


def test_analyze_query_invalid_types_normalized(monkeypatch):
    monkeypatch.setattr(
        qa_graph, "llm_chat",
        lambda messages, config, node_name, **kw: (
            '{"is_concrete": "yes", "key_terms": "не список", "suggested_clarification": 123}'
        ),
    )
    out = qa_graph.analyze_query(_state(query="вопрос"), _cfg())
    assert out["query_analysis"]["is_concrete"] is True  # не bool → fallback
    assert out["query_analysis"]["key_terms"] == []
    assert out["query_analysis"]["suggested_clarification"] is None


def test_analyze_query_history_included(monkeypatch):
    from langchain_core.messages import HumanMessage

    captured = {}

    def fake_llm(messages, config, node_name, **kw):
        captured["messages"] = messages
        return '{"is_concrete": true}'

    monkeypatch.setattr(qa_graph, "llm_chat", fake_llm)
    qa_graph.analyze_query(
        _state(query="вопрос", messages=[HumanMessage(content="прошлый вопрос")]),
        _cfg(),
    )
    roles = [m["role"] for m in captured["messages"]]
    assert roles == ["system", "user", "user"]
    assert captured["messages"][1]["content"] == "прошлый вопрос"


# ─── Узел 2: search_node (раздел 5.2) ─────────────────────────────────

def test_search_node_full_pipeline(monkeypatch):
    _patch_search_deps(monkeypatch)
    points = [_point(score=0.8), _point(score=0.9, document_id="doc-2")]
    monkeypatch.setattr(qa_graph, "hybrid_search", lambda *a, **k: points)
    monkeypatch.setattr(qa_graph, "filter_by_rrf_score", lambda c, threshold=0.15: c)
    monkeypatch.setattr(
        qa_graph, "rerank_siliconflow",
        lambda q, docs, key, top_n: [0.95, 0.7],
    )

    out = qa_graph.search_node(_state(), _cfg())

    results = out["search_results"]
    assert len(results) == 2
    first = results[0]
    assert first["rank"] == 1
    assert first["score"] == 0.95  # реранк-скор
    assert first["rrf_score"] == 0.8  # point.score (RRF)
    assert first["chunk_id"] == "c1"
    assert first["document_id"] == "doc-1"
    assert first["title"] == "ГОСТ 31996-2012"
    assert first["text"] == "Текст пункта документа"
    assert first["quality"] == "точное"  # 0.95 >= QUALITY_EXACT


def test_search_node_empty_candidates(monkeypatch):
    _patch_search_deps(monkeypatch)
    monkeypatch.setattr(qa_graph, "hybrid_search", lambda *a, **k: [])
    out = qa_graph.search_node(_state(), _cfg())
    assert out == {"search_results": []}


def test_search_node_rerank_fallback_rrf_order(monkeypatch):
    _patch_search_deps(monkeypatch)
    points = [
        _point(score=0.3, document_id="a"),
        _point(score=0.9, document_id="b"),
    ]
    monkeypatch.setattr(qa_graph, "hybrid_search", lambda *a, **k: points)
    monkeypatch.setattr(qa_graph, "filter_by_rrf_score", lambda c, threshold=0.15: c)

    def rerank_down(*a, **k):
        raise RuntimeError("rerank down")

    monkeypatch.setattr(qa_graph, "rerank_siliconflow", rerank_down)

    out = qa_graph.search_node(_state(), _cfg())
    results = out["search_results"]
    assert len(results) == 2
    # Fallback: RRF-порядок (сортировка по point.score), score = rrf_score
    assert results[0]["document_id"] == "b"
    assert results[0]["score"] == 0.9
    assert "error" not in out


def test_search_node_uses_config_params(monkeypatch):
    _patch_search_deps(monkeypatch)
    captured = {}
    points = [_point(score=0.8), _point(score=0.7), _point(score=0.6)]

    def fake_hybrid(client, collection, sparse_model, query, api_key, top_k=30, payload_filter=None):
        captured.update(collection=collection, query=query, api_key=api_key, top_k=top_k)
        return points

    def fake_filter(candidates, threshold=0.15):
        captured["threshold"] = threshold
        return candidates

    def fake_rerank(query, docs, api_key, top_n):
        captured["top_n"] = top_n
        return [0.9, 0.5, 0.1]

    monkeypatch.setattr(qa_graph, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qa_graph, "filter_by_rrf_score", fake_filter)
    monkeypatch.setattr(qa_graph, "rerank_siliconflow", fake_rerank)

    cfg = _cfg(retrieve_k=10, final_k=2, rrf_threshold=0.2)
    out = qa_graph.search_node(_state(active_query="активный запрос"), cfg)

    assert captured["query"] == "активный запрос"
    assert captured["collection"] == "technical_standard"
    assert captured["top_k"] == 10
    assert captured["threshold"] == 0.2
    assert captured["top_n"] == 2
    assert captured["api_key"] == "k-sf"  # SiliconFlow для embedding/rerank
    assert len(out["search_results"]) == 2  # final_k ограничил выдачу


def test_search_node_hybrid_search_error(monkeypatch):
    _patch_search_deps(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(qa_graph, "hybrid_search", boom)
    out = qa_graph.search_node(_state(), _cfg())
    assert out["search_results"] == []
    assert "Поиск временно недоступен" in out["error"]


def test_search_node_uses_query_when_active_missing(monkeypatch):
    _patch_search_deps(monkeypatch)
    captured = {}

    def fake_hybrid(client, collection, sparse_model, query, api_key, top_k=30, payload_filter=None):
        captured["query"] = query
        return []

    monkeypatch.setattr(qa_graph, "hybrid_search", fake_hybrid)
    qa_graph.search_node(_state(active_query=""), _cfg())
    assert captured["query"] == "вопрос"


# ─── user_query: изоляция целенаправленного поиска от истории ──────────

def test_search_node_targeted_refs_from_user_query_not_history(monkeypatch):
    """«табл. 19» в контексте истории (query) не включает targeted-поиск,
    если нового вопроса (user_query) она не касается (инцидент СП 52.13330)."""
    _patch_search_deps(monkeypatch)
    monkeypatch.setattr(qa_graph, "hybrid_search", lambda *a, **k: [])

    blob = ("Предыдущий вопрос: ...\nПредыдущий ответ: ... [ГОСТ 31996-2012, табл. 19]\n\n"
            "Новый вопрос: По СП 52.13330 какая норма освещенности в детском саду")
    out = qa_graph.search_node(
        _state(query=blob, active_query=blob,
               user_query="По СП 52.13330 какая норма освещенности в детском саду"),
        _cfg(),
    )
    assert out["search_results"] == []  # ушёл в hybrid, а не в targeted


def test_search_node_targeted_ref_query_passed(monkeypatch):
    """search_node передаёт user_query в _targeted_search_results как ref_query,
    сужение по документу продолжит видеть полный query."""
    _patch_search_deps(monkeypatch)
    captured = {}

    def fake_targeted(query, client, cfg, ref_query=None):
        captured["query"] = query
        captured["ref_query"] = ref_query
        return None

    monkeypatch.setattr(qa_graph, "_targeted_search_results", fake_targeted)
    monkeypatch.setattr(qa_graph, "hybrid_search", lambda *a, **k: [])
    qa_graph.search_node(
        _state(query="блоб с историей", active_query="блоб с историей",
               user_query="покажи таблицу 19"), _cfg())
    assert captured["ref_query"] == "покажи таблицу 19"
    assert captured["query"] == "блоб с историей"


def test_initial_state_user_query_defaults_to_query():
    g = object.__new__(qa_graph.QAGraph)
    assert g._initial_state("вопрос")["user_query"] == "вопрос"
    assert g._initial_state("блоб", "чистый вопрос")["user_query"] == "чистый вопрос"


# ─── Фикс 3.1: жизненный цикл QdrantClient в search_node ──────────────

class _FakeQdrantClient:
    """Тестовая замена QdrantClient с подсчётом close()."""

    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def test_search_node_closes_qdrant_client(monkeypatch):
    """После поиска клиент закрывается (замок базы освобождается)."""
    fake = _FakeQdrantClient()
    monkeypatch.setattr(qa_graph, "_get_qdrant_client", lambda path: fake)
    monkeypatch.setattr(qa_graph, "_get_sparse_model", lambda: object())
    monkeypatch.setattr(qa_graph, "hybrid_search", lambda *a, **k: [])
    monkeypatch.setattr(qa_graph, "filter_by_rrf_score", lambda c, threshold=0.15: c)
    monkeypatch.setattr(qa_graph, "rerank_siliconflow", lambda q, d, key, top_n: [])

    out = qa_graph.search_node(_state(), _cfg())
    assert out == {"search_results": []}
    assert fake.closed == 1
    assert qa_graph._qdrant_clients == {}  # кэш пуст — клиент забыт


def test_search_node_closes_client_on_hybrid_error(monkeypatch):
    """При падении hybrid_search клиент всё равно закрывается."""
    fake = _FakeQdrantClient()
    monkeypatch.setattr(qa_graph, "_get_qdrant_client", lambda path: fake)
    monkeypatch.setattr(qa_graph, "_get_sparse_model", lambda: object())

    def boom(*a, **k):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(qa_graph, "hybrid_search", boom)
    out = qa_graph.search_node(_state(), _cfg())
    assert "Поиск временно недоступен" in out["error"]
    assert fake.closed == 1


def test_search_node_closes_client_on_targeted_path(monkeypatch):
    """Целенаправленный поиск тоже закрывает клиент (3.1)."""
    fake = _FakeQdrantClient()
    monkeypatch.setattr(qa_graph, "_get_qdrant_client", lambda path: fake)
    monkeypatch.setattr(qa_graph, "_get_sparse_model", lambda: object())
    monkeypatch.setattr(
        qa_graph, "_targeted_search_results",
        lambda query, client, cfg, ref_query=None: [{"chunk_id": "c1", "score": 1.0}],
    )
    monkeypatch.setattr(qa_graph, "hybrid_search", lambda *a, **k: (_ for _ in ()).throw(AssertionError("not called")))

    out = qa_graph.search_node(_state(query="покажи таблицу Б.1"), _cfg())
    assert out["search_results"] == [{"chunk_id": "c1", "score": 1.0}]
    assert fake.closed == 1


def test_close_qdrant_client_idempotent():
    """Повторный close не падает и не дублирует эффект (3.1)."""
    fake = _FakeQdrantClient()
    qa_graph._close_qdrant_client("/tmp/x", fake)
    qa_graph._close_qdrant_client("/tmp/x", fake)  # второй раз — безопасно
    assert fake.closed == 2  # close() у QdrantClient идемпотентен, у нас счётчик


# ─── Фикс 3.3: целенаправленный поиск таблиц/рисунков ─────────────────

def test_targeted_search_results_no_refs_returns_none(monkeypatch):
    """Запрос без явной ссылки «Таблица/Рисунок N» → None (семантический поиск)."""
    monkeypatch.setattr(
        qa_graph, "_find_asset_chunks_by_caption",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должен вызываться")),
    )
    assert qa_graph._targeted_search_results("Какие токи допустимы?", object(), _cfg()) is None


def test_targeted_search_results_finds_chunk_by_caption(monkeypatch):
    """«покажи таблицу Б.1 СП 89.13330» → чанк из Qdrant со score=1.0."""
    monkeypatch.setattr(
        qa_graph, "_find_asset_chunks_by_caption",
        lambda asset_type, ref_num: [("СП 89.13330.2016", "sp89_kotelnye/_h59")],
    )
    monkeypatch.setattr(
        qa_graph, "_scroll_chunks_by_ids",
        lambda client, collection, chunk_ids: [_point(chunk_id="sp89_kotelnye/_h59", document_id="СП 89.13330.2016")],
    )
    out = qa_graph._targeted_search_results("покажи таблицу Б.1 СП 89.13330", object(), _cfg())
    assert out is not None
    assert len(out) == 1
    assert out[0]["chunk_id"] == "sp89_kotelnye/_h59"
    assert out[0]["score"] == 1.0  # → evaluate_results направит в generate_answer


def test_targeted_search_results_chunk_not_found_returns_none(monkeypatch):
    """Caption есть в assets, но чанка нет в Qdrant → None (семантический fallback)."""
    monkeypatch.setattr(
        qa_graph, "_find_asset_chunks_by_caption",
        lambda asset_type, ref_num: [("СП 89.13330.2016", "sp89_kotelnye/_h59")],
    )
    monkeypatch.setattr(qa_graph, "_scroll_chunks_by_ids", lambda *a, **k: [])
    assert qa_graph._targeted_search_results("покажи таблицу Б.1", object(), _cfg()) is None


def test_targeted_search_results_ambiguous_doc_returns_none(monkeypatch):
    """Один номер в caption у РАЗНЫХ документов и запрос не называет документ
    → None: не рискуем отправить LLM чужую таблицу, продолжаем семантический поиск."""
    monkeypatch.setattr(
        qa_graph, "_find_asset_chunks_by_caption",
        lambda asset_type, ref_num: [("Документ А", "a1"), ("Документ Б", "b1")],
    )
    monkeypatch.setattr(
        qa_graph, "_scroll_chunks_by_ids",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должен вызываться")),
    )
    assert qa_graph._targeted_search_results("покажи таблицу 19", object(), _cfg()) is None


def test_targeted_search_results_doc_name_disambiguates(monkeypatch):
    """Запрос называет документ («СП 89.13330») → берём только его чанк."""
    monkeypatch.setattr(
        qa_graph, "_find_asset_chunks_by_caption",
        lambda asset_type, ref_num: [
            ("СП 89.13330.2016", "sp89_kotelnye/_h59"),
            ("Документ Б", "b1"),
        ],
    )
    monkeypatch.setattr(
        qa_graph, "_scroll_chunks_by_ids",
        lambda client, collection, chunk_ids: [_point(chunk_id=cid, document_id="СП 89.13330.2016") for cid in chunk_ids],
    )
    out = qa_graph._targeted_search_results("покажи таблицу Б.1 СП 89.13330", object(), _cfg())
    assert out is not None
    assert [r["chunk_id"] for r in out] == ["sp89_kotelnye/_h59"]


# ─── Узел 3: evaluate_results (раздел 5.3) ────────────────────────────

def test_evaluate_results_empty_ask_clarification():
    assert qa_graph.evaluate_results(_state(search_results=[])) == "ask_clarification"


def test_evaluate_results_good_generate_answer():
    state = _state(search_results=[{"score": 0.85}, {"score": 0.3}])
    assert qa_graph.evaluate_results(state) == "generate_answer"


def test_evaluate_results_medium_reformulate():
    state = _state(search_results=[{"score": 0.55}], reformulate_count=0)
    assert qa_graph.evaluate_results(state) == "reformulate_query"


def test_evaluate_results_medium_max_attempts_ask():
    state = _state(search_results=[{"score": 0.55}], reformulate_count=2)
    assert qa_graph.evaluate_results(state) == "ask_clarification"


def test_evaluate_results_bad_ask_clarification():
    state = _state(search_results=[{"score": 0.2}])
    assert qa_graph.evaluate_results(state) == "ask_clarification"


def test_evaluate_results_boundaries():
    # 0.7 — не > 0.7 → reformulate (при попытках < 2)
    assert qa_graph.evaluate_results(_state(search_results=[{"score": 0.7}], reformulate_count=0)) == "reformulate_query"
    # 0.4 — >= 0.4 → reformulate
    assert qa_graph.evaluate_results(_state(search_results=[{"score": 0.4}], reformulate_count=0)) == "reformulate_query"
    # 0.71 — > 0.7 → generate_answer
    assert qa_graph.evaluate_results(_state(search_results=[{"score": 0.71}])) == "generate_answer"
    # 0.39 — < 0.4 → ask_clarification
    assert qa_graph.evaluate_results(_state(search_results=[{"score": 0.39}])) == "ask_clarification"


def test_evaluate_results_missing_score_treated_as_zero():
    state = _state(search_results=[{"document_id": "x"}])
    assert qa_graph.evaluate_results(state) == "ask_clarification"


def test_evaluate_results_uses_config_thresholds():
    cfg = _cfg(score_good_threshold=0.9, score_medium_threshold=0.8, max_reformulate_attempts=1)
    assert qa_graph.evaluate_results(_state(search_results=[{"score": 0.85}]), cfg) == "reformulate_query"
    assert qa_graph.evaluate_results(_state(search_results=[{"score": 0.95}]), cfg) == "generate_answer"
    assert qa_graph.evaluate_results(_state(search_results=[{"score": 0.85}], reformulate_count=1), cfg) == "ask_clarification"


# ─── Узел 4: reformulate_query (раздел 5.4) ───────────────────────────

def test_reformulate_query_success(monkeypatch):
    monkeypatch.setattr(qa_graph, "llm_chat",
                        lambda messages, config, node_name, **kw: "сечение кабеля по ГОСТ 31996")
    out = qa_graph.reformulate_query(
        _state(
            query="про кабели", active_query="кабели",
            search_results=[{"document_id": "d1", "title": "ГОСТ 31996-2012", "score": 0.5, "text": "текст"}],
        ),
        _cfg(),
    )
    assert out["active_query"] == "сечение кабеля по ГОСТ 31996"
    assert out["reformulate_count"] == 1


def test_reformulate_query_empty_response_keeps_query(monkeypatch):
    monkeypatch.setattr(qa_graph, "llm_chat",
                        lambda messages, config, node_name, **kw: "   ")
    out = qa_graph.reformulate_query(
        _state(query="вопрос", active_query="активный", reformulate_count=1), _cfg()
    )
    assert "active_query" not in out  # не меняется
    assert out["reformulate_count"] == 2


def test_reformulate_query_llm_error_keeps_query(monkeypatch):
    def boom(messages, config, node_name, **kw):
        raise RuntimeError("down")

    monkeypatch.setattr(qa_graph, "llm_chat", boom)
    out = qa_graph.reformulate_query(_state(query="вопрос", active_query="активный"), _cfg())
    assert "active_query" not in out
    assert out["reformulate_count"] == 1


def test_reformulate_query_strips_quotes(monkeypatch):
    monkeypatch.setattr(qa_graph, "llm_chat",
                        lambda messages, config, node_name, **kw: '"новый запрос"')
    out = qa_graph.reformulate_query(_state(query="вопрос", active_query="старый"), _cfg())
    assert out["active_query"] == "новый запрос"


def test_reformulate_query_prompt_and_params(monkeypatch):
    captured = {}

    def fake_llm(messages, config, node_name, provider_override=None,
                 model_override=None):
        captured.update(messages=messages, node_name=node_name)
        return "новый запрос"

    monkeypatch.setattr(qa_graph, "llm_chat", fake_llm)
    qa_graph.reformulate_query(
        _state(
            query="вопрос", active_query="активный",
            search_results=[{"document_id": "d1", "title": "ГОСТ 31996-2012", "score": 0.55, "text": "пункт 1 текст"}],
            reformulate_count=1,
        ),
        _cfg(),
    )

    system = captured["messages"][0]["content"]
    assert "Исходный запрос: активный" in system
    assert "ГОСТ 31996-2012" in system
    assert "Переформулируй запрос" in system
    assert captured["node_name"] == "reformulate_query"  # параметры узла — в llm_chat
    # Активный запрос передаётся как user-сообщение рядом с system-промптом.
    assert len(captured["messages"]) == 2
    assert captured["messages"][-1] == {"role": "user", "content": "активный"}


# ─── Узел 5: ask_clarification (раздел 5.5) ───────────────────────────

def test_ask_clarification_interrupt_and_merge(monkeypatch):
    monkeypatch.setattr(
        qa_graph, "llm_chat",
        lambda messages, config, node_name, **kw: '{"questions": ["Какой ГОСТ?", "Какой аспект?"]}',
    )
    captured = {}

    def fake_interrupt(payload):
        captured["payload"] = payload
        return "нужен ГОСТ 31996"

    monkeypatch.setattr(qa_graph, "interrupt", fake_interrupt)

    out = qa_graph.ask_clarification(_state(query="про кабели"), _cfg())

    assert captured["payload"] == "• Какой ГОСТ?\n• Какой аспект?"
    assert out["active_query"] == "про кабели. Уточнение: нужен ГОСТ 31996"
    assert out["needs_clarification"] is None
    assert out["reformulate_count"] == 0  # сброс счётчика
    assert len(out["messages"]) == 1
    assert out["messages"][0].type == "human"
    assert out["messages"][0].content == "нужен ГОСТ 31996"


def test_ask_clarification_prompt_and_params(monkeypatch):
    captured = {}

    def fake_llm(messages, config, node_name, provider_override=None,
                 model_override=None):
        captured.update(messages=messages, node_name=node_name)
        return '{"questions": ["Уточните документ?"]}'

    monkeypatch.setattr(qa_graph, "llm_chat", fake_llm)
    monkeypatch.setattr(qa_graph, "interrupt", lambda payload: "ответ")

    qa_graph.ask_clarification(_state(query="расскажи про нормативы"), _cfg())

    system = captured["messages"][0]["content"]
    assert "Пользователь спросил: расскажи про нормативы" in system
    assert "Сформулируй 1–2 КОНКРЕТНЫХ уточняющих вопроса" in system
    assert captured["node_name"] == "ask_clarification"  # параметры узла — в llm_chat


def test_ask_clarification_non_json_fallback_question(monkeypatch):
    monkeypatch.setattr(qa_graph, "llm_chat",
                        lambda messages, config, node_name, **kw: "не json")
    captured = {}

    def fake_interrupt(payload):
        captured["payload"] = payload
        return "ответ"

    monkeypatch.setattr(qa_graph, "interrupt", fake_interrupt)

    out = qa_graph.ask_clarification(_state(query="вопрос"), _cfg())
    assert captured["payload"].startswith("• Уточните, пожалуйста")
    assert out["active_query"].endswith("Уточнение: ответ")


def test_ask_clarification_empty_user_response(monkeypatch):
    monkeypatch.setattr(qa_graph, "llm_chat",
                        lambda messages, config, node_name, **kw: '{"questions": ["q1"]}')
    monkeypatch.setattr(qa_graph, "interrupt", lambda payload: "")
    out = qa_graph.ask_clarification(_state(query="вопрос"), _cfg())
    assert out["active_query"] == "вопрос. Уточнение: "
    assert "messages" not in out  # пустой ответ не добавляется в историю


# ─── Узел 6: generate_answer (раздел 5.6) ─────────────────────────────

def test_generate_answer_empty_results_no_llm(monkeypatch):
    calls = {"n": 0}

    def fake_llm(*a, **k):
        calls["n"] += 1
        return "ответ"

    monkeypatch.setattr(qa_graph, "llm_chat", fake_llm)
    out = qa_graph.generate_answer(_state(search_results=[]), _cfg())
    assert calls["n"] == 0
    assert "не найдено релевантных" in out["final_answer"]


def test_generate_answer_with_citations(monkeypatch):
    calls = {"n": 0}

    def fake_llm(*a, **k):
        calls["n"] += 1
        return "Согласно [ГОСТ 31996-2012, п. 5.2] сечение кабеля ..."

    monkeypatch.setattr(qa_graph, "llm_chat", fake_llm)
    out = qa_graph.generate_answer(
        _state(search_results=[{"document_id": "d1", "score": 0.8, "text": "текст"}]),
        _cfg(),
    )
    assert calls["n"] == 1
    assert "ГОСТ 31996-2012" in out["final_answer"]


def test_generate_answer_regenerates_without_citations(monkeypatch):
    answers = iter([
        "Ответ без цитат",
        "Ответ с цитатой [ГОСТ 31996-2012, п. 5.2]",
    ])
    captured = []

    def fake_llm(messages, config, node_name, **kw):
        captured.append(messages[0]["content"])
        return next(answers)

    monkeypatch.setattr(qa_graph, "llm_chat", fake_llm)
    out = qa_graph.generate_answer(
        _state(search_results=[{"document_id": "d1", "score": 0.8, "text": "текст"}]),
        _cfg(),
    )
    assert len(captured) == 2
    assert "ОБЯЗАТЕЛЬНО укажи источник" in captured[1]
    assert out["final_answer"] == "Ответ с цитатой [ГОСТ 31996-2012, п. 5.2]"


def test_generate_answer_two_attempts_still_no_citations(monkeypatch):
    calls = {"n": 0}

    def fake_llm(messages, config, node_name, **kw):
        calls["n"] += 1
        return "всё ещё без цитат"

    monkeypatch.setattr(qa_graph, "llm_chat", fake_llm)
    out = qa_graph.generate_answer(
        _state(search_results=[{"document_id": "d1", "score": 0.8, "text": "текст"}]),
        _cfg(),
    )
    assert calls["n"] == 2  # max 1 доп. попытка
    assert out["final_answer"] == "всё ещё без цитат"


def test_generate_answer_prompt_and_params(monkeypatch):
    captured = {}

    def fake_llm(messages, config, node_name, provider_override=None,
                 model_override=None):
        captured.update(messages=messages, node_name=node_name)
        return "ответ [ГОСТ 31996-2012, п. 1]"

    monkeypatch.setattr(qa_graph, "llm_chat", fake_llm)
    qa_graph.generate_answer(
        _state(
            query="какой кабель",
            search_results=[
                {"document_id": "d1", "title": "ГОСТ 31996-2012", "section_path": "Раздел 1",
                 "score": 0.85, "text": "Текст пункта"}
            ],
        ),
        _cfg(),
    )

    system = captured["messages"][0]["content"]
    assert "Ты — эксперт по нормативным документам" in system
    assert "ВОПРОС: какой кабель" in system
    assert "[1] d1" in system
    assert "ГОСТ 31996-2012" in system
    assert captured["node_name"] == "generate_answer"  # параметры узла — в llm_chat
    # Исходный запрос передаётся как user-сообщение рядом с system-промптом.
    assert len(captured["messages"]) == 2
    assert captured["messages"][-1] == {"role": "user", "content": "какой кабель"}


def test_generate_answer_llm_error_fallback(monkeypatch):
    def boom(messages, config, node_name, **kw):
        raise RuntimeError("llm down")

    monkeypatch.setattr(qa_graph, "llm_chat", boom)
    out = qa_graph.generate_answer(
        _state(search_results=[{"document_id": "d1", "score": 0.8, "text": "текст"}]),
        _cfg(),
    )
    assert "временно недоступен" in out["final_answer"]
    assert "LLM API недоступен" in out["error"]


# ─── Расчёты по формулам: execute_calculation ─────────────────────────

def test_execute_calculation_no_code_block_unchanged():
    answer = "Просто текст с цитатой [ГОСТ 31996-2012, п. 5.2]"
    assert qa_graph.execute_calculation(answer) == answer


def test_execute_calculation_executes_python_block():
    answer = "Расчёт:\n```python\nprint(2 + 3)\n```"
    out = qa_graph.execute_calculation(answer)
    assert "```python\nprint(2 + 3)\n```" in out
    assert "**Результат:**" in out
    assert "5" in out


def test_execute_calculation_inserts_result_after_block():
    answer = (
        "Формула:\n```python\n"
        "h = 30 / 1.2\n"
        "print(f\"h = {h:.1f}\")\n"
        "```\nКонец"
    )
    out = qa_graph.execute_calculation(answer)
    # Результат вставляется после блока кода, до остального текста
    assert out.index("**Результат:**") > out.index("```python")
    assert out.index("Конец") > out.index("**Результат:**")
    assert "h = 25.0" in out


def test_execute_calculation_multiple_blocks():
    answer = (
        "```python\nprint(1)\n```\n"
        "```python\nprint(2)\n```"
    )
    out = qa_graph.execute_calculation(answer)
    assert out.count("**Результат:**") == 2
    assert "1" in out
    assert "2" in out


def test_execute_calculation_bans_input():
    answer = "```python\nx = input('> ')\nprint(x)\n```"
    out = qa_graph.execute_calculation(answer)
    assert "интерактивный ввод" in out


def test_execute_calculation_bans_while_true():
    answer = "```python\nwhile True:\n    pass\n```"
    out = qa_graph.execute_calculation(answer)
    assert "бесконечный цикл" in out


def test_execute_calculation_runtime_error():
    answer = "```python\nraise ValueError('boom')\n```"
    out = qa_graph.execute_calculation(answer)
    assert "Ошибка" in out
    assert "boom" in out


def test_execute_calculation_timeout():
    # Бесконечный цикл, не ловящийся строковым запретом (не «while True»)
    answer = "```python\nfor _ in iter(int, 1):\n    pass\n```"
    out = qa_graph.execute_calculation(answer, timeout=1)
    assert "превышено время исполнения" in out


def test_execute_calculation_no_output():
    answer = "```python\nx = 1 + 1\n```"
    out = qa_graph.execute_calculation(answer)
    assert "выполнено без вывода" in out


def test_execute_calculation_ignores_non_python_fences():
    answer = "```bash\necho hi\n```"
    assert qa_graph.execute_calculation(answer) == answer


def test_generate_answer_prompt_has_calculation_rule(monkeypatch):
    captured = {}

    def fake_llm(messages, config, node_name, **kw):
        captured["system"] = messages[0]["content"]
        return "ответ [ГОСТ 31996-2012, п. 1]"

    monkeypatch.setattr(qa_graph, "llm_chat", fake_llm)
    qa_graph.generate_answer(
        _state(
            query="высота молниеотвода",
            search_results=[{"document_id": "d1", "score": 0.8, "text": "текст"}],
        ),
        _cfg(),
    )
    system = captured["system"]

# ══════════════════════════════════════════════════════════════════════
# Юнит 3: сборка графа и QAGraph (разделы 4, 2.2, 6.4)
# ══════════════════════════════════════════════════════════════════════

def _patch_qa_env(monkeypatch):
    """Замокать тяжёлые зависимости QAGraph (клиенты/эмбеддер)."""
    monkeypatch.setattr(qa_graph, "_get_qdrant_client", lambda path: object())
    monkeypatch.setattr(qa_graph, "_get_sparse_model", lambda: object())


def _patch_good_search(monkeypatch, rerank_score=0.9, rrf_score=0.9):
    """Мок поиска: один результат с реранк-скором > 0.7."""
    points = [_point(score=rrf_score, document_id="doc-1")]
    monkeypatch.setattr(qa_graph, "hybrid_search", lambda *a, **k: points)
    monkeypatch.setattr(qa_graph, "filter_by_rrf_score", lambda c, threshold=0.15: c)
    monkeypatch.setattr(
        qa_graph, "rerank_siliconflow", lambda q, d, k, top_n: [rerank_score]
    )


def _patch_llm_sequence(monkeypatch, responses):
    """llm_chat отдаёт ответы из iter() по одному за вызов."""
    it = iter(responses)
    monkeypatch.setattr(
        qa_graph, "llm_chat",
        lambda messages, config, node_name, **kw: next(it),
    )


# ─── _route_after_analyze (таблица 4.2) ───────────────────────────────

def test_route_after_analyze_concrete_to_search():
    state = _state(query="вопрос", query_analysis={"is_concrete": True})
    assert qa_graph._route_after_analyze(state) == "search"


def test_route_after_analyze_abstract_to_ask():
    state = _state(query="вопрос", query_analysis={"is_concrete": False})
    assert qa_graph._route_after_analyze(state) == "ask_clarification"


def test_route_after_analyze_missing_falls_back_to_search():
    assert qa_graph._route_after_analyze(_state(query_analysis=None)) == "search"
    assert qa_graph._route_after_analyze(_state()) == "search"  # ключа нет


# ─── build_graph (раздел 4) ───────────────────────────────────────────

def test_build_graph_compiled_with_all_nodes():
    graph = qa_graph.build_graph()
    assert hasattr(graph, "invoke")
    assert hasattr(graph, "stream")
    nodes = set(graph.get_graph().nodes)
    for expected in (
        "analyze_query", "search", "evaluate_results",
        "reformulate_query", "ask_clarification", "generate_answer",
    ):
        assert expected in nodes


def test_build_graph_edges_exactly_per_section_42():
    """Топология рёбер — строго таблица 4.2, без лишних переходов."""
    graph = qa_graph.build_graph()
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    expected = {
        ("__start__", "analyze_query"),
        ("analyze_query", "search"),
        ("analyze_query", "ask_clarification"),
        ("ask_clarification", "search"),
        ("search", "evaluate_results"),
        ("evaluate_results", "generate_answer"),
        ("evaluate_results", "reformulate_query"),
        ("evaluate_results", "ask_clarification"),
        ("reformulate_query", "search"),
        ("generate_answer", "__end__"),
    }
    assert edges == expected


def test_build_graph_default_checkpointer():
    """Без явного checkpointer граф компилируется с MemorySaver
    (без него interrupt() в ask_clarification не работает, раздел 5.5)."""
    graph = qa_graph.build_graph()
    assert graph.checkpointer is not None


# ─── QAGraph.__init__ (разделы 2.2, 6.4) ──────────────────────────────

def test_qagraph_init_does_not_open_qdrant_client(monkeypatch):
    """Фикс 3.1: QdrantClient при инициализации НЕ открывается.

    Локальный Qdrant держит эксклюзивный файловый замок на папку базы,
    поэтому клиент создаётся на время каждого поиска в search_node и
    закрывается в finally — иначе create_index.py нельзя запустить при
    работающем боте без pkill. Sparse-эмбеддер и API-ключ по-прежнему
    инициализируются.
    """
    calls = {}

    def fake_client(path):
        calls["path"] = path
        return "client"

    def fake_sparse():
        return "sparse"

    monkeypatch.setattr(qa_graph, "_get_qdrant_client", fake_client)
    monkeypatch.setattr(qa_graph, "_get_sparse_model", fake_sparse)

    cfg = _cfg(qdrant_path="/tmp/qdrant-test")
    qa = qa_graph.QAGraph(cfg)

    assert calls == {}  # клиент при инициализации не создаётся
    assert qa.client is None
    assert qa.sparse_model == "sparse"
    assert qa.api_key is None  # API key is resolved lazily by search calls
    # Provider keys are resolved lazily by individual API calls.
    assert not hasattr(qa, "deepseek_api_key")
    assert qa.graph is not None
    assert hasattr(qa.graph, "invoke")
    assert qa.checkpointer is not None


def test_qagraph_init_default_config(monkeypatch):
    _patch_qa_env(monkeypatch)
    qa = qa_graph.QAGraph(_cfg())
    assert isinstance(qa.config, qa_graph.QAGraphConfig)
    assert qa.config.collection == "technical_standard"
    assert qa.config.qdrant_path == "./qdrant_data"


# ─── QAGraph.run / stream / resume (приёмка юнита 3) ──────────────────

def test_qagraph_run_good_query_returns_answer(monkeypatch):
    """Приёмка: хороший запрос → ответ (без уточнения)."""
    _patch_qa_env(monkeypatch)
    _patch_good_search(monkeypatch)
    _patch_llm_sequence(monkeypatch, [
        '{"is_concrete": true, "key_terms": ["кабель"], "suggested_clarification": null}',
        "Согласно [ГОСТ 31996-2012, п. 5.2] сечение кабеля ...",
    ])

    qa = qa_graph.QAGraph(_cfg())
    result = qa.run("какое сечение кабеля по ГОСТ 31996")

    assert result["final_answer"]
    assert "ГОСТ 31996-2012" in result["final_answer"]
    assert "__interrupt__" not in result
    assert len(result["search_results"]) == 1
    assert result["search_results"][0]["document_id"] == "doc-1"


def test_qagraph_run_bad_query_interrupts(monkeypatch):
    """Приёмка: абстрактный запрос → interrupt с уточняющим вопросом."""
    _patch_qa_env(monkeypatch)
    _patch_llm_sequence(monkeypatch, [
        '{"is_concrete": false, "key_terms": [], '
        '"suggested_clarification": "Укажите номер документа"}',
        '{"questions": ["Какой ГОСТ вас интересует?"]}',
    ])
    # hybrid_search НЕ мокается: на этом пути он не должен вызываться

    qa = qa_graph.QAGraph(_cfg())
    result = qa.run("расскажи про нормативы")

    assert "__interrupt__" in result
    assert result.get("final_answer") is None
    assert "Какой ГОСТ вас интересует?" in result["__interrupt__"][0].value


def test_qagraph_resume_after_interrupt_answers(monkeypatch):
    """Приёмка: плохой запрос → уточнение → ответ пользователя → ответ."""
    _patch_qa_env(monkeypatch)
    _patch_good_search(monkeypatch)
    _patch_llm_sequence(monkeypatch, [
        '{"is_concrete": false, "key_terms": [], "suggested_clarification": null}',
        '{"questions": ["Какой ГОСТ вас интересует?"]}',
        # при resume узел ask_clarification переисполняется (LangGraph):
        '{"questions": ["Какой ГОСТ вас интересует?"]}',
        "По ГОСТ 31996 [ГОСТ 31996-2012, п. 5.2] сечение кабеля ...",
    ])

    qa = qa_graph.QAGraph(_cfg())
    first = qa.run("расскажи про нормативы")
    assert "__interrupt__" in first
    assert first.get("final_answer") is None

    second = qa.resume("нужен ГОСТ 31996")

    assert second["final_answer"]
    assert "ГОСТ 31996-2012" in second["final_answer"]
    assert "__interrupt__" not in second
    assert second["active_query"] == "расскажи про нормативы. Уточнение: нужен ГОСТ 31996"
    assert second["reformulate_count"] == 0  # сброс счётчика после уточнения


def test_qagraph_stream_yields_node_updates(monkeypatch):
    """stream() — режим отслеживания прогресса по узлам (updates)."""
    _patch_qa_env(monkeypatch)
    _patch_good_search(monkeypatch)
    _patch_llm_sequence(monkeypatch, [
        '{"is_concrete": true, "key_terms": [], "suggested_clarification": null}',
        "Ответ с цитатой [ГОСТ 31996-2012, п. 1]",
    ])

    qa = qa_graph.QAGraph(_cfg())
    chunks = list(qa.stream("вопрос про кабель"))

    names = [list(c.keys())[0] for c in chunks]
    assert "analyze_query" in names
    assert "generate_answer" in names
    last = chunks[-1]
    assert "generate_answer" in last
    assert "ГОСТ 31996-2012" in last["generate_answer"]["final_answer"]


def test_qagraph_resume_without_run_raises(monkeypatch):
    _patch_qa_env(monkeypatch)
    qa = qa_graph.QAGraph(_cfg())
    with pytest.raises(RuntimeError, match="resume"):
        qa.resume("ответ")


def test_qagraph_run_passes_config_to_nodes(monkeypatch):
    """QAGraphConfig доходит до узлов через RunnableConfig (qa_config)."""
    _patch_qa_env(monkeypatch)
    captured = {}
    points = [_point(score=0.8), _point(score=0.7)]

    def fake_hybrid(client, collection, sparse_model, query, api_key, top_k=30, payload_filter=None):
        captured.update(collection=collection, top_k=top_k)
        return points

    monkeypatch.setattr(qa_graph, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qa_graph, "filter_by_rrf_score", lambda c, threshold=0.15: c)
    monkeypatch.setattr(qa_graph, "rerank_siliconflow", lambda q, d, k, top_n: [0.9, 0.5])
    _patch_llm_sequence(monkeypatch, [
        '{"is_concrete": true, "key_terms": [], "suggested_clarification": null}',
        "Ответ [ГОСТ 31996-2012, п. 1]",
    ])

    cfg = _cfg(retrieve_k=12, final_k=1)
    qa = qa_graph.QAGraph(cfg)
    result = qa.run("вопрос")

    assert captured["collection"] == "technical_standard"
    assert captured["top_k"] == 12
    assert len(result["search_results"]) == 1  # final_k=1


def test_qagraph_medium_score_reformulates_then_answers(monkeypatch):
    """Средний скор → reformulate_query → search (2-й заход) → ответ."""
    _patch_qa_env(monkeypatch)
    search_calls = {"n": 0}

    def fake_hybrid(*a, **k):
        search_calls["n"] += 1
        return [_point(score=0.9, document_id="doc-1")]

    monkeypatch.setattr(qa_graph, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(qa_graph, "filter_by_rrf_score", lambda c, threshold=0.15: c)

    def fake_rerank(q, d, k, top_n):
        # 1-й заход — средний скор (0.5), 2-й — хороший (0.9)
        return [0.5] if search_calls["n"] == 1 else [0.9]

    monkeypatch.setattr(qa_graph, "rerank_siliconflow", fake_rerank)
    _patch_llm_sequence(monkeypatch, [
        '{"is_concrete": true, "key_terms": [], "suggested_clarification": null}',
        "сечение кабеля по ГОСТ 31996",  # reformulate_query
        "Ответ [ГОСТ 31996-2012, п. 5.2]",
    ])

    qa = qa_graph.QAGraph(_cfg())
    result = qa.run("про кабели")

    assert search_calls["n"] == 2
    assert result["reformulate_count"] == 1
    assert "ГОСТ 31996-2012" in result["final_answer"]


def test_qagraph_bad_results_after_retries_interrupts(monkeypatch):
    """После 2 переформулировок (3 поиска) со средним скором → уточнение."""
    _patch_qa_env(monkeypatch)
    monkeypatch.setattr(qa_graph, "hybrid_search", lambda *a, **k: [_point(score=0.9)])
    monkeypatch.setattr(qa_graph, "filter_by_rrf_score", lambda c, threshold=0.15: c)
    monkeypatch.setattr(qa_graph, "rerank_siliconflow", lambda q, d, k, top_n: [0.5])
    _patch_llm_sequence(monkeypatch, [
        '{"is_concrete": true, "key_terms": [], "suggested_clarification": null}',
        "переформулировка 1",
        "переформулировка 2",
        '{"questions": ["Уточните номер документа?"]}',
    ])

    qa = qa_graph.QAGraph(_cfg())
    result = qa.run("вопрос")

    assert "__interrupt__" in result
    assert result["reformulate_count"] == 2  # счётчик достиг лимита


# ══════════════════════════════════════════════════════════════════════
# Юнит 4: CLI (раздел 10)
# ══════════════════════════════════════════════════════════════════════


class FakeInterrupt:
    """Имитация langgraph Interrupt: у текста есть атрибут .value."""

    def __init__(self, value):
        self.value = value


class FakeQA:
    """Имитация QAGraph для CLI: stream/resume_stream без реального графа."""

    def __init__(self, chunks, resume_chunks=None):
        self.chunks = chunks
        self.resume_chunks = resume_chunks or []
        self.resume_calls = []

    def stream(self, query):
        yield from self.chunks

    def resume_stream(self, user_response):
        self.resume_calls.append(user_response)
        yield from self.resume_chunks


def _capture():
    """Возвращает print_fn и список собранных строк."""
    lines = []

    def _fn(*args):
        lines.append(" ".join(str(a) for a in args))

    return _fn, lines


# ─── build_parser ─────────────────────────────────────────────────────

def test_cli_parser_query():
    args = qa_graph.build_parser().parse_args(["--query", "высота молниеотвода зона Б"])
    assert args.query == "высота молниеотвода зона Б"
    assert args.interactive is False
    assert args.verbose is False
    assert args.qdrant_path == "./qdrant_data"
    assert args.api_key is None
    assert args.config == qa_graph.DEFAULT_SEARCH_CONFIG_PATH
    assert args.llm_provider is None
    assert args.llm_model is None


def test_cli_parser_interactive():
    args = qa_graph.build_parser().parse_args(["--interactive", "--verbose"])
    assert args.query is None
    assert args.interactive is True
    assert args.verbose is True


def test_cli_parser_path_and_key():
    args = qa_graph.build_parser().parse_args(
        ["--query", "q", "--qdrant-path", "/tmp/qd", "--api-key", "k-test"]
    )
    assert args.qdrant_path == "/tmp/qd"
    assert args.api_key == "k-test"


def test_cli_parser_llm_args():
    args = qa_graph.build_parser().parse_args(
        ["--query", "q",
         "--config", "/tmp/cfg.yaml",
         "--providers_config", "/tmp/providers.yaml",
         "--llm-provider", "siliconflow",
         "--llm-model", "Qwen/Qwen3-32B"]
    )
    assert args.config == "/tmp/cfg.yaml"
    assert args.llm_provider == "siliconflow"
    assert args.llm_model == "Qwen/Qwen3-32B"


def test_cli_parser_no_args():
    args = qa_graph.build_parser().parse_args([])
    assert args.query is None
    assert args.interactive is False


# ─── _log_node_step / _print_interrupt_value ──────────────────────────

def test_cli_log_node_step_analyze():
    fn, lines = _capture()
    qa_graph._log_node_step("analyze_query",
                            {"query_analysis": {"is_concrete": True}},
                            verbose=False, print_fn=fn)
    assert lines == ["  [analyze_query] is_concrete=True"]


def test_cli_log_node_step_search_verbose_shows_results():
    fn, lines = _capture()
    update = {"search_results": [
        {"rank": 1, "title": "СП 89.13330.2016", "score": 0.92},
        {"rank": 2, "title": "ГОСТ 31996-2012", "score": 0.5},
    ]}
    qa_graph._log_node_step("search", update, verbose=True, print_fn=fn)
    assert lines[0] == "  [search] результатов: 2"
    assert lines[1] == "      1. СП 89.13330.2016 | score=0.920"
    assert lines[2] == "      2. ГОСТ 31996-2012 | score=0.500"


def test_cli_log_node_step_search_nonverbose_counts_only():
    fn, lines = _capture()
    update = {"search_results": [{"rank": 1, "title": "X", "score": 0.9}]}
    qa_graph._log_node_step("search", update, verbose=False, print_fn=fn)
    assert lines == ["  [search] результатов: 1"]


def test_cli_log_node_step_generate_answer():
    fn, lines = _capture()
    qa_graph._log_node_step("generate_answer", {}, verbose=False, print_fn=fn)
    assert lines == ["  [generate_answer] ответ сформирован"]


def test_cli_print_interrupt_value():
    fn, lines = _capture()
    value = qa_graph._print_interrupt_value((FakeInterrupt("Какой ГОСТ?"),), fn)
    assert value == "Какой ГОСТ?"


def test_cli_print_interrupt_value_empty():
    fn, lines = _capture()
    assert qa_graph._print_interrupt_value((), fn) is None
    assert qa_graph._print_interrupt_value(None, fn) is None


# ─── run_single_query ─────────────────────────────────────────────────

def test_cli_run_single_query_good_path():
    """Хороший запрос: узлы логируются, ответ печатается, interrupt нет."""
    chunks = [
        {"analyze_query": {"query_analysis": {"is_concrete": True}}},
        {"search": {"search_results": [
            {"rank": 1, "title": "СП 89.13330.2016", "score": 0.9},
        ]}},
        {"evaluate_results": {}},
        {"generate_answer": {"final_answer": "Высота зоны Б — 20 м [СП 89.13330.2016, п. 16.1]"}},
    ]
    qa = FakeQA(chunks)
    fn, lines = _capture()
    result = qa_graph.run_single_query(qa, "высота молниеотвода зона Б", print_fn=fn)

    assert result == "Высота зоны Б — 20 м [СП 89.13330.2016, п. 16.1]"
    text = "\n".join(lines)
    assert "Запрос: высота молниеотвода зона Б" in text
    assert "[analyze_query]" in text
    assert "[search] результатов: 1" in text
    assert "[generate_answer]" in text
    assert "Ответ:" in text
    assert "Высота зоны Б — 20 м" in text


def test_cli_run_single_query_verbose():
    """--verbose: результаты поиска выводятся построчно."""
    chunks = [
        {"search": {"search_results": [
            {"rank": 1, "title": "СП 89.13330.2016", "score": 0.9},
        ]}},
        {"generate_answer": {"final_answer": "Ответ [СП 89.13330.2016, п. 1]"}},
    ]
    qa = FakeQA(chunks)
    fn, lines = _capture()
    qa_graph.run_single_query(qa, "вопрос", verbose=True, print_fn=fn)

    text = "\n".join(lines)
    assert "1. СП 89.13330.2016 | score=0.900" in text


def test_cli_run_single_query_interrupt_then_resume():
    """Запрос → уточнение → ответ пользователя → ответ."""
    chunks = [
        {"analyze_query": {"query_analysis": {"is_concrete": False}}},
        {"__interrupt__": (FakeInterrupt("Уточните номер документа"),)},
    ]
    resume_chunks = [
        {"ask_clarification": {"active_query": "вопрос. Уточнение: ГОСТ 31996"}},
        {"search": {"search_results": [
            {"rank": 1, "title": "ГОСТ 31996-2012", "score": 0.9},
        ]}},
        {"evaluate_results": {}},
        {"generate_answer": {"final_answer": "Сечение — 4 мм² [ГОСТ 31996-2012, п. 5.2]"}},
    ]
    qa = FakeQA(chunks, resume_chunks)
    fn, lines = _capture()

    def fake_input(prompt):
        assert prompt == qa_graph.CLI_ANSWER_PROMPT
        return "ГОСТ 31996"

    result = qa_graph.run_single_query(qa, "какой кабель", input_fn=fake_input, print_fn=fn)

    assert qa.resume_calls == ["ГОСТ 31996"]
    assert result == "Сечение — 4 мм² [ГОСТ 31996-2012, п. 5.2]"
    text = "\n".join(lines)
    assert "[УТОЧНЕНИЕ] Уточните номер документа" in text
    assert "Сечение — 4 мм²" in text


def test_cli_run_single_query_no_answer():
    """Граф завершился без final_answer → «Ответ не получен», None."""
    chunks = [
        {"search": {"search_results": []}},
        {"evaluate_results": {}},
    ]
    qa = FakeQA(chunks)
    fn, lines = _capture()
    result = qa_graph.run_single_query(qa, "вопрос", print_fn=fn)
    assert result is None
    assert "Ответ не получен." in "\n".join(lines)


# ─── run_interactive ──────────────────────────────────────────────────

def test_cli_run_interactive_exit_command():
    qa = FakeQA([{"generate_answer": {"final_answer": "Ответ [ГОСТ 31996-2012, п. 1]"}}])
    fn, lines = _capture()
    inputs = iter(["вопрос про кабель", "exit"])

    def fake_input(prompt):
        return next(inputs)

    qa_graph.run_interactive(qa, input_fn=fake_input, print_fn=fn)

    text = "\n".join(lines)
    assert "Интерактивный режим" in text
    assert "Ответ [ГОСТ 31996-2012, п. 1]" in text
    assert "До свидания!" in text


def test_cli_run_interactive_eof_clean_exit():
    qa = FakeQA([])
    fn, lines = _capture()

    def eof(prompt):
        raise EOFError

    qa_graph.run_interactive(qa, input_fn=eof, print_fn=fn)
    assert "До свидания!" in "\n".join(lines)


def test_cli_run_interactive_ctrl_c_clean_exit():
    qa = FakeQA([])
    fn, lines = _capture()

    def ctrl_c(prompt):
        raise KeyboardInterrupt

    qa_graph.run_interactive(qa, input_fn=ctrl_c, print_fn=fn)
    assert "До свидания!" in "\n".join(lines)


def test_cli_run_interactive_skips_empty_query():
    qa = FakeQA([{"generate_answer": {"final_answer": "Ответ [ГОСТ 31996-2012, п. 1]"}}])
    fn, lines = _capture()
    inputs = iter(["", "   ", "кабель", "exit"])

    def fake_input(prompt):
        return next(inputs)

    qa_graph.run_interactive(qa, input_fn=fake_input, print_fn=fn)
    text = "\n".join(lines)
    assert text.count("Ответ [ГОСТ 31996-2012, п. 1]") == 1
    assert "До свидания!" in text


# ─── main() ───────────────────────────────────────────────────────────

def _mock_llm_config_ok(monkeypatch):
    """Замокать предварительную проверку split search/provider configs."""
    monkeypatch.setattr(qa_graph, "load_search_config", lambda path: {"nodes": {"generate_answer": {"temperature": 0.0, "max_tokens": 2048}}})
    monkeypatch.setattr(qa_graph, "_get_providers", lambda path=None: {"providers": {"deepseek": {"base_url": "x", "api_key_env": "DEEPSEEK_API_KEY", "models": {"deepseek-v4-flash": "chat"}}}, "roles": {"build_search_index": {"query_processing": {"provider": "deepseek", "model": "deepseek-v4-flash", "fallback": {}}}}})
    monkeypatch.setattr(qa_graph, "get_api_key", lambda cfg, provider, override=None: override or "k-llm")

def test_cli_main_no_args_prints_help(capsys):
    rc = qa_graph.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--query" in out
    assert "--interactive" in out


def test_cli_main_single_query(monkeypatch, capsys):
    _mock_llm_config_ok(monkeypatch)
    qa = FakeQA([{"generate_answer": {"final_answer": "Высота зоны Б — 20 м [СП 89.13330.2016, п. 16.1]"}}])
    monkeypatch.setattr(qa_graph, "QAGraph", lambda cfg: qa)
    rc = qa_graph.main(["--query", "высота молниеотвода зона Б"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Высота зоны Б — 20 м" in out


def test_cli_main_interactive_mode(monkeypatch, capsys):
    _mock_llm_config_ok(monkeypatch)
    qa = FakeQA([{"generate_answer": {"final_answer": "Ответ [ГОСТ 31996-2012, п. 1]"}}])
    monkeypatch.setattr(qa_graph, "QAGraph", lambda cfg: qa)
    inputs = iter(["exit"])

    def fake_input(prompt):
        return next(inputs)

    rc = qa_graph.main(["--interactive"], input_fn=fake_input)
    assert rc == 0
    assert "До свидания!" in capsys.readouterr().out


def test_cli_main_passes_config(monkeypatch):
    _mock_llm_config_ok(monkeypatch)
    captured = {}

    class StubQA:
        def __init__(self, cfg):
            captured["cfg"] = cfg

        def stream(self, query):
            yield from ()

        def resume_stream(self, user_response):
            yield from ()

    monkeypatch.setattr(qa_graph, "QAGraph", StubQA)
    rc = qa_graph.main(
        ["--query", "q", "--qdrant-path", "/tmp/qd", "--api-key", "k-test",
         "--config", "/tmp/cfg.yaml",
         "--providers_config", "/tmp/providers.yaml",
         "--llm-provider", "siliconflow",
         "--llm-model", "Qwen/Qwen3-32B"]
    )
    assert rc == 0
    assert captured["cfg"].qdrant_path == "/tmp/qd"
    assert captured["cfg"].llm_api_key == "k-test"
    assert captured["cfg"].search_config_path == "/tmp/cfg.yaml"
    assert captured["cfg"].providers_path == "/tmp/providers.yaml"
    assert captured["cfg"].llm_provider == "siliconflow"
    assert captured["cfg"].llm_model == "Qwen/Qwen3-32B"
    assert captured["cfg"].llm_api_key == "k-test"  # --api-key переопределяет ключ из конфига


# ─── QAGraph.resume_stream (юнит 4, CLI-вспомогательный) ─────────────

def test_qagraph_resume_stream_yields_updates_after_interrupt(monkeypatch):
    """Приёмка: плохой запрос → interrupt → resume_stream → ответ."""
    _patch_qa_env(monkeypatch)
    _patch_good_search(monkeypatch)
    _patch_llm_sequence(monkeypatch, [
        '{"is_concrete": false, "key_terms": [], "suggested_clarification": null}',
        '{"questions": ["Какой ГОСТ вас интересует?"]}',
        '{"questions": ["Какой ГОСТ вас интересует?"]}',
        "По ГОСТ 31996 [ГОСТ 31996-2012, п. 5.2] сечение кабеля ...",
    ])

    qa = qa_graph.QAGraph(_cfg())
    first = qa.run("расскажи про нормативы")
    assert "__interrupt__" in first

    chunks = list(qa.resume_stream("нужен ГОСТ 31996"))
    names = [list(c.keys())[0] for c in chunks]
    assert "ask_clarification" in names
    assert "search" in names
    assert "generate_answer" in names
    last = chunks[-1]
    assert "ГОСТ 31996-2012" in last["generate_answer"]["final_answer"]
