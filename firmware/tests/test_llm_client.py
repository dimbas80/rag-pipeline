from firmware.src import llm_client

def test_chat_completion_requests(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {'choices':[{'message':{'content':'ok'}}]}
    seen={}
    monkeypatch.setattr(llm_client.requests,'post',lambda *a,**k:(seen.update(k=k) or R()))
    assert llm_client.chat_completion({'base_url':'https://x','model':'m'},[], 'secret')=='ok'
    assert seen['k']['headers']['Authorization']=='Bearer secret'
