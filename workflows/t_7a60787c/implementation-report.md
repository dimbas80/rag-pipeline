# Implementation report — t_7a60787c

## Задача
Дочистить cleanup в `scripts/deploy.sh`: он не удалял часть не-runtime каталогов
(решение 35 недовыполнено). Добавить единый `find` по трём корням, удаляющий
`tests`, `docs`, `.worktrees`, `.pytest_cache`, `workflows`, `.hermes`, `include`, `lib`.

## Что сделано
Изменён только `scripts/deploy.sh` (cleanup-блок heredoc, этап 4).

1. **Единый `find`** по `/root/RAG/interface_RAG`, `/root/RAG/Create_Markdown_YA`,
   `/root/RAG/Build_Search_index`, рекурсивно удаляющий каталоги:
   `tests`, `docs`, `.worktrees`, `.pytest_cache`, `workflows`, `.hermes`, `include`, `lib`:
   ```bash
   find /root/RAG/interface_RAG /root/RAG/Create_Markdown_YA /root/RAG/Build_Search_index \
        -type d \
        \( -name tests -o -name docs -o -name .worktrees -o -name .pytest_cache \
           -o -name workflows -o -name .hermes -o -name include -o -name lib \) \
        -prune -exec rm -rf {} + 2>/dev/null || true
   ```
2. **Убрано дублирование**: из явных `rm -rf` удалены теперь покрытые find-ом пути
   (interface_RAG `docs`/`tests`/`.pytest_cache`/`workflows`/`.hermes`/`firmware/include`/`firmware/lib`;
   BSI `.worktrees`). Сохранены BSI `example`/`snapshots` и все удаления `.env`/конфигов/`tmp`/`bot.log*`.
3. **`__pycache__` не тронут**: отдельный глубокий `find -type d -name __pycache__` и
   явные `rm -rf .../__pycache__` остались как были (runtime-артефакт, возвращается после рестарта).
4. **dry-run**: find-чистка идёт через существующий `run_remote_script`/heredoc — в dry-run
   не исполняется, только план. Описание `run_remote_script` дополнено списком удаляемых
   каталогов, чтобы они были видны в плане.

Не тронуты: rsync-манифест, симлинки (этап 4 выше), блок 5.5 бота, консолидация, бэкап.

## Проверка
- `bash -n scripts/deploy.sh` → OK (exit 0).
- `./scripts/deploy.sh --dry-run` → exit 0; в плане этапа 4 видно:
  `rm -rf tests/docs/.worktrees/.pytest_cache/workflows/.hermes/include/lib рекурсивно
  в interface_RAG + Create_Markdown_YA + Build_Search_index; + example/snapshots/tmp/__pycache__`.
- Логика find-выражения проверена локально на `/usr/bin/find` (тестовая структура):
  удаляет вложенные `tests`/`docs`/`include`/`lib`/`.worktrees`/`.pytest_cache`/`workflows`/`.hermes`,
  не трогает файлы вида `include.txt` и родительские каталоги; `-prune` корректно не спускается
  внутрь удаляемых каталогов.
- Экранирование `\( ... \)` внутри не-экранированного heredoc подтверждено (бэкслеши перед
  скобками сохраняются — как и у уже работавшей строки `find ... \( -name '*.gitkeep' ... \)`).

## Ограничения
- Реальный `--apply` не запускался (запрещено задачей; деплой — прерогатива оркестратора).
- Локальный wrapper `rtk` перехватывает `find ... -exec rm -rf` в интерактивной оболочке;
  на удалённом LXC используется штатный GNU `/usr/bin/find`, поэтому на прод поведение корректно.
