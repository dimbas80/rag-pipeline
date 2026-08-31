"""Статус и управление systemd-сервисом Телеграм-бота (решение №36).

Веб-сервис interface-rag.service на проде работает от root, поэтому
systemctl доступен без повышений. Имя юнита зафиксировано константой —
пользовательский ввод в argv не попадает (shell=False, паттерн jobs.py).

Проверка связи с Telegram (getMe) отдельна от статуса процесса: инцидент
«сервис active, но бот молчит» (сеть/прокси) виден только так.
"""
from __future__ import annotations

import subprocess

import requests

UNIT = "interface-rag-bot.service"
_SHOW_PROPERTIES = "LoadState,ActiveState,SubState,NRestarts,ExecMainStartTimestamp"
_TELEGRAM_TIMEOUT = 5  # сек: чёрная дыра в сети не должна подвешивать запрос статуса


def _systemctl_show() -> dict[str, str]:
    result = subprocess.run(
        ["systemctl", "show", UNIT, "--property=" + _SHOW_PROPERTIES, "--no-pager"],
        capture_output=True, text=True, timeout=10, shell=False,
    )
    props: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key.strip()] = value.strip()
    return props


def _telegram_reachable(token: str) -> bool:
    try:
        response = requests.get(
            "https://api.telegram.org/bot" + token + "/getMe",
            timeout=_TELEGRAM_TIMEOUT,
        )
        return response.status_code == 200
    except requests.RequestException:
        return False


def status(token: str | None = None) -> dict:
    """Статус юнита + (если задан токен) связность с Telegram API.

    Токен в ответ никогда не включается — только булевы признаки.
    """
    try:
        props = _systemctl_show()
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "unit": UNIT, "reason": "systemctl недоступен"}
    if props.get("LoadState") != "loaded":
        return {
            "available": False,
            "unit": UNIT,
            "reason": "юнит не установлен (LoadState=" + (props.get("LoadState") or "?") + ")",
        }
    info: dict = {
        "available": True,
        "unit": UNIT,
        "active": props.get("ActiveState") == "active",
        "sub_state": props.get("SubState", ""),
        "restarts": int(props.get("NRestarts") or 0),
        "started_at": props.get("ExecMainStartTimestamp", ""),
        "token_configured": bool(token),
    }
    if token:
        info["telegram_ok"] = _telegram_reachable(token)
    return info


def restart(token: str | None = None) -> dict:
    """Перезапустить юнит бота и вернуть свежий статус."""
    subprocess.run(
        ["systemctl", "restart", UNIT],
        capture_output=True, text=True, timeout=60, shell=False, check=True,
    )
    return status(token)
