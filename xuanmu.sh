#!/usr/bin/env bash
# ============================================================
# XuanMu RedTeam Agent - 启动/停止一体脚本
# 用法:
#   ./xuanmu.sh start    启动 (系统 PG + 前端构建 + root 后端)
#   ./xuanmu.sh stop     停止后端（默认保留系统 PG）
#   ./xuanmu.sh stop --with-db  停止后端和系统 PG
#   ./xuanmu.sh restart  重启
#   ./xuanmu.sh status   查看状态
#   ./xuanmu.sh db-setup 初始化/校验系统 PostgreSQL
#   ./xuanmu.sh prepare  安装依赖并构建前端
#   ./xuanmu.sh gen-api  重新生成前端 API 契约 (openapi.json -> schema.ts)
# 可选环境变量:
#   XUANMU_PG_SERVICE=postgresql-16  多版本环境显式指定 systemd 单元
#   XUANMU_AUTO_INIT_DB=0            禁止初始化空的系统 PostgreSQL 集群
# ============================================================
set -eo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
VENV_DIR="$PROJECT_DIR/.venv"
CONFIG_FILE="$PROJECT_DIR/.xuanmu/config.json"
PID_FILE="$PROJECT_DIR/.xuanmu/app.pid"
LOG_FILE="$PROJECT_DIR/.xuanmu/backend.log"
PG_BINDIR=""
PG_PSQL=""
PG_ISREADY=""
PG_SERVICE=""
PG_STOP_SKIPPED=0
DISTRO_ID="unknown"
DISTRO_FAMILY="unknown"
DISTRO_NAME="Unknown Linux"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
ok()   { echo -e "  ${GREEN}✅${NC} $1"; }
info() { echo -e "  ${YELLOW}ℹ️ ${NC} $1"; }
err()  { echo -e "  ${RED}❌${NC} $1"; }

# ---------- 识别 Linux 发行版 ----------
detect_distro() {
    local ID="" ID_LIKE="" PRETTY_NAME=""
    if [ "$(uname -s)" != "Linux" ]; then
        err "当前脚本仅支持 Linux"
        exit 1
    fi
    if [ -r /etc/os-release ]; then
        . /etc/os-release
    fi

    DISTRO_ID="${ID:-unknown}"
    DISTRO_NAME="${PRETTY_NAME:-$DISTRO_ID}"
    case "$DISTRO_ID ${ID_LIKE:-}" in
        *debian*) DISTRO_FAMILY="debian" ;;
        *fedora*|*rhel*|*centos*) DISTRO_FAMILY="rhel" ;;
        *arch*) DISTRO_FAMILY="arch" ;;
        *suse*) DISTRO_FAMILY="suse" ;;
        *) DISTRO_FAMILY="unknown" ;;
    esac
}

# ---------- 检查目标 PostgreSQL 是否接受连接 ----------
pg_running() {
    "$PG_ISREADY" -q -h "$DB_HOST" -p "$DB_PORT" -d postgres 2>/dev/null
}

# ---------- 发行版权限适配 ----------
as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo -- "$@"
    else
        err "该操作需要 root 权限，但系统未安装 sudo"
        return 1
    fi
}

as_postgres() {
    if [ "$(id -un)" = "postgres" ]; then
        "$@"
    elif [ "$(id -u)" -eq 0 ] && command -v runuser >/dev/null 2>&1; then
        runuser -u postgres -- "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo -u postgres -- "$@"
    else
        err "无法切换到 postgres 系统用户，请以 root 运行或安装 sudo"
        return 1
    fi
}

# ---------- 读取并校验 config.json ----------
read_db_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        err "未找到配置文件 $CONFIG_FILE"
        info "请先复制 .xuanmu/config.json.example 并完成数据库配置"
        exit 1
    fi
    local PY OUT
    if [ -x "$VENV_DIR/bin/python" ]; then
        PY="$VENV_DIR/bin/python"
    else
        PY=$(command -v python3 2>/dev/null || true)
    fi
    [ -n "$PY" ] || { err "未找到 python3，无法读取配置文件"; exit 1; }

    if ! OUT=$("$PY" - "$CONFIG_FILE" <<'PYEOF' 2>&1
import json
import re
import shlex
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    root = json.load(fh)

cfg = root.get("database")
if not isinstance(cfg, dict):
    raise ValueError("缺少 database 配置")

values = {}
for key in ("host", "database", "username", "password"):
    value = cfg.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"database.{key} 必须是非空字符串")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"database.{key} 不能包含换行或 NUL 字符")
    values[key] = value

port = cfg.get("port")
if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
    raise ValueError("database.port 必须是 1-65535 的整数")
if values["database"] in {"postgres", "template0", "template1"}:
    raise ValueError("database.database 不能使用 PostgreSQL 维护数据库")
if values["username"] == "postgres":
    raise ValueError("database.username 不能使用 PostgreSQL 管理员账号 postgres")
for key in ("database", "username"):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,62}", values[key]):
        raise ValueError(f"database.{key} 只能包含字母、数字、点、下划线和连字符")

system = root.get("system", {})
app_port = system.get("listen_port", 8000)
if isinstance(app_port, bool) or not isinstance(app_port, int) or not 1 <= app_port <= 65535:
    raise ValueError("system.listen_port 必须是 1-65535 的整数")

for name, value in (
    ("DB_HOST", values["host"]),
    ("DB_PORT", port),
    ("DB_NAME", values["database"]),
    ("DB_USER", values["username"]),
    ("DB_PASS", values["password"]),
    ("APP_PORT", app_port),
):
    print(f"{name}={shlex.quote(str(value))}")
PYEOF
    ); then
        err "无法解析 $CONFIG_FILE: $OUT"
        exit 1
    fi
    eval "$OUT"

    case "$DB_HOST" in
        127.0.0.1|localhost|::1) ;;
        *)
            err "系统 PostgreSQL 模式仅支持本机数据库，当前 database.host=$DB_HOST"
            exit 1
            ;;
    esac
}

# ---------- 探测同一套 PostgreSQL 客户端工具 ----------
use_pg_bindir() {
    local DIR="$1"
    if [ -x "$DIR/psql" ] && [ -x "$DIR/pg_isready" ]; then
        PG_BINDIR="$DIR"
        PG_PSQL="$DIR/psql"
        PG_ISREADY="$DIR/pg_isready"
        return 0
    fi
    return 1
}

detect_pg_tools() {
    local DIR PSQL_PATH BEST_DIR="" BEST_MAJOR=-1 VERSION MAJOR
    if command -v pg_config >/dev/null 2>&1; then
        DIR=$(pg_config --bindir 2>/dev/null || true)
        [ -n "$DIR" ] && use_pg_bindir "$DIR" && return 0
    fi

    PSQL_PATH=$(command -v psql 2>/dev/null || true)
    if [ -n "$PSQL_PATH" ]; then
        DIR=$(dirname "$PSQL_PATH")
        use_pg_bindir "$DIR" && return 0
    fi

    for DIR in /usr/lib/postgresql/*/bin /usr/pgsql-*/bin \
        /usr/lib/postgresql*/bin /usr/local/pgsql/bin; do
        [ -x "$DIR/psql" ] && [ -x "$DIR/pg_isready" ] || continue
        VERSION=$($DIR/psql --version 2>/dev/null || true)
        VERSION=${VERSION#*PostgreSQL) }
        MAJOR=${VERSION%%[!0-9]*}
        if [ -n "$MAJOR" ] && [ "$MAJOR" -gt "$BEST_MAJOR" ]; then
            BEST_MAJOR=$MAJOR
            BEST_DIR="$DIR"
        fi
    done
    [ -n "$BEST_DIR" ] && use_pg_bindir "$BEST_DIR" && return 0

    err "未找到 psql 和 pg_isready，请先安装 PostgreSQL 服务端及客户端"
    print_pg_install_hint
    exit 1
}

print_pg_install_hint() {
    case "$DISTRO_FAMILY" in
        debian) info "安装提示: sudo apt install postgresql postgresql-client" ;;
        rhel) info "安装提示: sudo dnf install postgresql-server postgresql" ;;
        arch) info "安装提示: sudo pacman -S postgresql" ;;
        suse) info "安装提示: sudo zypper install postgresql-server postgresql" ;;
        *) info "请使用当前发行版的软件包管理器安装 PostgreSQL 服务端和客户端" ;;
    esac
}

print_pg_init_hint() {
    local VERSION MAJOR SETUP_TOOL
    VERSION=$($PG_PSQL --version 2>/dev/null || true)
    VERSION=${VERSION#*PostgreSQL) }
    MAJOR=${VERSION%%[!0-9]*}

    case "$DISTRO_FAMILY" in
        rhel)
            SETUP_TOOL=$(command -v "postgresql-$MAJOR-setup" 2>/dev/null || true)
            if [ -n "$SETUP_TOOL" ]; then
                info "未初始化时执行: sudo $SETUP_TOOL initdb"
            else
                info "未初始化时执行: sudo postgresql-setup --initdb"
            fi
            ;;
        arch) info "未初始化时执行: sudo -iu postgres initdb --locale=C.UTF-8 --encoding=UTF8 -D /var/lib/postgres/data" ;;
        debian) info "若系统没有集群，请使用 pg_createcluster 创建与配置端口匹配的集群" ;;
        suse) info "请检查 /var/lib/pgsql/data 和发行版 PostgreSQL 初始化文档" ;;
    esac
}

init_system_pg_if_needed() {
    local UNIT="$1" VERSION MAJOR DATA_DIR SETUP_TOOL
    [ "${XUANMU_AUTO_INIT_DB:-1}" != "0" ] || return 0

    case "$DISTRO_FAMILY" in
        arch)
            DATA_DIR="/var/lib/postgres/data"
            if as_postgres test -f "$DATA_DIR/PG_VERSION"; then
                return 0
            fi
            [ -x "$PG_BINDIR/initdb" ] || { err "未找到 initdb: $PG_BINDIR/initdb"; return 1; }
            info "检测到 Arch Linux PostgreSQL 尚未初始化: $DATA_DIR"
            as_postgres "$PG_BINDIR/initdb" --locale=C.UTF-8 --encoding=UTF8 \
                --auth-local=peer --auth-host=scram-sha-256 -D "$DATA_DIR" || return 1
            ok "PostgreSQL 系统集群初始化完成"
            ;;
        rhel)
            if [[ "$UNIT" =~ ^postgresql-([0-9]+)\.service$ ]]; then
                MAJOR="${BASH_REMATCH[1]}"
                DATA_DIR="/var/lib/pgsql/$MAJOR/data"
                for SETUP_TOOL in "$PG_BINDIR/postgresql-$MAJOR-setup" \
                    "/usr/pgsql-$MAJOR/bin/postgresql-$MAJOR-setup"; do
                    [ -x "$SETUP_TOOL" ] && break
                    SETUP_TOOL=""
                done
                if [ -z "$SETUP_TOOL" ]; then
                    SETUP_TOOL=$(command -v "postgresql-$MAJOR-setup" 2>/dev/null || true)
                fi
            else
                DATA_DIR="/var/lib/pgsql/data"
                SETUP_TOOL=$(command -v postgresql-setup 2>/dev/null || true)
            fi
            if as_postgres test -f "$DATA_DIR/PG_VERSION"; then
                return 0
            fi
            [ -n "$SETUP_TOOL" ] || { err "未找到 PostgreSQL 集群初始化工具"; return 1; }
            info "检测到 PostgreSQL 尚未初始化: $DATA_DIR"
            if [ "$(basename "$SETUP_TOOL")" = "postgresql-setup" ]; then
                as_root "$SETUP_TOOL" --initdb || return 1
            else
                as_root "$SETUP_TOOL" initdb || return 1
            fi
            ok "PostgreSQL 系统集群初始化完成"
            ;;
    esac
}

# ---------- 系统 PostgreSQL 服务适配 ----------
systemd_unit_exists() {
    [ "$(systemctl show -p LoadState --value "$1" 2>/dev/null || true)" = "loaded" ]
}

detect_systemd_service() {
    local UNIT STATE REST
    local -a UNITS=()

    if [ -n "${XUANMU_PG_SERVICE:-}" ]; then
        UNIT="$XUANMU_PG_SERVICE"
        [[ "$UNIT" == *.service ]] || UNIT="$UNIT.service"
        if ! systemd_unit_exists "$UNIT"; then
            err "XUANMU_PG_SERVICE 指定的单元不存在: $UNIT"
            return 2
        fi
        PG_SERVICE="$UNIT"
        return 0
    fi

    while read -r UNIT STATE REST; do
        [ -n "$UNIT" ] || continue
        [[ "$UNIT" == *@* ]] && continue
        [[ "$UNIT" == postgresql*.service ]] || continue
        UNITS+=("$UNIT")
    done < <(systemctl list-unit-files --type=service --no-legend --no-pager 'postgresql*.service' 2>/dev/null || true)

    if [ "${#UNITS[@]}" -eq 1 ]; then
        PG_SERVICE="${UNITS[0]}"
        return 0
    fi
    if [ "${#UNITS[@]}" -gt 1 ]; then
        err "检测到多个 PostgreSQL systemd 单元: ${UNITS[*]}"
        info "请设置 XUANMU_PG_SERVICE=<单元名> 后重试"
        return 2
    fi
    return 1
}

start_debian_cluster() {
    local VERSION CLUSTER PORT STATUS REST
    command -v pg_lsclusters >/dev/null 2>&1 || return 1
    command -v pg_ctlcluster >/dev/null 2>&1 || return 1

    while read -r VERSION CLUSTER PORT STATUS REST; do
        [ "$PORT" = "$DB_PORT" ] || continue
        PG_SERVICE="cluster:$VERSION:$CLUSTER"
        if [ "$STATUS" != "online" ]; then
            as_root pg_ctlcluster "$VERSION" "$CLUSTER" start || return 1
        fi
        return 0
    done < <(pg_lsclusters --no-header 2>/dev/null || true)
    return 1
}

start_pg_service() {
    local DETECT_STATUS=0
    if start_debian_cluster; then
        return 0
    fi

    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        detect_systemd_service || DETECT_STATUS=$?
        if [ "$DETECT_STATUS" -eq 0 ]; then
            init_system_pg_if_needed "$PG_SERVICE" || return 1
            as_root systemctl start "$PG_SERVICE" || return 1
            return 0
        fi
        [ "$DETECT_STATUS" -eq 2 ] && return 1
    fi

    if command -v service >/dev/null 2>&1; then
        PG_SERVICE="postgresql"
        init_system_pg_if_needed "$PG_SERVICE" || return 1
        as_root service postgresql start || return 1
        return 0
    fi

    err "无法找到可用的 PostgreSQL 系统服务"
    print_pg_install_hint
    return 1
}

stop_debian_cluster() {
    local VERSION CLUSTER PORT STATUS REST
    command -v pg_lsclusters >/dev/null 2>&1 || return 1
    command -v pg_ctlcluster >/dev/null 2>&1 || return 1

    while read -r VERSION CLUSTER PORT STATUS REST; do
        [ "$PORT" = "$DB_PORT" ] || continue
        PG_SERVICE="cluster:$VERSION:$CLUSTER"
        if [ "$STATUS" = "online" ]; then
            as_root pg_ctlcluster "$VERSION" "$CLUSTER" stop || return 1
        fi
        return 0
    done < <(pg_lsclusters --no-header 2>/dev/null || true)
    return 1
}

stop_pg_service() {
    local DETECT_STATUS=0
    read_db_config
    detect_pg_tools

    if ! pg_running; then
        info "配置的 PostgreSQL 端点未运行，不停止任何系统数据库服务"
        PG_STOP_SKIPPED=1
        return 0
    fi
    verify_system_pg_endpoint || return 1

    if stop_debian_cluster; then
        return 0
    fi

    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        detect_systemd_service || DETECT_STATUS=$?
        if [ "$DETECT_STATUS" -eq 0 ]; then
            verify_systemd_service_owner "$PG_SERVICE" || return 1
            as_root systemctl stop "$PG_SERVICE" || return 1
            return 0
        fi
        [ "$DETECT_STATUS" -eq 2 ] && return 1
    fi

    if command -v service >/dev/null 2>&1; then
        PG_SERVICE="postgresql"
        as_root service postgresql stop || return 1
        return 0
    fi

    err "无法确定应停止的 PostgreSQL 系统服务"
    return 1
}

detect_pg_service() {
    local VERSION CLUSTER PORT STATUS REST
    if command -v pg_lsclusters >/dev/null 2>&1; then
        while read -r VERSION CLUSTER PORT STATUS REST; do
            if [ "$PORT" = "$DB_PORT" ]; then
                PG_SERVICE="cluster:$VERSION:$CLUSTER ($STATUS)"
                return 0
            fi
        done < <(pg_lsclusters --no-header 2>/dev/null || true)
    fi

    if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
        if detect_systemd_service; then
            local STATE
            STATE=$(systemctl is-active "$PG_SERVICE" 2>/dev/null || true)
            PG_SERVICE="$PG_SERVICE (${STATE:-unknown})"
            return 0
        fi
    fi

    if command -v service >/dev/null 2>&1; then
        PG_SERVICE="postgresql"
        return 0
    fi
    return 1
}

wait_for_pg() {
    local ATTEMPT
    for ((ATTEMPT = 1; ATTEMPT <= 30; ATTEMPT++)); do
        pg_running && return 0
        sleep 1
    done
    return 1
}

verify_system_pg_endpoint() {
    local SERVER_INFO SERVER_PORT LISTEN_ADDRESSES ADDRESS MATCHED=0
    local -a PG_LISTEN_ITEMS=()
    if ! SERVER_INFO=$(as_postgres "$PG_PSQL" -X -v ON_ERROR_STOP=1 -p "$DB_PORT" \
        -d postgres -AtF '|' -c "SELECT current_setting('port'), current_setting('listen_addresses')"); then
        err "无法通过系统 PostgreSQL socket 连接端口 $DB_PORT"
        return 1
    fi
    IFS='|' read -r SERVER_PORT LISTEN_ADDRESSES <<< "$SERVER_INFO"
    if [ "$SERVER_PORT" != "$DB_PORT" ]; then
        err "系统 PostgreSQL socket 端口 $SERVER_PORT 与配置端口 $DB_PORT 不一致"
        return 1
    fi

    IFS=',' read -ra PG_LISTEN_ITEMS <<< "$LISTEN_ADDRESSES"
    for ADDRESS in "${PG_LISTEN_ITEMS[@]}"; do
        ADDRESS=${ADDRESS//[[:space:]]/}
        case "$DB_HOST:$ADDRESS" in
            127.0.0.1:\*|127.0.0.1:localhost|127.0.0.1:127.0.0.1) MATCHED=1 ;;
            localhost:\*|localhost:localhost|localhost:127.0.0.1|localhost:::1) MATCHED=1 ;;
            ::1:\*|::1:localhost|::1:::1) MATCHED=1 ;;
        esac
    done
    if [ "$MATCHED" -ne 1 ]; then
        err "系统 PostgreSQL listen_addresses=$LISTEN_ADDRESSES 未覆盖 $DB_HOST"
        info "拒绝修改可能不属于该系统服务的数据库实例"
        return 1
    fi
    return 0
}

ensure_app_hba_rule() {
    local HBA_FILE PY CHANGED
    HBA_FILE=$(as_postgres "$PG_PSQL" -X -v ON_ERROR_STOP=1 -p "$DB_PORT" \
        -d postgres -Atc "SHOW hba_file") || return 1
    PY=$(command -v python3 2>/dev/null || true)
    [ -n "$PY" ] || { err "未找到 python3，无法配置应用数据库认证"; return 1; }

    CHANGED=$(as_postgres env XUANMU_PROJECT_DIR="$PROJECT_DIR" \
        "$PY" - "$HBA_FILE" "$DB_NAME" "$DB_USER" <<'PYEOF'
import os
import stat
import sys

path, database, user = sys.argv[1:]
project_id = __import__("hashlib").sha256(os.environ["XUANMU_PROJECT_DIR"].encode()).hexdigest()[:12]
marker_prefix = f"# xuanmu managed:{project_id}"
marker = f"{marker_prefix} {database}/{user}"
rules = (
    f'host "{database}" "{user}" 127.0.0.1/32 scram-sha-256 {marker}\n'
    f'host "{database}" "{user}" ::1/128 scram-sha-256 {marker}\n'
)
with open(path, encoding="utf-8") as fh:
    original = fh.read()
preserved = "".join(line for line in original.splitlines(keepends=True) if marker_prefix not in line)
updated = rules + preserved
if updated == original:
    print("0")
    raise SystemExit

temporary = f"{path}.xuanmu.{os.getpid()}"
mode = stat.S_IMODE(os.stat(path).st_mode)
with open(temporary, "w", encoding="utf-8") as fh:
    fh.write(updated)
os.chmod(temporary, mode)
os.replace(temporary, path)
print("1")
PYEOF
    ) || return 1

    if [ "$CHANGED" = "1" ]; then
        as_postgres "$PG_PSQL" -X -v ON_ERROR_STOP=1 -p "$DB_PORT" \
            -d postgres -c "SELECT pg_reload_conf()" >/dev/null || return 1
        ok "应用数据库 SCRAM 认证规则已更新"
    fi
}

verify_systemd_service_owner() {
    local UNIT="$1" POSTMASTER_PID MAIN_PID
    if ! POSTMASTER_PID=$(as_postgres "$PG_PSQL" -X -v ON_ERROR_STOP=1 -p "$DB_PORT" \
        -d postgres -Atc "SELECT split_part(pg_read_file(current_setting('data_directory') || '/postmaster.pid'), chr(10), 1)"); then
        err "无法读取目标 PostgreSQL 的 postmaster PID"
        return 1
    fi
    MAIN_PID=$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null || true)
    if [ -z "$MAIN_PID" ] || [ "$MAIN_PID" = "0" ] || [ "$MAIN_PID" != "$POSTMASTER_PID" ]; then
        err "$UNIT 的 MainPID=${MAIN_PID:-unknown} 与目标 PostgreSQL PID=$POSTMASTER_PID 不一致"
        info "拒绝停止与配置端点不匹配的系统服务"
        return 1
    fi
    return 0
}

# ---------- 确保应用数据库角色和数据库存在 ----------
ensure_pg_user() {
    local PY
    if [ -x "$VENV_DIR/bin/python" ]; then
        PY="$VENV_DIR/bin/python"
    else
        PY=$(command -v python3 2>/dev/null || true)
    fi

    if ! "$PY" - "$CONFIG_FILE" <<'PYEOF' | as_postgres env \
        "PGOPTIONS=-c password_encryption=scram-sha-256" \
        "$PG_PSQL" -X -v ON_ERROR_STOP=1 -p "$DB_PORT" -d postgres >/dev/null
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    db = json.load(fh)["database"]

def ident(value):
    return '"' + value.replace('"', '""') + '"'

def literal(value):
    return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"

user = db["username"]
password = db["password"]
database = db["database"]
user_ident = ident(user)
delimiter_number = 0
delimiter = "$xuanmu_0$"
while delimiter in user or delimiter in password or delimiter in database:
    delimiter_number += 1
    delimiter = f"$xuanmu_{delimiter_number}$"

print(f"DO {delimiter}")
print("BEGIN")
print(f"  IF EXISTS (SELECT FROM pg_roles WHERE rolname = {literal(user)} AND rolsuper) THEN")
print("    RAISE EXCEPTION 'refusing to reuse an existing superuser role';")
print("  END IF;")
print(f"  IF EXISTS (SELECT FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba WHERE d.datname = {literal(database)} AND r.rolname <> {literal(user)}) THEN")
print("    RAISE EXCEPTION 'refusing to take ownership of an existing database';")
print("  END IF;")
print(f"  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {literal(user)}) THEN")
print(f"    CREATE ROLE {user_ident} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {literal(password)};")
print(f"    COMMENT ON ROLE {user_ident} IS 'xuanmu-managed';")
print("  ELSE")
print(f"    IF COALESCE((SELECT shobj_description(oid, 'pg_authid') FROM pg_roles WHERE rolname = {literal(user)}), '') <> 'xuanmu-managed' THEN")
if user == "root" and database == "z3r0":
    print(f"      IF NOT EXISTS (SELECT FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba WHERE d.datname = {literal(database)} AND r.rolname = {literal(user)}) THEN")
    print("        RAISE EXCEPTION 'refusing to reuse an unmanaged role';")
    print("      END IF;")
    print(f"      COMMENT ON ROLE {user_ident} IS 'xuanmu-managed';")
else:
    print("      RAISE EXCEPTION 'refusing to reuse an unmanaged role';")
print("    END IF;")
print(f"    ALTER ROLE {user_ident} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {literal(password)};")
print("  END IF;")
print("END")
print(f"{delimiter};")
print(f"SELECT format('CREATE DATABASE %I OWNER %I', {literal(database)}, {literal(user)}) WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = {literal(database)})\\gexec")
PYEOF
    then
        err "无法使用 postgres 系统账号配置数据库，请检查本地认证和数据库端口"
        exit 1
    fi
    ok "数据库用户 $DB_USER / 库 $DB_NAME 已就绪"
}

validate_app_db() {
    if ! PGPASSWORD="$DB_PASS" "$PG_PSQL" -X -w -h "$DB_HOST" -p "$DB_PORT" \
        -U "$DB_USER" -d "$DB_NAME" -tAc "SELECT 1" >/dev/null 2>&1; then
        err "应用账号无法连接 $DB_HOST:$DB_PORT/$DB_NAME"
        info "请检查 PostgreSQL listen_addresses、pg_hba.conf 和 config.json 凭据"
        exit 1
    fi
    ok "应用数据库 TCP 连接验证通过"
}

# ---------- 启动 PostgreSQL ----------
start_pg() {
    echo "[1/4] 检查 PostgreSQL..."
    read_db_config
    detect_pg_tools
    info "使用 PostgreSQL 客户端: $PG_BINDIR"

    if pg_running; then
        ok "PostgreSQL 已在 $DB_HOST:$DB_PORT 接受连接"
    else
        echo "  ⏳ 启动系统 PostgreSQL..."
        if ! start_pg_service; then
            err "PostgreSQL 系统服务启动失败，请检查服务状态和集群初始化"
            print_pg_install_hint
            print_pg_init_hint
            exit 1
        fi
        if ! wait_for_pg; then
            err "系统服务已启动，但 $DB_HOST:$DB_PORT 在 30 秒内未就绪"
            info "请确认系统 PostgreSQL 的 port 与 config.json 一致，并检查服务日志"
            print_pg_init_hint
            exit 1
        fi
        ok "PostgreSQL 启动成功 ($DB_HOST:$DB_PORT)"
    fi
    verify_system_pg_endpoint || exit 1
    ensure_pg_user
    ensure_app_hba_rule || exit 1
    validate_app_db
}

# ---------- 加载虚拟环境 ----------
load_venv() {
    echo "[2/4] 加载 Python 虚拟环境..."
    if [ ! -d "$VENV_DIR" ]; then
        info "未找到 venv，创建中..."
        python3 -m venv "$VENV_DIR"
        "$VENV_DIR/bin/pip" install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
    fi
    ok "虚拟环境就绪: $("$VENV_DIR/bin/python" --version)"
}

# ---------- 构建前端 (智能判断) ----------
build_frontend() {
    echo "[3/4] 检查前端是否需要构建..."
    local DIST="$PROJECT_DIR/web/dist-app"

    if [ ! -d "$PROJECT_DIR/web/node_modules" ]; then
        info "安装前端依赖 (npm install)..."
        (cd "$PROJECT_DIR/web" && npm install)
    fi

    local NEED_BUILD=0
    if [ ! -d "$DIST" ]; then
        NEED_BUILD=1
        info "未检测到 dist，需要首次构建"
    else
        local LATEST_SRC
        LATEST_SRC=$(find "$PROJECT_DIR/web/src" -type f \
            \( -name '*.ts' -o -name '*.tsx' -o -name '*.vue' -o -name '*.html' -o -name '*.css' \) \
            -newer "$DIST" -print -quit 2>/dev/null)
        if [ -z "$LATEST_SRC" ]; then
            LATEST_SRC=$(find "$PROJECT_DIR/web" -maxdepth 1 -type f \
                \( -name 'package.json' -o -name 'package-lock.json' -o -name 'vite.config.*' \) \
                -newer "$DIST" -print -quit 2>/dev/null)
        fi
        if [ -n "$LATEST_SRC" ]; then
            NEED_BUILD=1
            info "检测到源码变动: $(basename "$LATEST_SRC")"
        fi
    fi

    if [ "$NEED_BUILD" -eq 1 ]; then
        info "运行 npm run build..."
        (cd "$PROJECT_DIR/web" && npm run build)
        if [ -d "$DIST" ]; then
            ok "前端构建完成: web/dist"
        else
            err "前端构建失败，未生成 dist"
            exit 1
        fi
    else
        ok "前端无变动，跳过构建"
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
    local EXPECTED_CMD="$VENV_DIR/bin/python $PROJECT_DIR/main.py"
    local -a PIDS=()
    mapfile -t PIDS < <(pgrep -f -x -- "$EXPECTED_CMD" 2>/dev/null || true)
    [ "${#PIDS[@]}" -eq 1 ] || return 1
    printf '%s\n' "${PIDS[0]}"
}

backend_pid() {
    local PID="" START="" CURRENT_START
    if [ -f "$PID_FILE" ]; then
        read -r PID START < "$PID_FILE" || true
    fi
    CURRENT_START=$(process_start_time "$PID" 2>/dev/null || true)
    if [[ "$PID" =~ ^[0-9]+$ ]] && [ -n "$START" ] && [ "$CURRENT_START" = "$START" ]; then
        printf '%s\n' "$PID"
        return 0
    fi

    PID=$(discover_backend_pid || true)
    [ -n "$PID" ] || return 1
    START=$(process_start_time "$PID" 2>/dev/null || true)
    [ -n "$START" ] || return 1
    printf '%s %s\n' "$PID" "$START" > "$PID_FILE"
    printf '%s\n' "$PID"
}

start_backend() {
    echo "[4/4] 启动 XuanMu 后端服务 (root, 后台)..."
    local PID
    PID=$(backend_pid || true)
    if [ -n "$PID" ]; then
        info "后端已在运行 (PID $PID)，跳过"
        return
    fi

    mkdir -p "$PROJECT_DIR/.xuanmu"
    : > "$PID_FILE"
    as_root bash -c '
        cd "$1"
        setsid nohup "$2" "$3" >> "$4" 2>&1 < /dev/null &
        pid=$!
        read -ra stat_fields < "/proc/$pid/stat"
        printf "%s %s\n" "$pid" "${stat_fields[21]}" > "$5"
    ' _ "$PROJECT_DIR" "$VENV_DIR/bin/python" "$PROJECT_DIR/main.py" "$LOG_FILE" "$PID_FILE"

    sleep 2
    PID=$(backend_pid || true)
    if [ -z "$PID" ]; then
        rm -f "$PID_FILE"
        err "后端启动失败，请查看 $LOG_FILE"
        exit 1
    fi
    ok "后端启动成功 (root, PID $PID)"
}

# ---------- 停止 ----------
stop_all() {
    local WITH_DB="${1:-}"
    echo "停止 XuanMu 服务..."
    "$PROJECT_DIR/stop.sh"

    if [ "$WITH_DB" = "--with-db" ]; then
        echo "  ⏳ 停止系统 PostgreSQL..."
        stop_pg_service
        if [ "$PG_STOP_SKIPPED" -eq 1 ]; then
            return 0
        fi
        local ATTEMPT
        for ((ATTEMPT = 1; ATTEMPT <= 15; ATTEMPT++)); do
            pg_running || break
            sleep 1
        done
        if pg_running; then
            err "PostgreSQL 系统服务停止后仍在 $DB_HOST:$DB_PORT 接受连接"
            return 1
        fi
        ok "PostgreSQL 系统服务已停止"
    else
        info "系统 PostgreSQL 保持运行；如需停止请使用: $0 stop --with-db"
    fi
}

tcp_listening() {
    local PY
    if [ -x "$VENV_DIR/bin/python" ]; then
        PY="$VENV_DIR/bin/python"
    else
        PY=$(command -v python3 2>/dev/null || true)
    fi
    [ -n "$PY" ] || return 1
    "$PY" - "$1" <<'PYEOF' >/dev/null 2>&1
import socket
import sys

with socket.socket() as sock:
    sock.settimeout(0.5)
    raise SystemExit(sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) != 0)
PYEOF
}

# ---------- 状态 ----------
show_status() {
    echo "=== XuanMu 服务状态 ==="
    local BPID
    read_db_config
    detect_pg_tools
    BPID=$(backend_pid || true)
    if [ -n "$BPID" ]; then
        local BUSER
        BUSER=$(ps -o user= -p "$BPID" 2>/dev/null)
        echo "  后端: ${GREEN}运行中${NC} (PID $BPID, 用户 $BUSER)"
    else
        echo "  后端: ${RED}未运行${NC}"
    fi

    if detect_pg_service; then
        echo "  PostgreSQL 服务: $PG_SERVICE"
    else
        echo "  PostgreSQL 服务: ${YELLOW}未识别${NC}"
    fi

    if pg_running; then
        echo "  PostgreSQL 连接: ${GREEN}就绪${NC} ($DB_HOST:$DB_PORT)"
    else
        echo "  PostgreSQL 连接: ${RED}未就绪${NC} ($DB_HOST:$DB_PORT)"
    fi

    if tcp_listening "$APP_PORT"; then
        echo "  后端端口 $APP_PORT: ${GREEN}监听中${NC}"
    else
        echo "  后端端口 $APP_PORT: ${RED}未监听${NC}"
    fi
}

# ---------- 重新生成前端 API 契约 ----------
gen_api() {
    echo "重新生成前端 API 契约 (openapi.json -> schema.ts)..."
    load_venv

    info "导出 OpenAPI schema (export_schema.py)..."
    "$VENV_DIR/bin/python" scripts/export_schema.py
    ok "openapi.json + constants.ts 已更新"

    if [ ! -d "$PROJECT_DIR/web/node_modules" ]; then
        info "安装前端依赖 (npm install)..."
        (cd "$PROJECT_DIR/web" && npm install)
    fi
    info "生成 schema.ts (openapi-typescript)..."
    (cd "$PROJECT_DIR/web" && npm run generate:api)
    ok "schema.ts 已更新"

    info "运行类型检查 (typecheck)..."
    (cd "$PROJECT_DIR/web" && npm run typecheck)
    ok "类型检查通过"

    echo ""
    echo "  ✅ 生成完毕！请检查 git diff 并提交："
    echo "     git add web/openapi.json web/src/shared/api/generated/"
    echo "     git commit -m \"chore: regenerate API schema\""
    echo "     git push"
}

# ---------- 主入口 ----------
main() {
detect_distro
echo "系统发行版: $DISTRO_NAME ($DISTRO_FAMILY)"
case "${1:-start}" in
    start)
        start_pg
        load_venv
        build_frontend
        start_backend
        echo ""
        echo "  API:  http://127.0.0.1:$APP_PORT"
        echo "  Docs: http://127.0.0.1:$APP_PORT/docs"
        echo "  日志: $LOG_FILE"
        ;;
    stop)
        if [ -n "${2:-}" ] && [ "$2" != "--with-db" ]; then
            echo "用法: $0 stop [--with-db]"
            exit 1
        fi
        stop_all "${2:-}"
        ;;
    restart)
        echo "===== 重启 XuanMu 后端 ====="
        "$PROJECT_DIR/stop.sh"
        start_pg
        load_venv
        build_frontend
        start_backend
        ;;
    status)
        show_status
        ;;
    db-setup)
        start_pg
        ;;
    prepare)
        load_venv
        build_frontend
        ;;
    gen-api)
        gen_api
        ;;
    *)
        echo "用法: $0 {start|stop [--with-db]|restart|status|db-setup|prepare|gen-api}"
        exit 1
        ;;
esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
