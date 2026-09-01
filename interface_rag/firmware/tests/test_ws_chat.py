"""Тесты живого стриминга чата по WS (решение 30, архитектура t_b551380a §10).

Проверяют, что node-события уходят клиенту ПО МЕРЕ выполнения графа (а не
пачкой после list(...)-материализации), сохраняется семантика
__interrupt__ → clarification без финального ответа, ошибка графа шлёт
error и закрывает соединение, а state накапливается до _format.
"""
import time

import pytest
from fastapi.testclient import TestClient

from firmware.src import app


class FakeInterrupt:
    value = "Уточните запрос"


class FakeSession:
    def __init__(self, chunks, error=None, delay=0.0):
        self._chunks, self._error, self._delay = chunks, error, delay
        self._format_calls = 0
        self._last_state = None

    def stream(self, text):
        for c in self._chunks:
            if self._delay:
                time.sleep(self._delay)
            yield c
        if self._error is not None:
            raise self._error

    def resume(self, text):
        return {"type": "answer", "answer": "REPLY", "cited_chunk_ids": [], "sources": [], "images": []}

    def _format(self, state):
        self._format_calls += 1
        self._last_state = dict(state)
        return {"type": "answer", "answer": "ОТВЕТ", "cited_chunk_ids": [], "sources": [], "images": []}


def _client(monkeypatch, session):
    monkeypatch.setattr(app, "ChatSession", lambda cfg: session)
    return TestClient(app.app)


def test_streams_nodes_in_order_then_answer(monkeypatch):
    session = FakeSession([{"search": {"search_results": [1]}},
                           {"generate_answer": {"final_answer": "X"}}])
    client = _client(monkeypatch, session)
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"type": "query", "text": "q"})
        assert ws.receive_json() == {"type": "node", "node": "search"}
        assert ws.receive_json() == {"type": "node", "node": "generate_answer"}
        msg = ws.receive_json()
        assert msg["type"] == "answer"
        assert msg["answer"] == "ОТВЕТ"
        # state накоплен по узлам и передан в _format целиком
        assert session._format_calls == 1
        assert session._last_state == {"search_results": [1], "final_answer": "X"}


def test_interrupt_sends_clarification_without_answer(monkeypatch):
    session = FakeSession([{"__interrupt__": (FakeInterrupt(),)}])
    client = _client(monkeypatch, session)
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"type": "query", "text": "q"})
        msg = ws.receive_json()
        assert msg == {"type": "clarification", "text": "Уточните запрос"}
        # финальный _format(state) НЕ вызывается — answer не шлётся
        assert session._format_calls == 0


def test_graph_error_sends_error_then_closes(monkeypatch):
    session = FakeSession([{"search": {"search_results": [1]}}], error=RuntimeError("boom"))
    client = _client(monkeypatch, session)
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"type": "query", "text": "q"})
        assert ws.receive_json() == {"type": "node", "node": "search"}
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "boom" in msg["text"]
        # «не молчим»: error пришёл, затем сервер закрывает соединение
        with pytest.raises(Exception):
            ws.receive_json()


def test_disconnect_mid_stream_does_not_hang(monkeypatch):
    session = FakeSession([{"search": {}}, {"generate_answer": {}}, {"more": {}}], delay=0.05)
    client = _client(monkeypatch, session)
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"type": "query", "text": "q"})
        assert ws.receive_json() == {"type": "node", "node": "search"}
        ws.close()  # клиент уходит mid-stream — сервер не должен зависнуть
    # with-блок завершился: producer остановлен по stop-флагу, джойн не блокирует
