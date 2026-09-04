#!/usr/bin/env bash
#
# Установка rag-pipeline на чистую машину/контейнер (Debian/Ubuntu, systemd).
#
# Ставит системные пакеты, собирает venv из requirements всех трёх компонентов,
# разворачивает прод-макет в каталог установки:
#   <install>/{interface_RAG,Create_Markdown_YA,Build_Search_index}  — код
#   <install>/config/                                                — общий конфиг
#   <install>/venv/                                                  — зависимости
# регистрирует systemd-юниты (веб-интерфейс + telegram-бот, автозапуск)
# и печатает адрес веб-интерфейса.
#
# Секреты НЕ спрашивает: в config/.env создаются плейсхолдеры, ключи и
# корень документов (BASE_DIR) заполняются позже через веб-интерфейс
# (Настройки → Папки / Телеграм).
#
# Примеры:
#   scripts/install.sh                              # интерактивный диалог
#   scripts/install.sh --host 0.0.0.0 --port 8081   # без вопросов
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

say()  { printf '\n== %s ==\n' "$*"; }
step() { printf '  [шаг]  %s\n' "$*"; }
die()  { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

# --- Параметры: флаги → значения по умолчанию ---
INSTALL_DIR="/opt/rag-pipeline"
BASE_DIR="/mnt/sdb/!База_ГОСТ"
BIND_HOST="127.0.0.1"
PORT="8081"
while [ $# -gt 0 ]; do
  case "$1" in
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    --base-dir)    BASE_DIR="$2"; shift 2 ;;
    --host)        BIND_HOST="$2"; shift 2 ;;
    --port)        PORT="$2"; shift 2 ;;
    -h|--help)     sed -n '1,22p' "$0"; exit 0 ;;
    *) die "Неизвестный аргумент: $1 (см. --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "запустите от root (sudo)"

# --- Диалог (если флаги не заданы явно и есть TTY) ---
prompt() { # prompt <имя> <текущее> <подсказка> [проверка]
  local value="$2"
  if [ -t 0 ]; then
    read -r -e -p "$3 [$value]: " value || value="$2"
    [ -n "$value" ] || value="$2"
  fi
  printf '%s' "$value"
}

say "Параметры установки"
if [ -t 0 ]; then
  INSTALL_DIR="$(prompt install "$INSTALL_DIR" "Каталог установки")"
  BASE_DIR="$(prompt base "$BASE_DIR" "Корневая папка документов (BASE_DIR)")"
  BIND_HOST="$(prompt host "$BIND_HOST" "Веб-интерфейс слушать: 127.0.0.1 (только локально) или 0.0.0.0 (все адреса)")"
  PORT="$(prompt port "$PORT" "Порт веб-интерфейса")"
fi
case "$BIND_HOST" in 127.0.0.1|0.0.0.0) ;; *) die "--host: только 127.0.0.1 или 0.0.0.0" ;; esac
case "$PORT" in ''|*[!0-9]*) die "--port: число" ;; esac
[ "$(echo "$INSTALL_DIR" | cut -c1)" = "/" ] || die "каталог установки должен быть абсолютным путём"
[ "$(echo "$BASE_DIR" | cut -c1)" = "/" ] || die "BASE_DIR должен быть абсолютным путём"
printf '  каталог: %s\n  корень документов: %s\n  веб-интерфейс: http://%s:%s/\n' \
  "$INSTALL_DIR" "$BASE_DIR" "$BIND_HOST" "$PORT"

# --- 1. Системные зависимости ---
say "1. Системные пакеты"
MISSING=()
for cmd in python3 pip3 rsync curl; do
  command -v "$cmd" >/dev/null 2>&1 || MISSING+=("$cmd")
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  step "apt-get install: ${MISSING[*]}"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip rsync curl >/dev/null
else
  step "все на месте (python3, pip3, rsync, curl)"
fi

# --- 2. Код в прод-макет ---
say "2. Код → $INSTALL_DIR"
mkdir -p "$INSTALL_DIR/config"
EXCLUDES=(
  --exclude='.git/' --exclude='docs/' --exclude='workflows/' --exclude='.hermes/'
  --exclude='tests/' --exclude='.pytest_cache/' --exclude='README*'
  --exclude='__pycache__/' --exclude='*.pyc' --exclude='tmp/'
  --exclude='bot.log*' --exclude='.env'
)
step "rsync interface_rag → interface_RAG"
rsync -a "${EXCLUDES[@]}" "$REPO_ROOT/interface_rag/" "$INSTALL_DIR/interface_RAG/"
step "rsync create_markdown → Create_Markdown_YA"
rsync -a "${EXCLUDES[@]}" "$REPO_ROOT/create_markdown/" "$INSTALL_DIR/Create_Markdown_YA/"
step "rsync build_search_index → Build_Search_index"
rsync -a "${EXCLUDES[@]}" "$REPO_ROOT/build_search_index/" "$INSTALL_DIR/Build_Search_index/"

# --- 3. venv + зависимости всех трёх компонентов ---
say "3. venv и зависимости"
[ -d "$INSTALL_DIR/venv" ] || python3 -m venv "$INSTALL_DIR/venv"
step "pip install -r requirements (create_markdown, build_search_index, interface_rag)"
"$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install -q \
  -r "$INSTALL_DIR/Create_Markdown_YA/firmware/src/requirements.txt" \
  -r "$INSTALL_DIR/Build_Search_index/firmware/src/requirements.txt" \
  -r "$INSTALL_DIR/interface_RAG/firmware/src/requirements.txt"

# --- 4. Общий конфиг ---
say "4. Конфиги → $INSTALL_DIR/config"
step "providers.yaml, create_markdown_config.yaml, search_config.yaml из репозитория"
install -m 600 "$REPO_ROOT/create_markdown/firmware/src/providers.yaml" "$INSTALL_DIR/config/providers.yaml"
install -m 600 "$REPO_ROOT/create_markdown/firmware/src/create_markdown_config.yaml" "$INSTALL_DIR/config/create_markdown_config.yaml"
install -m 600 "$REPO_ROOT/build_search_index/firmware/src/search_config.yaml" "$INSTALL_DIR/config/search_config.yaml"
# Импорт-тайм Build_Search_index читает search_config.yaml/providers.yaml рядом с кодом
# → симлинки на общий конфиг (как в deploy.sh).
ln -sfn "$INSTALL_DIR/config/search_config.yaml" "$INSTALL_DIR/Build_Search_index/firmware/src/search_config.yaml"
ln -sfn "$INSTALL_DIR/config/providers.yaml" "$INSTALL_DIR/Build_Search_index/firmware/src/providers.yaml"
# Локальные копии Create_Markdown_YA не нужны: интерфейс передаёт конфиги через argv.
rm -f "$INSTALL_DIR/Create_Markdown_YA/firmware/src/providers.yaml"

step "interface_RAG/config.yaml (среда prod: $BIND_HOST:$PORT, BASE_DIR=$BASE_DIR)"
"$INSTALL_DIR/venv/bin/python" - "$REPO_ROOT/interface_rag/config.yaml" \
  "$INSTALL_DIR/interface_RAG/config.yaml" "$INSTALL_DIR" "$BASE_DIR" "$BIND_HOST" "$PORT" <<'PYEOF'
import sys, yaml
src, dst, install, base, host, port = sys.argv[1:7]
raw = yaml.safe_load(open(src, encoding="utf-8")) or {}
prod = {
    "host": host, "port": int(port),
    "config_dir": f"{install}/config",
    "upload": {"base_dir": base, "max_mb": 500},
    "pipelines": {
        "create_markdown_dir": f"{install}/Create_Markdown_YA/firmware/src",
        "build_search_index_dir": f"{install}/Build_Search_index/firmware/src",
    },
    "qdrant": {"path": f"{base}/Markdown/qdrant_data",
               "collection": "technical_standard", "write_enabled": True},
    "base_markdown": f"{base}/Markdown",
}
out = {"active": "prod", "prod": prod,
       "prompts": raw.get("prompts", {}), "model_tags": raw.get("model_tags", {})}
with open(dst, "w", encoding="utf-8") as f:
    f.write("# Сгенерировано scripts/install.sh — одна среда «prod».\n")
    yaml.safe_dump(out, f, allow_unicode=True, sort_keys=False)
PYEOF

step "config/.env — плейсхолдеры (заполните через веб-интерфейс)"
if [ ! -f "$INSTALL_DIR/config/.env" ]; then
  cat > "$INSTALL_DIR/config/.env" <<ENVEOF
# Общий .env: читается веб-интерфейсом, ботом (EnvironmentFile) и пайплайнами.
# Заполняйте ключи через веб-интерфейс (Настройки) — они сохранятся сюда.
# BASE_DIR — корневая папка документов (Настройки → Папки).
BASE_DIR=$BASE_DIR
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=
YANDEX_API_KEY=
YANDEX_FOLDER_ID=
DEEPSEEK_API_KEY=
PROVOD_API_KEY=
ENVEOF
fi
chmod 600 "$INSTALL_DIR/config/.env"

# --- 5. systemd: веб-интерфейс + бот ---
say "5. systemd-юниты"
step "установка interface-rag.service и interface-rag-bot.service"
sed -e "s#/root/RAG#$INSTALL_DIR#g" \
    -e "s#--host [^ ]*#--host $BIND_HOST#" \
    -e "s#--port [0-9]*#--port $PORT#" \
    "$REPO_ROOT/interface_rag/scripts/interface-rag.service" \
    > /etc/systemd/system/interface-rag.service
sed -e "s#/root/RAG#$INSTALL_DIR#g" \
    "$REPO_ROOT/interface_rag/scripts/interface-rag-bot.service" \
    > /etc/systemd/system/interface-rag-bot.service
chmod 644 /etc/systemd/system/interface-rag.service /etc/systemd/system/interface-rag-bot.service
systemctl daemon-reload
systemctl enable --now interface-rag.service interface-rag-bot.service

# --- 6. Smoke + адрес ---
say "6. Проверка"
URL_HOST="$BIND_HOST"
if [ "$BIND_HOST" = "0.0.0.0" ]; then
  DETECTED="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$DETECTED" ] && URL_HOST="$DETECTED"
fi
URL="http://$URL_HOST:$PORT/"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1; then
    step "веб-интерфейс отвечает ✓"
    WEB_OK=1; break
  fi
  sleep 1
done
[ "${WEB_OK:-0}" = 1 ] || step "внимание: веб-интерфейс ещё не отвечает (смотрите journalctl -u interface-rag)"

say "Готово"
printf 'Веб-интерфейс:  %s\n' "$URL"
printf 'Бот:            %s\n' "$(systemctl is-active interface-rag-bot.service 2>/dev/null || echo '?')"
printf '\nЧто осталось сделать:\n'
printf '  1. Откройте %s → Настройки → «Телеграм» и введите TELEGRAM_BOT_TOKEN,\n' "$URL"
printf '     затем нажмите «Перезапустить бота» (там же — остальные API-ключи).\n'
printf '  2. Корень документов уже записан: BASE_DIR=%s (меняется в Настройки → «Папки»).\n' "$BASE_DIR"
printf '  3. После смены папок в «Папках» перезапустите бота в разделе «Телеграм».\n'
