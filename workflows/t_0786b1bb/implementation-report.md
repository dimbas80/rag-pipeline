# Implementation report — t_0786b1bb

## Задача

Убрать хардкод-промты AI из `firmware/src/pipeline.py`:
- `_call_ai_api()` больше не использует `AI_CLEANUP_DEFAULT_PROMPT` как дефолт;
- `recognize_tables_vision()` больше не использует встроенный vision-промт как дефолт;
- константа `AI_CLEANUP_DEFAULT_PROMPT` удалена полностью.

Промты теперь берутся строго из конфига (`config_ai.yaml`), при отсутствии — `sys.exit(1)`.

## Изменённые файлы

- `firmware/src/pipeline.py` — единственный production-файл:
  - удалена константа `AI_CLEANUP_DEFAULT_PROMPT` (была ~стр. 2300–2320);
  - `_call_ai_api()`: `prompt = ai_cfg.get("prompt")` + проверка → `log.error("не задан промт: укажите prompt в секции ai_postprocess конфига"); sys.exit(1)`;
  - `recognize_tables_vision()`: `prompt = vision_cfg.get("prompt")` + проверка → `log.error("не задан промт: укажите prompt в секции table_vision конфига"); sys.exit(1)`.
- `firmware/src/test_no_hardcoded_prompts.py` — добавлен тест (рядом с существующим `test_ai_table.py`).

`config_ai.yaml` НЕ менялся (в нём уже есть оба промта: `ai_postprocess.prompt` и `table_vision.prompt`).

## Проверка (критерии приёмки)

1. Без `prompt` в `ai_postprocess` → `SystemExit(1)` с сообщением «не задан промт: укажите prompt в секции ai_postprocess конфига» — ✅
2. Без `prompt` в `table_vision` → `SystemExit(1)` с сообщением «не задан промт: укажите prompt в секции table_vision конфига» — ✅
3. С корректным `config_ai.yaml` — работает как раньше:
   - `_call_ai_api()` принял промт из конфига и дошёл до реального API-вызова (вернулся живой ответ от провайдера) — ✅
   - `recognize_tables_vision()` принял промт из конфига, дошёл до цикла распознавания — ✅
4. Константа `AI_CLEANUP_DEFAULT_PROMPT` удалена (grep по исходнику — 0 совпадений, `hasattr(pipeline, ...)` — False) — ✅

## Валидация

- `python3 -m py_compile firmware/src/pipeline.py` — OK.
- `python3 firmware/src/test_no_hardcoded_prompts.py` — все 5 проверок пройдены (обе ветки exit, обе ветки «работает», константа удалена).
- Проверка на реальном `config_ai.yaml` (yaml.safe_load + вызов функций) — оба промта приняты, exit не сработал.

## Известные ограничения

- В `_call_ai_api()` fallback-ветка (`config.get("ai_postprocess", config)`) сохраняет прежнее поведение: если секции `ai_postprocess` нет вовсе, `prompt` будет отсутствовать → теперь это тоже падает с «не задан промт». Это соответствует требованию «только из config.yaml, без дефолта».
- Тест `test_no_hardcoded_prompts.py` не требует сети: ветки «без prompt» проверяются до API-вызова; ветки «с prompt» не зависят от результата API.

## Отклонений от архитектуры

Нет. Изменения ограничены тремя указанными в задаче местами.
