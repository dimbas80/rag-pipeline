<!--
  STATE.md — current snapshot of this project's state.
  Git-tracked. History of changes/decisions lives in `git log -- STATE.md`,
  not inline in this file — keep this file a SNAPSHOT, not a changelog.
  When something here changes, overwrite the relevant section and commit;
  don't append a dated entry and leave the old one below it.
-->

# Project State

_Last updated: 2026-08-15 — by: orchestrator — task: фикс ID-маркеров + R-2 завершены (оба PASS)

## Working functionality

- PDF/DOCX/DOC → Markdown pipeline работает через Yandex Vision OCR; AI-режимы используют конфиг `config_ai.yaml`.
- Для входного `.md` в `Markdown/<stem>/` флаг `--rag` выполняет только RAG-индексацию без OCR и без изменения проверенного Markdown/`image/`.
- RAG v2 строит токен-ориентированные чанки Qwen3, `{stem}_chunks.jsonl` и `{stem}_assets.json`; производные файлы записываются атомарно.
- **Многостраничные таблицы** (полный цикл): `extract_table_images()` группирует вырезки по номеру таблицы + пространственно (продолжение на след. странице) → `component_images` в `tmp/<stem>/table_images.json`; `_merge_by_component_images()` склеивает части в одну MD-таблицу после `_inject_table_ids`; `_build_asset_registry()` восстанавливает `image_paths` (все компоненты) по имени файла после снятия ID-маркеров AI-постобработкой.
- Проверено на СП 89.13330.2016: 17 вырезок → **12 логических таблиц**; Таблица Б.1 → `image_paths = [table_3, table_4, table_5]`, Ж.1 → `[table_13, table_14, table_15]`, И.1 → `[table_16, table_17]`.
- Регрессионный прогон: `cd firmware && python3 -m pytest tests/ -q --ignore=tests/test_gap_filling.py` — **258 passed** (2026-08-14).

## Known issues

- `firmware/tests/test_gap_filling.py` содержит 8 предсуществующих падений; полный регрессионный прогон выполняется с `--ignore=tests/test_gap_filling.py`.
- Порог Jaccard `0.10` для fallback-привязки таблиц подобран на СП89; на других документах может потребоваться калибровка. Слабая уверенность логируется WARNING, но не блокирует результат.
- `test_gemini_full_pdf.py` падает на `import fitz` в изолированной среде coder-воркера (не в рабочем окружении проекта) — окружение, не дефект кода.
- Полная переиндексация корпуса (кроме СП89) после исправления table→image не выполнялась.
- Слабая уверенность привязки Б.1 (Jaccard 0.11) и Ж.1 (0.27) при объединении в одну таблицу — ожидаемо (слияние снижает оценку), не блокирует результат.

## In progress

- Нет активных задач.

## Planned / backlog

- Выполнить sweep по остальным документам корпуса: повторно построить RAG assets с новой логикой component_images, затем переиндексировать фактическое хранилище consumer'а.
- Отдельно разобраться с `test_gap_filling.py`; не включать в зелёный прогон до исправления.
- Закоммитить оставшиеся untracked: `AGENTS.md`, `.hermes/STATE.md`, workflow-артефакты `workflows/t_*` (раздельно от кода).

## Recent decisions

- **Kanban-доска**: оркестратор обязан использовать доску с именем проекта (`create_markdown_ya`), НЕ `default`; при отсутствии — создавать. Правило зафиксировано в `orchestration.yml` (секция `kanban.board.rule`) и в шаблонах `!template/{software,embedded}-project/.hermes/`.
- `orchestration.yml` и оба шаблона приведены к валидному YAML (убраны markdown-блоки ```` ``` ```` внутри YAML и плоская вложенность ключей).
- Объединение многостраничных таблиц — детерминированное, по `component_images` (spatial grouping), а не по маркерам «Продолжение/Окончание таблицы» (Yandex OCR их не выдаёт) и не в надежде на deepseek.
- `image_path` таблиц не выводится позиционным счётчиком; связь по `<!-- t_pN_M -->` + persisted map, fallback по содержимому с диагностикой.
- После изменений пайплайна обязательны независимый pytest-прогон и реальная проверка на документе.
- **ID-маркеры таблиц** (2026-08-15): `extract_table_images()` назначал id по boundingBox Yandex, а `_inject_table_ids()` пересчитывал по `page_boundaries` → на границе страницы маркер съезжал, AI-сверка резолвила в чужой vision-эталон. Решение: id рождается в `parse_yandex_json_to_md()` (тот же порядок итерации `pages`/`tables[]`, что в вырезке — id совпадает); группировка `component_images` — только spatial-pass, без голого `table_num` (номера не уникальны между разделами/приложениями).
- **R-2 (2026-08-15, коммит fdee917):** spatial-pass `extract_table_images()` расширен предикатом `_is_continuation_caption()` — подписи «Продолжение/Окончание таблицы N» теперь тоже группируются (все страницы в `component_images`/`image_paths`), при совпадении `table_num` с головой и соблюдении геометрии. Босая «Таблица N» по-прежнему не группируется (дефект 2 не возвращается).

## Project instructions

- Инструкции для coding agents — в `AGENTS.md` в корне; layout, команды, CLI, env vars, pitfalls.
- Production-код в `firmware/src/`, тесты в `firmware/tests/`, `workflows/` — только orchestration artifacts.
- `.hermes/STATE.md` обновляет только Orchestrator; контекст профилям — через Kanban task body.

## Verification basis

- Живой прогон СП 89.13330.2016 (2026-08-14): 12 таблиц, component_images + image_paths подтверждены чтением фактического `assets.json`.
- Коммиты: `13b55f7` (fix tables), `2522cf0` (chore orchestration) — поверх `d71e8c4`.
- Тесты: 258 passed (`--ignore=tests/test_gap_filling.py`).
- История архитектуры: `docs/architecture/rag-v2-architecture.md`, `docs/architecture/decision-records/adr-010-rag-semantic-assets-and-token-chunking.md`.

## Notes

- Этот файл — snapshot, не changelog. Историю — через `git log -- .hermes/STATE.md`.
- Не коммитить секреты из `.env`, временные каталоги, кэши, незапрошенные generated outputs.
