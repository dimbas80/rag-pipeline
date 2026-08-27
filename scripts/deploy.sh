#!/usr/bin/env bash
#
# Deploy interface_RAG на LXC 192.0.2.21 («чистый прод», решения 18–19).
#
# По умолчанию — dry-run: печатает полный план (бэкап + консолидация +
# rsync-манифест + симлинк + очистка + рестарт + smoke), ничего не меняет.
# Реальный деплой:  scripts/deploy.sh --apply
#
# Требования на машине запуска: sshpass, rsync, python3 (с PyYAML).
# Доступ к LXC: root/root (LAN, решение №8).
#
# Примеры:
#   scripts/deploy.sh            # dry-run
#   scripts/deploy.sh --apply    # реальный деплой (после ревью, оркестратор)
set -euo pipefail

MODE="dry-run"
for arg in "$@"; do
  case "$arg" in
    --apply) MODE="apply" ;;
    --dry-run) MODE="dry-run" ;;
    -h|--help) sed -n '1,20p' "$0"; exit 0 ;;
    *) echo "Неизвестный аргумент: $arg (допустимо: --dry-run | --apply)" >&2; exit 2 ;;
  esac
done

# --- Константы (архитектура §4.10, §8.1) ---
LXC=192.0.2.21
LXC_USER=root
LXC_PASS="${LXC_PASS:-root}"
LXC_ROOT=/root/RAG
CONFIG_DIR=/root/RAG/config
SERVICE=interface-rag.service

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_ROOT="$(dirname "$SCRIPT_DIR")"
DEV_CMY="${DEV_CMY:-/root/projects/Create_Markdown_YA}"
DEV_BSI="${DEV_BSI:-/root/projects/Build_Search_index}"

STAGE="$(mktemp -d /tmp/interface-rag-deploy.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT

SSH_BASE=(sshpass -p "$LXC_PASS" ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10)
RSYNC_E="sshpass -p $LXC_PASS ssh -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"

say()  { printf '\n== %s ==\n' "$*"; }
plan() { printf '  [план] %s\n' "$*"; }
step() { printf '  [шаг]  %s\n' "$*"; }

ssh_lxc() { "${SSH_BASE[@]}" "$LXC_USER@$LXC" "$@"; }
remote_ok() { ssh_lxc "true" 2>/dev/null; }

run_local() {
  local desc="$1"; shift
  if [[ "$MODE" == "apply" ]]; then
    echo "  [выполнено] $desc"
    "$@"
  else
    plan "$desc"
  fi
}

run_remote() {
  local desc="$1"; shift
  if [[ "$MODE" == "apply" ]]; then
    echo "  [выполнено] $desc"
    ssh_lxc "$@"
  else
    plan "$desc"
  fi
}

# Принимает heredoc-скрипт на stdin и исполняет его на LXC (apply) или показывает план.
run_remote_script() {
  local desc="$1"
  if [[ "$MODE" == "apply" ]]; then
    echo "  [выполнено] $desc"
    ssh_lxc bash -s
  else
    plan "$desc"
    cat >/dev/null
  fi
}

echo "Deploy interface_RAG → LXC $LXC ($MODE)"
echo "  DEV_ROOT=$DEV_ROOT, DEV_CMY=$DEV_CMY, DEV_BSI=$DEV_BSI"
echo "  CONFIG_DIR=$CONFIG_DIR (remote)"

# --- 0. Доступность LXC (dry-run терпит недоступность; apply — нет) ---
say "0. Проверка доступности LXC"
REMOTE_AVAILABLE=0
if remote_ok; then
  echo "  LXC доступен (ssh $LXC_USER@$LXC)"
  REMOTE_AVAILABLE=1
else
  echo "  LXC НЕ доступен — dry-run строит план по локальным данным (read-only с LXC пропущены)"
  if [[ "$MODE" == "apply" ]]; then
    echo "  Ошибка: для --apply требуется доступ к LXC" >&2
    exit 1
  fi
fi

# Read-only выгрузка текущих конфигов LXC в staging (для консолидации/плана).
if [[ "$REMOTE_AVAILABLE" == "1" ]]; then
  mkdir -p "$STAGE/remote/config" "$STAGE/remote/interface_RAG" "$STAGE/remote/CMY" "$STAGE/remote/BSI"
  while IFS='|' read -r src dst; do
    # NB: < /dev/null — иначе ssh съедает остаток stdin heredoc цикла
    ssh_lxc "cat '$src' 2>/dev/null" < /dev/null > "$dst" || true
  done <<FILES
/root/RAG/config/.env|$STAGE/remote/config/.env
/root/RAG/interface_RAG/.env|$STAGE/remote/interface_RAG/.env
/root/RAG/Create_Markdown_YA/firmware/src/.env|$STAGE/remote/CMY/.env
/root/RAG/Create_Markdown_YA/firmware/src/providers.yaml|$STAGE/remote/CMY/providers.yaml
/root/RAG/Build_Search_index/firmware/src/.env|$STAGE/remote/BSI/.env
/root/RAG/Build_Search_index/firmware/src/providers.yaml|$STAGE/remote/BSI/providers.yaml
FILES
fi

# --- 1. Бэкап текущих конфигов прода ---
say "1. Бэкап конфигов прода (копия перед изменением)"
TS="$(date +%s)"
cat <<REMOTE | run_remote_script "бэкап в $CONFIG_DIR.bak.$TS (config/ + interface_RAG/.env + CMY/.env+providers.yaml + BSI/.env+providers.yaml)"
set -e
TS="$TS"
BK="$CONFIG_DIR.bak.\$TS"
mkdir -p "\$BK/config" "\$BK/interface_RAG" "\$BK/CMY" "\$BK/BSI"
if [ -d "$CONFIG_DIR" ]; then cp -a "$CONFIG_DIR/." "\$BK/config/"; fi
for pair in \
    "interface_RAG:/root/RAG/interface_RAG/.env:.env" \
    "CMY:/root/RAG/Create_Markdown_YA/firmware/src/.env:.env" \
    "CMY:/root/RAG/Create_Markdown_YA/firmware/src/providers.yaml:providers.yaml" \
    "BSI:/root/RAG/Build_Search_index/firmware/src/.env:.env" \
    "BSI:/root/RAG/Build_Search_index/firmware/src/providers.yaml:providers.yaml"; do
  sub="\${pair%%:*}"; rest="\${pair#*:}"; src="\${rest%%:*}"; name="\${rest##*:}"
  if [ -f "\$src" ]; then cp -a "\$src" "\$BK/\$sub/\$name"; fi
done
echo "BACKUP_DIR=\$BK"
REMOTE

# --- 2. Консолидация конфигов в staging (выполняется всегда; в apply — пуш на LXC) ---
say "2. Консолидация общих конфигов в $CONFIG_DIR"
CONSOLIDATE_ARGS=(--out "$STAGE/config" --dev-cmy "$DEV_CMY" --dev-bsi "$DEV_BSI")
if [[ "$REMOTE_AVAILABLE" == "1" ]]; then
  CONSOLIDATE_ARGS+=(--remote "$STAGE/remote")
fi
python3 "$SCRIPT_DIR/consolidate_config.py" "${CONSOLIDATE_ARGS[@]}"

run_remote "создать $CONFIG_DIR на LXC" "mkdir -p $CONFIG_DIR"
if [[ "$MODE" == "apply" ]]; then
  rsync -az -e "$RSYNC_E" "$STAGE/config/" "root@$LXC:$CONFIG_DIR/"
  echo "  [выполнено] push staging → $CONFIG_DIR"
else
  plan "push staging → $CONFIG_DIR (providers.yaml, create_markdown_config.yaml, search_config.yaml, .env)"
fi

# --- 3. rsync-манифест «чистого» прода (архитектура §8.3) ---
say "3. rsync-манифест (только нужные файлы)"
EXCLUDES=(
  --exclude='.git/' --exclude='docs/' --exclude='workflows/' --exclude='.hermes/'
  --exclude='tests/' --exclude='.pytest_cache/' --exclude='README*'
  --exclude='__pycache__/' --exclude='*.pyc' --exclude='tmp/'
  --exclude='bot.log*' --exclude='request_bot.py' --exclude='.env'
)

run_local "rsync interface_RAG: config.yaml → /root/RAG/interface_RAG/" \
  rsync -az -e "$RSYNC_E" "${EXCLUDES[@]}" "$DEV_ROOT/config.yaml" "root@$LXC:/root/RAG/interface_RAG/"
run_local "rsync interface_RAG: firmware/src/ → /root/RAG/interface_RAG/firmware/" \
  rsync -az -e "$RSYNC_E" "${EXCLUDES[@]}" "$DEV_ROOT/firmware/src" "root@$LXC:/root/RAG/interface_RAG/firmware/"
run_local "rsync Create_Markdown_YA: create_markdown.py → /root/RAG/Create_Markdown_YA/firmware/src/" \
  rsync -az -e "$RSYNC_E" "$DEV_CMY/firmware/src/create_markdown.py" "root@$LXC:/root/RAG/Create_Markdown_YA/firmware/src/"
run_local "rsync Build_Search_index: create_index/qa_graph/search/llm_providers → /root/RAG/Build_Search_index/firmware/src/" \
  rsync -az -e "$RSYNC_E" \
    "$DEV_BSI/firmware/src/create_index.py" \
    "$DEV_BSI/firmware/src/qa_graph.py" \
    "$DEV_BSI/firmware/src/search.py" \
    "$DEV_BSI/firmware/src/llm_providers.py" \
    "root@$LXC:/root/RAG/Build_Search_index/firmware/src/"
run_local "rsync Build_Search_index: telegram_bot/asset_helpers.py → /root/RAG/Build_Search_index/firmware/src/telegram_bot/" \
  rsync -az -e "$RSYNC_E" "$DEV_BSI/firmware/src/telegram_bot/asset_helpers.py" "root@$LXC:/root/RAG/Build_Search_index/firmware/src/telegram_bot/"

# --- 4. Симлинк импорт-тайм providers.yaml + очистка устаревших копий конфигов ---
say "4. Симлинк providers.yaml (импорт-тайм BSI) + очистка устаревших копий"
run_remote "симлинк $CONFIG_DIR/providers.yaml → Build_Search_index/firmware/src/providers.yaml" \
  "ln -sfn $CONFIG_DIR/providers.yaml /root/RAG/Build_Search_index/firmware/src/providers.yaml"

cat <<REMOTE | run_remote_script "очистка dev-артефактов и старых копий конфигов (после бэкапа)"
set -e
# interface_RAG: устаревшие копии конфигов и dev-артефакты
rm -f  /root/RAG/interface_RAG/.env
rm -rf /root/RAG/interface_RAG/docs /root/RAG/interface_RAG/tests \
       /root/RAG/interface_RAG/.pytest_cache /root/RAG/interface_RAG/workflows \
       /root/RAG/interface_RAG/.hermes /root/RAG/interface_RAG/.gitignore \
       /root/RAG/interface_RAG/README.md \
       /root/RAG/interface_RAG/firmware/src/__pycache__
# Create_Markdown_YA: у пайплайна нет своих копий конфигов (всё через CLI-ключи)
rm -f  /root/RAG/Create_Markdown_YA/firmware/src/.env \
       /root/RAG/Create_Markdown_YA/firmware/src/providers.yaml
rm -rf /root/RAG/Create_Markdown_YA/firmware/src/tmp \
       /root/RAG/Create_Markdown_YA/firmware/src/__pycache__
# Build_Search_index: остаётся симлинк providers.yaml; удаляем мусор
rm -f  /root/RAG/Build_Search_index/firmware/src/.env
rm -rf /root/RAG/Build_Search_index/firmware/src/__pycache__ \
       /root/RAG/Build_Search_index/firmware/src/telegram_bot/__pycache__
rm -f  /root/RAG/Build_Search_index/firmware/src/telegram_bot/bot.log*
echo "CLEANED"
REMOTE

# --- 5. Рестарт systemd ---
say "5. Рестарт systemd $SERVICE"
run_remote "systemctl restart $SERVICE" "systemctl restart $SERVICE"
run_remote "проверка is-active $SERVICE" "systemctl is-active $SERVICE"

# --- 6. Smoke-тест (цикл ожидания готовности порта) ---
# uvicorn может стартовать дольше, чем systemd показывает is-active → ждём HTTP 200.
say "6. Smoke: ожидание HTTP 200 на http://127.0.0.1/ (до 10 попыток, пауза 2с)"
if [[ "$MODE" == "apply" ]]; then
  SMOKE_ATTEMPTS=10
  SMOKE_INTERVAL=2
  SMOKE_CODE=""
  for i in $(seq 1 "$SMOKE_ATTEMPTS"); do
    SMOKE_CODE="$(ssh_lxc "curl -sf http://127.0.0.1/ -o /dev/null -w '%{http_code}'" || echo FAIL)"
    echo "  [шаг]  smoke: попытка $i/$SMOKE_ATTEMPTS → HTTP $SMOKE_CODE"
    if [[ "$SMOKE_CODE" == "200" ]]; then
      echo "  [выполнено] smoke: HTTP $SMOKE_CODE (сервис готов)"
      break
    fi
    if [[ "$i" -lt "$SMOKE_ATTEMPTS" ]]; then
      sleep "$SMOKE_INTERVAL"
    fi
  done
  if [[ "$SMOKE_CODE" != "200" ]]; then
    echo "  Ошибка: smoke не прошёл после $SMOKE_ATTEMPTS попыток (последний ответ: HTTP $SMOKE_CODE)" >&2
    exit 1
  fi
else
  plan "цикл ожидания готовности: до 10 попыток \"curl -sf http://127.0.0.1/ -o /dev/null -w '%{http_code}'\" с паузой 2с, выход при HTTP 200 (в dry-run реальные запросы не выполняются)"
fi

say "Готово ($MODE)"
if [[ "$MODE" == "dry-run" ]]; then
  echo "  Для реального деплоя (после ревью): scripts/deploy.sh --apply"
fi
