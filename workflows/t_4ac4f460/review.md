# Review: дочистка cleanup deploy.sh (coder-фикс t_7a60787c)

## Verdict

PASS

## Scope

Проверен coder-фикс cleanup-блока в `scripts/deploy.sh` (commit 39d7295), который
заменяет явные `rm -rf` dev-каталогов на единый рекурсивный `find ... -prune -exec rm -rf {} +`
по трём прод-корням. Ревью проведено независимо по коду (git show) и живым проверкам
(`bash -n`, `--dry-run`, read-only SSH-инспекция прод-LXC 192.0.2.21), не по отчёту кодера.

## Requirements (answer key)

| # | Требование | Статус | Доказательство |
|---|---|---|---|
| 1 | cleanup удаляет `tests/`, `docs/`, `.worktrees/`, `.pytest_cache/`, `workflows/`, `.hermes/`, `firmware/include/`, `firmware/lib/` рекурсивно для трёх проектов | PASS | `find` (строки 224–228) по корням interface_RAG/Create_Markdown_YA/Build_Search_index, `-name tests -o -name docs -o -name .worktrees -o -name .pytest_cache -o -name workflows -o -name .hermes -o -name include -o -name lib` |
| 2 | единый `find -type d -name X -prune -exec rm -rf {} +` | PASS | ровно одна `find`-конструкция с `-prune -exec rm -rf {} +` (строки 224–228) |
| 3 | dry-run НЕ исполняет чистку (heredoc/run_remote_script) | PASS | `run_remote_script` в dry-run: `plan "$desc"; cat >/dev/null` (строки 80–82). В `--dry-run` виден только `[план]`, без выполнения |
| 4 | `__pycache__`-чистка не сломана | PASS | отдельный `find -type d -name __pycache__ -prune -exec rm -rf {} +` (230–231) + явные `rm -rf .../__pycache__` сохранены; `__pycache__` сознательно исключён из «не-runtime» find (комментарий 223) |
| 5 | rsync-манифест не сломан | PASS | блок 3 (155–187) не изменён |
| 6 | симлинки не сломаны | PASS | `ln -sfn` providers.yaml/search_config.yaml (191–194) не изменены |
| 7 | блок 5.5 бота не сломан | PASS | блок 5.5 (242–256) не изменён |
| 8 | `git diff --stat` = только deploy.sh | PASS | `1 file changed, 11 insertions(+), 7 deletions(-)` |

## Проверки (выполнены самостоятельно)

1. `bash -n scripts/deploy.sh` — exit 0 (синтаксис OK).
2. `./scripts/deploy.sh --dry-run` — exit 0; план показывает блок 4 «очистка dev-артефактов…»
   с удалением tests/docs/.worktrees/.pytest_cache/workflows/.hermes/include/lib для трёх проектов,
   без ошибок; блоки 3/4/5/5.5/6 на месте.
3. `git show 39d7295` — изменения только в cleanup-heredoc (11 вставок, 7 удалений); нет дублирования
   явных `rm -rf` (удалены строки с явным `rm -rf docs/tests/.pytest_cache/workflows/.hermes/firmware/include/firmware/lib`).
4. Read-only SSH на прод-LXC 192.0.2.21:
   - три корня существуют;
   - `find ... -name include -o -name lib` на прод НЕ находит ни одного каталога include/lib —
     нет перекрытия с runtime (эти каталоги уже вычищены предыдущими деплоями);
   - нет `venv`/`site-packages`/`node_modules` под корнями; общий venv лежит вне корней (`/root/RAG/venv`);
   - `__pycache__` под корнями есть (interface_RAG, BSI) — их уберёт отдельный `__pycache__`-find.

## Findings

- LOW (не блокирует): `-name include` / `-name lib` в едином find не привязаны к `firmware/`
  (не `-path '*/firmware/include'`). Сегодня на dev и prod это совпадает с `firmware/include`/`firmware/lib`
  (пустые `.gitkeep`-маркеры, не runtime), а rsync-манифест (блок 3) никогда не пушит include/lib,
  поэтому фактического перекрытия нет. Но если в будущем под один из корней попадёт runtime-каталог
  `lib`/`include` (например, вложенный venv или вендор-библиотека), cleanup его молча удалит.
  Отмечено как риск, требование answer key (`find -type d -name X`) при этом выполнено.

## Risks

- Латентное расширение матча `-name include`/`-name lib` при появлении runtime-каталога с таким именем
  под прод-корнями (см. Finding LOW). Смягчается тем, что блок 3 rsync их не доставляет.

## Notes

- `set -e` в heredoc не ломается: `find ... 2>/dev/null || true` явно маскирует exit-code.
- `-prune` не спускается внутрь удаляемых каталогов, поэтому `-exec rm -rf {} +` не трогает
  родителей и не вызывает гонок обхода; вложенные совпадения вычищаются одним `rm -rf`.
- `--apply` не запускался (деплой — после ревью, оркестратор); проверка выполнена read-only.
