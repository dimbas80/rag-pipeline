# Implementation Report — архитектура итерации 5 (t_d5c4c10a)

> Профиль: architect. Дата: 2026-08-28. Проект: interface_RAG.
> Вход: решения 1–39 + исходники `firmware/src/` + `create_markdown.py` (Create_Markdown_YA, чтение).

## Что спроектировано

Итерация 5 — три фичи (решения 40–46), только на стороне интерфейса. Пайплайны
Create_Markdown_YA / Build_Search_index НЕ меняются.

1. **«Удалить результат»** (кнопка рядом со «Стоп», область 3) — файловая операция интерфейса.
2. **Реворк настроек**: «Провайдеры и роли» → «Провайдеры» + «Роли»; карточки провайдеров
   (редактируемые), per-provider «Обновить модели»/«Удалить» с защитой, каскадные селекты ролей.
3. **onclose/onerror сброс «Думаю…»** — закрытие known issue из `.hermes/STATE.md`.

Артефакты: `docs/architecture/architecture.md` (§13 + трассируемость), 
`docs/architecture/implementation-plan.md` (этапы 5.1–5.4), 
`docs/product/decision-log.md` (решения 40–46), этот отчёт.

## Ключевые решения (детали в decision-log.md)

- **Судьба `.md` при удалении результата — НЕ удалять.** Проверенный Markdown — самостоятельная
  ценность; согласуется с решением 39 (переиндексация без пересохранения). Зафиксировано в №40.
- **Точный список удаляемых артефактов** (выверен по `create_markdown.py` §2.1/main):
  `tmp/<stem>/` (OCR-кэш `yandex_result.json`, `raw.md`, `table_*.md`, PDF для DOCX, копия
  `table_images.json`), `Markdown/<stem>/image/`, `Markdown/<stem>/table_images.json`. Сохраняются
  `.md`, `_reg.yaml`, `_chunks.jsonl`, `_assets.json`, исходный файл, общий лог, `.ai_checkpoints`.
- **Последствие удаления (проверено по коду):** `_run_rag_only` при отсутствии `image/` пишет
  warning «реестр активов будет пустым» и продолжает — повторная индексация по сохранённому `.md`
  не падает, лишь без картинок в активах.
- **Защита удаления провайдера:** назначенного в роль (`provider`/`fallback.provider`) удалить
  нельзя — 409 с перечнем ролей. Каскадное обнуление ролей отклонено.
- **Схема хранения/отображения ключей:** значения — только в `config/.env` (единый источник,
  решения 19/22); в карточке — маска `••••<last4>` + редактирование через существующий
  `PUT /api/settings/env`. Значения НЕ переносятся в `providers.yaml` (`api_key_env` — только имя).
- **Имя провайдера** — редактируемо (атомарное переименование с переписыванием ссылок ролей);
  `api_key_env` — внутренний, не отображается.
- **Роли** — каскадные «Провайдер»→«Модель», синхронные пары сохранены (решения 6/9: chat →
  ai_postprocess+query_processing; vision → table_vision+registration_vision).

## Контракты эндпоинтов (для Coder)

- `POST /api/documents/{sid}/delete-result` → `{deleted[], removed, kept{md,reg,chunks,assets}, message}`.
- `PUT /api/settings/providers/{name}` → `{name?, base_url?, models?}`; rename переписывает роли.
- `DELETE /api/settings/providers/{name}` → 409 при использовании в ролях, 200 `{deleted}`.
- `POST /api/settings/providers/{name}/refresh` → `{ok, models|error}` (изоляция ошибки).
- Существующие `PUT /api/settings/env`, `PUT /api/settings/providers/roles`, `POST .../add`,
  `POST .../refresh` (глобальный) — без изменений.

Новые функции `providers_api.py`: `refresh_provider`, `update_provider`, `delete_provider`
(+ `_referenced_roles`). Хелпер удаления `_delete_result(cfg, stem)` — в `app.py`.

## Не регрессирует по решениям 1–39

- Единый источник ключей (`.env`) и их маскирование — сохранены (№43, §13.2).
- Атомарная запись конфигов (`config_ui.write_yaml`/`write_env`) — сохранена (новые эндпоинты идут
  через `config_ui`/`providers_api.write_yaml`).
- Синхронные роли (решения 6/9) — сохранены через `sync_role_models`.
- Единая кнопка «Сохранить» — сохранена (расширена шагом провайдеров).
- Права файлов (0777/0666), gating шагов, дедуп, стриминг WS, md-переиндексация — не затрагиваются.

## Открытые вопросы / риски (не блокируют)

- Порядок сохранения в `saveSettings()`: провайдеры → роли → env → graph → qdrant. Rename
  провайдера и одновременная правка роли в одном «Сохранить» обрабатываются последовательно
  (rename раньше ролей), поэтому ссылки ролей остаются консистентными.
- Проверку пер-провайдерного refresh на живом `/models` dev-инстансе выполняет Coder (реальные
  ключи в `config/.env`); в тестах — мок `scan_and_tag_models`.
- `.md` после «Удалить результат» остаётся с битыми ссылками на картинки (image/ удалён) — ожидаемо
  и задокументировано; MD-вьювер от этого не падает (картинки просто не грузятся).
