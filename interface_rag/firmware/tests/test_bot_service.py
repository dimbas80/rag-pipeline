"""Тесты bot_service: статус/перезапуск systemd-юнита Телеграм-бота."""
import subprocess

import pytest
import requests

from firmware.src import bot_service


class _RunResult:
    def __init__(self, stdout=""):
        self.stdout = stdout


def _patch_show(monkeypatch, stdout):
    monkeypatch.setattr(bot_service.subprocess, "run", lambda *a, **k: _RunResult(stdout))


# --- status: парсинг systemctl show ---

def test_status_parses_systemctl_show_properties(monkeypatch):
    _patch_show(monkeypatch, (
        "LoadState=loaded\n"
        "ActiveState=active\n"
        "SubState=running\n"
        "NRestarts=3\n"
        "ExecMainStartTimestamp=Mon 2026-08-31 22:00:52 +07\n"
    ))
    info = bot_service.status(None)
    assert info["available"] is True
    assert info["active"] is True
    assert info["sub_state"] == "running"
    assert info["restarts"] == 3
    assert info["started_at"] == "Mon 2026-08-31 22:00:52 +07"
    assert info["token_configured"] is False
    # без токена проверка Telegram не выполняется — ключа нет в ответе
    assert "telegram_ok" not in info


def test_status_reports_inactive_unit(monkeypatch):
    _patch_show(monkeypatch, "LoadState=loaded\nActiveState=failed\nSubState=failed\nNRestarts=1\n")
    info = bot_service.status(None)
    assert info["available"] is True
    assert info["active"] is False


def test_status_unit_not_installed_marks_unavailable(monkeypatch):
    _patch_show(monkeypatch, "LoadState=not-found\n")
    info = bot_service.status(None)
    assert info["available"] is False


def test_status_without_systemctl_marks_unavailable(monkeypatch):
    def boom(*args, **kwargs):
        raise FileNotFoundError("systemctl")
    monkeypatch.setattr(bot_service.subprocess, "run", boom)
    info = bot_service.status(None)
    assert info["available"] is False


# --- status: проверка связи с Telegram API (getMe) ---

def _active_unit(monkeypatch):
    _patch_show(monkeypatch, "LoadState=loaded\nActiveState=active\nSubState=running\nNRestarts=0\n")


def test_status_with_token_checks_telegram_ok(monkeypatch):
    _active_unit(monkeypatch)
    monkeypatch.setattr(bot_service.requests, "get", lambda *a, **k: type("R", (), {"status_code": 200})())
    info = bot_service.status("tok")
    assert info["token_configured"] is True
    assert info["telegram_ok"] is True


def test_status_with_token_telegram_unreachable(monkeypatch):
    _active_unit(monkeypatch)
    def network_down(*args, **kwargs):
        raise requests.RequestException("ConnectTimeout")
    monkeypatch.setattr(bot_service.requests, "get", network_down)
    info = bot_service.status("tok")
    assert info["telegram_ok"] is False


def test_status_getme_uses_token_and_timeout(monkeypatch):
    _active_unit(monkeypatch)
    captured = {}

    def fake_get(url, timeout=None):
        captured["url"], captured["timeout"] = url, timeout
        return type("R", (), {"status_code": 200})()

    monkeypatch.setattr(bot_service.requests, "get", fake_get)
    bot_service.status("tok-123")
    assert captured["url"] == "https://api.telegram.org/bottok-123/getMe"
    assert captured["timeout"] == 5


# --- restart ---

def test_restart_invokes_systemctl_with_fixed_unit_argv(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return _RunResult("LoadState=loaded\nActiveState=active\nSubState=running\nNRestarts=0\n")

    monkeypatch.setattr(bot_service.subprocess, "run", fake_run)
    monkeypatch.setattr(bot_service, "_telegram_reachable", lambda token: True)
    info = bot_service.restart("tok")
    restart_calls = [argv for argv, _ in calls if argv[1:2] == ["restart"]]
    assert restart_calls == [["systemctl", "restart", bot_service.UNIT]]
    restart_kwargs = dict(calls[0][1])
    assert restart_kwargs.get("shell") is False
    assert info["active"] is True
    assert info["telegram_ok"] is True


def test_restart_propagates_systemctl_error(monkeypatch):
    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "systemctl restart")
    monkeypatch.setattr(bot_service.subprocess, "run", boom)
    with pytest.raises(subprocess.CalledProcessError):
        bot_service.restart(None)
