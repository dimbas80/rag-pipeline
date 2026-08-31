# Review — t_c1a7ec42 (финальное ревью Fix 4 остаточных пунктов)

## Verdict

**PASS**

`assign_rework_to=` (не требуется).

Все четыре остаточных пункта из `workflows/t_e7ebcb27/review.md` закрыты и проверены
вживую (не по отчёту coder'а и не по handoff родителя t_7371c0a8): контракт «Добавить
провайдера» согласован (200 вместо 422), тесты gating'а индекса и `ChatSession._format`
есть и проходят, `settings_status` читает объединённые роли обоих пайплайнов,
`_write_combined` раскладывает провайдеров по pipeline-ролям без дублирования
чужого реестра. `pytest firmware/tests -q` → **19 passed**.

## Scope

Проверялись фактические файлы и реальное исполнение:

- `firmware/src/app.py` (`ProviderAdd`/`ProviderModel`, `/api/settings/providers/add`,
  `settings_status`, `_combined_providers`, `_write_combined`), `firmware/src/providers_api.py`,
  `firmware/src/config_ui.py`, `firmware/src/chat_api.py`, `firmware/src/static/app.js`.
- `firmware/tests/test_app.py`, `firmware/tests/test_chat_api.py`, `firmware/tests/test_providers_api.py`.
- `docs/product/decision-log.md` (№5, №6, №9).
- `workflows/t_e7ebcb27/review.md` (исходный вердикт с 4 findings), `workflows/t_7371c0a8/review.md`.

## Проверяемые пункты (check-list задачи)

| # | Пункт | Статус | Доказательство (вживую) |
|---|---|---|---|
| 1 | Контракт `models` «Добавить провайдера» | PASS | `ProviderAdd.models = list[str] \| list[ProviderModel]` (app.py:52-56); роут нормализует `[{name,tag}]` → `[name,...]` (app.py:293). TestClient end-to-end: `POST /api/settings/providers/add` с `models:[{name:'gpt-4o',tag:'vision'},...]` → **200** (не 422), провайдер записан в create-реестр, теги сопоставлены (`gpt-4o→vision`, `text-embedding-3-small→embedding`). Форма `list[str]` тоже 200. Дубль имени → 400. `AttributeError` отсутствует. app.js:7 шлёт `models: scan.models \|\| []` = `[{name,tag}]` — совпадает с бэкендом. |
| 2 | Тест gating'а `POST /api/documents/{sid}/index` | PASS | `test_index_requires_markdown_registration_and_write_permission` (test_app.py:21-29): без `.md`/`_reg.yaml` → **400**. Ветка `write_enabled=False` отдельно не параметризована (одно `and`-условие, app.py:152) — см. Caveats. |
| 2 | Проверка `ChatSession._format` (обход `qa_graph`) | PASS | `test_chat_format_uses_graph_result_and_image_selection` (test_chat_api.py:15-23): `ChatSession.__new__` без импорта тяжёлого `qa_graph`, мок `select_images`, проверка `type/answer/images` из `_format`. Равносильно/сильнее мока `qa_graph`. |
| 3 | Регресс `settings_status` | PASS | `settings_status` читает `_combined_providers()` (app.py:333) и роли `build_search_index.*`. Вживую: после назначения `build_search_index.embedding` → `settings_status()['roles']['build_search_index.embedding'] is True`. Тест `test_settings_status_reads_combined_pipeline_roles` (test_app.py:31-36) подтверждает. |
| 4 | `_write_combined` не дублирует чужой providers-реестр | PASS | Вживую на temp-путях: create-файл получил `{openai, anthropic}` (role-owned + unassigned), search-файл — только `{openai}` (role-owned по embedding). Полного реестра в оба файла нет; `build_search_index` не попадает в create-файл, `create_markdown` — в search-файл. Неназначенный провайдер остаётся в create-реестре. Комментарии по-прежнему стираются (см. Caveats — опц. часть исходного LOW-пункта). |

## Tests executed

```
python3 -m pytest firmware/tests -q                                              -> 19 passed (зелёный, без --ignore)
python3 -m pytest firmware/tests -q --ignore=firmware/tests/test_gap_filling.py  -> 19 passed
python3 -m compileall -q firmware/src firmware/tests                             -> OK
```

Плюс ручной вживую прогон (не в pytest): end-to-end `/api/settings/providers/add`
обеими формами `models` через TestClient (200/200/400), `settings_status` с
`build_search_index.embedding`, `_write_combined` на синтетических данных с проверкой
разноса реестра по ролям и отсутствия дублирования.

Примечание: `firmware/tests/` этого репозитория не содержит `test_gap_filling.py`
(он в пайплайнах-соседях), поэтому `pytest firmware/tests -q` зелёный без `--ignore`;
флаг в отчёте — безвредный перенос конвенции соседних репозиториев.

## Findings

Блокирующих замечаний нет.

## Caveats (не блокируют)

1. **LOW — комментарии в `providers.yaml` по-прежнему стираются.** `_write_combined` →
   `config_ui.write_yaml` использует `yaml.safe_dump` (config_ui.py:17); параметр
   `comments_preserving` в сигнатуре не задействован. Подтверждено вживую: после
   `_write_combined` комментарий из create-файла пропал. Это косметическая/опциональная
   часть исходного LOW-пункта #4 из t_e7ebcb27 («по возможности»); дублирование —
   основная функциональная часть — устранено. Приемлемое решение зафиксировано.
2. **LOW — тест gating'а покрывает ветку «нет `.md`/`_reg.yaml`», но не `write_enabled=False`**
   отдельно. Функционально гейт — одно `and`-условие (app.py:152); регресс «индекс закрыт
   без предварительных шагов» покрыт. Отдельная параметризация желательна, не обязательна.
3. **INFO — поле `tag` в `ProviderModel` объявляется, но не используется** в add-флоу:
   роут берёт только `.name` и повторно тегирует через `tag_model`. Согласовано со scan
   (тот же `tag_model`), избыточно, но не ошибка.
4. **INFO (наблюдение ревьюера, pre-existing, вне 4 пунктов) — `read_yaml` падает с**
   `FileNotFoundError` на несуществующем файле; `add_provider`/`_combined_providers`
   предполагают, что `providers.yaml` уже существует. В реальном деплое оба файла
   создаются пайплайнами (присутствуют в `Create_Markdown_YA/` и `Build_Search_index/`),
   поэтому на проде не воспроизводится. Не блокирует, но стоит помнить при первой
   инициализации окружения.

## Risks

- При будущем появлении собственных top-level ключей у search-файла `_combined_providers`
  сохраняет только `providers`/`roles` search-стороны — прочие top-level ключи search-файла
  теряются при `_write_combined`. Вне рамок текущей задачи.
- Реальный чат/конвертация/индексация на dev не воспроизводятся (нет `.env`, `uploads/`,
  `qdrant_data`); проверка на TestClient/заглушках. `BASE_MARKDOWN` захардкожен в `qa_graph.py`
  соседнего пайплайна.

## Notes

- Пайплайны `Create_Markdown_YA` / `Build_Search_index` не изменялись (только чтение providers.yaml).
- Редактирований production-кода ревьюером не выполнялось.
- Родитель t_7371c0a8 уже зафиксировал PASS; это финальное независимое подтверждение
  (дополнительный вживую прогон, не дублирующий его отчёт).
