# Рефакторинг конфигурации LLM: search_config.yaml + роли providers.yaml + fallback

Статус: проектный документ (архитектура). Код НЕ пишется здесь — реализацию
выполняет coder по этому документу.

Связано с: `docs/software/langgraph-rag-architecture.md`, раздел 6.

---

## 1. Цель и границы

Разделить конфигурацию LLM в проекте `Build_Search_index` на два независимых
источника истины:

1. **`search_config.yaml`** (бывш. `llm_config.yaml`) — только параметры **узлов
   графа** (секция `nodes`): `temperature` / `max_tokens` для четырёх узлов
   `analyze_query`, `reformulate_query`, `ask_clarification`, `generate_answer`.
2. **`providers.yaml`** — реестр провайдеров + назначение ролей пайплайнов
   (`roles.*`). Вызовы chat/embedding/rerank читают модель/провайдера/ключ/URL
   из роли `roles.build_search_index`, а не из `search_config.yaml` и не из
   хардкода.

Дополнительно вводится **fallback-механизм**: при недоступности основной модели
подроли вызов переключается на fallback-модель (если она задана).

**Что НЕ меняется** (требование №8): граф, логика узлов, промпты, алгоритм
поиска и реранка, цитирование, жизненный цикл `QdrantClient` и кэш
`SparseTextEmbedding`. Меняется только «обвязка» — какой провайдер/модель/ключ/
URL используется для конкретного вызова.

---

## 2. Итоговые схемы конфигов

### 2.1. `firmware/src/search_config.yaml` (итог)

```yaml
# Конфигурация узлов поискового графа QA.
# Только параметры узлов графа: temperature / max_tokens.
# Провайдеры/модели/ключи/URL — в providers.yaml (роль roles.build_search_index).

nodes:
  analyze_query:
    temperature: 0.0
    max_tokens: 256
  reformulate_query:
    temperature: 0.3
    max_tokens: 256
  ask_clarification:
    temperature: 0.3
    max_tokens: 512
  generate_answer:
    temperature: 0.0
    max_tokens: 2048
```

Правила:
- Никаких `default_provider` / `default_model` / `providers`.
- Секция `nodes` обязательна, непустая; у каждого узла — числовые
  `temperature` и `max_tokens` (значения переносятся из текущего
  `llm_config.yaml` без изменений).

### 2.2. `firmware/src/providers.yaml` (итог, добавлена `roles.build_search_index`)

```yaml
# Реестр LLM-провайдеров и назначение ролей.
# Секреты не хранятся: api_key_env содержит только имя переменной .env.
providers:
  deepseek:
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
    models:
      deepseek-v4-flash: chat
      deepseek-v4-pro: chat
      deepseek-v4-flash-vision-exp: vision
  provod:
    base_url: https://api.provod.ai/v1
    api_key_env: PROVOD_API_KEY
    models:
      glm-4.5v: vision
      deepseek-v4-pro: chat
      google/gemini-2.5-flash: chat
  anymodel:
    base_url: https://anymodel.org/v1
    api_key_env: ANYMODEL_API_KEY
    models:
      glm/glm-4.6v: vision
      glm/glm-5.2: chat
  zai-custom:
    base_url: https://api.z.ai/api/paas/v4
    api_key_env: Z_AI_API_KEY
    models:
      glm-4.6v-flash: vision
  siliconflow:
    base_url: https://api.siliconflow.com/v1
    api_key_env: SILICONFLOW_API_KEY
    models:
      Qwen/Qwen3.5-35B-A3B: chat
      Qwen/Qwen3-32B: chat
      Qwen/Qwen3-Embedding-8B: embedding
      Qwen/Qwen3-Reranker-8B: rerank

roles:
  create_markdown:            # НЕ ТРОГАТЬ — секция существующего пайплайна
    table_vision:
      provider: provod
      model: glm-4.5v
      fallback:
        provider: anymodel
        model: glm/glm-4.6v
    ai_postprocess:
      provider: deepseek
      model: deepseek-v4-pro
      fallback:
        provider: provod
        model: deepseek-v4-pro
  build_search_index:         # НОВАЯ секция
    query_processing:
      provider: deepseek
      model: deepseek-v4-flash
      fallback:
        provider: provod
        model: deepseek-v4-pro
    embedding:
      provider: siliconflow
      model: Qwen/Qwen3-Embedding-8B
      fallback: {}            # пусто — fallback отсутствует
    rerank:
      provider: siliconflow
      model: Qwen/Qwen3-Reranker-8B
      fallback: {}            # пусто — fallback отсутствует
```

Структурные правила `roles.*` (единообразно для всех пайплайнов, включая
`create_markdown`):

- Подроль — словарь с обязательными `provider` (str) и `model` (str).
- `fallback` — необязательный словарь `{provider, model}`. Допустимы три
  представления «нет fallback»: ключ отсутствует, `fallback: null`, либо
  `fallback: {}` (пустой словарь). Все три НЕ являются ошибкой загрузки.
- Если `fallback` задан — у него обязательны оба поля `provider` и `model`.
  Малформированный fallback (есть `provider`, нет `model` и т.п.) — ошибка
  резолва роли (не загрузки файла).

> Примечание для coder: `providers.yaml` в репозитории НЕ закоммичен (git `??`),
> в нём незакоммиченная работа пользователя. Редактировать ТОЛЬКО добавлением
> `roles.build_search_index`; `providers` и `roles.create_markdown` оставить
> байт-в-байт. Запрещено `git restore/checkout/stash/clean`.

---

## 3. Endpoint-mapping (требование №4)

`base_url` провайдера хранится БЕЗ суффикса endpoint. Полный URL строится
функцией-конструктором:

```
capability → суффикс
  chat      → /chat/completions
  embedding → /embeddings
  rerank    → /rerank

endpoint = providers[provider]["base_url"].rstrip("/") + suffix
```

Примеры:

| provider | base_url | capability | итоговый endpoint |
|---|---|---|---|
| deepseek | `https://api.deepseek.com/v1` | chat | `https://api.deepseek.com/v1/chat/completions` |
| siliconflow | `https://api.siliconflow.com/v1` | embedding | `https://api.siliconflow.com/v1/embeddings` |
| siliconflow | `https://api.siliconflow.com/v1` | rerank | `https://api.siliconflow.com/v1/rerank` |
| zai-custom | `https://api.z.ai/api/paas/v4` | chat | `https://api.z.ai/api/paas/v4/chat/completions` |

---

## 4. Общий загрузчик ролей — новый модуль `llm_providers.py`

### 4.1. Обоснование выбора (новый модуль vs функции в существующих)

Требование: `qa_graph.py`, `search.py`, `create_index.py` должны использовать
ОДИН механизм резолва embedding/rerank/chat без дублирования.

- **В `qa_graph.py` нельзя**: `search.py` и `create_index.py` не должны
  импортировать `qa_graph.py` (это тяжёлый модуль с LangGraph, и он сам
  импортирует `search.py` — получится цикл `qa_graph → search → qa_graph`).
- **В `search.py` нельзя**: `qa_graph.py` импортирует `search.py`; если туда же
  положить загрузчик, `create_index.py` станет зависим от модуля поиска (лишняя
  связность), а `search.py` уже отвечает за логику поиска, не за конфигурацию.
- **Вывод**: отдельный leaf-модуль `firmware/src/llm_providers.py`, зависящий
  только от stdlib (`os`, `pathlib`), `yaml` и `python-dotenv`. Его безопасно
  импортируют все три модуля; никаких циклов.

### 4.2. Публичный API модуля

```python
# firmware/src/llm_providers.py
DEFAULT_PROVIDERS_PATH = str(Path(__file__).resolve().parent / "providers.yaml")

CAPABILITY_ENDPOINT = {
    "chat":      "/chat/completions",
    "embedding": "/embeddings",
    "rerank":    "/rerank",
}

class ProviderConfigError(ValueError):
    """Ошибка структуры providers.yaml / неизвестный provider/model/role."""

class ProviderUnavailableError(RuntimeError):
    """Модель не ответила (сеть/HTTP/малформированный ответ) после всех попыток."""


def load_providers(path: str | None = None) -> dict:
    """Загрузка + валидация реестра providers.yaml.

    Валидирует ТОЛЬКО секцию providers: непустой словарь; у каждого провайдера
    строковые base_url (непустой) и api_key_env, models — непустой dict
    {имя: capability}. Секцию roles НЕ валидирует глубоко (роли проверяются
    лениво в resolve_subrole). Пустой/отсутствующий fallback — НЕ ошибка.
    При отсутствии файла/ошибке YAML/структуры — ProviderConfigError.
    """

def _get_providers(path: str | None = None) -> dict:
    """load_providers с кэшем по пути (как текущий _llm_config_cache)."""

def resolve_subrole(cfg: dict, pipeline: str, subrole: str) -> dict:
    """Резолв подроли пайплайна.

    Возвращает:
      {"provider": str, "model": str,
       "fallback": {"provider": str, "model": str} | None}

    Валидация: pipeline существует в cfg["roles"]; subrole существует; у подроли
    есть provider+model; provider существует в cfg["providers"]; model есть в
    providers[provider]["models"]. fallback: отсутствует/None/{} -> None; иначе
    проверяется, что provider+model fallback существуют.
    Любое нарушение — ProviderConfigError.
    """

def get_endpoint(cfg: dict, provider: str, capability: str) -> str:
    """base_url провайдера (без суффикса) + суффикс capability (раздел 3)."""

def get_api_key(cfg: dict, provider: str, override: str | None = None) -> str:
    """API-ключ: override (--api-key) -> env(api_key_env) -> .env.
    Нет ключа — ProviderConfigError с именем переменной."""

def run_with_fallback(spec: dict, attempt_fn, capability: str):
    """Универсальный цикл «основная -> fallback» (раздел 5)."""
```

> Импорты: `qa_graph.py` использует `resolve_subrole`/`get_endpoint`/`get_api_key`/
> `run_with_fallback` для роли `build_search_index.query_processing` (chat);
> `search.py` — для `embedding` и `rerank`; `create_index.py` — для `embedding`.

### 4.3. Кто что импортирует

| Модуль | Импорт из `llm_providers` | Используемая подроль |
|---|---|---|
| `qa_graph.py` | `load_providers`/`_get_providers`, `resolve_subrole`, `get_endpoint`, `get_api_key`, `run_with_fallback`, `ProviderUnavailableError` | `build_search_index.query_processing` |
| `search.py` | те же + `resolve_subrole` | `build_search_index.embedding`, `build_search_index.rerank` |
| `create_index.py` | те же | `build_search_index.embedding` |

Кэширование: `qa_graph.py`, `search.py`, `create_index.py` используют общий
`_get_providers()` (кэш по пути в `llm_providers`), поэтому файл парсится один
раз на процесс независимо от числа импортёров.

---

## 5. Алгоритм fallback (требование №3)

### 5.1. Семантика «модель не ответила»

`attempt_fn` (одна попытка вызова одной модели, с внутренним retry) бросает
`ProviderUnavailableError`, если модель «не ответила». К «не ответила» относятся:

- `requests.RequestException` (таймаут, разрыв соединения, DNS);
- HTTP 4xx/5xx (`resp.raise_for_status()` → `requests.HTTPError`);
- малформированный ответ: `KeyError`/`IndexError`/`ValueError` при разборе
  `choices[0].message.content` / `data[...].embedding` / `results[...]`;
- отсутствующий API-ключ провайдера (`get_api_key` → нет ключа) — трактуется
  как «эту модель вызвать нельзя», т.е. тоже «не ответила» (это позволяет
  переключиться на fallback, у которого ключ есть).

НЕ относятся к «не ответила» (не запускают fallback, пробрасываются сразу):

- структурные ошибки конфигурации `ProviderConfigError` (неизвестный
  provider/model/role) — они выявляются `resolve_subrole`/`get_endpoint` до
  сетевого вызова и означают ошибку конфига, а не сбой модели.

### 5.2. Порядок «основная → fallback»

```
run_with_fallback(spec, attempt_fn, capability):
    targets = [spec]
    if spec["fallback"]:            # fallback задан (не None/{} )
        targets.append(spec["fallback"])
    errors = []
    for target in targets:
        try:
            return attempt_fn(target)          # собственный retry внутри
        except ProviderUnavailableError as exc:
            errors.append((target, exc))
            log.warning(f"{capability}: {target} не ответил: {exc}")
            continue
    # сюда не дошли только если все targets упали
    if len(targets) == 1:
        raise ProviderUnavailableError(
            f"{capability}: модель {spec['provider']}/{spec['model']} "
            f"не ответила и fallback не задан — невозможно продолжить работу: "
            f"{errors[0]}")
    raise ProviderUnavailableError(
        f"{capability}: ни основная модель {spec['provider']}/{spec['model']}, "
        f"ни fallback не ответили: {errors}")
```

Правила (зафиксированы в требованиях):

1. fallback пустой (`{}`), `None` или отсутствует → при загрузке/резолве НЕ
   ошибка; `resolve_subrole` возвращает `fallback=None`.
2. Основная модель не ответила:
   - fallback задан → пробуем fallback (со СВОИМ `base_url`/`api_key_env`
     провайдера fallback);
   - fallback НЕ задан → `ProviderUnavailableError` с текстом «невозможно
     продолжить работу».
3. fallback задан, но тоже не ответил → `ProviderUnavailableError` (обе модели
   исчерпаны).

### 5.3. Взаимодействие retry и fallback

Retry и fallback — разные уровни:

- **retry** — внутри `attempt_fn`: одна модель, `API_RETRIES=3` попыток,
  экспоненциальный backoff `API_RETRY_BACKOFF=2.0` (как сейчас). Исчерпаны →
  `ProviderUnavailableError` → управление возвращается в `run_with_fallback`.
- **fallback** — снаружи: переход на другую модель/провайдера после того, как
  основная исчерпала все retry.

Итого максимум `(1 основной + 1 fallback) × 3 retry` HTTP-вызовов на подроль.

### 5.4. Реализация `attempt_fn` по capabilities

`attempt_fn(target)` — замыкание, которое потребитель передаёт в
`run_with_fallback`. Оно закрывает над `cfg` (провайдеры), `override` (CLI-ключ)
и полезной нагрузкой вызова.

- **chat** (`qa_graph.py`): `_chat_once(target, messages, node_params)`
  - url = `get_endpoint(cfg, target["provider"], "chat")`
  - key = `get_api_key(cfg, target["provider"], override=llm_api_key)`
  - payload `{model: target["model"], messages, temperature, max_tokens}`
  - возвращает `data["choices"][0]["message"]["content"]`.
- **embedding query** (`search.py`): `_embed_query_once(target, text)`
  - url = `get_endpoint(cfg, provider, "embedding")`; payload
    `{model, input:[text], encoding_format:"float"}`; возвращает
    `data["data"][0]["embedding"]`.
- **embedding batch** (`create_index.py`): `_embed_batch_once(target, texts)`
  - та же схема, но `input: texts` и сортировка по `index`.
- **rerank** (`search.py`): `_rerank_once(target, query, documents, top_n)`
  - url = `get_endpoint(cfg, provider, "rerank")`; payload
    `{model, query, documents, top_n}`; возвращает `relevance_score` по индексам.

---

## 6. Структура кода после рефакторинга (по файлам)

### 6.1. `firmware/src/qa_graph.py`

- `DEFAULT_LLM_CONFIG_PATH` → `DEFAULT_SEARCH_CONFIG_PATH` = путь к
  `search_config.yaml`; добавить `DEFAULT_PROVIDERS_PATH` = путь к
  `providers.yaml` (рядом с модулем).
- `load_llm_config()` → `load_search_config()`: валидирует ТОЛЬКО секцию
  `nodes` (как сейчас, но без providers/default_*).
- Удалить `get_chat_url()` / `get_api_key()` / `_llm_config_cache` /
  `_get_llm_config()` — их роль переходит в `llm_providers`.
- `llm_chat(messages, config, node_name, provider_override, model_override, timeout)`:
  - `search_cfg = load_search_config(config.search_config_path)` → `nodes[node_name]`
    (temperature/max_tokens);
  - `providers_cfg = _get_providers(config.providers_path)`;
  - `spec = resolve_subrole(providers_cfg, "build_search_index", "query_processing")`;
  - применить CLI-переопределения поверх `spec` (раздел 7);
  - `return run_with_fallback(spec, attempt_fn=_chat_once(...), capability="chat")`.
- `QAGraphConfig`:
  - `llm_config_path` → `search_config_path` (default `DEFAULT_SEARCH_CONFIG_PATH`);
  - добавить `providers_path: str = DEFAULT_PROVIDERS_PATH`;
  - `llm_provider` / `llm_model` / `llm_api_key` — оставить (переопределения
    query_processing);
  - УДАЛИТЬ `siliconflow_base_url` и `siliconflow_api_key` (base_url/ключ
    embedding/rerank теперь из `providers.yaml`).
- `search_node`:
  - больше НЕ вызывает `resolve_api_key()` (SILICONFLOW_API_KEY); ключ и модель
    embedding/rerank резолвятся внутри `search.py` из `providers.yaml`.
  - Вызовы `hybrid_search(...)` / `rerank_siliconflow(...)` передают ключ как
    override (`cfg.llm_api_key`, он же CLI `--api-key`) — подробнее раздел 7.3.
  - Логика поиска/реранка, `_get_qdrant_client`/`_close_qdrant_client`/
    `_get_sparse_model` — БЕЗ изменений.
- `main()`: предпроверка `load_search_config(args.llm_config)` +
  `get_api_key(...)` меняется на `load_search_config(...)` +
  `resolve_subrole(load_providers(...), "build_search_index", "query_processing")`
  + `get_api_key(...)` для query_processing (код возврата 2 сохраняется).

### 6.2. `firmware/src/search.py`

- Удалить хардкод-константы `EMBED_MODEL`, `RERANK_MODEL`,
  `SILICONFLOW_BASE_URL`, `EMBED_API_URL`, `RERANK_API_URL` (как единственный
  источник модели/URL). Оставить `SPARSE_MODEL` (локальный fastembed — не LLM).
- Добавить ленивые кэшируемые резолверы:
  `_embedding_spec()` → `resolve_subrole(_get_providers(), "build_search_index", "embedding")`,
  `_rerank_spec()` → `resolve_subrole(..., "rerank")`.
- `embed_query_siliconflow(query, api_key=None, model=None)`:
  - model/endpoint берутся из `_embedding_spec()` (если `model` не передан);
  - ключ: `api_key` (override) или `get_api_key(cfg, provider)` из роли;
  - вызов через `run_with_fallback(embedding_spec, attempt_fn=_embed_query_once, "embedding")`.
- `rerank_siliconflow(query, documents, api_key=None, top_n, model=None)` —
  аналогично через `_rerank_spec()` + `run_with_fallback`.
- `hybrid_search(...)` — сигнатура и логика НЕ меняются; `api_key` пробрасывается
  в `embed_query_siliconflow` как override.
- `resolve_api_key(cli_key)` → переиспользует `get_api_key(_get_providers(),
  "siliconflow", override=cli_key)` (тот же порядок: `--api-key` → env → `.env`).
- CLI `--api-key` продолжает переопределять ключ embedding/rerank (siliconflow).

### 6.3. `firmware/src/create_index.py`

- Удалить хардкод `EMBED_MODEL`, `SILICONFLOW_BASE_URL`, `EMBED_API_URL`.
- `embed_texts_siliconflow(texts, api_key=None, model=None)`:
  - model/endpoint из `resolve_subrole(..., "embedding")`;
  - вызов через `run_with_fallback(embedding_spec, attempt_fn=_embed_batch_once, "embedding")`.
- `resolve_api_key(cli_key)` → `get_api_key(_get_providers(), "siliconflow",
  override=cli_key)`.
- Остальное (validation, payload, выбор коллекции) — без изменений.

### 6.4. `firmware/src/telegram_bot/bot.py`

- `LLM_CONFIG = .../llm_config.yaml` → путь к `search_config.yaml`
  (переименовать переменную в `SEARCH_CONFIG`, при желании добавить
  `PROVIDERS_CONFIG = .../providers.yaml`).
- `QAGraphConfig(... llm_config_path=LLM_CONFIG ...)` →
  `QAGraphConfig(search_config_path=SEARCH_CONFIG, providers_path=PROVIDERS_CONFIG)`.

### 6.5. Документация

- `README.md`: строки про `llm_config.yaml` (пример конфига, таблица CLI,
  таблица `QAGraphConfig`, раздел «Конфигурация LLM») → `search_config.yaml` +
  описание `roles.build_search_index` и fallback.
- `docs/software/langgraph-rag-architecture.md`, раздел 6 (и упоминания
  `llm_config.yaml`/`DEFAULT_LLM_CONFIG_PATH` в разделах 6.2/6.5/8): обновить
  под новую схему (можно отдельным коммитом после реализации — по договорённости
  с пользователем).

---

## 7. CLI-переопределения и API-ключи (требования №5, №6)

### 7.1. `--llm-provider` / `--llm-model` → переопределяют `query_processing`

- `--llm-provider X` → `spec["provider"] = X`; `--llm-model Y` →
  `spec["model"] = Y`.
- Переопределённый провайдер должен существовать в `providers.yaml` (иначе
  `get_endpoint`/`get_api_key` бросят `ProviderConfigError`) — то же поведение,
  что сейчас.
- `fallback` роли при переопределении ОСТАЁТСЯ как в конфиге (переопределение
  меняет только основную модель). Если пользователь хочет отключить fallback —
  правит `providers.yaml`.

### 7.2. `--api-key` → переопределяет ключ query_processing

- Попадает в `QAGraphConfig.llm_api_key`; в `_chat_once` передаётся как
  `override` в `get_api_key(cfg, provider, override=...)`.

### 7.3. Ключ embedding/rerank и обратная совместимость `--api-key`

Сейчас `qa_graph.main()` передаёт `args.api_key` и в `siliconflow_api_key`
(embedding/rerank), и в `llm_api_key` (chat). Сохраняем этот UX:

- embedding/rerank ключ читается из `providers.yaml` (`api_key_env` =
  `SILICONFLOW_API_KEY`) через `.env`;
- `search_node` передаёт `cfg.llm_api_key` (CLI `--api-key`) как override в
  `hybrid_search`/`rerank_siliconflow` (которые пробрасывают его в
  `embed_query_siliconflow`/`rerank_siliconflow`). Так `--api-key` по-прежнему
  переопределяет и chat, и embedding/rerank.

---

## 8. Карта изменений по файлам (для coder)

| Файл | Действие | Ключевые правки |
|---|---|---|
| `firmware/src/llm_config.yaml` | `git mv` → `search_config.yaml`, урезать | оставить только `nodes` |
| `firmware/src/providers.yaml` | правка в месте (untracked!) | добавить `roles.build_search_index` (3 подроли), НЕ трогать `create_markdown` |
| `firmware/src/llm_providers.py` | **создать** | общий загрузчик (раздел 4) |
| `firmware/src/qa_graph.py` | правка | `search_config_path`/`providers_path`; `load_search_config`; `llm_chat` через `resolve_subrole`+`run_with_fallback`; убрать `get_chat_url`/`get_api_key`; `search_node` без `resolve_api_key`; `main()` |
| `firmware/src/search.py` | правка | убрать хардкод EMBED/RERANK/SILICONFLOW; резолв через роли + fallback; `resolve_api_key` через `llm_providers.get_api_key` |
| `firmware/src/create_index.py` | правка | убрать хардкод EMBED; резолв через роль embedding + fallback; `resolve_api_key` через `llm_providers` |
| `firmware/src/telegram_bot/bot.py` | правка | путь `search_config.yaml` (+ `providers.yaml`) |
| `firmware/tests/test_llm_providers.py` | **создать** | тесты загрузчика (раздел 9) |
| `firmware/tests/test_qa_graph.py` | правка | фикстуры под новую схему, перенос тестов get_chat_url/get_api_key/load, fallback-тесты `llm_chat`, обновить `search_node`/`QAGraphConfig` |
| `firmware/tests/test_search_index.py` | правка | тесты embed/rerank под резолв из providers.yaml |
| `firmware/tests/test_create_index_cli.py` | правка (миним.) | env-пин можно оставить; проверить, что embed-путь не сломан |
| `README.md` | правка | раздел конфигурации |

---

## 9. План тестов

### 9.1. Новый модуль `firmware/tests/test_llm_providers.py`

Загрузка/валидация:
- `load_providers` валидный файл (обе роли) → dict, `fallback` пустой остаётся `{}`/нормализуется в `None` в `resolve_subrole`;
- отсутствующий файл → `ProviderConfigError`;
- битый YAML → `ProviderConfigError`;
- `providers` отсутствует/пуст → `ProviderConfigError`;
- провайдер без `base_url` / без `api_key_env` / `models` не dict → `ProviderConfigError`;
- **fallback пустой (`{}`) и fallback отсутствующий — загрузка НЕ падает** (обе формы).

`resolve_subrole`:
- валидная подроль с fallback → `fallback` dict;
- fallback `{}` / `null` / отсутствует → `fallback is None`;
- неизвестный pipeline / подроль / provider / model → `ProviderConfigError`;
- малформированный fallback (нет `model`) → `ProviderConfigError`.

`get_endpoint`:
- chat/embedding/rerank дают `/chat/completions`, `/embeddings`, `/rerank`;
- `base_url` с хвостовым `/` нормализуется (`rstrip("/")`).

`get_api_key`:
- override побеждает; env побеждает; `.env`-fallback; нет ключа → `ProviderConfigError`.

`run_with_fallback`:
- основная отвечает → fallback НЕ вызывается (проверка через `attempt_fn`-счётчик);
- основная падает (`ProviderUnavailableError`) → fallback вызывается и его ответ возвращается;
- основная падает, fallback `None` → `ProviderUnavailableError` c «невозможно продолжить работу»;
- обе падают → `ProviderUnavailableError`;
- `ProviderConfigError` из `attempt_fn` НЕ перехватывается (пробрасывается).

### 9.2. `test_qa_graph.py` — правки

- Заменить `LLM_CONFIG_TMPL` на два шаблона: `SEARCH_CONFIG_TMPL` (только
  `nodes`) и `PROVIDERS_TMPL` (провайдеры + `roles.build_search_index`).
- `_write_llm_config` → `_write_search_config` + добавить `_write_providers`;
  `_cfg_llm` → передаёт `search_config_path` + `providers_path`.
- Тесты `load_llm_config_*` → переписать под `load_search_config` (валидация
  только `nodes`); тесты `get_chat_url_*`/`get_api_key_*` перенести в
  `test_llm_providers.py` (как `get_endpoint`/`get_api_key`).
- Тесты `llm_chat_*`: URL по-прежнему `https://api.deepseek.com/v1/chat/completions`,
  модель `deepseek-v4-flash`, ключ `DEEPSEEK_API_KEY` (поведение не изменилось).
  Добавить: `llm_chat` при падении основной модели переключается на fallback
  (`provod`/`deepseek-v4-pro`, ключ `PROVOD_API_KEY`); при пустом fallback —
  `ProviderUnavailableError`.
- `test_qa_graph_config_defaults` / `test_qa_graph_config_override`: поля
  `search_config_path`/`providers_path` вместо `llm_config_path`; убрать
  утверждения про `siliconflow_base_url`/`siliconflow_api_key`.
- `search_node`-тесты: `search_node` больше не резолвит SiliconFlow-ключ —
  обновить `test_search_node_uses_config_params` (ключ теперь передаётся как
  override/None, а не `"k-sf"`); моки `hybrid_search`/`rerank_siliconflow`
  остаются.

### 9.3. `test_search_index.py` — правки

- Тесты `test_embed_texts_siliconflow_request_and_parse`,
  `test_rerank_siliconflow_request_and_parse`, `test_embed_query_uses_instruction`
  сейчас ссылаются на `create_index.EMBED_API_URL` / `search.RERANK_API_URL` /
  хардкод-модели. После рефакторинга констант нет — тесты должны:
  (а) подсунуть временный `providers.yaml` (через `monkeypatch` пути
  `DEFAULT_PROVIDERS_PATH`) и проверять URL/модель из роли, либо
  (б) проверять, что запрос ушёл на `{base_url}/embeddings` / `{base_url}/rerank`
  с моделью из роли.

### 9.4. `test_create_index_cli.py`

- Функционально не затрагивается (тесты про discovery/коллекции). Проверить,
  что `os.environ.setdefault("SILICONFLOW_BASE_URL", ...)` не мешает (можно
  оставить или удалить — константы больше не используются).

---

## 10. Критерии приёмки (что должен продемонстрировать coder)

1. `pytest firmware/tests -q` — все зелёные (включая новые тесты fallback и
   `test_llm_providers.py`).
2. Дымовая проверка: `search_config.yaml` (только `nodes`) и `providers.yaml`
   (`roles.build_search_index` с пустыми `fallback` у embedding/rerank) парсятся
   без ошибок.
3. Живой smoke (если есть сеть и ключи в `.env`): `python search.py --query "…"`
   и/или `python qa_graph.py --query "…"` — реальный ответ или честная ошибка.
4. `roles.create_markdown` в `providers.yaml` не изменена; `providers.yaml` не
   потерял незакоммиченную работу пользователя.

---

## 11. Открытые вопросы / риски

1. **fallback при CLI-переопределении**: решено оставлять fallback из конфига
   при `--llm-provider/--llm-model` (меняется только основная модель). Если
   нужна другая семантика — уточнить у пользователя.
2. **`--api-key` и embedding/rerank**: решено сохранить текущее поведение
   (один ключ переопределяет и chat, и siliconflow). При желании можно сузить
   `--api-key` только до query_processing и завести отдельный флаг — требует
   решения пользователя.
3. **Валидация capability** (`models: {name: capability}`): решено НЕ
   enforce'ить (capability в providers.yaml — декларативная справка). При
   желании можно добавить warning в `resolve_subrole`.
