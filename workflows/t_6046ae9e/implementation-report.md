# Implementation report — t_6046ae9e

Фаза 1 (backend) второй итерации `interface_RAG`: решения 20–29 по архитектуре
`workflows/t_8b227daf/architecture-report.md`. UI/static — НЕ трогались (фаза 2).

---

## 1. Что изменено (файлы)

### Производственный код

| Файл | Изменение |
|---|---|
| `firmware/src/fs_perms.py` | **новый** — точечные хелперы прав «общей» базы: `ensure_dir(path, mode=0o777)`, `write_bytes/write_text/write_text_atomic(..., mode=0o666)`, `chmod_copy(..., mode=0o666)`. Явный chmod/mkdir-mode, БЕЗ глобального `umask 000` (решение №22) |
| `firmware/src/app.py` | `upload` → `fs_perms.ensure_dir` + `fs_perms.write_bytes(0o666)`; `register` → `fs_perms.ensure_dir(s["reg"].parent)`; `registration_prefill` → новый контракт (сначала читает `_reg.yaml`, иначе vision); новые `GET/PUT /api/settings/qdrant` (решение №29) |
| `firmware/src/registration.py` | `write_reg_yaml` → **upsert**: сохраняет существующий slug, всегда ровно одна запись `documents.<slug>` (исторические дубли схлопываются); каталог 0777, файл и `.bak` 0666 через `fs_perms` (решения №22, №24). Добавлены `read_reg_record()` / `read_reg_slug()` (решение №28) |
| `firmware/src/jobs.py` | `_child_umask_zero()` + `preexec_fn=_child_umask_zero` в обоих `Popen` (`_run`, `_run_sequence`) — детский umask 0 только для subprocess пайплайнов (решение №22, §3.2 архитектуры) |
| `firmware/src/deploy_config.py` | поле `qdrant_path` → `qdrant_path_default` + свойство `qdrant_path` (приоритет: process-env `QDRANT_PATH` > `.env` > config.yaml) + свойство `qdrant_path_override` (решение №29, §5.2) |
| `firmware/src/config_ui.py` | `write_env`/`write_yaml`: после `os.replace` → `os.chmod(path, 0o600)`; `.bak` → `0o600` (решение №22, §3.3) |
| `scripts/consolidate_config.py` | `.env`-консолидация: добавлен первоочередной источник `remote/config/.env` (LXC) и `dev config/.env` (интерфейс dev) — чтобы `QDRANT_PATH` переживал деплой (решение №29, §5.4; флаг архитектора) |
| `scripts/deploy.sh` | выгрузка `/root/RAG/config/.env` → `$STAGE/remote/config/.env` (первый источник консолидации); `rm`/бэкап legacy `.env` сохранены как были |

### Тесты

| Файл | Изменение |
|---|---|
| `firmware/tests/test_perms.py` | **новый** — `fs_perms` даёт 0777/0666 (создание и приведение существующих) |
| `firmware/tests/test_registration.py` | расширен — upsert не плодит slug, сохраняет ключ, ровно одна запись; чистка исторических дублей; права `_reg.yaml`/`.bak`/каталога; `read_reg_record`/`read_reg_slug` |
| `firmware/tests/test_app.py` | расширен — prefill из `_reg.yaml` (`source="reg_yaml"`, OCR не вызывается), fallback на vision, `GET/PUT /api/settings/qdrant` (запись/сброс/валидация) |
| `firmware/tests/test_deploy_config.py` | расширен — `qdrant_path` property: default, override из `os.environ`, из `.env`, приоритет process-env > .env, пустое значение → fallback |
| `firmware/tests/test_jobs.py` | расширен — `preexec_fn=_child_umask_zero` передаётся в `Popen` (`_run` и `_run_sequence`, мок) + реальный subprocess с umask 0 в ребёнке |
| `firmware/tests/test_config_ui.py` | расширен — после `write_env`/`write_yaml` права 0600 (файл и `.bak`) |

---

## 2. Решения по правам (решение №22)

- **Каталоги базы** (`upload.base_dir`, `base_markdown`, `qdrant_data`; на проде
  `/mnt/sdb/!База_ГОСТ/*`) — `0777`, **файлы** — `0666`. Механизм — **точечный**:
  новый `fs_perms.py` с явным `mkdir(mode)/os.chmod(mode)` в точках записи
  интерфейса (upload, register/`write_reg_yaml`).
- **Файлы пайплайнов** (subprocess) — **детский `umask 0`** через
  `preexec_fn=_child_umask_zero` в `jobs.py` (только в дочернем процессе;
  родительский umask и `.env` не затрагиваются). `shell=False` и списки argv
  сохранены.
- **Конфиги интерфейса** — ограничительные: `.env` = `0600`, `.env.bak` = `0600`,
  `*.yaml` в `config_dir` = `0600`, `.bak` = `0600` (`config_ui.write_env/write_yaml`).
- Глобальный `umask 000` НЕ используется.

## 3. Схема `_reg.yaml` и upsert (решение №24)

Каноническая схема подтверждена пайплайном: `Create_Markdown_YA/firmware/src/create_markdown.py`
`_REG_FIELD_ORDER` — ровно 15 полей, совпадает с `registration.FIELDS`
(включая `document_type`/`domain`); сопоставление документа с записью — по
`source_file` (`_find_doc_key`), не по slug; `create_index.py` читает
`*_chunks.jsonl`/`*_assets.json`, `--qdrant-path` передаётся из интерфейса.

`write_reg_yaml` теперь:
1. сохраняет существующий slug при повторной регистрации
   (порядок: `fields.slug` → первый ключ существующего файла → `make_slug`);
2. пишет `data["documents"] = {slug: record}` — **ровно одна запись**, исторические
   дубли схлопываются в первый ключ;
3. атомарная запись через `fs_perms.write_text_atomic` (0666), `.bak` — `0666`.

Потребители (`--rag` / `create_index.py`) совместимы без изменений кода пайплайнов.

---

## 4. Как проверено (тесты/команды)

- `python3 -m pytest firmware/tests -q` → **62 passed** (было 33; добавлено 29).
- `python3 -m py_compile firmware/src/*.py scripts/consolidate_config.py` → OK.
- `bash -n scripts/deploy.sh` → OK.
- `python3 scripts/consolidate_config.py --out <tmp>` → exit 0; в консолидированный
  `.env` попадает `QDRANT_PATH` из `config/.env`, права `.env` = 0600.
- `bash scripts/deploy.sh --dry-run` → exit 0; `env sources` начинаются с
  `remote/config/.env` (LXC доступен, выгрузка реальная, read-only).
- Ручной смоук (sandbox): повторная регистрация → slug стабилен
  (`GOST_18410_kabeli`), одна запись, title обновлён; права `_reg.yaml`=0666,
  `.bak`=0666, каталог=0777; `read_reg_record`/`read_reg_slug` корректны;
  `.env` после `write_env` = 0600 с `QDRANT_PATH`.
- Импорт `from firmware.src.app import app` с реальным dev-конфигом → OK (36 роутов).

## 5. Что НЕ сделано (передаётся в фазу 2 — UI)

- `static/index.html`, `static/app.js`, `static/settings.js`, `static/style.css` —
  чат одним окном (№25), сайдбар настроек (№27), секция «Папка Qdrant» в UI (№29),
  sticky-шапка (№26), статус-селект/`reg-feedback` (№20/№21), дефолт
  `ignore_sections` в поле формы (№23), маппинг полей prefill из `_reg.yaml` (№28).
  Backend-контракты для них готовы: `GET/PUT /api/settings/qdrant`,
  `POST .../register/prefill` (`source`, `exists`, `fields` со всеми 15 полями + `slug`).

## 6. Известные ограничения / риски

- Права применяются к **вновь создаваемым** файлам/каталогам. Для приведения уже
  лежащих под `!База_ГОСТ` файлов к 0777/0666 — разовая команда
  `chmod -R`/`find` на проде при выкладке (риск №8 архитектуры; вне критического пути).
- `preexec_fn(umask 0)` — стандартная идиома, риск deadlock практически нулевой;
  план Б (argv-обёртка `-c "os.umask(0); os.execv(...)"`) описан в архитектуре §3.2.
- `QDRANT_PATH` сохраняется деплоем только после выкладки этой версии
  `deploy.sh`/`consolidate_config.py` (до этого ключ мог теряться — флаг архитектора).
- Dev не пишет в боевую базу Qdrant (`write_enabled: false` в dev-config) — без изменений.
