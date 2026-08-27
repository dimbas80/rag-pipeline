# Review — t_7371c0a8 (закрытие 4 остаточных пунктов из t_e7ebcb27)

## Verdict

**PASS**

`assign_rework_to=` (не требуется).

Все четыре остаточных пункта финального ревью Rework 3 закрыты и проверены вживую
(не по отчёту coder'а): контракт «Добавить провайдера» согласован (200 вместо 422),
тесты gating'а индекса и `ChatSession._format` добавлены и проходят, `settings_status`
читает объединённые роли обоих пайплайнов, `_write_combined` раскладывает провайдеров
по pipeline-ролям без дублирования. Полный прогон 19 passed, compileall OK.

## Scope

Проверялись фактические файлы и реальное исполнение:

- `firmware/src/app.py` (`ProviderAdd`/`ProviderModel`, `/api/settings/providers/add`,
  `settings_status`, `_combined_providers`, `_write_combined`), `firmware/src/providers_api.py`,
  `firmware/src/config_ui.py`, `firmware/src/chat_api.py`, `firmware/src/static/app.js`.
- `firmware/tests/test_app.py`, `firmware/tests/test_chat_api.py`, `firmware/tests/test_providers_api.py`.
- `workflows/t_7371c0a8/implementation-report.md`.

## Проверяемые пункты (check-list задачи)

| # | Пункт | Статус | Доказательство |
|---|---|---|---|
| 1 | HIGH — контракт `models` «Добавить провайдера» | PASS | `ProviderAdd.models = list[str] \| list[ProviderModel]` (app.py:48-56); роут нормализует `[{name,tag}]` → `[name,...]` (app.py:293). Эмпирика: POST `/api/settings/providers/add` с `models:[{name,tag},...]` вернул **200** (не 422), провайдер записан в create-реестр, `AttributeError` отсутствует. Pydantic-парсинг обеих форм (`list[str]` и `list[ProviderModel]`) проверен отдельно. |
| 2 | MEDIUM — тест gating'а `POST /api/documents/{sid}/index` | PASS | `test_index_requires_markdown_registration_and_write_permission` (test_app.py:21-29): без `.md`/`_reg.yaml` → **400**. Ветка `write_enabled=False` отдельно не параметризована (см. Caveats). |
| 2 | MEDIUM — мок `qa_graph`, проверка `ChatSession._format` | PASS | `test_chat_format_uses_graph_result_and_image_selection` (test_chat_api.py:15-23): конструирует `ChatSession` через `__new__` (без импорта тяжёлого `qa_graph`), мокает `select_images`, проверяет `type/answer/images` из `_format`. Подход «обойти граф» равносилен/сильнее мока `qa_graph`. |
| 3 | MEDIUM — регресс `settings_status` | PASS | `settings_status` читает `_combined_providers()` и роли `build_search_index.*` (app.py:331-336). Тест `test_settings_status_reads_combined_pipeline_roles` (test_app.py:31-36) подтверждает `build_search_index.embedding is True`. |
| 4 | LOW — `_write_combined` не дублирует providers-реестр | PASS | Эмпирика на temp-путях: create-файл получил `p_vision + p_unassigned`, search-файл — только `p_embed`; роли разнесены; неназначенный провайдер остался в create-реестре. Дублирования полного реестра в оба файла нет. |

## Tests executed

```
python3 -m pytest firmware/tests -q --ignore=firmware/tests/test_gap_filling.py   -> 19 passed
python3 -m pytest firmware/tests/test_app.py firmware/tests/test_chat_api.py firmware/tests/test_providers_api.py -q -> 9 passed
python3 -m compileall -q firmware/src firmware/tests                              -> OK
```

Плюс ручные (не в pytest) проверки вживую: end-to-end `/api/settings/providers/add`
с `[{name,tag}]` через TestClient (200, запись в create-реестр, models нормализованы,
search-реестр пуст) и `_write_combined` на синтетических данных (разнесение по ролям,
неназначенный провайдер в create).

## Findings

Нет блокирующих замечаний.

## Caveats (не блокируют)

1. **LOW — комментарии в `providers.yaml` по-прежнему стираются.** `_write_combined` →
   `config_ui.write_yaml` использует `yaml.safe_dump` (config_ui.py:17); параметр
   `comments_preserving` в сигнатуре не задействован. Пункт 4 сформулирован как
   «по возможности» — дублирование устранено, сохранение комментариев осталось
   незакрытым best-effort'ом.
2. **LOW — тест gating'а покрывает ветку «нет `.md`/`_reg.yaml`», но не `write_enabled=False`.**
   Функционально гейт — одно `and`-условие (app.py:152), регресс «индекс закрыт без
   предварительных шагов» покрыт; отдельная параметризация `write_enabled` желательна,
   но не обязательна.
3. **INFO — поле `tag` в `ProviderModel` объявляется, но не используется** в add-флоу:
   роут берёт только `.name` и повторно вычисляет тег через `tag_model` (детерминировано
   от имени). Избыточно, но согласовано со scan (тот же `tag_model`).

## Risks

- При будущем изменении структуры `providers.yaml` (появление собственных top-level
  ключей у search-файла) `_combined_providers` сохраняет только `providers`/`roles`
  search-стороны — прочие top-level ключи search-файла теряются при `_write_combined`.
  Вне рамок текущей задачи, но стоит помнить.

## Notes

- Не трогал пайплайны `Create_Markdown_YA` / `Build_Search_index` (только чтение).
- Редактирований production-кода ревьюером не выполнялось.
