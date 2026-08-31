# Review — Итерация 5 (t_9f9bb5ad)

**Вердикт: PASS**

Рецензент: reviewer. Дата: 2026-08-28. Проверено независимо по фактическому коду, тестам и живому запуску на dev-инстансе 127.0.0.1:8099 — НЕ по отчёту кодера.

## Scope

Итерация 5 веб-интерфейса interface_RAG (решения 40–46): «Удалить результат», реворк «Провайдеры»/«Роли», onclose/onerror сброс «Думаю…». Критерии соответствия — `docs/architecture/architecture.md` §13 и `docs/architecture/implementation-plan.md` (этапы 5.1–5.4).

Изменённые файлы (git diff): `firmware/src/app.py`, `firmware/src/providers_api.py`, `firmware/src/static/app.js`, `firmware/src/static/index.html`, `firmware/src/static/settings.js`, `firmware/src/static/style.css`, `firmware/tests/test_app.py`, `firmware/tests/test_providers_api.py` + docs (architecture, implementation-plan, decision-log). Сторонних изменений нет.

## Требования (соответствие по пунктам задачи)

### 1. «Удалить результат» (решение №40) — PASS

- **Точный состав удаления против §13.1.** `_delete_result(cfg, stem)` (`app.py:219`) удаляет ровно три цели: `<upload_base_dir>/tmp/<stem>/` (rmtree), `<base_markdown>/<stem>/image/` (rmtree), `<base_markdown>/<stem>/table_images.json` (unlink). **Не удаляются**: `<stem>.md`, `<stem>_reg.yaml`, `<stem>_chunks.jsonl`, `<stem>_assets.json`, исходный файл, `tmp/Create_Markdown_VisionOCR.log`, `tmp/.ai_checkpoints/`. Соответствует таблице §13.1 и решению №40.
- **Живой E2E** (реальная сессия через `POST /api/documents`, посаженные артефакты): ответ `{"removed":3, deleted:[tmp/<stem>, image/, table_images.json], kept:{md,reg,chunks,assets}=все present}`. После вызова на диске: `tmp/<stem>/` и `image/` отсутствуют, `table_images.json` отсутствует; `<stem>.md`, `_reg.yaml`, `_chunks.jsonl`, `_assets.json`, исходник — на месте.
- **Подтверждение**: `confirm()` в `app.js:537` перечисляет удаляемое/сохраняемое.
- **Безопасность при отсутствии артефактов**: повторный вызов идемпотентен — `removed:0`, message «Нечего удалять — промежуточных артефактов нет» (проверено live, второй вызов).
- **Безопасность путей**: `_valid_stem` (regex `^[\w.а-яА-ЯёЁ -]+$` + запрет `..`, `/`, `\`, ведущей точки) + `resolve()` + `relative_to()` containment-проверка до удаления (`_under`, `path == root` отклоняется). pytest `test_delete_result_rejects_unsafe_stem` покрывает `../evil`, `a/b`, `..`, ``, `x\y`, `.hidden`.
- **Маршрут**: stem берётся только из сессии (клиент не задаёт путь); чужой sid → 404, невалидный stem → 400 (проверено live и тестом).
- **Права файлов (контракт 0777/0666 через `fs_perms`)**: delete-result только удаляет (rmtree/unlink), файлов не создаёт — контракт не нарушается. Сохраняемые файлы создаются на шаге upload через `fs_perms.ensure_dir(0777)`/`write_bytes(0666)` (`app.py:106–112`), права сохраняются. `.env`/`providers.yaml` остаются 0600 (подтверждено `ls -l config/`).

### 2. «Провайдеры» (решения 41–45) — PASS

- **Карточки**: в браузере 5 карточек (deepseek 3, provod 46, anymodel 82, zai-custom 10, siliconflow 76 моделей), каждая с редактируемыми имя/base_url, ключом-маской, `<details>` списка моделей с тег-`<select>` и «×», «+ модель», «Обновить модели», «Удалить». Секция `provider-keys` удалена из `NAV_SECTIONS`.
- **Редактирование всех полей**: имя (rename, №44), base_url, ключ (env-row карточки), модели (PUT `models`). Сбор через `collectProviderEdits()` → `PUT /api/settings/providers/{name}`.
- **«Удалить» с защитой (№41)**: `delete_provider` → `_referenced_roles` сканирует `roles.*.*.provider` и `roles.*.*.fallback.provider`. Live: `DELETE /api/settings/providers/deepseek` (назначен) → **409** «Провайдер используется ролями: build_search_index.query_processing, create_markdown.ai_postprocess»; `DELETE ghost` → 404.
- **«Обновить модели» per-provider (№45)**: `refresh_provider` сканирует `/models` только целевого провайдера, ошибка изолируется `{ok:false,error}` без падения; запись в yaml только при изменении списка. Live: `POST /providers/ghost-provider/refresh` → 404. Изоляция подтверждена тестом `test_refresh_provider_single_isolates_error` (только `providers[name]` обновляется).
- **Ключи (№43)**: значения только в `config/.env` (0600). `GET /api/settings/env` возвращает только маски `••••<last4>` (live-проверка всех 8 ключей). `GET /api/settings/providers` возвращает `api_key_env` = имя переменной, но НЕ значение. В карточках ключи маскированы (`••••28cb` и т.д.). `collectEnv` пропускает значения с префиксом маски; запись — через `PUT /api/settings/env`. Дампов providers.yaml/.env нет.

### 3. «Роли» (решение №42) — PASS

- **Отдельные списки**: «Провайдер» и «Модель» — два независимых `<select>` (`data-role-provider` / `data-role-model`).
- **Каскадность**: в браузере подтверждено — chat: deepseek → модели deepseek (3); vision: provod → фильтр по тегу `vision` (glm-4.5v, glm-4.6v); embedding: siliconflow → только embedding-модели (3); rerank: siliconflow → только rerank-модели. Fallback — те же два каскадных селекта (заполнены: chat fallback provod→deepseek-v4-pro, vision fallback anymodel→glm/glm-4.6v).
- **Смена провайдера → модели только его**: интерактивная проверка через `dispatchEvent('change')` — смена chat provider на provod перестроила список на 47 опций (пустая + 46 provod) и сбросила модель в «». `repopulateRoleModels` сохраняет модель вне списка опцией «… (вне списка)».
- Синхронные пары пишутся через `PUT /api/settings/providers/roles` (sync_role_models); live `PUT /roles` → 200 (маршрут не затенён `/{name}`, закрыто тестом `test_put_roles_route_not_shadowed_by_name_route`).

### 4. onclose/onerror сброс «Думаю…» (решение №46) — PASS

- `app.js:163–168` в `openChat()`: `ws.onclose` очищает `#chat-status`, сбрасывает `awaitingClarification`, ставит «Соединение прервано — повторите вопрос» (warn); `ws.onerror` очищает статус. «Думаю…» ставится в `sendChat()` (`app.js:177`) и снимается в `onmessage` (answer/clarification/error). Дословно соответствует §13.3. Бэкенд не менялся.

### 5. Регрессии — PASS

- Загрузка (живой multipart upload → сессия создана), регистрация/конвертация/индексация/чат/настройки — покрыты pytest (95 passed).
- Единая кнопка «Сохранить» сохранена (`saveSettings`: провайдеры → роли → граф → env → qdrant); глобальная «Обновить модели» и `settings-refresh` убраны (эндпоинт `POST /providers/refresh` оставлен для совместимости — по №45).
- Gating шагов: `updateGates` (`app.js:234`) — `register/convert/index/stop/delete-result` + `rolesConfigured` через `rolesReady()` (`/api/settings/status`, роли `table_vision`+`ai_postprocess`). Не сломано.
- Атомарность `providers.yaml`/`.env`: `write_yaml`/`write_env` — temp + `os.replace` + chmod 0600 (`.bak` тоже 0600). Проверено в `config_ui.py:17–31,52–73`.

### 6. Тесты — PASS (фактический прогон)

- `cd firmware && python3 -m pytest tests/ -q` → **95 passed, 1 warning** (было 79; +16 новых: 9 в test_app.py, 7 в test_providers_api.py). Тесты ассертят поведение (удаление ровно трёх артефактов + сохранение .md/_reg/_chunks/_assets, идемпотентность, stem-traversal, 409-guard, rename-каскад ссылок ролей, per-provider refresh изоляция, приоритет `/roles` над `/{name}`), а не снапшоты.
- `python3 -m py_compile src/*.py` → OK. `node --check settings.js app.js` → OK.

### 7. Dev-инстанс (порт 8099) — PASS

- Процесс жив (`uvicorn app:app --host 127.0.0.1 --port 8099`, PID 110886, свежий рестарт). `/api/settings/status` → 200. Живые проверки новых эндпоинтов и удаление артефактов на реальном каталоге — см. выше (delete-result removed=3 с сохранением .md, DELETE назначенного → 409, ghost → 404, `/roles` не затенён).

## Замечания (неблокирующие, LOW)

1. **LOW — stem с символами вне `[\w.а-яА-ЯёЁ -]`** (`app.py:208`, `/api/files/markdown/{stem}:310`). Документ с именем вида «ГОСТ (ред.1).pdf» загрузится (upload не валидирует stem), но «Удалить результат»/вьювер .md вернут 400/404. Является явно заданным контрактом §13.1 (regex совпадает с существующим маршрутом markdown), т.е. НЕ дефект реализации; отмечаю как осведомлённость, не требующую правок.

2. **LOW — UX: «Обновить модели»/«Удалить» до сохранения** (`settings.js:466,481`). Кнопки используют `origName` из `data-provider`; если пользователь переименовал провайдера в поле, но не нажал «Сохранить», refresh/delete сработают по старому имени, а `loadSettings()` перерисует карточку и потеряет несохранённый rename. Пробел требований (архитектура не регламентирует это состояние). Не влияет на корректность сохранённых данных.

3. **LOW — `deleted` в ответе содержит абсолютные пути** (`app.py:252`). Соответствует контракту §13.1; приложение без авторизации (решение №8), пути уже видны в `/api/documents/{sid}`. Не новый риск.

Классификация: пп.1–3 — пробелы/нюансы требований, НЕ дефекты реализации и НЕ дефекты архитектуры. Отклонений реализации от архитектуры §13 не обнаружено.

## Примечание о среде (side-effect рецензента, восстановлено)

При независимой проверке маршрута `PUT /api/settings/providers/roles` тест записал роль `embedding` = deepseek/deepseek-v4-pro, что изменило исходное значение (siliconflow/Qwen/Qwen3-Embedding-8B). Восстановлено тем же API (`PUT /roles` с исходным spec); `providers.yaml` и `.bak` сверены — идентичны исходному состоянию, тестовые артефакты (`uploads/tmp/`, `uploads/Markdown/<stem>/`, исходник) удалены. `.hermes/`, `.git`, пайплайны не трогались; `git restore/checkout/stash` не выполнялись.

## Итог

Все 4 фичи работают по фактическому запуску, ключи не утекают (только маски), регрессий нет, тесты зелёные (95 passed). Критерий PASS выполнен.
