# Implementation report — t_2c838a99

## Summary

Объединил флаги `--ai` и `--ai-table` в один `--ai` в `firmware/src/pipeline.py`.

Теперь `--ai` автоматически выполняет полный AI-цикл:
1. Vision-распознавание таблиц (Gemini) — бывший `--ai-table` (Этап 4b)
2. Gap-filling таблиц — сверка MD с vision-результатом (Этап 6)
3. AI-постобработка итогового MD (Этап 7)

Флаг `--ai-table` удалён: `argparse` больше не принимает его
(`error: unrecognized arguments: --ai-table`).

## Files changed

- `firmware/src/pipeline.py`
  - `parse_args()`: удалён аргумент `--ai-table`; обновлён help флага `--ai`
    («Полный AI-цикл: vision-распознавание таблиц + gap-filling + AI-постобработка»);
    из epilog-примеров убраны строки с `--ai-table`.
  - `process_file()`: удалён параметр `use_ai_table` (и его docstring-описание);
    `if use_ai_table and pages:` → `if use_ai and pages:` (Этап 4b);
    `if use_ai and use_ai_table:` → `if use_ai:` (Этап 6 gap-filling).
    Логика этапов не менялась — только условия-гварды.
  - `main()`: убраны `args.ai_table` из лога и вызова `process_file()`;
    `load_config()` теперь грузится только по `args.ai` (убран `or args.ai_table`).
  - Шапка файла: описание AI-постобработки обновлено (vision → gap-filling → постобработка);
    заголовок секции 4b — «(в составе --ai)».
- `firmware/src/test_ai_table.py`
  - `test_cli_flag_ai_table` → `test_cli_flag_ai_table_removed`: проверяет, что
    `--ai-table` отвергается парсером, а `--ai` парсится.
  - `test_process_file_signature`: теперь проверяет, что `use_ai_table` НЕ в
    сигнатуре `process_file()`.
- `firmware/src/test_gap_filling.py`
  - Убран параметр `use_ai_table` из хелпера `_run_process_file()` и из прямых
    вызовов `process_file()` (позиционные аргументы).

## Not changed

- `firmware/src/config_ai.yaml` — не тронут (секции `ai_table` / `table_vision`
  остаются; удаляется только CLI-флаг, не конфиг).
- Логика этапов обработки (порядок, чанкинг, промты) — не менялась.

## Validation

- `python3 -m pytest test_ai_table.py test_gap_filling.py test_latex_caret_spaces.py test_no_hardcoded_prompts.py -q`
  → **44 passed**
- `python3 pipeline.py --help` — флаг `--ai-table` отсутствует, help `--ai` обновлён
- `python3 pipeline.py -i test.pdf --ai-table` → `error: unrecognized arguments: --ai-table`
- `python3 -m py_compile pipeline.py` — OK
- `parse_args(['-i','x.pdf','--ai'])` → `args.ai = True`, атрибут `ai_table` отсутствует
- Сигнатура: `process_file(input_path, use_ai, config, api_key, folder_id, output_base, tmp_base)`
