#!/usr/bin/env python3
"""Rate-limit и tuning поведения AI-постобработки (_call_ai_api / ai_postprocess).

Мотивация: провайдер zai-custom (https://api.z.ai) отдаёт 429 Too Many Requests
на серии больших запросов (чанк до AI_MAX_CHARS + max_tokens=64000, запросы
спина к спине). Контракт:
  1. 429 обрабатывается отдельно: Retry-After (если есть, с ограничением
     сверху) либо экспоненциальный backoff 15/30/60/120с; бюджет попыток —
     rate_limit_retries (по умолчанию 5), НЕ тратит общие 3 попытки;
  2. настраиваемые per-config параметры: max_tokens, chunk_max_chars,
     request_interval_sec (пауза МЕЖДУ чанками), rate_limit_retries;
  3. при исчерпании попыток — явная log.error и сохранение исходного чанка,
     провайдер с фолбэком продолжает обработку остальных чанков.
"""
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import create_markdown


# ═══════════════════════════════════════════════════════════════════════════
# Фейковый httpx-транспорт: последовательность ответов + запись payload'ов
# ═══════════════════════════════════════════════════════════════════════════


class FakeResp:
    def __init__(self, status_code=200, content="ok", headers=None, finish_reason="stop",
                 text=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text or ""
        self._content = content
        self._finish = finish_reason

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"Client error '{self.status_code}' for url")

    def json(self):
        return {"choices": [{"message": {"content": self._content},
                             "finish_reason": self._finish}]}


class FakeClient:
    def __init__(self, responses, calls, payloads):
        self._responses = list(responses)
        self._index = 0
        self.calls = calls
        self.payloads = payloads
        self.stream_used = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _next(self, url, json):
        self.calls.append(url)
        self.payloads.append(json)
        resp = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return resp

    def post(self, url, json=None, headers=None):
        return self._next(url, json)

    def stream(self, method, url, json=None, headers=None):
        assert method == "POST"
        self.stream_used = True
        return self._next(url, json)


class FakeStreamResp:
    """Мок httpx-стриминга: SSE-строки через iter_lines, read()/text для ошибок."""

    def __init__(self, status_code=200, lines=None, text=""):
        self.status_code = status_code
        self._lines = lines or []
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.text.encode()

    def iter_lines(self):
        yield from self._lines


import json as _json


def _sse(pieces, finish="stop", with_reasoning=False):
    lines = []
    if with_reasoning:
        lines.append("data: " + _json.dumps(
            {"choices": [{"delta": {"reasoning_content": "размышления модели"}}]}))
    for piece in pieces:
        lines.append("data: " + _json.dumps({"choices": [{"delta": {"content": piece}}]}))
    lines.append("data: " + _json.dumps({"choices": [{"delta": {}, "finish_reason": finish}]}))
    lines.append("data: [DONE]")
    return lines


def _make_client(responses):
    """Один общий FakeClient на все попытки (патч httpx.Client через
    return_value): последовательность ответов читается сквозно, последний
    ответ повторяется при исчерпании."""
    calls, payloads = [], []
    return FakeClient(responses, calls, payloads), calls, payloads


BASE_CFG = {
    "provider": "zai-custom",
    "model": "glm-5.3",
    "base_url": "https://api.z.ai/api/paas/v4",
    "prompt": "системный промпт",
}

ENV = {"ZAI_KEY": "k"}


def _call_ai(cfg, responses, sleep_mock, with_client=False):
    client, calls, payloads = _make_client(responses)
    cfg = {**cfg, "api_key_env": cfg.get("api_key_env", "ZAI_KEY")}
    with patch.object(create_markdown.httpx, "Client", return_value=client), \
         patch.object(create_markdown.time, "sleep", sleep_mock), \
         patch.dict(os.environ, ENV, clear=False):
        result = create_markdown._call_ai_api("входной текст", cfg)
    if with_client:
        return result, calls, payloads, client
    return result, calls, payloads


# ═══════════════════════════════════════════════════════════════════════════
# 1. 429: backoff, Retry-After, бюджет, независимость от общих попыток
# ═══════════════════════════════════════════════════════════════════════════


def test_429_retries_with_exponential_backoff_then_success():
    sleeps = []
    result, calls, _ = _call_ai(
        BASE_CFG,
        [FakeResp(status_code=429), FakeResp(content="обработано")],
        sleeps.append,
    )
    assert result == "обработано"
    assert sleeps == [15]
    assert calls == ["https://api.z.ai/api/paas/v4/chat/completions"] * 2


def test_429_respects_retry_after_header():
    sleeps = []
    result, _, _ = _call_ai(
        BASE_CFG,
        [FakeResp(status_code=429, headers={"Retry-After": "7"}), FakeResp(content="ок")],
        sleeps.append,
    )
    assert result == "ок"
    assert sleeps == [7]


def test_429_exhausts_budget_returns_none_with_explicit_error(caplog):
    sleeps = []
    with caplog.at_level(logging.ERROR):
        result, calls, _ = _call_ai(
            BASE_CFG, [FakeResp(status_code=429)] * 5, sleeps.append
        )
    assert result is None
    assert len(calls) == 5
    assert sleeps == [15, 30, 60, 120]  # без паузы после последней неудачи
    assert any("429" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


def test_429_does_not_consume_generic_retries():
    sleeps = []
    result, calls, _ = _call_ai(
        BASE_CFG,
        [FakeResp(status_code=429), FakeResp(status_code=503), FakeResp(content="ок")],
        sleeps.append,
    )
    assert result == "ок"
    assert len(calls) == 3
    assert sleeps == [15, 5]


def test_429_primary_exhausted_then_fallback():
    sleeps = []
    cfg = {
        **BASE_CFG,
        "fallback": {"base_url": "https://fallback.example/v1", "api_key_env": "ZAI_KEY"},
    }
    result, calls, _ = _call_ai(
        cfg, [FakeResp(status_code=429)] * 5 + [FakeResp(content="fallback результат")],
        sleeps.append,
    )
    assert result == "fallback результат"
    assert calls[4].endswith("/api/paas/v4/chat/completions")
    assert calls[5] == "https://fallback.example/v1/chat/completions"


# ═══════════════════════════════════════════════════════════════════════════
# 1a. Стриминг (stream: true) — длинные генерации на z.ai иначе рвут коннект
# ═══════════════════════════════════════════════════════════════════════════


def test_stream_true_accumulates_delta_content_and_ignores_reasoning():
    sleeps = []
    responses = [FakeStreamResp(lines=_sse(["при", "вет"], with_reasoning=True))]
    result, calls, payloads, client = _call_ai(
        {**BASE_CFG, "stream": True}, responses, sleeps.append, with_client=True
    )
    assert result == "привет"
    assert client.stream_used
    assert payloads[0]["stream"] is True
    assert sleeps == []


def test_stream_finish_reason_length_retries_then_none():
    sleeps = []
    responses = [FakeStreamResp(lines=_sse(["усечено"], finish="length"))]
    result, calls, _, _ = _call_ai(
        {**BASE_CFG, "stream": True}, responses, sleeps.append, with_client=True
    )
    assert result is None
    assert len(calls) == 3
    assert sleeps == [5, 5, 5]


def test_stream_429_balance_fails_fast():
    balance_body = ('{"error":{"code":"1113","message":"Insufficient balance '
                    'or no resource package. Please recharge."}}')
    sleeps = []
    result, calls, _, _ = _call_ai(
        {**BASE_CFG, "stream": True},
        [FakeStreamResp(status_code=429, text=balance_body)],
        sleeps.append, with_client=True,
    )
    assert result is None
    assert len(calls) == 1
    assert sleeps == []


def test_stream_disabled_by_default_uses_plain_post():
    sleeps = []
    result, calls, payloads, client = _call_ai(
        BASE_CFG, [FakeResp(content="ок")], sleeps.append, with_client=True
    )
    assert result == "ок"
    assert not client.stream_used
    assert "stream" not in payloads[0]


def test_stream_cut_without_done_retries_then_none():
    """Обрыв стрима без [DONE] и без finish_reason — неполный ответ не
    принимается как успех: ретраи, затем None (переход к fallback)."""
    truncated = ["data: " + _json.dumps({"choices": [{"delta": {"content": "хвост"}}]})]
    sleeps = []
    result, calls, _, _ = _call_ai(
        {**BASE_CFG, "stream": True},
        [FakeStreamResp(lines=truncated)],
        sleeps.append, with_client=True,
    )
    assert result is None
    assert len(calls) == 3
    assert sleeps == [5, 5, 5]


def test_stream_done_without_finish_reason_is_success():
    """[DONE] без явного finish_reason (бывает у совместимых API) — успех."""
    lines = ["data: " + _json.dumps({"choices": [{"delta": {"content": "полный"}}]}),
             "data: [DONE]"]
    sleeps = []
    result, calls, _, _ = _call_ai(
        {**BASE_CFG, "stream": True}, [FakeStreamResp(lines=lines)],
        sleeps.append, with_client=True,
    )
    assert result == "полный"
    assert len(calls) == 1


# ═══════════════════════════════════════════════════════════════════════════
# 1b. 429 от z.ai кодом 1113 (insufficient balance) — НЕ rate limit
# ═══════════════════════════════════════════════════════════════════════════


def test_429_insufficient_balance_fails_fast_without_backoff(caplog):
    """429 с кодом 1113 «Insufficient balance» — повтор бессилен: один запрос,
    без бэкоффа, явная ошибка с телом ответа, переход к fallback."""
    balance_body = ('{"error":{"code":"1113","message":"Insufficient balance '
                    'or no resource package. Please recharge."}}')
    sleeps = []
    with caplog.at_level(logging.WARNING):
        result, calls, _ = _call_ai(
            BASE_CFG, [FakeResp(status_code=429, text=balance_body)], sleeps.append
        )
    assert result is None
    assert len(calls) == 1
    assert sleeps == []
    assert any("1113" in r.getMessage() and "Insufficient balance" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.ERROR)


def test_429_generic_body_is_logged_with_backoff(caplog):
    """Обычный 429 (не баланс) — прежнее поведение: backoff + попытка повторить,
    тело ответа в логе для диагностики."""
    rate_body = '{"error":{"code":"1302","message":"Rate limit reached per minute"}}'
    sleeps = []
    with caplog.at_level(logging.WARNING):
        result, calls, _ = _call_ai(
            BASE_CFG,
            [FakeResp(status_code=429, text=rate_body), FakeResp(content="ок")],
            sleeps.append,
        )
    assert result == "ок"
    assert sleeps == [15]
    assert any("Rate limit reached per minute" in r.getMessage()
               for r in caplog.records)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Tunables в payload запроса
# ═══════════════════════════════════════════════════════════════════════════


def test_max_tokens_from_config():
    sleeps = []
    _, _, payloads = _call_ai(
        {**BASE_CFG, "max_tokens": 8192}, [FakeResp(content="ок")], sleeps.append
    )
    assert payloads[0]["max_tokens"] == 8192


def test_max_tokens_default_is_64000():
    sleeps = []
    _, _, payloads = _call_ai(BASE_CFG, [FakeResp(content="ок")], sleeps.append)
    assert payloads[0]["max_tokens"] == 64000


# ═══════════════════════════════════════════════════════════════════════════
# 3. ai_postprocess: chunk_max_chars, request_interval_sec, явная ошибка
# ═══════════════════════════════════════════════════════════════════════════


def _two_section_md():
    sec_a = "## 1. Раздел А\n\n" + " AAA" * 60
    sec_b = "## 2. Раздел Б\n\n" + " BBB" * 60
    return sec_a + "\n\n" + sec_b


@pytest.fixture
def _chdir_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.mark.usefixtures("_chdir_tmp")
def test_ai_postprocess_chunk_max_chars_tunable():
    cfg = {**BASE_CFG, "chunk_max_chars": 200}
    expected = len(create_markdown._chunk_text(_two_section_md(), max_chars=200))
    assert expected > 1
    with patch("create_markdown._call_ai_api", side_effect=lambda t, c, ctx="": t + "!") as mock_call:
        create_markdown.ai_postprocess(_two_section_md(), cfg, "doc")
    assert mock_call.call_count == expected


@pytest.mark.usefixtures("_chdir_tmp")
def test_ai_postprocess_request_interval_sleeps_between_chunks():
    cfg = {**BASE_CFG, "chunk_max_chars": 200, "request_interval_sec": 2.5}
    expected = len(create_markdown._chunk_text(_two_section_md(), max_chars=200))
    sleeps = []
    with patch("create_markdown._call_ai_api", side_effect=lambda t, c, ctx="": t + "!"), \
         patch.object(create_markdown.time, "sleep", sleeps.append):
        create_markdown.ai_postprocess(_two_section_md(), cfg, "doc")
    assert sleeps == [2.5] * (expected - 1)  # между чанками, не после последнего


@pytest.mark.usefixtures("_chdir_tmp")
def test_ai_postprocess_no_sleep_by_default():
    cfg = {**BASE_CFG, "chunk_max_chars": 200}
    sleeps = []
    with patch("create_markdown._call_ai_api", side_effect=lambda t, c, ctx="": t + "!"), \
         patch.object(create_markdown.time, "sleep", sleeps.append):
        create_markdown.ai_postprocess(_two_section_md(), cfg, "doc")
    assert sleeps == []


@pytest.mark.usefixtures("_chdir_tmp")
def test_ai_postprocess_failed_chunk_logs_explicit_error(caplog):
    cfg = {**BASE_CFG, "chunk_max_chars": 200}
    counter = {"n": 0}

    def fake_ai(text, c, ctx=""):
        counter["n"] += 1
        return None if counter["n"] == 1 else "ответ по чанку"

    with caplog.at_level(logging.ERROR), \
         patch("create_markdown._call_ai_api", side_effect=fake_ai):
        result = create_markdown.ai_postprocess(_two_section_md(), cfg, "doc")
    assert any("doc [ч.1]" in r.getMessage() and "без обработки" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.ERROR)
    chunks = create_markdown._chunk_text(_two_section_md(), max_chars=200)
    assert chunks[0].strip() in result  # чанк сохранён без обработки


# ═══════════════════════════════════════════════════════════════════════════
# 4. Объединённый этап 6+7 (process_file): interval + явная ошибка чанка
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def fake_env(tmp_path):
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    file_tmp_dir = tmp_path / "tmp" / "input"
    file_tmp_dir.mkdir(parents=True)
    return {
        "pdf": str(pdf),
        "out_base": str(tmp_path / "out"),
        "tmp_base": str(tmp_path / "tmp"),
        "file_tmp_dir": str(file_tmp_dir),
    }


def _ai_pages():
    return [{
        "result": {
            "textAnnotation": {
                "width": 1240, "height": 1754,
                "blocks": [], "tables": [], "pictures": [],
            }
        }
    }]


def _run_process_file(fake_env, config, md_text, api_side_effect):
    with patch("create_markdown.send_to_yandex_ocr", return_value=_ai_pages()), \
         patch("create_markdown.parse_yandex_json_to_md", return_value=(md_text, [], [])), \
         patch("create_markdown.extract_table_images", return_value=[]), \
         patch("create_markdown.run_script_postprocess", side_effect=lambda md, img, **kw: md), \
         patch("create_markdown._call_ai_api", side_effect=api_side_effect) as mock_call:
        ok = create_markdown.process_file(
            fake_env["pdf"], True, config,
            api_key="key", folder_id="folder",
            output_base=fake_env["out_base"], tmp_base=fake_env["tmp_base"],
        )
    return ok, mock_call


def test_unified_stage_request_interval(fake_env):
    counter = {"n": 0}

    def fake_ai(text, cfg, ctx=""):
        counter["n"] += 1
        return text + f" [OK{counter['n']}]"

    cfg = {"ai_postprocess": {"prompt": "p", "chunk_max_chars": 200,
                              "request_interval_sec": 2}}
    expected = len(create_markdown._chunk_text(_two_section_md(), max_chars=200))
    assert expected > 1
    sleeps = []
    with patch.object(create_markdown.time, "sleep", sleeps.append):
        ok, mock_call = _run_process_file(fake_env, cfg, _two_section_md(), fake_ai)
    assert ok is True
    assert mock_call.call_count == expected
    assert sleeps == [2] * (expected - 1)


def test_unified_stage_failed_chunk_logs_explicit_error(fake_env, caplog):
    counter = {"n": 0}

    def fake_ai(text, cfg, ctx=""):
        counter["n"] += 1
        return None if counter["n"] == 1 else text + " [OK]"

    cfg = {"ai_postprocess": {"prompt": "p", "chunk_max_chars": 200}}
    with caplog.at_level(logging.ERROR):
        ok, mock_call = _run_process_file(fake_env, cfg, _two_section_md(), fake_ai)
    assert ok is True
    assert mock_call.call_count > 1
    assert any("[ч.1]" in r.getMessage() and "без обработки" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.ERROR)
    out_md = Path(fake_env["out_base"]) / "input" / "input.md"
    content = out_md.read_text(encoding="utf-8")
    first_chunk = create_markdown._chunk_text(_two_section_md(), max_chars=200)[0]
    assert first_chunk.strip() in content  # исходный чанк сохранён


# ═══════════════════════════════════════════════════════════════════════════
# 5. Проброс tunables при разрешении ролей в main (_merge_ai_tunables)
# ═══════════════════════════════════════════════════════════════════════════


def test_merge_ai_tunables_keeps_whitelist_drops_rest():
    raw = {
        "prompt": "длинный промпт",
        "max_tokens": 4096,
        "chunk_max_chars": 12000,
        "request_interval_sec": 20,
        "rate_limit_retries": 8,
        "stream": True,
        "cache_dir": "/some/path",
    }
    resolved = {"provider": "zai-custom", "model": "glm-5.3",
                "base_url": "https://api.z.ai/api/paas/v4", "api_key_env": "Z_AI_API_KEY"}
    merged = create_markdown._merge_ai_tunables(raw, resolved)
    assert merged["provider"] == "zai-custom"
    assert merged["max_tokens"] == 4096
    assert merged["chunk_max_chars"] == 12000
    assert merged["request_interval_sec"] == 20
    assert merged["rate_limit_retries"] == 8
    assert merged["stream"] is True
    assert "prompt" not in merged and "cache_dir" not in merged
    # входные словари не мутируются
    assert "max_tokens" not in resolved


# ═══════════════════════════════════════════════════════════════════════════
# 6. Харднинг тюнингов
# ═══════════════════════════════════════════════════════════════════════════


def test_ai_setting_tolerates_empty_nested_section():
    # ai_postprocess: None в полном конфиге не должен ронять чтение тюнинга
    assert create_markdown._ai_setting({"ai_postprocess": None}, "max_tokens", 64000) == 64000
    assert create_markdown._ai_setting({"max_tokens": 100}, "max_tokens", 64000) == 100


@pytest.mark.usefixtures("_chdir_tmp")
def test_negative_request_interval_does_not_sleep():
    cfg = {**BASE_CFG, "chunk_max_chars": 200, "request_interval_sec": -5}
    sleeps = []
    with patch("create_markdown._call_ai_api", side_effect=lambda t, c, ctx="": t + "!"), \
         patch.object(create_markdown.time, "sleep", sleeps.append):
        create_markdown.ai_postprocess(_two_section_md(), cfg, "doc")
    assert sleeps == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
