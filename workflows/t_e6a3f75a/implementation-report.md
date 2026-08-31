# Отчёт об имплементации — Итерация 5 (t_e6a3f75a)

Дата: 2026-08-28. Архитектура: architecture.md §13, implementation-plan.md (этапы 5.1–5.4), решения 40–46.

## 1. «Удалить результат» (решение №40)

**Backend (`firmware/src/app.py`)**
- `_STEM_RE` / `_valid_stem(stem)` — валидация stem: regex `^[\w.а-яА-ЯёЁ -]+$`, без `..`, `/`, `\`, без ведущей точки (скрытые каталоги).
- `_delete_result(cfg, stem)` — удаляет ровно три артефакта:
  `<upload_base_dir>/tmp/<stem>/`, `<base_markdown>/<stem>/image/`, `<base_markdown>/<stem>/table_images.json`.
  Каждый путь `resolve()`-ится и проверяется на префикс разрешённого корня до удаления
  (двойная защита: stem-валидация + containment-проверка). Возвращает
  `{deleted, removed, kept{md,reg,chunks,assets}, message}`; повторный вызов идемпотентен
  («Нечего удалять — промежуточных артефактов нет»).
- Маршрут `POST /api/documents/{sid}/delete-result` — stem берётся только из сессии
  (клиент не может задать произвольный путь), чужой sid → 404, невалидный stem → 400.

**Frontend (`static/index.html`, `static/app.js`)**
- Кнопка «Удалить результат» (`#delete-result`, danger) рядом со «Стоп» в зоне прогресса.
- Доступность: `disabled = !state.sid` в `updateGates()`.
- `confirm()` с перечнем удаляемого/сохраняемого; результат — в `#doc-status`
  (ok при удалении, warn при «нечего удалять»), затем `updateGates()` обновляет UI.

## 2. Раздел настроек «Провайдеры» (решения 41–45)

**Backend (`firmware/src/providers_api.py`)**
- `_referenced_roles(data, name)` — роли (pipeline.role), где провайдер — основная модель или fallback.
- `update_provider(path, name, patch)` — правка имени/base_url/models; переименование
  атомарно переносит ключ и переписывает все ссылки `roles.*.*.provider` и
  `roles.*.*.fallback.provider` (№44); конфликты имён/пустое имя/плохой base_url → ValueError;
  неизвестный тег модели → авто-тегирование `tag_model()`.
- `delete_provider(path, name)` — защита №41: используется ролями → ValueError со списком ролей.
- `refresh_provider(path, name, env)` — скан `/models` только целевого провайдера (№45);
  ошибка изолируется (`{ok: false, error}`); запись в yaml только при изменении списка.

**Маршруты (`app.py`)** — `PUT /api/settings/providers/{name}`,
`DELETE /api/settings/providers/{name}` (409 при использовании),
`POST /api/settings/providers/{name}/refresh` (404 для неизвестного).
Статические `/roles`, `/refresh`, `/add`, `/scan` зарегистрированы раньше `{name}` —
приоритет сохранён (закрыто регрессионным тестом).

**Frontend (`static/settings.js`, `static/style.css`)**
- Секции разделены: «Провайдеры» и «Роли» (NAV_SECTIONS).
- Карточка на провайдер: редактируемые имя и base_url, ключ API (маска из GET /env,
  редактирование через общий collectEnv → PUT /env; значение НЕ переносится в providers.yaml, №43),
  `<details>` со списком моделей (имя + тег select + × удалить), «+ модель» (инлайн-добавление),
  «Обновить модели» (пер-provider, сразу на сервер, результат ✓/✗ в карточке, ошибка одного
  не роняет других), «Удалить» (confirm → пометка в `state.removedProviders`, баннер
  «Помечены к удалению…» с «Вернуть все», фактическое удаление — при «Сохранить», 409 показывается статусом).
- Единая кнопка «Сохранить» сохранена: правки карточек → PUT, пометки удаления → DELETE,
  затем роли/граф/env/qdrant как раньше. Удалены мёртвые `refreshModels()`, глобальная кнопка
  «Обновить модели» и карточка «Ключи API провайдеров» (переехала в карточки).

## 3. Раздел «Роли»: каскад Провайдер → Модель (решение №42)

- В каждой роли (chat/vision/embedding/rerank) — два отдельных селекта: «Провайдер» и
  «Модель»; список моделей фильтруется по выбранному провайдеру и тегу роли
  (`repopulateRoleModels`). Смена провайдера сбрасывает модель.
- Роль, ссылающаяся на модель вне списка (например, удалённую из карточки), сохраняется
  опцией «… (вне списка)» — прежнее поведение bindRoleControls.
- Fallback — те же два каскадных селекта (отдельный провайдер + модель), чекбокс как раньше.
- Синхронные роли (chat → ai_postprocess+query_processing, vision → table_vision+registration_vision)
  пишутся вместе через прежний `PUT /api/settings/providers/roles` (sync).

## 4. Сброс «Думаю…» при обрыве WS (решение №46)

`static/app.js openChat()`: `ws.onclose` — очищает статус, снимает `awaitingClarification`,
ставит «Соединение прервано — повторите вопрос» (warn); `ws.onerror` — очищает статус.

## Отклонения от архитектуры

Нет. Реализация следует §13 и решениям 40–46.

## Проверка (фактический вывод)

1. `python3 -m py_compile firmware/src/*.py` → OK.
2. `node --check settings.js && node --check app.js` → JS-OK.
3. `cd firmware && python3 -m pytest tests/ -q` → **95 passed, 1 warning** (было 79; добавлено 16 тестов).
   Новые в `tests/test_providers_api.py` (7): rename с каскадом ссылок, конфликты/плохой url/пустое имя,
   правка models+base_url с нормализацией тега, guard-удаление (409-сценарий, KeyError), per-provider refresh
   (ошибка/без ключа/успех с записью). Новые в `tests/test_app.py` (9): удаление ровно трёх артефактов
   с сохранением .md/_reg/_chunks/_assets, идемпотентность, недопущение unsafe-stem
   (`../evil`, `a/b`, `.hidden`, `x\y`…), эндпоинт на 404/200, PUT/DELETE/refresh-маршруты,
   регрессия приоритета `/roles` над `/{name}`.
4. Ручная проверка на dev-инстансе 127.0.0.1:8099 (рестарт тем же способом запуска —
   `python3 -m uvicorn app:app --host 127.0.0.1 --port 8099` из `firmware/src`; старый процесс 44070 остановлен,
   новый поднят и оставлен работать):
   - `POST /api/documents/{sid}/delete-result` на реальной сессии с посаженными артефактами →
     `{"removed":3, kept.md — на месте}`, `ls Markdown/e2e-iter5/` → только `e2e-iter5.md`; tmp/ и image/ удалены.
   - `DELETE /api/settings/providers/deepseek` (используется ролями) → **409**
     «Провайдер используется ролями: build_search_index.query_processing, create_markdown.ai_postprocess».
   - `DELETE|POST refresh` неизвестного провайдера → 404; `PUT /providers/roles` → 200 (не затенён).
   - `/static/settings.js` отдаётся с новым кодом; тестовые файлы/сессии убраны.
5. Ключи: GET /providers и ответы ошибок не содержат значений ключей (маска только в /env,
   существующий тест `test_settings_response_does_not_expose_env_values` проходит).

## Что осталось

- Ничего не заблокировано. Прод-деплой — вне рамок карточки (deploy.sh запускается отдельно).
- Reviewer-карточка t_9f9bb5ad уже создана оркестратором как child — завершение этой карточки
  (kanban_complete) релизует её.
