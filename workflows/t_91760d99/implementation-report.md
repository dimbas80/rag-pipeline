# Implementation-report — QDRANT_PATH: только .env интерфейса + нормализация пути (t_91760d99)

> Coder-фикс по архитектуре `workflows/t_12a1b3fd/architecture-report.md`.
> Устранение заражения process-env пайплайном (голый `load_dotenv()` в
> `Build_Search_index/firmware/src/llm_providers.py:93` клал `QDRANT_PATH` из
> СВОЕГО `.env` в `os.environ` процесса интерфейса).

---

## 1. Что изменено

### 1.1 `firmware/src/deploy_config.py` — `qdrant_path_override` (единственный файл фикса по §4.1 архитектуры)

```python
@property
def qdrant_path_override(self) -> str | None:
    """QDRANT_PATH: только из .env интерфейса (<config_dir>/.env, пишет UI через
    config_ui.write_env). process-env НЕ читается: пайплайн голым load_dotenv()
    засоряет os.environ значением QDRANT_PATH из СВОЕГО .env — это не источник
    истины для интерфейса. None — override отсутствует.

    Чтение .env ленивое, без кэша (файл крошечный, доступ 1–2 раза на запрос).
    """
    return read_env_raw(self.env_file).get("QDRANT_PATH") or None
```

- Убрана ветка `os.environ.get("QDRANT_PATH")` (была строкой 41).
- Docstring обновлён: приоритет `.env` интерфейса → `None`; process-env не читается.
- `qdrant_path` и `load()` — без изменений.
- Импорт `os` оставлен — `load()` использует `os.environ.get("INTERFACE_RAG_ENV")`.
- `config_ui.py`, `chat_api.py`, `qdrant_api.py`, пайплайны — не тронуты.

### 1.2 `firmware/src/app.py` — `update_qdrant` (LOW из ревью, та же область)

Нормализация пути после проверки абсолютности:

```python
path = (payload.path or "").strip()
if path and not Path(path).is_absolute():
    raise HTTPException(400, "Путь должен быть абсолютным")
if path:
    # Нормализация после проверки абсолютности: /tmp/../etc → /etc, чтобы
    # путь с компонентами «..» не сохранялся в .env как есть.
    path = str(Path(path).resolve())
config_ui.write_env(cfg.env_file, {"QDRANT_PATH": path})
return settings_qdrant()
```

Остальная логика endpoint не менялась (пустая строка = сброс, запись через
`config_ui.write_env`, `0600`, ответ `settings_qdrant()`).

### 1.3 `firmware/tests/test_deploy_config.py` — тесты по §5.1–5.3(а) архитектуры

| Тест | Действие |
|---|---|
| `test_qdrant_path_override_from_process_env` | **Переписан**: process-env `QDRANT_PATH` БЕЗ записи в `.env` → `qdrant_path_override is None`, путь = дефолт |
| `test_qdrant_path_process_env_beats_env_file` | **Инвертирован** и переименован → `test_qdrant_path_env_file_beats_process_env`: и `.env` с `QDRANT_PATH`, и process-env → побеждает `.env` интерфейса |
| `test_qdrant_path_ignores_process_env_pollution` | **Новый регрессионный** (сценарий бага, §5.2): `.env` интерфейса = `/interface/qdrant`, process-env «загрязнён» прод-путём пайплайна → `.env` победил, путь НЕ прод |
| `test_qdrant_path_survives_load_dotenv` | **Новый интеграционный** (§5.3(а)): НАСТОЯЩИЙ `load_dotenv(dotenv_path=фикстура-пайплайна)` мутирует `os.environ` → `cfg.qdrant_path` не сменился на прод-путь |

Оставлены без изменений: `test_qdrant_path_default_when_no_override`,
`test_qdrant_path_override_from_env_file`, `test_qdrant_path_empty_env_file_value_falls_back`.

### 1.4 `firmware/tests/test_app.py` — один новый тест

`test_put_qdrant_settings_normalizes_dotdot_path`: `PUT /api/settings/qdrant`
с путём `/tmp/../etc` → 200, в `.env` сохраняется нормализованный путь
(в данном окружении `str(Path("/etc"))`), ответ `path` тоже нормализован.
Существующие тесты `test_app.py` (в т.ч. `_fake_cfg` с `.env`-only семантикой
`qdrant_path_override`) не изменялись.

---

## 2. Проверка (реальный запуск, не по отчёту)

### 2.1 Юнит-тесты

```
$ python3 -m pytest firmware/tests -q
65 passed, 1 warning in 1.56s
```

База: 62 зелёных до фикса → 65 после (62 + 2 новых в test_deploy_config +
1 новый в test_app; один тест приоритета переписан, один инвертирован).
Регрессий нет: `INTERFACE_RAG_ENV` из process-env в `load()` не сломан
(покрыт `test_load_dev_and_env_override`), `import os` в `deploy_config.py`
остался легитимным.

### 2.2 Живой смоук — воспроизведение цепочки бага через реальный код пайплайна

Скрипт `/tmp/qdrant_smoke_test.py` (TestClient + настоящий `DeployConfig` +
`import llm_providers` из `/root/projects/Build_Search_index`):

```
BEFORE pollution: {'path': '/tmp/qdrant_smoke/qdrant_data', 'default': '/tmp/qdrant_smoke/qdrant_data', 'overridden': False}
get_api_key завершился ошибкой (ожидаемо): ProviderConfigError
QDRANT_PATH в os.environ после get_api_key(): '/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data'
AFTER pollution:  {'path': '/tmp/qdrant_smoke/qdrant_data', 'default': '/tmp/qdrant_smoke/qdrant_data', 'overridden': False}
SMOKE OK: overridden:false до и после заражения process-env; путь не изменился
```

Что смоук доказывает:

1. `.env` интерфейса без `QDRANT_PATH` → `GET /api/settings/qdrant` =
   `overridden:false`, путь = дефолт из config.yaml.
2. Вызов `get_api_key()` пайплайна реально выполнил голый `load_dotenv()`
   и положил `QDRANT_PATH=/mnt/sdb/…/qdrant_data` (прод-путь) в `os.environ`
   процесса — т.е. заражение воспроизведено, это не фиктивная эмуляция.
3. ПОСЛЕ заражения `overridden` остался `false`, эффективный путь не изменился —
   фикс работает. До фикса ветка `os.environ.get("QDRANT_PATH")` вернула бы
   прод-путь (`overridden:true`).

### 2.3 Обратная связь (не сломана)

`PUT /api/settings/qdrant` с абсолютным путём → запись `QDRANT_PATH` в `.env`
(`overridden:true`), сброс пустой строкой → удаление ключа — покрыто
существующими `test_put_qdrant_settings_writes_env` /
`test_put_qdrant_settings_empty_resets_override` / `test_put_qdrant_settings_rejects_relative_path`
и новым normalization-тестом; все зелёные.

---

## 3. Отклонения от архитектуры (локальные решения, в рамках объёма)

1. Тест `test_qdrant_path_process_env_beats_env_file` переименован в
   `test_qdrant_path_env_file_beats_process_env` — старое имя описывало
   инвертированное поведение и вводило бы в заблуждение.
2. В `test_qdrant_path_survives_load_dotenv` добавлен
   `monkeypatch.delenv("QDRANT_PATH", raising=False)` перед `load_dotenv` —
   имитация чистого старта dev-сервера (как в цепочке бага) и защита от
   утечки env в остальные тесты сессии. Семантика §5.3(а) не изменена.
3. Добавлен тест `test_app.py::test_put_qdrant_settings_normalizes_dotdot_path`
   под LOW-правку `update_qdrant` (§5.4 архитектуры говорил «изменений не
   требуется» про СУЩЕСТВУЮЩИЕ тесты — они не тронуты; новый тест аддитивен).

## 4. Границы соблюдены

- Пайплайны `/root/projects/Build_Search_index`, `/root/projects/Create_Markdown_YA` — не изменены.
- `chat_api.py`, `qdrant_api.py`, `config_ui.py` — не изменены (§4.3 архитектуры).
- `.hermes/STATE.md` — не тронут; git restore/checkout/stash не использовались.
- Секретов в коде/отчёте нет.

## 5. Известные ограничения / вне объёма

- Латентный риск из §3.3 архитектуры (резолв API-ключей in-process чата не из
  `config/.env` интерфейса; на prod — из `os.environ` systemd) остаётся —
  рекомендация на отдельную задачу (инъекция ключей `config/.env` в
  `os.environ` в `ChatSession` перед построением `QAGraph`). В этот фикс не входило.
- Будущее расширение `INTERFACE_RAG_QDRANT_PATH` (неймспейс интерфейса) —
  зафиксировано контрактом в §2.3 архитектуры, не реализовано.
- Смоук проводился на dev-окружении (TestClient), не на живом prod-сервере.
