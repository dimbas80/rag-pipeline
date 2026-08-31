# Architecture-report — унификация ключей in-process чата + дочистка .env пайплайнов (t_6f25646e)

> Финальная архитектура по пункту 1 от пользователя. Продолжает решения №18/№19
> («чистый прод», единый config) и фикс QDRANT_PATH (t_12a1b3fd, решение №29).
> Границы: пайплайны (`Build_Search_index`, `Create_Markdown_YA`) НЕ меняются;
> `load_dotenv()` в `get_api_key` и явный `load_dotenv(.../.env)` в `request_bot.py`
> остаются как есть. Здесь — только схема + точный список правок для coder-фикса
> (t_a7d01588) и шаги для оркестратора по чистке прода.

---

## 1. Подтверждённые факты (проверено по коду и live-файлам)

### 1.1 Асимметрия источников ключей

| Потребитель | Источник ключей | Как | Свежесть |
|---|---|---|---|
| subprocess (convert/index/refresh) | `config/.env` интерфейса | `jobs.build_env(env_file)` → `env=dict(os.environ); env.update(read_env_raw(env_file))` (override) | свежие на каждый job — правки UI применяются сразу |
| чат in-process | `.env` пайплайна (dev) / `os.environ` systemd (prod) | `get_api_key()` → голый `load_dotenv()` (setdefault) → `os.environ.get` | старые; правки UI до чата не доходят |

`jobs.build_env` — `firmware/src/jobs.py:30-39`, **override**-семантика (config/.env поверх process-env).
`get_api_key` — `Build_Search_index/firmware/src/llm_providers.py:86-97`; `load_dotenv()` на строке 93,
`os.environ.get(env_name)` на 94. `load_dotenv` по умолчанию `override=False` (setdefault).

### 1.2 Единственная точка load_dotenv в рантайме чата

`llm_providers` импортируется цепочкой `qa_graph` → `llm_providers`, но `load_dotenv` вызывается
**лениво, только внутри `get_api_key`** (не при импорте). Интерфейсный LLM-клиент
(`firmware/src/llm_client.py`) принимает ключ явным параметром и `load_dotenv` не вызывает;
`registration.vision_prefill` получает `env=build_env(cfg.env_file)` (`app.py:136`) и берёт ключ из
переданного env, а не из process-env. Значит **до первого `graph.run()` (т.е. до первого
`get_api_key()`) `load_dotenv` в процессе интерфейса не выполняется**. Единственное место, где
интерфейс строит `QAGraph` — `ChatSession.__post_init__` (`chat_api.py:29-40`), вызывается на каждое
WebSocket-подключение (`app.py:352`).

### 1.3 Инвентаризация `.env` (проверено live)

dev (`/root/projects/...`):

| Файл | Размер | Ключи |
|---|---|---|
| `interface_RAG/config/.env` | 468 B | YANDEX_API_KEY, YANDEX_FOLDER_ID, DEEPSEEK_API_KEY, PROVOD_API_KEY, ANYMODEL_API_KEY, Z_AI_API_KEY, SILICONFLOW_API_KEY, TELEGRAM_BOT_TOKEN (**канон, полный**) |
| `Build_Search_index/.env` (корень) | 354 B | SILICONFLOW_API_KEY, DEEPSEEK_API_KEY, TELEGRAM_BOT_TOKEN, QDRANT_PATH, PROVOD_API_KEY, TELEGRAM_ALLOWED_USERS |
| `Create_Markdown_YA/.env` (корень) | 94 B | YANDEX_API_KEY, YANDEX_FOLDER_ID |
| `Create_Markdown_YA/firmware/src/.env` | 331 B | YANDEX_API_KEY, YANDEX_FOLDER_ID, PROVOD_API_KEY, DEEPSEEK_API_KEY, ANYMODEL_API_KEY, Z_AI_API_KEY |

prod (`/root/RAG/`, **недоступен с dev — только LXC 192.0.2.21 через ssh**): по задаче остались
корневые `/root/RAG/Build_Search_index/.env` и `/root/RAG/Create_Markdown_YA/.env`
(нарушение решений 18–19; `Build_Search_index/.env` — источник заражения `QDRANT_PATH`).

**Важное расхождение с текстом задачи:** `AITUNNEL_API_KEY` **нигде не существует** — ни в одном
`.env`, ни в `providers.yaml`, ни в коде трёх репозиториев (grep `aitunnel`/`AITUNNEL` — пусто).
Фактические fallback-роли в едином `config/providers.yaml`: `anymodel` (table_vision/registration_vision
fallback → `ANYMODEL_API_KEY`), `zai-custom` (`Z_AI_API_KEY`), `provod` (`PROVOD_API_KEY`).
embedding/rerank — `siliconflow` без fallback. Все эти ключи **уже есть** в `config/.env`.
Инъекция ключ-агностична (кладёт весь `.env`), поэтому механизм корректен независимо от наличия
конкретного ключа; вопрос «нужен ли Aitunnel-провайдер/ключ» — отдельно, оркестратору (см. §7).

---

## 2. A — Инъекция ключей чата: точка и семантика

### 2.1 Решение: инъекция в `ChatSession.__post_init__`, ПЕРВЫМ действием, override-семантика

```python
# firmware/src/chat_api.py
import os  # добавить

try:
    from .deploy_config import DeployConfig
except ImportError:  # direct `uvicorn app:app` from firmware/src
    from deploy_config import DeployConfig

# Паттерн импорта read_env_raw — дословно как в jobs.py:13-15 (оба варианта запуска).
try:
    from firmware.src.config_ui import read_env_raw
except ImportError:  # pragma: no cover - direct `uvicorn app:app` from firmware/src
    from config_ui import read_env_raw


def _inject_env_file(env_file) -> None:
    """Инъекция ключей <config_dir>/.env в os.environ (override, как jobs.build_env).

    Единый источник API-ключей in-process чата: пайплайновый get_api_key()
    (llm_providers.py) резолвит ключи голым load_dotenv(setdefault) →
    os.environ.get. Положив ключи config/.env заранее с override, гарантируем,
    что чат видит те же ключи, что subprocess (build_env). Повторный вызов
    идемпотентен; ключи, которых нет в .env, из os.environ НЕ удаляются
    (systemd Environment= на проде сохраняется).
    """
    os.environ.update(read_env_raw(env_file))


@dataclass
class ChatSession:
    cfg: DeployConfig

    def __post_init__(self) -> None:
        # ДО импорта пайплайна (qa_graph→llm_providers) и ДО первого graph.run() —
        # гарантированно раньше load_dotenv() внутри get_api_key().
        _inject_env_file(self.cfg.env_file)
        _pipeline_paths(self.cfg)
        from qa_graph import QAGraph, QAGraphConfig
        # ... далее без изменений
```

`_inject_env_file` вынесен в отдельную функцию специально, чтобы тестировать его **без** тяжёлого
импорта `qa_graph` (см. §5).

### 2.2 Почему `__post_init__`, а не «один раз на старте приложения»

Стартовая инъекция (вариант 1: после `cfg = load()` в `app.py:31`, до импортов пайплайна) читает
`config/.env` ровно один раз. Но `get_api_key` на каждый прогон перечитывает `os.environ` через
`load_dotenv()` (setdefault). При смене/добавлении ключа через UI (`PUT /api/settings/env` → `write_env`)
без рестарта процесса стартовая инъекция оставит в `os.environ` **старое** значение, а
`load_dotenv` его не перетрёт (setdefault) → баг «правки UI не доходят до чата» **не устраняется**.

Инъекция в `__post_init__` повторяется на каждую новую WebSocket-сессию (каждое подключение создаёт
новый `ChatSession`) и заново читает `config/.env`. Это **ровно** та гранулярность, что у subprocess:
там `build_env` читает `config/.env` свежим на каждый job. Гранулярность «job ↔ сессия» симметрична.

### 2.3 Почему override (`os.environ.update`), а не setdefault

`os.environ.update(read_env_raw(env_file))` — дословно механизм `build_env`. Обоснование крайними
случаями:

1. **Ключ, ранее «загрязнённый» `load_dotenv`.** На dev первый прогон графа: `get_api_key` →
   `load_dotenv()` находит `Build_Search_index/.env` и setdefault-ом кладёт `DEEPSEEK_API_KEY`,
   `PROVOD_API_KEY`, `SILICONFLOW_API_KEY` и т.д. в `os.environ`. Если бы инъекция была setdefault —
   она бы **не смогла** заменить эти значения значениями из `config/.env`. override исправляет
   заражение. Это же гарантирует, что `config/.env` (авторитетный источник) всегда побеждает.
2. **Смена ключа через UI без рестарта.** В новой сессии `config/.env` содержит новое значение;
   override заменяет старое значение в `os.environ`. setdefault оставил бы старое.
3. **Повторная сессия.** override с теми же значениями — идемпотентный no-op; утечки нет.
4. **Неразрушающе.** `os.environ.update` добавляет/обновляет ТОЛЬКО ключи из `.env`, никогда не
   удаляет прочие — `systemd Environment=`/`EnvironmentFile=` на проде (если заведутся) сохранятся.

Направление «load_dotenv не перетрёт ключи» обеспечено автоматически: `get_api_key` делает
`load_dotenv()` (setdefault) ПОСЛЕ нашей инъекции и не может перезаписать уже установленные ключи.

### 2.4 Не сломать QDRANT_PATH-фикс

`deploy_config.qdrant_path_override` (t_12a1b3fd) читает **только** `read_env_raw(self.env_file)`
и **не читает** process-env. Инъекция пишет в process-env, но:
- резолвер QDRANT_PATH это не читает → `cfg.qdrant_path` не меняется от инъекции;
- `QAGraphConfig(qdrant_path=str(self.cfg.qdrant_path))` вычисляется из `cfg.qdrant_path` (фиксованный
  резолвер), а не из `os.environ`;
- если `config/.env` не содержит `QDRANT_PATH` (текущее состояние — ключ вычищен), инъекция его и не
  добавит; если пайплайн позже загрязнит `os.environ["QDRANT_PATH"]` через свой `load_dotenv` —
  интерфейс это игнорирует (регрессия уже покрыта `test_qdrant_path_ignores_process_env_pollution`).

Инъекция **ортогональна** QDRANT_PATH-фиксу. Никаких правок в `deploy_config.py` не требуется.

---

## 3. B — Дочистка прода: корневые `.env` пайплайнов

### 3.1 Что удалять и где

Корневые `.env` пайплайнов существуют только на проде (`/root/RAG/...`), который **недоступен с
dev-машины**. Реальную чистку выполняет **оркестратор** через `scripts/deploy.sh --apply`; coder
только правит `deploy.sh` (бэкап + удаление) и `consolidate_config.py` (источники). На dev корневые
`.env` пайплайнов **НЕ трогать** — они нужны для standalone-запусков пайплайнов вне интерфейса
(`Create_Markdown_YA/AGENTS.md`: «пайплайн читает `.env` из `firmware/src/.env`»); после нашей
инъекции dev-чат корректно резолвит ключи из `config/.env` поверх них.

Удаляются на проде (добавить в cleanup-скрипт `deploy.sh`, шаг 4):

```
/root/RAG/Build_Search_index/.env
/root/RAG/Create_Markdown_YA/.env
```

Оба — legacy-остаток решений 18–19; `Build_Search_index/.env` — тот самый источник заражения
`QDRANT_PATH` (ключ с прод-путём `/mnt/sdb/...`).

### 3.2 Судьба `request_bot.py`

Факты: бот **не запущен** (нет процесса и нет systemd-юнита); `request_bot.py` **исключён из
rsync-манифеста** (`deploy.sh:162 --exclude='request_bot.py'`), т.е. на проде его присутствие —
исторический артефакт, а не часть «чистого прода». Он читает `.env` явно:
`load_dotenv(path/../../../.env)` = `Build_Search_index/.env` (`request_bot.py:36`) и берёт
`TELEGRAM_BOT_TOKEN`, `QDRANT_PATH` (дефолт `/mnt/sdb/!База_ГОСТ/Markdown/qdrant_data`),
`TELEGRAM_ALLOWED_USERS`.

**Решение (зафиксировать):** удалить корневой `.env`; бота не реанимировать в рамках этого фикса.
Поддерживаемая чат-поверхность — WebSocket-чат интерфейса (не затронут). Если бот когда-либо
запускается вновь — это **отдельная задача**, и тогда ключи подаются через systemd
`Environment=`/`EnvironmentFile=` (или бот переводится на чтение `/root/RAG/config/.env`).
Зафиксировать это комментарием в `deploy.sh` рядом с cleanup.

### 3.3 Что терять нельзя (`TELEGRAM_*`)

| Ключ | Где сейчас | Судьба при удалении корневого `.env` |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | `config/.env` **и** `Build_Search_index/.env` | сохраняется (в `config/.env`) ✅ |
| `TELEGRAM_ALLOWED_USERS` | **только** `Build_Search_index/.env` | **теряется** ⚠️ (бот-only ключ) |
| `QDRANT_PATH` | только `Build_Search_index/.env` (прод-путь) | теряется — **намеренно** (это и есть заражение; интерфейс берёт дефолт из `config.yaml` или пишет свой через UI) |

`TELEGRAM_ALLOWED_USERS` — единственный реально теряемый ключ. Он нужен только боту (декоммишн), в
`providers.yaml` не участвует. Решение: (а) бэкап корневого `.env` **до** удаления (см. §4.1) делает
потерю обратимой; (б) если бота когда-нибудь включат — ключ добавляется через UI в `config/.env` или
в `EnvironmentFile=` systemd. Зафиксировать в отчёте деплоя, не блокирует фикс.

---

## 4. C — `deploy.sh` + `consolidate_config.py`

### 4.1 `scripts/deploy.sh` — три правки

**(1) Шаг 0 (FILES, строки 109-116).** Убрать строки `.env` legacy-источников (они больше не источники
консолидации, см. §4.2); сохранить `config/.env` и оба `providers.yaml`:

```
/root/RAG/config/.env|$STAGE/remote/config/.env
/root/RAG/Create_Markdown_YA/firmware/src/providers.yaml|$STAGE/remote/CMY/providers.yaml
/root/RAG/Build_Search_index/firmware/src/providers.yaml|$STAGE/remote/BSI/providers.yaml
```
(строки `interface_RAG/.env`, `CMY/.env`, `BSI/.env` — удалить.)

**(2) Шаг 1 (бэкап, цикл строк 128-136).** Добавить корневые `.env` в бэкап (чтобы удаление было
обратимым, прежде всего `TELEGRAM_ALLOWED_USERS`):

```
    "CMY:/root/RAG/Create_Markdown_YA/.env:.env.root" \
    "BSI:/root/RAG/Build_Search_index/.env:.env.root" \
```
(имена `.env.root` — чтобы не затёрся бэкап `firmware/src/.env` в тех же подкаталогах CMY/BSI).

**(3) Шаг 4 (cleanup, скрипт строк 186-206).** Добавить удаление корневых `.env` рядом с существующими
`rm -f .../firmware/src/.env`:

```bash
# Корневые .env пайплайнов — legacy остатки решений 18–19 (Build_Search_index/.env
# был источником заражения QDRANT_PATH). request_bot.py, который их читал, не
# разворачивается (исключён из rsync) и не запущен; при будущем запуске бота ключи
# подавать через systemd Environment=/EnvironmentFile= (отдельная задача).
rm -f  /root/RAG/Create_Markdown_YA/.env
rm -f  /root/RAG/Build_Search_index/.env
```

### 4.2 `scripts/consolidate_config.py` — канонический список источников `.env`

Сейчас (`main`, строки 129-142) 7 источников; приоритет — первый выигрывает (setdefault в `merge_env`):

```
remote/config/.env  >  remote/interface_RAG/.env  >  remote/CMY/.env  >  remote/BSI/.env
  >  dev config/.env  >  dev CMY/.env  >  dev BSI/.env
```

**Канонический список (итог) — 2 источника:**

```python
env_sources: list[tuple[str, Path]] = []
if remote:
    env_sources.append(("remote/config/.env", remote / "config/.env"))
env_sources.append(("dev config/.env", DEV_ROOT / "config/.env"))
env_sources = [(label, path) for label, path in env_sources if path.exists()]
```

Удаляются 5 legacy-источников: `remote/interface_RAG/.env`, `remote/CMY/.env`, `remote/BSI/.env`,
`dev CMY/.env`, `dev BSI/.env`.

Обоснование:
- `remote/config/.env` — авторитетный прод-конфиг (создан `deploy.sh` шаг 2), сохраняет prod-only ключи.
- `dev config/.env` — dev-мастер (UI пишет сюда), авторитетный источник добавлений.
- Все 8 ключей, на которые ссылаются `api_key_env` в едином `providers.yaml`, уже есть в `config/.env`
  (проверено); legacy-источники не добавляют ни одного нужного ключа. Единственный «лишний» ключ из
  legacy — `TELEGRAM_ALLOWED_USERS` (бот-only, декоммишн) — и `QDRANT_PATH` (заражение), оба должны
  **не** попадать в консолидированный `.env`. Поэтому исключение источников не теряет ничего нужного,
  а напротив — не даёт legacy `.env` заново контаминировать `config/.env` (включая `QDRANT_PATH`).
- Файлы legacy-источников на проде всё равно удаляются cleanup-ом (§4.1), поэтому фильтр `path.exists()`
  уже пропускал бы их; явное удаление из списка фиксирует намерение и упрощает dry-run-план.

`CANONICAL_KEYS` (строки 36-39) **оставить без изменений** — 8 ключей уже соответствуют `config/.env`.
Обновить только docstring модуля (блок «Источники»), чтобы отражал 2 источника вместо 7.

---

## 5. Точный список правок для coder-фикса (t_a7d01588)

### 5.1 `firmware/src/chat_api.py` (единственный runtime-файл)

| Место | Правка |
|---|---|
| импорты (1-12) | добавить `import os`; добавить импорт `read_env_raw` (try/except-паттерн, как в `jobs.py:13-15`) |
| новая функция | `_inject_env_file(env_file)` — `os.environ.update(read_env_raw(env_file))`, docstring из §2.1 |
| `ChatSession.__post_init__` (29-40) | первой строкой тела — `_inject_env_file(self.cfg.env_file)` ДО `_pipeline_paths` и ДО `from qa_graph import ...`; остальное без изменений |

### 5.2 `scripts/deploy.sh` — правки §4.1 (1)-(3).

### 5.3 `scripts/consolidate_config.py` — правки §4.2 (env_sources + docstring). `CANONICAL_KEYS` не менять.

### 5.4 НЕ менять (жёсткие границы)

- `Build_Search_index/**`, `Create_Markdown_YA/**` — `load_dotenv()` в `get_api_key` и явный
  `load_dotenv(...)` в `request_bot.py` остаются.
- `deploy_config.py` (QDRANT_PATH-фикс уже корректен), `config_ui.py` (`read_env_raw`/`write_env` родовые, без изменений), `app.py`, `jobs.py`.
- `.hermes/STATE.md`; `.env` интерфейса остаётся `0600` (инъекция только читает).
- Запрещено: `git restore`/`checkout`/`stash` конфигов; секреты в коде/отчётах; самому удалять файлы на проде.

---

## 6. Тесты (для coder-фикса)

Все прогоны: `python3 -m pytest firmware/tests -q` — зелёные.

### 6.1 `firmware/tests/test_chat_api.py` — добавить (через `_inject_env_file`, без импорта qa_graph)

```python
import os
from firmware.src.chat_api import _inject_env_file

def test_inject_env_file_overrides_stale_process_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("ANYMODEL_API_KEY=new_key\nZ_AI_API_KEY=z_key\n", encoding="utf-8")
    monkeypatch.setenv("ANYMODEL_API_KEY", "stale_from_pipeline_load_dotenv")
    monkeypatch.delenv("Z_AI_API_KEY", raising=False)
    _inject_env_file(env_file)
    assert os.environ["ANYMODEL_API_KEY"] == "new_key"   # override победил «загрязнение»
    assert os.environ["Z_AI_API_KEY"] == "z_key"
    monkeypatch.delenv("Z_AI_API_KEY", raising=False)     # не оставлять ключ в env

def test_inject_env_file_preserves_unrelated_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"; env_file.write_text("ONLY_FILE_KEY=v\n", encoding="utf-8")
    monkeypatch.setenv("UNRELATED_KEY", "keep")
    _inject_env_file(env_file)
    assert os.environ["UNRELATED_KEY"] == "keep"
    assert "PATH" in os.environ
    monkeypatch.delenv("ONLY_FILE_KEY", raising=False)

def test_inject_env_file_missing_file_noop(tmp_path):
    _inject_env_file(tmp_path / "does_not_exist.env")   # не должно падать
```

Примечание: `_inject_env_file` мутирует глобальный `os.environ` вне трекинга `monkeypatch` — поэтому
тесты используют уникальные имена ключей и чистят их `monkeypatch.delenv(..., raising=False)`.

### 6.2 QDRANT_PATH-регрессия (не сломать фикс)

Существующие `firmware/tests/test_deploy_config.py` уже покрывают изоляцию (в т.ч.
`test_qdrant_path_ignores_process_env_pollution` и `test_qdrant_path_survives_load_dotenv`). Добавить
один тест, что инъекция не влияет на резолвер:

```python
def test_env_injection_does_not_change_qdrant_path(tmp_path, monkeypatch):
    # config/.env с QDRANT_PATH, инъекция кладёт его в os.environ — резолвер обязан
    # читать .env (read_env_raw), а не process-env; результат тот же.
    env_file = tmp_path / ".env"; env_file.write_text("QDRANT_PATH=/interface/qdrant\n", encoding="utf-8")
    cfg = tmp_path / "config.yaml"; cfg.write_text(_config(dev_env_file=str(env_file)), encoding="utf-8")
    loaded = load(cfg)
    _inject_env_file(env_file)   # кладёт QDRANT_PATH=/interface/qdrant в os.environ
    assert loaded.qdrant_path_override == "/interface/qdrant"
    assert loaded.qdrant_path == Path("/interface/qdrant")
```

### 6.3 `scripts/consolidate_config.py` — проверить canonical-источники

Проверяется либо дымовым прогоном `python3 scripts/consolidate_config.py --out <tmp>` (assert в выводе
`env sources: remote/config/.env, dev config/.env` и отсутствие legacy-источников), либо (если coder
решит факторизовать) unit-тестом на `merge_env` с двумя источниками. `bash -n scripts/deploy.sh` — OK;
`scripts/deploy.sh --dry-run` — не падает (строит план; на недоступном LXC шаги read-only пропускаются).

---

## 7. Шаги для оркестратора (чистка прода — НЕ выполнять coder'у)

1. После PASS ревью coder-фикса: `scripts/deploy.sh --dry-run` — убедиться, что план включает
   удаление `/root/RAG/{Build_Search_index,Create_Markdown_YA}/.env` и бэкап `.env.root`.
2. `scripts/deploy.sh --apply` — выполняет бэкап (включая `.env.root`) → консолидацию (2 источника) →
   rsync → cleanup корневых `.env` → рестарт → smoke (HTTP 200).
3. Проверить на проде: `ssh ... 'ls -la /root/RAG/Build_Search_index/.env /root/RAG/Create_Markdown_YA/.env'`
   → файлы отсутствуют; `/root/RAG/config/.env` цел, `0600`.
4. Убедиться, что `TELEGRAM_ALLOWED_USERS` (если он нужен для будущего бота) либо вынесен в
   `config/.env` через UI, либо зафиксирован в `EnvironmentFile=` systemd — см. §3.3. Бот не запускается
   в этом фиксе.

---

## 8. Флаги / риски

1. **`AITUNNEL_API_KEY` не существует** нигде в репо (grep пусто; в `config/.env` его нет; провайдера
   `aitunnel` в едином `providers.yaml` нет). Инъекция ключ-агностична и эту нехватку не маскирует.
   Если планировался Aitunnel-провайдер для embedding/rerank — это отдельная задача (добавить провайдера
   в `providers.yaml` + ключ через UI); флаг оркестратору.
2. **`TELEGRAM_ALLOWED_USERS` теряется** при удалении корневого `Build_Search_index/.env` (бот-only,
   бот декоммишн). Обратимо через бэкап `.env.root`; осознанно (§3.3).
3. **Гранулярность свежести ключей чата** — «новая WebSocket-сессия»: уже открытая сессия продолжит
   держать старый ключ до переподключения. Это та же семантика, что у запущенного subprocess-job
   (начат до смены ключа). Приемлемо; при желании «горячей» смены ключа в живой сессии — вне объёма.
4. **Prod недоступен с dev** — все prod-правки только через `deploy.sh --apply`; прямой `rm` на LXC
   оркестратор делает через ssh в рамках деплоя, не «мимо» скрипта.

---

## 9. Итог

| Пункт | Решение |
|---|---|
| Точка инъекции | `ChatSession.__post_init__`, ПЕРВОЙ строкой, до импорта `qa_graph` |
| Семантика | override — `os.environ.update(read_env_raw(cfg.env_file))`, дословно `build_env` |
| Почему не старт | стартовая инъекция не доносит смену ключа из UI до чата без рестарта (setdefault в `get_api_key`) |
| QDRANT_PATH | не затронут: резолвер читает только `.env`, инъекция ортогональна |
| Чистка прода | удалить корневые `/root/RAG/{Build_Search_index,Create_Markdown_YA}/.env` через `deploy.sh` |
| `request_bot.py` | декоммишн подтверждён; при будущем запуске — systemd `Environment=`/`EnvironmentFile=` (отдельная задача) |
| Теряемые ключи | только `TELEGRAM_ALLOWED_USERS` (бот-only) — обратим через бэкап `.env.root` |
| Канон источников `.env` | `remote/config/.env` → `dev config/.env` (2 источника; 5 legacy удалены) |
| Правки coder | `chat_api.py` (инъекция), `deploy.sh` (бэкап+cleanup+FILES), `consolidate_config.py` (источники+docstring) |
