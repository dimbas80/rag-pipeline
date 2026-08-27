import os
from firmware.src.chat_api import select_images
from firmware.src.chat_api import ChatSession
from firmware.src.chat_api import _inject_env_file

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
