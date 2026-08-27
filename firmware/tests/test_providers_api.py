import pytest
from firmware.src import providers_api

def test_tag_model():
    assert providers_api.tag_model('text-embedding-3-small')=='embedding'
    assert providers_api.tag_model('qwen-vl')=='vision'

def test_scan_models(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return {'data':[{'id':'x'}]}
    monkeypatch.setattr(providers_api.requests,'get',lambda *a,**k:R())
    assert providers_api.scan_models('https://x','k')==['x']
