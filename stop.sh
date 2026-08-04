#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$PROJECT_DIR/.xuanmu/app.pid"
EXPECTED_CMD="$PROJECT_DIR/.venv/bin/python $PROJECT_DIR/main.py"

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo -- "$@"
    else
        echo "停止 root 后端需要 sudo" >&2
        return 1
    fi
}

process_start_time() {
    local PID="$1"
    local -a STAT_FIELDS=()
    [ -r "/proc/$PID/stat" ] || return 1
    read -ra STAT_FIELDS < "/proc/$PID/stat"
    [ "${#STAT_FIELDS[@]}" -ge 22 ] || return 1
    printf '%s\n' "${STAT_FIELDS[21]}"
}

discover_backend_pid() {
    local -a PIDS=()
    mapfile -t PIDS < <(pgrep -f -x -- "$EXPECTED_CMD" 2>/dev/null || true)
    [ "${#PIDS[@]}" -eq 1 ] || return 1
    printf '%s\n' "${PIDS[0]}"
}

APP_PID=""
APP_START=""
if [ -f "$PID_FILE" ]; then
    read -r APP_PID APP_START < "$PID_FILE" || true
fi

CURRENT_START=$(process_start_time "$APP_PID" 2>/dev/null || true)
if ! [[ "$APP_PID" =~ ^[0-9]+$ ]] || [ -z "$APP_START" ] || [ "$CURRENT_START" != "$APP_START" ]; then
    APP_PID=$(discover_backend_pid || true)
    APP_START=$(process_start_time "$APP_PID" 2>/dev/null || true)
fi

if [ -z "$APP_PID" ] || [ -z "$APP_START" ]; then
    rm -f "$PID_FILE"
    echo "XuanMu 未在运行"
    exit 0
fi

as_root kill "$APP_PID" 2>/dev/null || true
for ((ATTEMPT = 1; ATTEMPT <= 10; ATTEMPT++)); do
    [ -d "/proc/$APP_PID" ] || break
    sleep 1
done
if [ -d "/proc/$APP_PID" ]; then
    as_root kill -9 "$APP_PID" 2>/dev/null || true
    sleep 1
fi
if [ -d "/proc/$APP_PID" ]; then
    echo "无法停止 XuanMu (PID $APP_PID)" >&2
    exit 1
fi

rm -f "$PID_FILE"
echo "XuanMu 已停止；系统 PostgreSQL 保持运行"
