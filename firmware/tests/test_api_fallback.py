#!/usr/bin/env python3
"""Регрессии fallback для Vision и AI OpenAI-совместимых API."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import create_markdown


class _FakeResponse:
    def __init__(self, status_code=200, content="fallback result"):
        self.status_code = status_code
        self._content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class _FakeClient:
    def __init__(self, responses, calls):
        self.responses = iter(responses)
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        self.calls.append(args[0])
        return next(self.responses)


@pytest.mark.parametrize("status_code", [400, 401, 403])
def test_vision_auth_and_request_errors_go_to_fallback_without_retry(status_code):
    calls = []
    responses = [_FakeResponse(status_code), _FakeResponse(content="fallback")]
    client = _FakeClient(responses, calls)
    config = {
        "model": "primary",
        "base_url": "https://primary",
        "fallback": {"model": "secondary", "base_url": "https://fallback", "api_key_env": "FB_KEY"},
    }
    with patch.object(create_markdown.httpx, "Client", return_value=client), patch.object(create_markdown.time, "sleep") as sleep, patch.dict(os.environ, {"FB_KEY": "key"}):
        result = create_markdown._call_vision_api("img", "prompt", config, "primary-key")

    assert result == "fallback"
    assert calls == ["https://primary/chat/completions", "https://fallback/chat/completions"]
    sleep.assert_not_called()


@pytest.mark.parametrize("content", ["", "не смогла распознать"])
def test_vision_recognition_failure_goes_to_fallback_without_retry(content):
    calls = []
    client = _FakeClient([_FakeResponse(content=content), _FakeResponse(content="fallback")], calls)
    config = {"model": "primary", "base_url": "https://primary", "fallback": {"model": "secondary", "base_url": "https://fallback", "api_key_env": "FB_KEY"}}
    with patch.object(create_markdown.httpx, "Client", return_value=client), patch.object(create_markdown.time, "sleep") as sleep, patch.dict(os.environ, {"FB_KEY": "key"}):
        result = create_markdown._call_vision_api("img", "prompt", config, "primary-key")

    assert result == "fallback"
    assert calls[0] == "https://primary/chat/completions"
    assert len(calls) == 2
    sleep.assert_not_called()


@pytest.mark.parametrize("status_code", [400, 401, 403])
def test_ai_auth_and_request_errors_go_to_fallback_without_retry(status_code):
    calls = []
    client = _FakeClient([_FakeResponse(status_code), _FakeResponse(content="fallback")], calls)
    config = {
        "provider": "primary-provider",
        "model": "primary",
        "base_url": "https://primary",
        "prompt": "process",
        "fallback": {"provider": "secondary-provider", "model": "secondary", "base_url": "https://fallback", "api_key_env": "FB_KEY"},
    }
    env = {"DEEPSEEK_API_KEY": "primary-key", "FB_KEY": "fallback-key"}
    with patch.object(create_markdown.httpx, "Client", return_value=client), patch.object(create_markdown.time, "sleep") as sleep, patch.dict(os.environ, env, clear=False):
        result = create_markdown._call_ai_api("text", config)

    assert result == "fallback"
    assert calls == ["https://primary/chat/completions", "https://fallback/chat/completions"]
    sleep.assert_not_called()


@pytest.mark.parametrize("content", ["", "cannot recognize this table"])
def test_ai_recognition_failure_goes_to_fallback_without_retry(content):
    calls = []
    client = _FakeClient([_FakeResponse(content=content), _FakeResponse(content="fallback")], calls)
    config = {"provider": "primary-provider", "model": "primary", "base_url": "https://primary", "prompt": "process", "fallback": {"base_url": "https://fallback", "api_key_env": "FB_KEY"}}
    with patch.object(create_markdown.httpx, "Client", return_value=client), patch.object(create_markdown.time, "sleep") as sleep, patch.dict(os.environ, {"DEEPSEEK_API_KEY": "primary-key", "FB_KEY": "fallback-key"}, clear=False):
        result = create_markdown._call_ai_api("text", config)

    assert result == "fallback"
    assert len(calls) == 2
    sleep.assert_not_called()


def test_recognition_failure_does_not_reject_valid_markdown_table():
    assert create_markdown._is_recognition_failure("| A | B |\n|---|---|\n| 1 | 2 |") is False


def test_recognition_failure_detects_english_unable_phrase():
    assert create_markdown._is_recognition_failure("Sorry, I am unable to read this image") is True


def test_ai_503_retries_then_returns_success():
    calls = []
    client = _FakeClient([_FakeResponse(status_code=503), _FakeResponse(content="processed")], calls)
    config = {"provider": "primary-provider", "model": "primary", "base_url": "https://primary", "prompt": "process"}
    with patch.object(create_markdown.httpx, "Client", return_value=client), patch.object(create_markdown.time, "sleep") as sleep, patch.dict(os.environ, {"DEEPSEEK_API_KEY": "primary-key"}, clear=False):
        result = create_markdown._call_ai_api("text", config)

    assert result == "processed"
    assert calls == ["https://primary/chat/completions", "https://primary/chat/completions"]
    sleep.assert_called_once_with(5)


def test_ai_none_content_is_handled_as_recognition_failure():
    response = MagicMock(status_code=200)
    response.json.return_value = {"choices": [{"message": {"content": None}}]}
    client = _FakeClient([response, _FakeResponse(content="fallback")], [])
    with patch.object(create_markdown.httpx, "Client", return_value=client), patch.dict(os.environ, {"DEEPSEEK_API_KEY": "primary-key", "FB_KEY": "fallback-key"}, clear=False):
        result = create_markdown._call_ai_api("text", {"prompt": "process", "fallback": {"base_url": "https://fallback", "api_key_env": "FB_KEY"}})
    assert result == "fallback"