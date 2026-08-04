#!/usr/bin/env bash
# XuanMu RedTeam Agent - cross-distribution installation script
set -eo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'
NC=$'\033[0m'

log()  { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[X]${NC} $1"; exit 1; }
info() { echo -e "${BLUE}[~]${NC} $1"; }

DISTRO_ID="unknown"
DISTRO_FAMILY="unknown"
DISTRO_NAME="Unknown Linux"
PACKAGES=()
PYTHON_BIN=""

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo -- "$@"
    else
        err "安装系统依赖需要 root 权限，但系统未安装 sudo"
    fi
}

detect_distro() {
    local ID="" ID_LIKE="" PRETTY_NAME=""
    [ "$(uname -s)" = "Linux" ] || err "当前安装脚本仅支持 Linux"
    [ -r /etc/os-release ] || err "缺少 /etc/os-release，无法识别发行版"
    . /etc/os-release

    DISTRO_ID="${ID:-unknown}"
    DISTRO_NAME="${PRETTY_NAME:-$DISTRO_ID}"
    case "$DISTRO_ID ${ID_LIKE:-}" in
        *debian*) DISTRO_FAMILY="debian" ;;
        *fedora*|*rhel*|*centos*) DISTRO_FAMILY="rhel" ;;
        *arch*) DISTRO_FAMILY="arch" ;;
        *suse*) DISTRO_FAMILY="suse" ;;
        *) err "暂不支持该 Linux 发行版: $DISTRO_NAME" ;;
    esac
}

add_packages() {
    PACKAGES+=("$@")
}

find_supported_python() {
    local CANDIDATE
    for CANDIDATE in python3.14 python3.13 python3.12 python3; do
        command -v "$CANDIDATE" >/dev/null 2>&1 || continue
        if "$CANDIDATE" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' >/dev/null 2>&1; then
            PYTHON_BIN=$(command -v "$CANDIDATE")
            return 0
        fi
    done
    return 1
}

has_postgres_server() {
    local DIR
    if command -v pg_config >/dev/null 2>&1; then
        DIR=$(pg_config --bindir 2>/dev/null || true)
        [ -n "$DIR" ] && [ -x "$DIR/postgres" ] && return 0
    fi
    command -v postgres >/dev/null 2>&1 && return 0
    for DIR in /usr/lib/postgresql/*/bin /usr/pgsql-*/bin \
        /usr/lib/postgresql*/bin /usr/local/pgsql/bin; do
        [ -x "$DIR/postgres" ] && return 0
    done
    return 1
}

collect_packages() {
    local NEED_PYTHON=0 NEED_POSTGRES=0 NEED_NODE=0 NEED_OPENSSL=0 NEED_UTIL=0
    local PYTHON_VENV_PACKAGE=""
    if find_supported_python; then
        if ! "$PYTHON_BIN" -c "import venv, ensurepip" >/dev/null 2>&1; then
            NEED_PYTHON=1
            PYTHON_VENV_PACKAGE="$(basename "$PYTHON_BIN")-venv"
        fi
    elif ! command -v python3 >/dev/null 2>&1; then
        NEED_PYTHON=1
    fi
    command -v psql >/dev/null 2>&1 || NEED_POSTGRES=1
    command -v pg_isready >/dev/null 2>&1 || NEED_POSTGRES=1
    has_postgres_server || NEED_POSTGRES=1
    command -v node >/dev/null 2>&1 || NEED_NODE=1
    command -v npm >/dev/null 2>&1 || NEED_NODE=1
    command -v openssl >/dev/null 2>&1 || NEED_OPENSSL=1
    command -v setsid >/dev/null 2>&1 || NEED_UTIL=1

    case "$DISTRO_FAMILY" in
        debian)
            [ "$NEED_PYTHON" -eq 0 ] || add_packages python3 "${PYTHON_VENV_PACKAGE:-python3-venv}" python3-pip
            [ "$NEED_POSTGRES" -eq 0 ] || add_packages postgresql postgresql-client
            [ "$NEED_NODE" -eq 0 ] || add_packages nodejs npm
            [ "$NEED_OPENSSL" -eq 0 ] || add_packages openssl
            [ "$NEED_UTIL" -eq 0 ] || add_packages util-linux
            ;;
        rhel)
            [ "$NEED_PYTHON" -eq 0 ] || add_packages python3 python3-pip
            [ "$NEED_POSTGRES" -eq 0 ] || add_packages postgresql-server postgresql
            [ "$NEED_NODE" -eq 0 ] || add_packages nodejs npm
            [ "$NEED_OPENSSL" -eq 0 ] || add_packages openssl
            [ "$NEED_UTIL" -eq 0 ] || add_packages util-linux
            ;;
        arch)
            [ "$NEED_PYTHON" -eq 0 ] || add_packages python python-pip
            [ "$NEED_POSTGRES" -eq 0 ] || add_packages postgresql
            [ "$NEED_NODE" -eq 0 ] || add_packages nodejs npm
            [ "$NEED_OPENSSL" -eq 0 ] || add_packages openssl
            [ "$NEED_UTIL" -eq 0 ] || add_packages util-linux
            ;;
        suse)
            [ "$NEED_PYTHON" -eq 0 ] || add_packages python3 python3-pip python3-virtualenv
            [ "$NEED_POSTGRES" -eq 0 ] || add_packages postgresql-server postgresql
            [ "$NEED_NODE" -eq 0 ] || add_packages nodejs npm
            [ "$NEED_OPENSSL" -eq 0 ] || add_packages openssl
            [ "$NEED_UTIL" -eq 0 ] || add_packages util-linux
            ;;
    esac
}

install_packages() {
    if [ "${#PACKAGES[@]}" -eq 0 ]; then
        log "系统依赖已就绪"
        return
    fi

    info "安装系统依赖: ${PACKAGES[*]}"
    case "$DISTRO_FAMILY" in
        debian)
            as_root apt-get update
            as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${PACKAGES[@]}"
            ;;
        rhel) as_root dnf install -y "${PACKAGES[@]}" ;;
        arch) as_root pacman -S --needed --noconfirm "${PACKAGES[@]}" ;;
        suse) as_root zypper --non-interactive install "${PACKAGES[@]}" ;;
    esac
    log "系统依赖安装完成"
}

verify_runtime_versions() {
    command -v node >/dev/null 2>&1 || err "未找到 node"
    command -v npm >/dev/null 2>&1 || err "未找到 npm"
    command -v setsid >/dev/null 2>&1 || err "未找到 setsid (util-linux)"
    find_supported_python || err "需要 Python 3.12+；当前发行版仓库版本过旧时请先安装新版 Python"
    "$PYTHON_BIN" -c "import venv, ensurepip" >/dev/null 2>&1 \
        || err "$PYTHON_BIN 缺少 venv/ensurepip 支持"

    node - <<'JS' || err "需要 Node.js 20.19+ 或 22.12+"
const [major, minor] = process.versions.node.split('.').map(Number)
const supported = (major === 20 && minor >= 19) || (major === 22 && minor >= 12) || major > 22
process.exit(supported ? 0 : 1)
JS
    log "运行时版本: $($PYTHON_BIN --version), Node.js $(node --version)"
}

random_hex() {
    local BYTES="$1"
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex "$BYTES"
    else
        "$PYTHON_BIN" -c "import secrets; print(secrets.token_hex($BYTES))"
    fi
}

create_config() {
    local CONFIG_DIR="$PROJECT_DIR/.xuanmu"
    local CONFIG_FILE="$CONFIG_DIR/config.json"
    local EXAMPLE_FILE="$CONFIG_DIR/config.json.example"
    local OLD_UMASK DB_VALUE ADMIN_VALUE ENCRYPT_KEY TEMP_CONFIG

    mkdir -p "$CONFIG_DIR"
    chmod 700 "$CONFIG_DIR"
    if [ -f "$CONFIG_FILE" ]; then
        chmod 600 "$CONFIG_FILE"
        log "配置文件已存在，保留现有配置"
        return
    fi
    [ -f "$EXAMPLE_FILE" ] || err "未找到 $EXAMPLE_FILE"

    DB_VALUE="${DB_PASSWORD:-$(random_hex 16)}"
    ADMIN_VALUE="${ADMIN_PASSWORD:-$(random_hex 16)}"
    ENCRYPT_KEY=$(random_hex 32)
    TEMP_CONFIG="$CONFIG_DIR/.config.json.tmp.$$"
    OLD_UMASK=$(umask)
    umask 077
    trap 'rm -f "$TEMP_CONFIG"' EXIT
    EXAMPLE_FILE="$EXAMPLE_FILE" CONFIG_FILE="$TEMP_CONFIG" \
        DB_VALUE="$DB_VALUE" ADMIN_VALUE="$ADMIN_VALUE" \
        ADMIN_EMAIL_VALUE="${ADMIN_EMAIL:-admin@admin.com}" ENCRYPT_KEY="$ENCRYPT_KEY" \
        "$PYTHON_BIN" <<'PY'
import json
import os
import re

path = os.environ["CONFIG_FILE"]
with open(os.environ["EXAMPLE_FILE"], encoding="utf-8") as fh:
    cfg = json.load(fh)

cfg["system"]["encrypt_key"] = os.environ["ENCRYPT_KEY"]
cfg["system"]["bootstrap_admin"]["enabled"] = True
cfg["system"]["bootstrap_admin"]["password"] = os.environ["ADMIN_VALUE"]
cfg["system"]["bootstrap_admin"]["email"] = os.environ["ADMIN_EMAIL_VALUE"]
cfg["database"].update({
    "host": "127.0.0.1",
    "port": 5432,
    "database": "z3r0",
    "username": "xuanmu",
    "password": os.environ["DB_VALUE"],
})
for key in ("database", "username"):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,62}", cfg["database"][key]):
        raise ValueError(f"database.{key} contains unsupported characters")
for agent in cfg.get("agents", {}).values():
    agent["api_key"] = ""

with open(path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=4, ensure_ascii=False)
    fh.write("\n")
PY
    mv "$TEMP_CONFIG" "$CONFIG_FILE"
    trap - EXIT
    umask "$OLD_UMASK"
    log "配置文件已创建: $CONFIG_FILE"
    warn "数据库和管理员密码已随机生成，请妥善保管 $CONFIG_FILE"
}

setup_database() {
    info "初始化系统 PostgreSQL 和应用数据库..."
    bash "$PROJECT_DIR/xuanmu.sh" db-setup
    log "PostgreSQL 配置完成"
}

setup_python() {
    local VENV_DIR="$PROJECT_DIR/.venv"
    local INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}"
    info "配置 Python 虚拟环境..."
    if [ -x "$VENV_DIR/bin/python" ]; then
        "$VENV_DIR/bin/python" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' \
            || err "现有虚拟环境低于 Python 3.12，请移走 $VENV_DIR 后重试"
    else
        [ ! -e "$VENV_DIR" ] || err "$VENV_DIR 已存在但不完整，请检查或移走后重试"
        "$PYTHON_BIN" -m venv "$VENV_DIR" || err "创建 Python 虚拟环境失败"
    fi
    "$VENV_DIR/bin/python" -m pip install --index-url "$INDEX_URL" --upgrade pip
    "$VENV_DIR/bin/python" -m pip install --index-url "$INDEX_URL" -r "$PROJECT_DIR/requirements.txt"
    log "Python 依赖安装完成"
}

setup_frontend() {
    info "导出 API 契约并构建前端..."
    "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/export_schema.py"
    if [ -f "$PROJECT_DIR/web/package-lock.json" ]; then
        (cd "$PROJECT_DIR/web" && npm ci)
    else
        (cd "$PROJECT_DIR/web" && npm install)
    fi
    (cd "$PROJECT_DIR/web" && npm run build)
    log "前端构建完成"
}

verify_entrypoints() {
    chmod +x "$PROJECT_DIR/xuanmu.sh" "$PROJECT_DIR/start.sh" "$PROJECT_DIR/stop.sh"
    bash -n "$PROJECT_DIR/xuanmu.sh" "$PROJECT_DIR/start.sh" "$PROJECT_DIR/stop.sh"
    log "启动入口已就绪"
}

main() {
    echo "========================================"
    echo "  XuanMu RedTeam Agent v0.2.1"
    echo "  安装配置脚本"
    echo "========================================"

    detect_distro
    log "系统: $DISTRO_NAME ($DISTRO_FAMILY)"
    collect_packages
    install_packages
    verify_runtime_versions
    create_config
    setup_database
    setup_python
    setup_frontend
    verify_entrypoints

    echo ""
    echo "========================================"
    echo "  安装配置完成"
    echo "========================================"
    echo "  启动服务:  ./start.sh"
    echo "  停止服务:  ./stop.sh"
    echo "  查看状态:  ./xuanmu.sh status"
    echo "  配置文件:  .xuanmu/config.json"
    echo ""
    echo "  启动前请填写 agents 中的 api_key / base_url / model。"
    echo "========================================"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
