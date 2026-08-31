# Architecture-report — QDRANT_PATH: load_dotenv пайплайна засоряет process-env (t_12a1b3fd)

> Архитектура-фикс фазы 2 второй итерации (решение №29). Дефект дизайна фазы 1,
> найден кодером фазы 2 (t_842616ab §3) и подтверждён оркестратором.
>
> Границы: пайплайны (`Build_Search_index`, `Create_Markdown_YA`) НЕ меняются;
> `load_dotenv()` в `get_api_key` остаётся; правится только РЕЗОЛВИНГ `cfg.qdrant_path`
> в `firmware/src/deploy_config.py`. Код здесь не пишется — только схема + список правок
> для кодер-фикса.

---

## 1. Корневая причина (подтверждено по коду)

Единая точка резолва — `firmware/src/deploy_config.py:34-49`:

```python
@property
def qdrant_path_override(self) -> str | None:
    override = os.environ.get("QDRANT_PATH")          # (A) process-env — ПРИОРИТЕТ
    if not override:
        override = read_env_raw(self.env_file).get("QDRANT_PATH")   # (B) .env интерфейса
    return override or None
```

Цепочка заражения (dev, воспроизводимо):

1. Чистый старт dev-сервера: `os.environ` без `QDRANT_PATH`; `.env` интерфейса
   (`config/.env`) сейчас БЕЗ `QDRANT_PATH` (ключ вычищен при проверках №29; есть только в
   `.env.bak`). → `GET /api/settings/qdrant` = `overridden:false`, путь = дефолт.
2. Первый чат-запрос → `ChatSession.__post_init__` (`chat_api.py:29-40`) импортирует
   `qa_graph` → `llm_providers`. При реальном прогоне графа `llm_chat()` → `get_api_key()`
   (`llm_providers.py:86-97`) вызывает голый `load_dotenv()` (строка 93).
3. `load_dotenv()` → `find_dotenv()` поднимается от каталога вызывающего файла
   (`Build_Search_index/firmware/src/llm_providers.py`) вверх и находит
   `Build_Search_index/.env`, где лежит `QDRANT_PATH=/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data`.
   `load_dotenv` кладёт ключ в `os.environ` **процесса интерфейса** (setdefault — только если
   его ещё нет; на чистом старте — нет).
4. Теперь ветка (A) возвращает прод-путь пайплайна. Ленивое свойство перечитывает
   `os.environ` на каждом обращении → после первого чат-сообщения эффективный путь Qdrant
   молча становится прод-путём: `documents_in_base` / `settings_collections` / `chat` читают
   прод-базу, настройка пользователя и собственный `.env` интерфейса игнорируются.

Следствие бага: `overridden` перескакивает `false → true` БЕЗ записи в `.env` интерфейса.

---

## 2. Корректный порядок разрешения QDRANT_PATH (решение)

### 2.1 Рекомендация — читать ТОЛЬКО из `.env` интерфейса, process-env игнорировать

```python
@property
def qdrant_path_override(self) -> str | None:
    """QDRANT_PATH: только из .env интерфейса (<config_dir>/.env, пишет UI через
    config_ui.write_env). process-env НЕ читается: пайплайн голым load_dotenv()
    засоряет os.environ значением QDRANT_PATH из СВОЕГО .env — это не источник
    истины для интерфейса."""
    return read_env_raw(self.env_file).get("QDRANT_PATH") or None
```

`qdrant_path` (строка 46-49) остаётся без изменений: `override` или `qdrant_path_default`.

Порядок после фикса:

```
QDRANT_PATH_eff = .env интерфейса (config_dir/.env, UI через write_env)
                  → иначе qdrant_path_default (config.yaml qdrant.path)
```

### 2.2 Обоснование (почему не «снапшот в момент load()» и не «иное»)

Рассмотрены три варианта:

| Вариант | Суть | Вердикт |
|---|---|---|
| **A. Только `.env` интерфейса** | убрать ветку `os.environ.get("QDRANT_PATH")` | ✅ **рекомендуется** |
| B. Снапшот process-env в момент `load()` | заморозить `os.environ["QDRANT_PATH"]` в frozen-датаклассе до импорта пайплайна | рабочий, но сложнее и хранит env-переменную, которую никто легитимно не выставляет |
| C. Вычищать ключ после ChatSession (`os.environ.pop`) | хак на следствии, не на причине | ❌ отвергнут (t_842616ab §3 предлагал; лечит симптом, гонка остаётся) |

Почему A:

1. **Единственный легитимный писатель `QDRANT_PATH` — сам интерфейс**, через
   `PUT /api/settings/qdrant` → `config_ui.write_env(cfg.env_file, {"QDRANT_PATH": path})`
   (`app.py:325-336`). Писатель именно в `.env`, а не в process-env.
2. **Единственный писатель `QDRANT_PATH` в process-env — пайплайн** (`load_dotenv()`),
   т.е. ровно тот вектор заражения, который мы устраняем. Легитимного
   process-env-сеттера нет ни в `app.py`, ни в `deploy.sh`, ни в systemd-юните
   (там только `WorkingDirectory` + `INTERFACE_RAG_ENV=prod`, см. §4).
3. Вариант B сохраняет доверие к env-переменной, которую никто не должен выставлять,
   и привносит зависимость от порядка инициализации («снапшот до импорта пайплайна») —
   лишняя хрупкость без выигрыша.
4. Ленивость свойства перестаёт иметь значение: раз чтение `os.environ` убрано, момент
   обращения к `qdrant_path_override` больше не влияет на результат.

Чтение `INTERFACE_RAG_ENV` из process-env в `load()` (выбор секции dev/prod) **не трогаем** —
это легитимное разовое чтение на старте, до импорта пайплайна, к багу не относится.

### 2.3 Нужен ли process-env override вообще? — Нет (убрать)

- Текущий деплой не использует `Environment=QDRANT_PATH=` нигде (systemd-юнит задаёт только
  `WorkingDirectory` и `INTERFACE_RAG_ENV`; `deploy.sh` не экспортирует `QDRANT_PATH`).
- Операторский override уже покрыт: UI пишет путь в `.env` (`0600`, owner-only), а файл можно
  править и вручную. Авторитетный источник — один и тот же файл.

**Расширение на будущее (НЕ реализовывать сейчас, зафиксировать контракт):** если появится
потребность в env-override уровня systemd — вводить ОТДЕЛЬНОЕ имя под собственным неймспейсом
интерфейса, которое пайплайн физически не может выставить: `INTERFACE_RAG_QDRANT_PATH`
(прецедент уже есть — `INTERFACE_RAG_ENV`). Оно читается в `load()` один раз (как `INTERFACE_RAG_ENV`)
и кладётся в `DeployConfig` как явный `qdrant_path_override` с приоритетом НАД `.env`. Голый
`QDRANT_PATH` в process-env НЕ читать никогда — пайплайн пишет именно его.

---

## 3. Побочный эффект load_dotenv: API-ключи + какой .env чат использует на prod

### 3.1 Что кладёт load_dotenv в process-env (кроме QDRANT_PATH)

`Build_Search_index/.env` содержит также `SILICONFLOW_API_KEY`, `DEEPSEEK_API_KEY`,
`PROVOD_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`. После `load_dotenv()` всё это
(setdefault) попадает в `os.environ` интерфейса. Резолв API-ключей чата идёт in-process через
`get_api_key` → `load_dotenv()` → `os.environ.get(env_name)`.

**Фикс QDRANT_PATH не затрагивает `get_api_key` и не конфликтует с `.env` интерфейса:**
правка — только в `deploy_config.qdrant_path_override`; цепочка API-ключей остаётся ровно той же.
`get_api_key` читает конкретные имена `api_key_env` (из `providers.yaml`), а не `QDRANT_PATH`,
поэтому удаление ветки `os.environ["QDRANT_PATH"]` на резолв ключей не влияет.

### 3.2 Какой `.env` чат реально использует (важно для prod)

`find_dotenv()` (python-dotenv) стартует от файла `llm_providers.py`, а НЕ от CWD
(`usecwd=False`, не интерактив, не отладчик). Значит:

- **dev**: `load_dotenv()` находит `/root/projects/Build_Search_index/.env` →
  чат резолвит ключи из `.env` **пайплайна**, а не из `config/.env` интерфейса.
- **prod** (`/root/RAG/`, «чистый прод»): `deploy.sh` УДАЛЯЕТ все `.env` в дереве пайплайна
  (`rm -f …/Build_Search_index/firmware/src/.env`, `…/Create_Markdown_YA/firmware/src/.env`,
  `…/interface_RAG/.env`); остаётся только `/root/RAG/config/.env`. Подъём от
  `/root/RAG/Build_Search_index/firmware/src/` вверх `.env` НЕ находит
  (`firmware/src/.env` удалён, `firmware/.env`/`Build_Search_index/.env`/`RAG/.env` отсутствуют).
  → **на prod чат получает API-ключи НЕ из `.env`-файла, а из `os.environ`**, т.е. их обязан
  экспортировать systemd-юнит (`Environment=`/`EnvironmentFile=`). `config/.env` интерфейса в
  путь подъёма `find_dotenv` НЕ входит.

### 3.3 Следствие — латентный риск (вне объёма этого фикса, флаг оркестратору)

Резолв API-ключей асимметричен:

- **subprocess (convert/index/refresh)** — ключи из `config/.env` интерфейса, инъекция
  `jobs.build_env` приоритетна → правки ключей в UI применяются сразу. ✅
- **чат in-process** — ключи из `.env` пайплайна (dev) / из `os.environ` systemd (prod),
  `config/.env` интерфейса игнорируется. → добавление/смена ключа провайдера через UI
  (`POST /api/settings/providers/add`, `PUT /api/settings/env`) **не дойдёт до чата** без
  рестарта и, на prod, без ручной синхронизации systemd-окружения.

Это отдельная архитектурная тема (единый источник ключей для in-process чата) и в объём
данного фикса **не входит** — пайплайн менять нельзя, `load_dotenv()` в `get_api_key` остаётся.
Рекомендация на отдельную задачу: в `ChatSession` перед построением `QAGraph` инжектировать
ключи `config/.env` в `os.environ` (setdefault) тем же механизмом, что `build_env` для
subprocess — тогда чат и пайплайны будут резолвить ключи из одного источника. **Не делать в
этой задаче.**

---

## 4. Точный список правок (для кодер-фикса)

### 4.1 Единственный изменяемый файл — `firmware/src/deploy_config.py`

| Место | Правка |
|---|---|
| `qdrant_path_override` (строки 34-44) | Убрать ветку `os.environ.get("QDRANT_PATH")`; оставить только `read_env_raw(self.env_file).get("QDRANT_PATH")`. Обновить docstring (приоритет: `.env` интерфейса → `None`; process-env не читается, причина — заражение `load_dotenv` пайплайна). |
| Импорт `os` (строка 3) | Оставить — `os.environ.get("INTERFACE_RAG_ENV")` в `load()` использует `os`. Ничего не удалять. |

`qdrant_path` (46-49), `load()` (70-98) — БЕЗ изменений.

### 4.2 `firmware/src/config_ui.py` — изменений НЕ требуется

`write_env` (52-73) и `read_env_raw` (41-50) — родовые key-value, `QDRANT_PATH` уже
обрабатывается корректно (запись через UI, атомарность, `0600`, пустое значение = удаление).
Приоритет был только в `deploy_config`, не в `config_ui`.

### 4.3 Не менять (жёсткие границы)

- `Build_Search_index/**`, `Create_Markdown_YA/**` — `load_dotenv()` в `get_api_key` остаётся.
- `app.py` (`settings_qdrant`/`update_qdrant`), `chat_api.py`, `qdrant_api.py` — читают только
  `cfg.qdrant_path`/`cfg.qdrant_path_override`, менять не нужно.
- `.hermes/STATE.md` — не трогать.

---

## 5. Тесты

### 5.1 `firmware/tests/test_deploy_config.py` — переписать 2 теста приоритета

| Существующий тест | Действие |
|---|---|
| `test_qdrant_path_override_from_process_env` (93-99) | **Переписать**: `monkeypatch.setenv("QDRANT_PATH", …)` при отсутствии записи в `.env` → `qdrant_path_override is None`, `qdrant_path == qdrant_path_default`. (process-env больше НЕ создаёт override.) |
| `test_qdrant_path_process_env_beats_env_file` (112-119) | **Инвертировать**: и `.env` с `QDRANT_PATH`, и `QDRANT_PATH` в process-env → побеждает `.env` интерфейса (сценарий заражения). |

Оставить без изменений: `test_qdrant_path_default_when_no_override` (84-90),
`test_qdrant_path_override_from_env_file` (102-109), `test_qdrant_path_empty_env_file_value_falls_back`
(122-129).

### 5.2 Новый регрессионный тест (детерминированный, hermetic)

`test_qdrant_path_ignores_process_env_pollution` — ровно сценарий бага:

```python
def test_qdrant_path_ignores_process_env_pollution(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    env_file = tmp_path / ".env"
    env_file.write_text("QDRANT_PATH=/interface/qdrant\n", encoding="utf-8")
    cfg.write_text(_config(dev_env_file=str(env_file)), encoding="utf-8")
    loaded = load(cfg)
    # «Пайплайн загрязнил» process-env: load_dotenv положил QDRANT_PATH из СВОЕГО .env
    monkeypatch.setenv("QDRANT_PATH", "/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data")
    assert loaded.qdrant_path_override == "/interface/qdrant"   # .env интерфейса победил
    assert loaded.qdrant_path == Path("/interface/qdrant")      # НЕ прод-путь
```

Плюс негативный кейс (process-env сам по себе, без `.env` интерфейса → дефолт, `overridden is None`).

### 5.3 Интеграционный тест (по требованию задачи: «после импорта qa_graph/llm_providers cfg.qdrant_path не меняется»)

Два уровня; основной — детерминированный, дополнительный — настоящий end-to-end.

**(а) Основной, без тяжёлых зависимостей** — в `test_deploy_config.py`: вызвать НАСТОЯЩИЙ
`load_dotenv` (то, что реально мутирует `os.environ`) против фикстуры `.env` пайплайна с
`QDRANT_PATH=/mnt/sdb/…`, затем проверить, что `cfg.qdrant_path` не сменился:

```python
def test_qdrant_path_survives_load_dotenv(tmp_path, monkeypatch):
    from dotenv import load_dotenv
    env_file = tmp_path / ".env"; env_file.write_text("QDRANT_PATH=/interface/qdrant\n", encoding="utf-8")
    cfg = tmp_path / "config.yaml"; cfg.write_text(_config(dev_env_file=str(env_file)), encoding="utf-8")
    loaded = load(cfg)
    pipeline_env = tmp_path / "pipeline" / ".env"; pipeline_env.parent.mkdir(parents=True)
    pipeline_env.write_text("QDRANT_PATH=/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data\nDEEPSEEK_API_KEY=sk-x\n", encoding="utf-8")
    load_dotenv(dotenv_path=pipeline_env)          # эмулирует get_api_key() пайплайна
    assert loaded.qdrant_path_override == "/interface/qdrant"
    assert loaded.qdrant_path == Path("/interface/qdrant")
```

**Эксплицитный `dotenv_path` обязателен** — голый `load_dotenv()` в pytest поднимется от файла
теста (не от `llm_providers.py`) и найдёт не тот `.env`; фикстура воспроизводит семантику
заражения без привязки к расположению реального репо пайплайна.

**(б) Настоящий end-to-end (опционально, gated)** — отдельный файл
`firmware/tests/test_qdrant_env_isolation.py`: добавить `cfg.build_search_index_dir` в `sys.path`,
`import llm_providers` (лёгкий модуль: только yaml + dotenv), построить `DeployConfig` с
`env_file` указывающим на `.env` интерфейса, вызвать `llm_providers.get_api_key(providers_cfg, provider)`
(триггерит настоящий `load_dotenv()` из `llm_providers.py`), затем `assert cfg.qdrant_path`
не изменился на прод-путь. Пометить `@pytest.mark.skipif(not <bsi>/llm_providers.py exists)` —
тест зависит от наличия репо пайплайна рядом (на dev есть, на чистом CI — пропускается).

### 5.4 `firmware/tests/test_app.py` — изменений НЕ требуется

`_fake_cfg.qdrant_path_override` (строки 62-70) уже реализует `.env`-only семантику;
тесты №29 (248-282) остаются валидными.

### 5.5 Проверка прогона

`python3 -m pytest firmware/tests -q` — ожидаются зелёные (текущие 62 + новые/переписанные,
без регрессий по остальным модулям).

---

## 6. Итог

| Пункт | Решение |
|---|---|
| Порядок разрешения `QDRANT_PATH` | `.env` интерфейса → `qdrant_path_default`; process-env не читается |
| process-env override | Не нужен — убрать. Будущее расширение: `INTERFACE_RAG_QDRANT_PATH` (неймспейс интерфейса) при явной потребности |
| Резолв API-ключей чата | Не меняется; фикс его не трогает и не конфликтует с `.env` интерфейса |
| `.env` чата на prod | НЕ `config/.env`: dev — `.env` пайплайна, prod — `os.environ` из systemd (латентный риск — флаг, отдельная задача) |
| Правки | Только `deploy_config.py` (`qdrant_path_override`); `config_ui.py` без изменений |
| Тесты | Переписать 2 теста приоритета + регрессионный (pollution) + интеграционный (load_dotenv), опц. настоящий e2e (gated) |
