# Implementation-report — унификация ключей in-process чата + дочистка .env пайплайнов (t_a7d01588)

Реализация строго по архитектуре `workflows/t_6f25646e/architecture-report.md` (§5).

## 1. Что изменено

### 1.1 `firmware/src/chat_api.py` (единственный runtime-файл)

- Добавлен `import os`.
- Добавлен импорт `read_env_raw` try/except-паттерном, дословно как в `jobs.py:13-15`
  (package- и direct-запуск `uvicorn app:app`).
- Новая функция `_inject_env_file(env_file)` — `os.environ.update(read_env_raw(env_file))`,
  override-семантика, идентичная `jobs.build_env`; docstring из архитектуры §2.1.
- `ChatSession.__post_init__`: **первой строкой тела** — `_inject_env_file(self.cfg.env_file)`
  ДО `_pipeline_paths` и ДО `from qa_graph import ...` (комментарий-обоснование оставлен).

Семантика: инъекция повторяется на каждую WebSocket-сессию (новый `ChatSession` на
подключение), читает `config/.env` свежим — та же гранулярность, что `build_env` у
subprocess («сессия ↔ job»). Override гарантирует, что заражение от пайплайнового
`load_dotenv()` (setdefault) перетирается значением `config/.env`, и что смена ключа
через UI (`PUT /api/settings/env`) доходит до чата без рестарта. Неразрушающе:
`os.environ.update` не удаляет прочие ключи (`systemd Environment=` сохраняется).

### 1.2 `scripts/deploy.sh` — три правки архитектуры §4.1

1. **Шаг 0 (FILES)**: из read-only выгрузки удалены 3 legacy-источника
   (`interface_RAG/.env`, `CMY/.env`, `BSI/.env`); остались `config/.env` + оба
   `providers.yaml`; убран неиспользуемый каталог `$STAGE/remote/interface_RAG`.
2. **Шаг 1 (бэкап)**: добавлены корневые `.env` пайплайнов в бэкап
   (`CMY:/root/RAG/Create_Markdown_YA/.env:.env.root`, `BSI:/root/RAG/Build_Search_index/.env:.env.root`),
   чтобы удаление было обратимым (прежде всего `TELEGRAM_ALLOWED_USERS`); описание шага
   в dry-run-плане дополнено `.env.root`.
3. **Шаг 4 (cleanup)**: добавлено удаление корневых `.env` пайплайнов рядом с
   существующими `rm -f .../firmware/src/.env` + комментарий о `request_bot.py`
   (не разворачивается, исключён из rsync, не запущен; при будущем запуске — systemd
   `Environment=`/`EnvironmentFile=`, отдельная задача).

### 1.3 `scripts/consolidate_config.py` — архитектура §4.2

- `env_sources` сведён к 2 источникам: `remote/config/.env` → `dev config/.env`
  (5 legacy-источников удалены). `merge_env`/`CANONICAL_KEYS` НЕ тронуты —
  консолидация остаётся неразрушающей (setdefault, все ключи из источников сохраняются).
- Обновлён docstring модуля (блок «Источники») и комментарий у блока сборки `.env`.

### 1.4 `firmware/tests/` — архитектура §6.1-6.2

- `test_chat_api.py`: +3 теста инъекции через `_inject_env_file` (без импорта `qa_graph`):
  override побеждает «загрязнение» process-env; несвязанные ключи сохраняются;
  отсутствующий файл — no-op.
- `test_deploy_config.py`: +1 регрессия — `test_env_injection_does_not_change_qdrant_path`
  (инъекция кладёт `QDRANT_PATH` в `os.environ`, резолвер обязан читать только `.env`;
  с очисткой `QDRANT_PATH` из env после теста).

## 2. Проверка (реальные прогоны)

| Проверка | Результат |
|---|---|
| `python3 -m pytest firmware/tests -q` | **69 passed** (1 предупреждение StarletteDeprecation, не связано) |
| `bash -n scripts/deploy.sh` | OK |
| `python3 scripts/consolidate_config.py --out <tmp> --remote <tmp>/remote` | `env sources: remote/config/.env, dev config/.env` — ровно 2 источника, legacy отсутствуют; `AITUNNEL_API_KEY` сохранён из первого источника |
| `python3 scripts/consolidate_config.py --out <tmp>` (без remote) | `env sources: dev config/.env` — 1 источник, 8 канонических ключей |
| `scripts/deploy.sh --dry-run` | exit 0; план: бэкап с `.env.root` → консолидация (2 источника) → rsync → cleanup → рестарт → smoke |
| Живой смоук (реальный `config.yaml` + `config/.env`) | после `_inject_env_file(cfg.env_file)` в `os.environ` видны **все 8 ключей** `config/.env`, включая `ANYMODEL_API_KEY`/`Z_AI_API_KEY`; затем реальный `load_dotenv(Build_Search_index/.env)` (эмуляция `get_api_key`) **не смог перетереть** ни один инъецированный ключ (setdefault) |

Значения ключей нигде не печатались и в отчёт не включены.

## 3. Границы (соблюдены)

- Пайплайны `Build_Search_index/**`, `Create_Markdown_YA/**` — не тронуты.
- `deploy_config.py` (QDRANT_PATH-фикс), `config_ui.py`, `app.py`, `jobs.py` — без изменений.
- `.env` интерфейса 0600 — не менялся, инъекция только читает.
- `.hermes/STATE.md` — не тронут.
- Реальной чистки прода не выполнялось (только подготовлен код в `deploy.sh`).

## 4. Уточнение оператора (AITUNNEL_API_KEY)

Оператор скорректировал флаг архитектуры §7.1: `AITUNNEL_API_KEY` **реально существует**
на проде — в `remote/config/.env`, провайдер `Aitunnel` в прод `providers.yaml` стоит
fallback'ом для embedding/rerank. Это подтверждено живым dry-run деплоя (LXC доступен):
консолидированный набор ключей включает `AITUNNEL_API_KEY` из первого источника.
Фильтрации по этому ключу не производилось и не требуется: `merge_env` неразрушающий,
при сведении к 2 источникам ключ сохраняется автоматически. Механизм инъекции
ключ-агностичен (кладёт весь `.env`), поэтому на проде `AITUNNEL_API_KEY` попадёт в
`os.environ` чата тем же путём, что и остальные ключи.

## 5. Известные ограничения / флаги

1. **Свежесть ключей** — «новая WebSocket-сессия»: уже открытая сессия держит старый
   ключ до переподключения (та же семантика, что у запущенного job). Приемлемо, вне объёма.
2. **`TELEGRAM_ALLOWED_USERS`** теряется при удалении корневого `Build_Search_index/.env`
   (бот-only, декоммишн) — обратимо через бэкап `.env.root`; зафиксировано в архитектуре §3.3.
3. Живой dry-run показал, что прод `remote/config/.env` **сейчас ещё содержит**
   `QDRANT_PATH` и `TELEGRAM_ALLOWED_USERS` — консолидация их сохраняет (авторитетный
   источник, неразрушающий). Очистка `QDRANT_PATH` внутри `config/.env` — вне объёма
   этого фикса (UI/отдельное решение); удаление корневых `.env` пайплайнов при деплое —
   оркестратор через `scripts/deploy.sh --apply`.
4. Инъекция не маскирует отсутствие провайдера `aitunnel` в dev `providers.yaml` —
   вопрос отдельный, оркестратору.

## 6. Отклонений от архитектуры нет

Все правки — ровно по §5.1-5.4 (chat_api.py, deploy.sh, consolidate_config.py, тесты).
