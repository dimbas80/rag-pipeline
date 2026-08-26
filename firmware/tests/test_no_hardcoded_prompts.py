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
    """Хардкод-промт gap-filling убран из create_markdown.py — только из create_markdown_config.yaml."""
    src = Path(create_markdown.__file__).read_text(encoding="utf-8")
    check(
        "gap-filling хардкод-текст отсутствует",
        "Проверь все таблицы в Markdown-файле ниже" not in src,
    )
    check(
        "gap-filling берёт ai_postprocess",
        'config.get("ai_postprocess"' in src,
    )
    check(
        "gap-filling проверяет prompt",
        "укажите prompt в секции ai_postprocess конфига" in src,
    )


def test_config_sections():
    """create_markdown_config.yaml: секции table_vision/ai_postprocess/reg_extract;
    reg_extract содержит ТОЛЬКО prompt (провайдер/модель — один раз в ai_postprocess)."""
    import yaml

    cfg_path = Path(create_markdown.__file__).parent / "create_markdown_config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    check("table_vision секция есть", isinstance(cfg.get("table_vision"), dict))
    check("ai_postprocess секция есть", isinstance(cfg.get("ai_postprocess"), dict))
    ai = cfg.get("ai_postprocess") or {}
    check("ai_postprocess.prompt задан", bool(ai.get("prompt")))
    check("ai_postprocess.provider задан", bool(ai.get("provider")))
    check("ai_postprocess.model задан", bool(ai.get("model")))
    check("ai_postprocess.api_key_env задан", bool(ai.get("api_key_env")))
    check("ai_postprocess.base_url задан", bool(ai.get("base_url")))
    check("ai_postprocess.fallback задан", isinstance(ai.get("fallback"), dict))
    reg = cfg.get("reg_extract") or {}
    check("reg_extract.prompt задан", bool(reg.get("prompt")))
    check("reg_extract без провайдера (модель из ai_postprocess)", "provider" not in reg)
    check("reg_extract без модели", "model" not in reg)
    check("reg_extract без fallback", "fallback" not in reg)
    check("каталог documents удалён из конфига", "documents" not in cfg)


if __name__ == "__main__":
    test_ai_prompt_missing()
    test_vision_prompt_missing()
    test_ai_prompt_present()
    test_vision_prompt_present()
    test_constant_removed()
    test_gap_filling_hardcoded_prompt_removed()
    test_config_sections()
    print()
    if FAILURES:
        print(f"ПРОВАЛЕНО: {len(FAILURES)} проверок")
        sys.exit(1)
    print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
