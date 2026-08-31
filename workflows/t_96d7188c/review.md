# Review — унификация ключей in-process чата + дочистка .env пайплайнов (t_96d7188c)

## Verdict

**PASS**

## Scope

Ревью coder-фикса `t_a7d01588` (commit `9567023`) по архитектуре `t_6f25646e`.
Проверено по коду, живым запускам и реальному `git diff`, не по отчётам.
Ревью-ленза: execution (checkout + запуск + живой смоук, раунд 1 для этой карты).

Проверенные файлы:
- `firmware/src/chat_api.py` (единственный runtime-файл)
- `scripts/deploy.sh`, `scripts/consolidate_config.py`
- `firmware/tests/test_chat_api.py`, `firmware/tests/test_deploy_config.py`
- смежные (для верификации утверждений): `firmware/src/jobs.py`, `firmware/src/config_ui.py`,
  `firmware/src/deploy_config.py`, `Build_Search_index/firmware/src/llm_providers.py`

## Requirements (приёмочные пункты задачи)

| # | Требование | Вердикт | Доказательство |
|---|---|---|---|
| 1 | Инъекция `config/.env` в `os.environ` ДО первого импорта пайплайна и ДО `load_dotenv()` в `get_api_key` | PASS | `chat_api.py:52` `_inject_env_file(self.cfg.env_file)` стоит перед `from qa_graph import ...` (`:54`); модульные импорты (`:10-19`) тянут только `deploy_config`+`config_ui`; `llm_providers.get_api_key` вызывает `load_dotenv()` лениво (`:93`), не при импорте |
| 2 | После инъекции резолв видит ключи `config/.env`, в т.ч. `ANYMODEL_API_KEY`, `AITUNNEL_API_KEY`, `Z_AI_API_KEY`; семантика override; `load_dotenv` не перетирает | PASS | Живой смоук: все 8 ключей `config/.env` в `os.environ`; override победил «загрязнение»; `load_dotenv(Build_Search_index/.env)` (setdefault) перетёр **0** ключей. `AITUNNEL_API_KEY` в dev `config/.env` отсутствует (8 ключей), но подтверждён живым dry-run в прод `remote/config/.env` (11 ключей в консолидации, ключ сохраняется неразрушающим merge); инъекция ключ-агностична |
| 3 | QDRANT_PATH-фикс не сломан: `cfg.qdrant_path` читает ТОЛЬКО `.env`, не process-env | PASS | Живой смоук: пайплайновый `load_dotenv` загрязнил `os.environ["QDRANT_PATH"]=/mnt/sdb/...`, но `cfg.qdrant_path_override=None`, `cfg.qdrant_path=uploads/qdrant_data` (дефолт интерфейса). Резолвер `deploy_config.qdrant_path_override` читает только `read_env_raw(self.env_file)` |
| 4 | `deploy.sh`/`consolidate_config.py`: чистка корневых `.env` корректна; источники консолидации = 2 (`config/.env` первый/полный); `bash -n` OK; dry-run не падает | PASS | `deploy.sh` шаг 1: бэкап `.env.root` для CMY/BSI; шаг 4: `rm -f` корневых `.env` + комментарий о `request_bot.py`. `consolidate_config.py`: `env_sources` = `remote/config/.env` → `dev config/.env` (5 legacy удалены). `bash -n` OK; `--dry-run` exit 0, план корректен |
| 5 | Границы: пайплайны не изменены; `.env` 0600; нет секретов; `.hermes/STATE.md` не тронут | PASS | `git show --stat 9567023` = ровно 5 файлов (ни одного в `Build_Search_index/**`/`Create_Markdown_YA/**`). `config/.env` = 600. Эвристический скан diff на секреты — пусто; `chat_api.py` прочитан целиком — секретов нет. `STATE.md` последний раз менялся в `046c4fb` (не в этом фиксе) |
| 6 | `pytest firmware/tests -q` зелёный; новые тесты осмысленные | PASS | **69 passed**, 1 warning (StarletteDeprecation, не связан). Новые: 3 теста инъекции + 1 QDRANT-регрессия — проверяют поведение, не снапшотят константы |

## Отдельно: судьба `request_bot.py`

PASS. Явное решение зафиксировано в трёх местах:
- `deploy.sh:201-204` — комментарий при cleanup: бот не разворачивается (исключён из rsync,
  `--exclude='request_bot.py'` на `:161`), не запущен; при будущем запуске — systemd
  `Environment=`/`EnvironmentFile=` (отдельная задача).
- архитектура `t_6f25646e` §3.2 и §7.
- implementation-report §5.2.

Чистка не сломает бота молча: бот уже декоммишн (нет процесса/юнита, исключён из rsync),
а бэкап `.env.root` делает удаление обратимым.

## Findings

Блокирующих замечаний нет. Некритичные наблюдения (не влияют на PASS):

1. **LOW** — `AITUNNEL_API_KEY` отсутствует в dev `config/.env` (только прод). Инъекция
   ключ-агностична, поэтому на проде ключ попадёт в чат корректно; на dev fallback-роль
   `aitunnel` просто не сработает (в dev `providers.yaml` провайдера нет). Зафиксировано
   оператором/оркестратором, вне объёма фикса.
2. **LOW** — прод `remote/config/.env` всё ещё содержит `QDRANT_PATH` и `TELEGRAM_ALLOWED_USERS`;
   неразрушающий merge их сохраняет. Очистка `QDRANT_PATH` внутри `config/.env` — вне объёма
   (отдельное решение), удаление корневых `.env` пайплайнов выполнит оркестратор `--apply`.
3. **INFO** — гранулярность свежести ключей = «новая WebSocket-сессия»; уже открытая сессия
   держит старый ключ до переподключения. Симметрично subprocess-job, приемлемо.

## Tests executed (реальные прогоны)

| Команда | Результат |
|---|---|
| `python3 -m pytest firmware/tests -q` | 69 passed |
| `bash -n scripts/deploy.sh` | OK |
| `scripts/deploy.sh --dry-run` | exit 0; план: бэкап `.env.root` → консолидация 2 источника → rsync → cleanup → рестарт → smoke |
| `python3 scripts/consolidate_config.py --out <tmp>` (dev) | `env sources: dev config/.env`, 8 ключей |
| live dry-run с LXC | `env sources: remote/config/.env, dev config/.env`; 11 ключей (вкл. `AITUNNEL_API_KEY` из первого источника) |
| Живой смоук инъекции (`/tmp/smoke_inject.py`) | все 8 ключей в env; override победил; `load_dotenv` перетёр 0 ключей; QDRANT резолвер игнорирует process-env |

## Risks

- Потеря `TELEGRAM_ALLOWED_USERS` при удалении корневого `Build_Search_index/.env` на проде —
  обратима через бэкап `.env.root`; бот-only, декоммишн. Осознанно (арх. §3.3).
- Реальный `rm` на проде выполняется оркестратором через `deploy.sh --apply`, не в этом фиксе.

## Notes

Реализация дословно соответствует архитектуре §5.1–5.4. Отклонений нет. Семантика инъекции
(`os.environ.update(read_env_raw(env_file))`) идентична `jobs.build_env` — проверено по коду обоих.
