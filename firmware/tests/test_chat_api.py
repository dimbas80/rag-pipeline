import os
from firmware.src.chat_api import select_images
from firmware.src.chat_api import ChatSession
from firmware.src.chat_api import _inject_env_file
from firmware.src.chat_api import _EMPTY_ANSWER_FALLBACK

def test_select_images_returns_urls(monkeypatch, tmp_path):
    import sys, types
    mod=types.ModuleType('telegram_bot.asset_helpers')
    mod.resolve_images_to_send=lambda *a,**k: ([str(tmp_path/'doc'/'a.png')], ['Figure caption'])
    monkeypatch.setitem(sys.modules,'telegram_bot',types.ModuleType('telegram_bot'))
    monkeypatch.setitem(sys.modules,'telegram_bot.asset_helpers',mod)
    result=select_images([], '', 'рисунок', [], tmp_path)
    assert result[0]['url']=='/api/images/doc/a.png'
    assert result[0]['caption']=='Figure caption'
    assert result[0]['asset_type']=='image'

def test_chat_format_uses_graph_result_and_image_selection(monkeypatch, tmp_path):
    session = ChatSession.__new__(ChatSession)
    session.cfg = type("Config", (), {"base_markdown": tmp_path})()
    monkeypatch.setattr("firmware.src.chat_api.select_images", lambda *args: [{"url": "/api/images/doc/a.png"}])
    result = session._format({"final_answer": "Ответ", "cited_chunk_ids": ["c1"],
                             "search_results": [{"chunk_id": "c1", "text": "source"}]})
    assert result["type"] == "answer"
    assert result["answer"] == "Ответ"
    assert result["images"] == [{"url": "/api/images/doc/a.png"}]


# --- дедуп источников в _format (решение 31) ---

def _format_session(tmp_path):
    session = ChatSession.__new__(ChatSession)
    session.cfg = type("Config", (), {"base_markdown": tmp_path})()
    return session


def test_format_dedups_sources_by_document_id(monkeypatch, tmp_path):
    session = _format_session(tmp_path)
    monkeypatch.setattr("firmware.src.chat_api.select_images", lambda *args: [])
    result = session._format({"final_answer": "Ответ", "cited_chunk_ids": [],
                              "search_results": [
                                  {"chunk_id": "c1", "document_id": "doc-1", "title": "Док 1"},
                                  {"chunk_id": "c2", "document_id": "doc-1", "title": "Док 1"},
                                  {"chunk_id": "c3", "document_id": "doc-2", "title": "Док 2"},
                              ]})
    assert [s["chunk_id"] for s in result["sources"]] == ["c1", "c3"]  # порядок первого вхождения


def test_format_dedups_sources_fallback_title_trim_case(monkeypatch, tmp_path):
    session = _format_session(tmp_path)
    monkeypatch.setattr("firmware.src.chat_api.select_images", lambda *args: [])
    result = session._format({"final_answer": "Ответ", "cited_chunk_ids": [],
                              "search_results": [
                                  {"chunk_id": "c1", "title": "  ГОСТ Р 1.0-2019 "},
                                  {"chunk_id": "c2", "title": "гост р 1.0-2019"},
                              ]})
    assert [s["chunk_id"] for s in result["sources"]] == ["c1"]


def test_format_dedup_does_not_touch_search_results_for_images(monkeypatch, tmp_path):
    session = _format_session(tmp_path)
    captured = {}
    def fake_select_images(search_results, *args, **kwargs):
        captured["n"] = len(search_results)
        return []
    monkeypatch.setattr("firmware.src.chat_api.select_images", fake_select_images)
    result = session._format({"final_answer": "Ответ", "cited_chunk_ids": [],
                              "search_results": [
                                  {"chunk_id": "c1", "document_id": "doc-1"},
                                  {"chunk_id": "c2", "document_id": "doc-1"},
                              ]})
    assert captured["n"] == 2            # select_images видит ПОЛНЫЙ search_results
    assert len(result["sources"]) == 1   # а sources — дедуплицированный


# --- инъекция ключей config/.env в os.environ (решение t_a7d01588, архитектура §6.1) ---

def test_inject_env_file_overrides_stale_process_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ANYMODEL_API_KEY=new_key\nZ_AI_API_KEY=z_key\n", encoding="utf-8")
    monkeypatch.setenv("ANYMODEL_API_KEY", "stale_from_pipeline_load_dotenv")
    monkeypatch.delenv("Z_AI_API_KEY", raising=False)
    _inject_env_file(env_file)
    assert os.environ["ANYMODEL_API_KEY"] == "new_key"   # override победил «загрязнение»
    assert os.environ["Z_AI_API_KEY"] == "z_key"
    monkeypatch.delenv("Z_AI_API_KEY", raising=False)     # не оставлять ключ в env

def test_inject_env_file_preserves_unrelated_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"; env_file.write_text("ONLY_FILE_KEY=v\n", encoding="utf-8")
    monkeypatch.setenv("UNRELATED_KEY", "keep")
    _inject_env_file(env_file)
    assert os.environ["UNRELATED_KEY"] == "keep"
    assert "PATH" in os.environ
    monkeypatch.delenv("ONLY_FILE_KEY", raising=False)

def test_inject_env_file_missing_file_noop(tmp_path):
    _inject_env_file(tmp_path / "does_not_exist.env")   # не должно падать


# --- пустой/пробельный answer → fallback-текст (решение 33) ---

def test_format_empty_answer_returns_fallback_bubble(monkeypatch, tmp_path):
    session = _format_session(tmp_path)
    monkeypatch.setattr("firmware.src.chat_api.select_images", lambda *args: [])
    result = session._format({"final_answer": "", "cited_chunk_ids": ["c1"],
                              "search_results": [{"chunk_id": "c1"}]})
    assert result["type"] == "answer"                      # НЕ error, НЕ пустой пузырь
    assert result["answer"] == _EMPTY_ANSWER_FALLBACK
    assert result["cited_chunk_ids"] == []
    assert result["sources"] == []
    assert result["images"] == []

def test_format_whitespace_answer_returns_fallback_bubble(monkeypatch, tmp_path):
    session = _format_session(tmp_path)
    monkeypatch.setattr("firmware.src.chat_api.select_images", lambda *args: [])
    result = session._format({"final_answer": "   \n\t ", "cited_chunk_ids": [],
                              "search_results": []})
    assert result["type"] == "answer"
    assert result["answer"] == _EMPTY_ANSWER_FALLBACK

def test_format_non_string_answer_guard_does_not_crash(monkeypatch, tmp_path):
    # str(answer).strip() — безопасен и для нестроковых значений (если граф отдаст None — уже error).
    session = _format_session(tmp_path)
    monkeypatch.setattr("firmware.src.chat_api.select_images", lambda *args: [])
    result = session._format({"final_answer": None, "cited_chunk_ids": [], "search_results": []})
    assert result["type"] == "error"                       # None по-прежнему error (как было)
