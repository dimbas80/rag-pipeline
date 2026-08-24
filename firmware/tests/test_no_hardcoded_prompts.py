#!/usr/bin/env python3
"""Проверка критериев приёмки t_0786b1bb: убраны хардкод-промты из create_markdown.py."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))

import create_markdown

FAILURES = []


def check(name, cond, detail=""):
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def test_ai_prompt_missing():
    """Без prompt в ai_postprocess → sys.exit(1) с сообщением."""
    cfg = {"ai_postprocess": {"provider": "deepseek", "model": "m", "api_key_env": "NOPE"}}
    try:
        create_markdown._call_ai_api("text", cfg, "ctx")
    except SystemExit as e:
        check("ai_postprocess без prompt → exit", e.code == 1)
        return
    check("ai_postprocess без prompt → exit", False, "не вышел")


def test_vision_prompt_missing():
    """Без prompt в table_vision → sys.exit(1) с сообщением."""
    cfg = {"table_vision": {"model": "m", "api_key_env": "NOPE"}}
    try:
        create_markdown.recognize_tables_vision([], "imgdir", cfg, "tmp", "key")
    except SystemExit as e:
        check("table_vision без prompt → exit", e.code == 1)
        return
    check("table_vision без prompt → exit", False, "не вышел")


def test_ai_prompt_present():
    """С prompt в ai_postprocess — доходит до API-вызова (не падает на промте)."""
    cfg = {
        "ai_postprocess": {
            "provider": "deepseek",
            "model": "m",
            "api_key_env": "DEEPSEEK_API_KEY",
            "base_url": "https://api.deepseek.com/v1",
            "prompt": "Ты — редактор.",
        }
    }
    # Без ключа API уйдёт в fallback и вернёт None — это ожидаемое поведение «работает как раньше»
    # при условии, что не упало с «не задан промт».
    try:
        res = create_markdown._call_ai_api("text", cfg, "ctx")
        check("ai_postprocess с prompt → не падает на промте", res is None,
              f"returned {res} (ожидаемо: None без API-ключа)")
    except SystemExit as e:
        check("ai_postprocess с prompt → не падает на промте", False, f"SystemExit({e.code})")


def test_vision_prompt_present():
    """С prompt в table_vision — доходит до распознавания (не падает на промте)."""
    cfg = {
        "table_vision": {
            "model": "m",
            "api_key_env": "PROVOD_API_KEY",
            "prompt": "Переведи таблицу в markdown.",
        }
    }
    try:
        res = create_markdown.recognize_tables_vision([], "imgdir", cfg, "tmp", "key")
        check("table_vision с prompt → не падает на промте", res == 0, f"returned {res}")
    except SystemExit as e:
        check("table_vision с prompt → не падает на промте", False, f"SystemExit({e.code})")


def test_constant_removed():
    check("AI_CLEANUP_DEFAULT_PROMPT удалён", not hasattr(create_markdown, "AI_CLEANUP_DEFAULT_PROMPT"))


def test_gap_filling_hardcoded_prompt_removed():
    """Хардкод-промт gap-filling убран из create_markdown.py — только из config_ai.yaml."""
    src = Path(create_markdown.__file__).read_text(encoding="utf-8")
    check(
        "gap-filling хардкод-текст отсутствует",
        "Проверь все таблицы в Markdown-файле ниже" not in src,
    )
    check(
        "gap-filling берёт ai_table",
        'config.get("ai_table", {})' in src,
    )
    check(
        "gap-filling проверяет prompt из ai_table",
        "укажите prompt в секции ai_table конфига" in src,
    )


def test_config_ai_table_section():
    """config_ai.yaml содержит секцию ai_table с промптом."""
    import yaml

    cfg_path = Path(create_markdown.__file__).parent / "config_ai.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    ai_table = cfg.get("ai_table")
    check("ai_table секция есть", isinstance(ai_table, dict))
    if not isinstance(ai_table, dict):
        return
    check("ai_table.prompt задан", bool(ai_table.get("prompt")))
    check("ai_table.provider задан", bool(ai_table.get("provider")))
    check("ai_table.model задан", bool(ai_table.get("model")))
    check("ai_table.api_key_env задан", bool(ai_table.get("api_key_env")))
    check("ai_table.base_url задан", bool(ai_table.get("base_url")))
    check("ai_table.fallback задан", isinstance(ai_table.get("fallback"), dict))
    check(
        "существующие секции не тронуты",
        isinstance(cfg.get("table_vision"), dict)
        and isinstance(cfg.get("ai_postprocess"), dict),
    )


if __name__ == "__main__":
    test_ai_prompt_missing()
    test_vision_prompt_missing()
    test_ai_prompt_present()
    test_vision_prompt_present()
    test_constant_removed()
    test_gap_filling_hardcoded_prompt_removed()
    test_config_ai_table_section()
    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} проверок")
        sys.exit(1)
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
