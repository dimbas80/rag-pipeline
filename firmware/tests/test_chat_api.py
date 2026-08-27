from firmware.src.chat_api import select_images
from firmware.src.chat_api import ChatSession

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
