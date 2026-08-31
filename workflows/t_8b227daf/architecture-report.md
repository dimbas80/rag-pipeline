# Architecture report — t_8b227daf

Вторая итерация архитектуры `interface_RAG` (10 замечаний пользователя после приёмки).
Замечания соответствуют решениям 20–29 в `docs/product/decision-log.md`.

**Статус:** архитектура для передачи Coder'у. Код не писался, пайплайны не менялись,
`docs/product/*` и `.hermes/STATE.md` не трогались.

---

## 0. Резюме решений (замечание → решение)

| # | Замечание | Решение (кратко) |
|---|---|---|
| 1 | Статус-селект «не раскрывается» | Дубликат `id="reg-status"` (селект + `<span>`). Переименовать span; русские подписи, значения `active`/`inactive` |
| 2 | Сообщение регистрации возле кнопки | Единый `span.reg-feedback` рядом с кнопкой; убрать дубли в область загрузки |
| 3 | Права 0777/0666 под `!База_ГОСТ` | Явные `chmod`/`mkdir mode` в коде интерфейса + детский `umask 0` только для subprocess (`preexec_fn`). Без глобального umask |
| 4 | `ignore_sections` предзаполнен | Дефолт `«Предисловие, Содержание»` в поле формы (UI) |
| 5 | Одна запись на документ | `write_reg_yaml` — upsert: сохранять существующий slug, ровно одна запись в `documents` |
| 6 | Чат одним окном | Одно окно истории + единственная строка ввода внизу (query и reply) |
| 7 | Шапка sticky | `.site-header { position: sticky; top: 0; z-index }` |
| 8 | Настройки — сайдбар | Слева меню разделов, справа настройки выбранного раздела |
| 9 | Prefill из `_reg.yaml` | `/register/prefill` сначала читает существующий `_reg.yaml`, иначе vision |
| 10 | Папка Qdrant (персистентно) | Override `QDRANT_PATH` в `.env`; `DeployConfig.qdrant_path` → свойство |

---

## 1. Ключевые находки (root causes, проверено по коду)

### 1.1 Дубликат `id="reg-status"` — корень замечания №1 (высокая уверенность)

В `firmware/src/static/index.html` идентификатор `reg-status` используется **дважды**:

```html
<!-- строка 157 -->
<select id="reg-status" name="status">
  <option value="active">active — действует</option>
  <option value="inactive">inactive — не действует</option>
</select>
...
<!-- строка 190 -->
<span id="reg-status" class="status-line"></span>
```

`document.getElementById("reg-status")` возвращает **первый** элемент в порядке документа —
это `<select>`. Функция `setStatus("reg-status", …)` в `app.js` делает
`el.textContent = message` — на `<select>` это **удаляет все `<option>`** и подставляет
текстовый узел. Вызов происходит сразу после загрузки файла (`prefillRegistration()`),
поэтому селект теряет опции и «не раскрывается».

Вывод: `.field select` в `style.css` **без** `appearance:none` — CSS корректен, причину
надо искать в DOM. Корень — дубликат id, а не стили.

**Фикс (детально в §8):** переименовать `<span>` в `id="reg-feedback"`; обновить все
`setStatus("reg-status", …)` → `setStatus("reg-feedback", …)`; селект оставить с
`id="reg-status"`, но подписи сделать русскими (`активный`/`не активный`), значения —
`active`/`inactive`.

### 1.2 `write_reg_yaml` добавляет новый slug при повторной регистрации (№5)

`firmware/src/registration.py:55-65` всегда вычисляет `slug` через `make_slug(...)`
(с дедупликацией `existing=docs`), затем `docs[slug] = record`. При повторной регистрации
после изменения полей (напр. `document_id`) генерируется **новый** slug → в
`documents` появляется вторая запись. Файл `<stem>_reg.yaml` — per-document, в нём должна
быть ровно одна запись.

### 1.3 `registration_prefill` не проверяет существующий `_reg.yaml` (№9)

`app.py:118-125` (`POST /api/documents/{sid}/register/prefill`) всегда вызывает
`registration.vision_prefill(...)` (OCR первой страницы) и не смотрит на существующий
`<stem>_reg.yaml`. При повторной загрузке того же документа поля не восстанавливаются.

### 1.4 `cfg.qdrant_path` статичен — берётся из `config.yaml` при старте (№10)

`deploy_config.py` — frozen-dataclass, `qdrant_path` читается из `config.yaml`
(`qdrant.path`) один раз при `load()`. В рантайме его нельзя поменять из настроек.
Используется в `index_document` (`--qdrant-path`), `documents_in_base`,
`settings_collections`, `chat_api.ChatSession`.

### 1.5 Deploy `.env`-консолидация не читает `config_dir/.env` (риск для №10)

`scripts/deploy.sh` (строка 110) выгружает `/root/RAG/interface_RAG/.env`, а затем
(строка 188) **удаляет** его. При этом интерфейс пишет `.env` в
`/root/RAG/config/.env` (`config.yaml` prod `env_file`). `consolidate_config.py`
собирает `.env` из `interface_RAG/.env + CMY/.env + BSI/.env + dev CMY + dev BSI` —
источник `/root/RAG/config/.env` **не входит** в список. Следствие: ключ `QDRANT_PATH`
(и любые ключи, добавленные через UI) будут **потеряны при следующем деплое**, если не
добавить `config/.env` как первоочередной источник. Подробнее в §5.4.

### 1.6 Каноническая схема `_reg.yaml` подтверждена пайплайном

`Create_Markdown_YA/firmware/src/create_markdown.py` содержит
`_REG_FIELD_ORDER` (строки 4859-4864) — ровно 15 полей, **включая** `document_type` и
`domain`. Это совпадает с `registration.FIELDS` интерфейса. Значит `document_type`/`domain`
— легитимные поля канонической записи (не «лишние»), а
`_REG_DEFAULT_IGNORE = ["Предисловие", "Содержание"]` и
`_REG_COMPLETENESS_FIELDS` совпадают с текущим поведением интерфейса.

---

## 2. Каноническая схема `<stem>_reg.yaml` (замечание №5, раздел B)

Per-document файл `<stem>_reg.yaml` (рядом с `<stem>.md`, путь `base_markdown/<stem>/`).
Описывает **ровно один** документ.

```yaml
documents:
  <slug>:                       # ключ-слаг, стабильный при обновлении записи
    document_id: "ГОСТ 18410-73"          # обозначение (обязательное)
    document_id_alt: null                  # старое обозначение (СНиП → СП)
    document_type: "ГОСТ"                  # ГОСТ/СП/СО/СНиП/ПУЭ (обязательное)
    domain: "Кабели"                       # тематика, слово для слага (обязательное)
    title: "Кабели силовые…"               # заголовок (обязательное)
    edition: 1973                          # год издания (обязательное)
    date_enacted: "1973-01-01"             # дата ввода в действие (обязательное)
    date_amended: null                     # дата последних изменений
    amended_by: null                       # чем изменён
    source_file: "ГОСТ 18410-73.pdf"       # имя файла (авто, для сопоставления --rag)
    status: active                         # active | inactive
    status_reason: null                    # при inactive
    replaced_by_document_id: null          # при inactive
    replaced_by_doc_key: null              # при inactive
    ignore_sections:                       # исключить из индексации
      - "Предисловие"
      - "Содержание"
```

Контракты совместимости с потребителями (проверено):

- **`create_markdown.py --rag`** читает через `load_rag_config(config_path,
  reg_path=<stem>_reg.yaml)` → `documents.<slug>` (overlay). Сопоставление файла с записью
  — по `source_file` (`_find_doc_key`), **не** по slug. `validate_rag_config` проверяет
  только `status`/`status_reason`/`replaced_by_*` — прочие поля игнорируются.
- **`create_index.py`** не читает `_reg.yaml` напрямую; читает `*_chunks.jsonl` и
  `*_assets.json`. `document_type`/`domain` попадают в Qdrant-payload из `_assets.json`
  (top-level, `load_assets`, стр. 84-87) — туда их пишет `--rag` из `doc_meta`.
- `ignore_sections` → `build_rag_jsonl_v2` (стр. 4694) фильтрует секции.
- `status` → `build_rag_jsonl_v2` (стр. 4686-4692) кладётся в каждую JSONL-строку.

**Правило upsert (фикс №5):** ключ-слаг **сохраняется** при повторной регистрации
(стабильность `chunk_id = {slug}/{clause}`), запись **обновляется**; файл всегда содержит
ровно одну запись. Если в файле исторически скопились несколько ключей — оставить первый,
удалить остальные.

---

## 3. Механизм прав (замечание №3, раздел A)

Цель: файлы/каталоги под `/mnt/sdb/!База_ГОСТ` (загрузка, `Markdown/`, `qdrant_data/`)
получают `0666`/`0777`; конфиги интерфейса (`.env`, `*.yaml` в `config_dir`) —
ограничительные права (`.env` = `0600`). **Глобальный `umask 000` запрещён** (утечёт `.env`).

Два независимых контура записи:

### 3.1 Файлы, создаваемые интерфейсом (FastAPI)

Новый модуль `firmware/src/fs_perms.py` (чистые хелперы, без логики):

```python
# firmware/src/fs_perms.py
def ensure_dir(path, mode=0o777):        # mkdir parents + os.chmod(path, mode)
def write_bytes(path, data, mode=0o666): # write_bytes + os.chmod(path, mode)
def write_text(path, text, mode=0o666):  # write_text + os.chmod(path, mode)
def write_text_atomic(path, text, mode=0o666):  # temp + os.replace + chmod(mode)
def chmod_copy(src, dst, mode=0o666):    # shutil.copy2 + os.chmod(dst, mode)  (для .bak)
```

Точки вызова (замены `mkdir(...)`/`write_bytes(...)`/`write_text(...)` на «общих» путях):

| Место | Было | Стало |
|---|---|---|
| `app.py::upload` | `cfg.upload_base_dir.mkdir(...)` + `target.write_bytes(data)` | `fs_perms.ensure_dir(cfg.upload_base_dir)` + `fs_perms.write_bytes(target, data)` |
| `registration.py::write_reg_yaml` | `reg_path.parent.mkdir(...)` + `tmp.write_text(...)` | `fs_perms.ensure_dir(reg_path.parent)` + `fs_perms.write_text_atomic(reg_path, ..., 0o666)`; `.bak` через `fs_perms.chmod_copy(..., 0o666)` |

`_reg.yaml` попадает под `base_markdown/<stem>/` (в проде — под `!База_ГОСТ`), поэтому ему
нужен `0666`.

### 3.2 Файлы, создаваемые пайплайнами (subprocess)

Пайплайны пишут через `mkdir(parents=True, exist_ok=True)` **без** mode и
`write_text`/запись — наследуют umask родителя. Интерфейс спавнит их в `jobs.py`
(`subprocess.Popen`). Решение — **детский umask 0 только для subprocess** (процесс
интерфейса свой umask не меняет):

```python
# firmware/src/jobs.py
def _child_umask_zero() -> None:
    os.umask(0)  # в дочернем процессе: mkdir(0o777)/файлы(0o666) не урезаются umask'ом

subprocess.Popen(argv, cwd=cwd, env=env, shell=False,
                 preexec_fn=_child_umask_zero, ...)   # в _run и _run_sequence
```

`preexec_fn` выполняется в дочернем процессе между `fork` и `exec`; `os.umask(0)` — тривиальный
syscall-враппер, не берёт Python-локов (риск deadlock многопоточного сервера практически
нулевой). Это стандартная идиома; `shell=False` и списки argv сохраняются.

**Альтернатива без `preexec_fn` (если ревьюер захочет нулевой риск):** в `jobs.py` оборачивать
argv в `[sys.executable, "-c", "import os,sys;os.umask(0);os.execv(sys.argv[1],sys.argv[1:])",
sys.executable, <скрипт>, ...]`. В отчёте — как план Б; по умолчанию `preexec_fn`.

### 3.3 Конфиги интерфейса (ограничительные права)

- `config_ui.write_env`: после `os.replace(tmp, path)` → `os.chmod(path, 0o600)`; `.bak` →
  `0o600`.
- `config_ui.write_yaml`: после `os.replace(tmp, path)` → `os.chmod(path, 0o600)` (owner-only;
  `.bak` — `0o600`). Пайплайны читают yaml по CLI-ключу тем же пользователем — `0600` им не
  мешает. (Допустим и `0644`, если нужна групповая читаемость; по умолчанию `0600` как
  «максимально ограничительные».)

### 3.4 Изменения в пайплайнах — НЕ требуются

Оба контура закрываются без правки `/root/projects/Create_Markdown_YA` и
`/root/projects/Build_Search_index`: файлы пайплайнов покрывает детский `umask 0` (§3.2),
файлы интерфейса — явные `chmod` (§3.1). `deploy.sh`/`consolidate_config.py` менять под права
**не** нужно (они и так делают `os.chmod(env_path, 0o600)` для `.env`).

---

## 4. Prefill из `_reg.yaml` (замечание №9, раздел C)

`POST /api/documents/{sid}/register/prefill` меняет логику:

1. Если `s["reg"]` (`base_markdown/<stem>/<stem>_reg.yaml`) существует — прочитать
   `yaml.safe_load`, взять **единственную** запись `documents.<slug>` (первый ключ),
   вернуть её поля плоским dict. OCR **не** запускается.
2. Иначе — `registration.vision_prefill(...)` как сейчас.

Новый контракт ответа:

```json
{ "fields": { "...": "..." }, "source": "reg_yaml" | "vision",
  "exists": true | false, "image_available": true | false }
```

- `source="reg_yaml"` → `fields` = все 15 полей записи (в т.ч. `status`, `ignore_sections`,
  даты, `document_id_alt`, `replaced_by_*`, `status_reason`).
- `source="vision"` → `fields` = только распознанное подмножество
  (`document_id`, `title`, `document_type`, `domain_hint`→`domain`), `exists=false`.

Хелпер в `registration.py` (рядом с `vision_prefill`):

```python
def read_reg_record(reg_path) -> dict | None:
    # вернуть плоскую запись documents.<first-slug> или None, если файла нет/пусто
```

UI-сторона (`app.js::prefillRegistration`) заполняет **все** поля формы, когда
`source="reg_yaml"`; `ignore_sections` — `list → ", ".join(...)`; `status` — через
`fillSelect("reg-status", fields.status)`. Для `source="vision"` — поведение как сейчас
(только распознанные поля). (Маппинг id-полей формы ↔ ключи записи — в §10.2.)

---

## 5. Персистентность папки Qdrant (замечание №10, раздел D)

### 5.1 Где хранить

Хранить override в **`.env`** ключом `QDRANT_PATH` (абсолютный путь). Обоснование:

- `.env` уже «единый общий конфиг» интерфейса, пишется/читается через существующий
  `config_ui.write_env/read_env_raw`, маскируется в UI, переносится деплоем.
- `config.yaml` — конфиг **деплоя**, менять его из UI неправильно (frozen-структура,
  правки пользователя на dev).
- Новый отдельный конфиг-файл — лишняя сущность без выгоды.

### 5.2 Чтение/запись

- `DeployConfig`: поле `qdrant_path` → **свойство**, а значение из `config.yaml` остаётся
  как `qdrant_path_default`.

```python
# deploy_config.py (frozen dataclass, поле переименовано)
qdrant_path_default: Path   # из config.yaml qdrant.path (дефолт)

@property
def qdrant_path(self) -> Path:
    override = os.environ.get("QDRANT_PATH")
    if not override:
        from firmware.src.config_ui import read_env_raw  # lazy, нет цикла импорта
        override = read_env_raw(self.env_file).get("QDRANT_PATH")
    return Path(override) if override else self.qdrant_path_default
```

Приоритет: `QDRANT_PATH` в process-env > `QDRANT_PATH` в `.env` > `config.yaml qdrant.path`.
Чтение `.env` — ленивое, без кэша (файл крошечный; доступ 1–2 раза на запрос).

- Новые endpoints в `app.py`:

```python
@app.get("/api/settings/qdrant")   # → {path, default, overridden}
@app.put("/api/settings/qdrant")   # payload {path: str}
    # валидация: абсолютный путь; пустая строка = сбросить override (удалить QDRANT_PATH)
    # запись: config_ui.write_env(cfg.env_file, {"QDRANT_PATH": path_or_empty})
```

### 5.3 Передача пайплайну и потребителям

Все существующие потребители уже берут `cfg.qdrant_path` — после превращения в свойство
ничего менять не нужно:

- `index_document` → `--qdrant-path str(cfg.qdrant_path.resolve())` (обновится сам).
- `documents_in_base` / `settings_collections` → `cfg.qdrant_path`.
- `chat_api.ChatSession` → `QAGraphConfig(qdrant_path=str(self.cfg.qdrant_path))` (новый чат
  подхватывает новый путь).

`collection` и `write_enabled` остаются из `config.yaml` (в этой итерации не редактируются).

### 5.4 Интеграция с деплоем (обязательный флаг)

Чтобы `QDRANT_PATH` переживал деплой, в `scripts/deploy.sh` + `scripts/consolidate_config.py`
добавить `config_dir/.env` (прод `/root/RAG/config/.env`, dev `config/.env`) как
**первоочередной** источник `.env`-консолидации (перед CMY/BSI). Сейчас источник
`/root/RAG/interface_RAG/.env` устарел (env_file переехал в `config_dir`) и удаляется при
очистке — без правки `QDRANT_PATH` будет теряться. Это единственное требуемое изменение в
деплой-скриптах (не в пайплайнах).

---

## 6. Чат одним окном (замечание №6, раздел E)

### 6.1 DOM (замена текущего блока)

Убрать: `#query` (textarea сверху), `#answer`, `#reply-row`, `#reply-input`, `#reply-send`.

```html
<div class="chat-layout">
  <div class="chat-main">
    <div class="page-head">… (заголовок + подзаголовок) …</div>
    <div id="chat-messages" class="chat-messages"></div>   <!-- окно истории -->
    <div class="chat-input-row">                            <!-- единственный ввод ВНИЗУ -->
      <input id="chat-input" type="text" placeholder="Введите вопрос…">
      <button id="chat-send" class="btn primary">Отправить</button>
    </div>
    <span id="chat-status" class="status-line"></span>
  </div>
  <aside class="chat-side"> … «Показать документы в базе» + #base-docs … </aside>
</div>
```

### 6.2 Состояние (app.js)

```js
state.chat = {
  awaitingClarification: false,   // true после "clarification", false после answer/error
  history: []                     // [{role: "user"|"assistant"|"clarify", text, images?, sources?}]
};
```

Один ввод, два режима:

- `chat-send` → текст непустой:
  - если `state.chat.awaitingClarification` → `ws.send({type:"reply", text})`;
  - иначе → `ws.send({type:"query", text})`.
- Ввод очищается, в историю добавляется bubble пользователя.
- `Enter` в `#chat-input` = клик по `#chat-send`.

### 6.3 Рендер входящих сообщений (ws.onmessage)

| type | Действие |
|---|---|
| `node` | `#chat-status` = «Обрабатывается узел: …» (без bubble) |
| `clarification` | добавить assistant-bubble с текстом вопроса; `awaitingClarification=true` |
| `answer` | добавить assistant-bubble: текст + `<img>` (images) + блок «Источники» (sources); `awaitingClarification=false` |
| `error` | добавить assistant-bubble с ошибкой; `awaitingClarification=false` |

Автоскролл окна истории вниз при добавлении bubble. Протокол WebSocket (`/ws/chat`) не меняется
— сервер уже различает `query`/`reply` и отдаёт `answer`/`clarification`/`node`/`error`.

---

## 7. Сайдбар настроек (замечание №8, раздел F)

### 7.1 DOM (в `#settings-root`, рендерит `settings.js`)

```html
<div class="settings-toolbar">  <!-- единая «Сохранить» / «Обновить» / «Добавить провайдера» + бейджи -->
  …
</div>
<div class="settings-layout">
  <nav class="settings-nav">
    <button data-section="providers">Провайдеры и роли</button>
    <button data-section="yandex">Yandex OCR</button>
    <button data-section="provider-keys">Ключи API провайдеров</button>
    <button data-section="other-keys">Прочие ключи</button>
    <button data-section="graph">Параметры графа</button>
    <button data-section="qdrant">Папка Qdrant</button>       <!-- новое, №10 -->
  </nav>
  <div class="settings-content">
    <!-- все секции рендерятся здесь; видима только активная -->
  </div>
</div>
```

### 7.2 Модель видимости

**Рендерятся все секции**, но видима только активная (`state.activeSection`, дефолт
`providers`). Клик по пункту меню переключает `hidden`/`.active` у карточек. Это сохраняет
единую кнопку «Сохранить» без изменений: `saveSettings()` собирает значения из **всех** секций
(включая скрытые — `querySelectorAll` читает и `hidden`-поля).

### 7.3 Разделы (маппинг на текущий код `settings.js`)

| Раздел | Источник | Заметки |
|---|---|---|
| providers | `renderProvidersCard()` | 4 role-карточки (chat/vision/embedding/rerank) |
| yandex | `renderEnvCard()` — Yandex-строки | `KNOWN_ENV` секция `yandex` |
| provider-keys | `renderEnvCard()` — provider-строки | по `api_key_env` из `providers.yaml` |
| other-keys | `renderEnvCard()` — прочие | `KNOWN_ENV` секция `other` |
| graph | `renderGraphCard()` | `search_config.yaml` узлы |
| qdrant | **новое** | input текущего пути + «сбросить к дефолту» (№10) |

### 7.4 Секция «Папка Qdrant»

- `GET /api/settings/qdrant` при `loadSettings()` (дополнить `Promise.all`).
- Поле — текущий `path`, подпись «дефолт из config.yaml» при отсутствии override,
  чекбокс/кнопка «сбросить к дефолту».
- Сохранение — в единой `saveSettings()`: если значение изменилось/сброшено →
  `PUT /api/settings/qdrant`; затем перечитать настройки (обновить `path`).

---

## 8. Статус-селект — фикс (замечание №1, раздел G)

1. **Убрать дубликат id.** `<span id="reg-status">` → `id="reg-feedback"`. В `app.js` заменить
   все `setStatus("reg-status", …)` → `setStatus("reg-feedback", …)` (в `prefillRegistration`,
   обработчике `#register`).
2. **Русские подписи, английские значения** (совместимость с пайплайном):

```html
<select id="reg-status" name="status">
  <option value="active">активный</option>
  <option value="inactive">не активный</option>
</select>
```

3. Убедиться, что `.field select` **не** получает `appearance:none` (сейчас его нет — оставить).
   Селект раскрывается нативно.

---

## 9. Мелочи (замечания №2, №4, №7, раздел H)

### 9.1 Сообщение возле кнопки (№2)

- Единственная точка сообщений регистрации — `#reg-feedback` (span в `.form-actions` рядом с
  кнопкой «Зарегистрировать документ»).
- Убрать из обработчика регистрации `setStatus("doc-status", "Регистрация завершена…")` —
  весь успех/ошибку/prefill-предупреждения регистрации показывать в `#reg-feedback`.
- `#doc-status` (область загрузки) оставить только для событий **загрузки** файла.

### 9.2 `ignore_sections` предзаполнен (№4)

`<input id="reg-ignore_sections" value="Предисловие, Содержание">` (дефолт в разметке).
Бэкенд-дефолт `["Предисловие", "Содержание"]` в `write_reg_yaml` остаётся страховкой.

### 9.3 Sticky-шапка (№7)

```css
.site-header { position: sticky; top: 0; z-index: 50; }
```

Родители (`body`/`main`) не имеют `overflow`, поэтому sticky работает. Градиентная заливка
шапки непрозрачна — контент под ней не просвечивает.

---

## 10. Схема изменений и план реализации

### 10.0 Схема изменений (данные/потоки)

```
                        ┌─────────────────────────────────────────────┐
                        │            interface_RAG (FastAPI)          │
                        │                                             │
 settings(UI) ── PUT ──►│ /api/settings/qdrant ── write_env ──► .env  │
                        │       (QDRANT_PATH)          chmod 0600      │
                        │                                             │
                        │ cfg.qdrant_path (property) ◄── read .env ───┤
                        │   ├─► index_document ──► create_index.py     │
                        │   │        --qdrant-path                     │
                        │   ├─► documents_in_base / collections        │
                        │   └─► ChatSession ──► QAGraph                │
                        │                                             │
 upload(UI) ── POST ───►│ /api/documents ── fs_perms(0666) ──► !База   │
                        │                                             │
 register(UI) ─ POST ──►│ /register ── write_reg_yaml (upsert)        │
                        │      └─ fs_perms(0666) ──► <stem>_reg.yaml   │
                        │                                             │
 prefill(UI) ─ POST ───►│ /register/prefill                           │
                        │   ├─ _reg.yaml есть? → read_reg_record       │
                        │   └─ нет → vision_prefill (OCR)              │
                        │                                             │
 convert/index(UI) ────►│ jobs.py ── Popen(preexec_fn=umask 0) ──►    │
                        │      create_markdown.py / create_index.py   │
                        │      └─ файлы 0666/0777 под !База_ГОСТ       │
                        └─────────────────────────────────────────────┘
```

### 10.1 Фаза 1 — backend (первичная)

| Файл | Изменение |
|---|---|
| `firmware/src/fs_perms.py` | **новый** — `ensure_dir`/`write_bytes`/`write_text`/`write_text_atomic`/`chmod_copy` (§3.1) |
| `firmware/src/jobs.py` | `_child_umask_zero` + `preexec_fn` в `_run`/`_run_sequence` (§3.2) |
| `firmware/src/registration.py` | `write_reg_yaml` → upsert (сохранять slug, одна запись, `fs_perms`, chmod `.bak`) (§2/§4); добавить `read_reg_record()` (§4) |
| `firmware/src/app.py` | `upload` → `fs_perms` (§3.1); `registration_prefill` → проверка `_reg.yaml` + новый контракт (§4); `GET/PUT /api/settings/qdrant` (§5.2) |
| `firmware/src/deploy_config.py` | `qdrant_path` → property + `qdrant_path_default` (§5.2) |
| `firmware/src/config_ui.py` | `write_env`/`write_yaml` → явный `chmod 0600` (+`.bak`) (§3.3) |
| `scripts/consolidate_config.py` | добавить `config_dir/.env` как первоочередной источник `.env` (§5.4) |
| `scripts/deploy.sh` | читать `config_dir/.env` вместо/до `interface_RAG/.env`; не удалять его (§5.4) |

**Тесты (Фаза 1):**
- `firmware/tests/test_registration.py` (новый) — upsert: повторная регистрация не плодит slug,
  сохраняет ключ, ровно одна запись; права `_reg.yaml` = `0666`.
- `firmware/tests/test_app.py` — `registration_prefill` возвращает поля из `_reg.yaml`
  (`source="reg_yaml"`) и не зовёт OCR; `PUT /api/settings/qdrant` пишет `QDRANT_PATH` в `.env`.
- `firmware/tests/test_deploy_config.py` — `qdrant_path` property: override из `os.environ`,
  из `.env`, fallback на default.
- `firmware/tests/test_jobs.py` — `preexec_fn` присутствует в `Popen` (мок), дочерний umask 0.
- `firmware/tests/test_config_ui.py` — после `write_env`/`write_yaml` права `0600`.
- `firmware/tests/test_perms.py` (новый) — `fs_perms` даёт `0666`/`0777`.

### 10.2 Фаза 2 — UI (после Фазы 1)

| Файл | Изменение |
|---|---|
| `firmware/src/static/index.html` | №1 — `<span id="reg-status">`→`reg-feedback`, русские подписи селекта; №2 — сообщение у кнопки; №4 — `value="Предисловие, Содержание"`; №6 — чат-блок (окно истории + ввод внизу, убрать `#query/#answer/#reply-row`); №7 — (CSS); №8 — контейнер сайдбара настроек |
| `firmware/src/static/style.css` | №7 — `.site-header{position:sticky;top:0;z-index}`; №6 — `.chat-messages`/`.chat-input-row`/bubbles; №8 — `.settings-layout`/`.settings-nav`; `.field select` оставить без `appearance:none` |
| `firmware/src/static/app.js` | №6 — переписать чат (одно окно, один ввод, `awaitingClarification`, рендер bubbles); №9 — `prefillRegistration` заполняет все поля из `reg_yaml` (маппинг ниже); №1/№2 — `setStatus("reg-feedback", …)`, убрать дубль в `doc-status` |
| `firmware/src/static/settings.js` | №8 — сайдбар-разделы + переключение; №10 — секция «Папка Qdrant» + `GET/PUT /api/settings/qdrant` в `loadSettings`/`saveSettings` |

**Маппинг полей формы ↔ ключи записи (для prefill, №9):**

```
reg-document_id↔document_id, reg-document_id_alt↔document_id_alt,
reg-document_type↔document_type, reg-domain↔domain, reg-title↔title,
reg-edition↔edition, reg-date_enacted↔date_enacted, reg-date_amended↔date_amended,
reg-amended_by↔amended_by, reg-source_file↔source_file, reg-status↔status,
reg-status_reason↔status_reason, reg-replaced_by_document_id↔replaced_by_document_id,
reg-replaced_by_doc_key↔replaced_by_doc_key, reg-ignore_sections↔ignore_sections (list→", ")
```

**Критерии приёмки UI:**
- Селект «статус» раскрывается, подписи русские, в `_reg.yaml` значение `active`/`inactive`.
- Успех/ошибка регистрации — только возле кнопки «Зарегистрировать документ».
- Повторная регистрация после правки полей не создаёт второй записи (одна запись, тот же slug).
- Повторная загрузка того же файла восстанавливает поля из `_reg.yaml` (без OCR).
- Чат — одно окно с историей, один ввод внизу (запрос и ответ на уточнение).
- Шапка закреплена при прокрутке.
- Настройки: слева меню разделов, справа выбранный раздел; «Папка Qdrant» сохраняется
  персистентно и попадает в `--qdrant-path`.

---

## 11. Риски и флаги для оркестратора

1. **Дубликат `id="reg-status"`** — корень №1; если после фикса селект всё же не раскрывается,
   перепроверить, не остался ли второй `setStatus("reg-status", …)` (массовая замена обязательна).
2. **`preexec_fn` в многопоточном FastAPI** — теоретический риск deadlock (fork+exec). Здесь
   используется только `os.umask(0)` (без локов), риск практически нулевой; план Б — argv-обёртка
   `-c "os.umask(0); os.execv(...)"` (§3.2).
3. **Deploy потеряет `QDRANT_PATH`** без правки `deploy.sh`/`consolidate_config.py` (§5.4) —
   обязательная интеграция, иначе требование №10 не выполняется на проде после первого деплоя.
4. **`--reg` (TTY) не использовать** — веб пишет `_reg.yaml` сам; поведение сохранено.
5. **Дефис vs underscore флагов** — `--providers-config` (create_markdown) vs `--providers_config`
   (create_index); `--qdrant-path` у create_index. Не менять.
6. **Qdrant-замок** — `QdrantClient` открывается/закрывается на каждую операцию (уже так), не
   держать между запросами (иначе индексация не сможет открыть базу).
7. **Слаг-стабильность** — upsert сохраняет существующий slug, чтобы не ломать `chunk_id` в
   уже проиндексированном Qdrant. Если документ переименован настолько, что нужен новый slug —
   это отдельная ручная операция (вне итерации).
8. **Права уже существующих файлов** — механизм применяется к вновь создаваемым. Для приведения
   уже лежащих под `!База_ГОСТ` файлов/каталогов к `0777/0666` — разовая команда
   `chmod -R`/`find` на проде при выкладке (в deploy.sh опционально, вне критического пути).
