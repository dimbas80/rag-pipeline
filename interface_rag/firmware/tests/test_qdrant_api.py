from firmware.src import qdrant_api


class _FakeCountResult:
    count = 3


class _FakeClient:
    def __init__(self):
        self.calls = {}

    def count(self, collection_name=None, count_filter=None, **kwargs):
        self.calls["count"] = {"collection_name": collection_name, "filter": count_filter}
        return _FakeCountResult()

    def delete(self, collection_name=None, points_selector=None, **kwargs):
        self.calls["delete"] = {"collection_name": collection_name, "selector": points_selector}

    def close(self):
        self.calls["closed"] = True


def test_delete_document_filters_by_document_id_and_returns_count(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(qdrant_api, "_client", lambda path: fake)
    deleted = qdrant_api.delete_document("/tmp/qdrant", "technical_standard", "gost_r_51889_2011")
    assert deleted == 3
    selector = fake.calls["delete"]["selector"]
    condition = selector.must[0]
    assert condition.key == "document_id"
    assert condition.match.value == "gost_r_51889_2011"
    assert fake.calls["delete"]["collection_name"] == "technical_standard"
    # Тот же фильтр используется для подсчёта перед удалением.
    assert fake.calls["count"]["filter"] is selector
    assert fake.calls.get("closed") is True


def test_delete_document_closes_client_on_error(monkeypatch):
    class BrokenClient(_FakeClient):
        def count(self, **kwargs):
            raise RuntimeError("locked")

    fake = BrokenClient()
    monkeypatch.setattr(qdrant_api, "_client", lambda path: fake)
    try:
        qdrant_api.delete_document("/tmp/qdrant", "c", "d")
    except RuntimeError:
        pass
    else:
        raise AssertionError("ожидалось исключение от клиента")
    assert fake.calls.get("closed") is True
