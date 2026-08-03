#!/usr/bin/env bash
# ============================================================
# XuanMu RedTeam Agent - 启动/停止一体脚本
# 用法:
#   ./xuanmu.sh start    启动 (PG + 前端构建 + root 后端)
#   ./xuanmu.sh stop     停止 (后端 + PG)
#   ./xuanmu.sh restart  重启
#   ./xuanmu.sh status   查看状态
#   ./xuanmu.sh gen-api  重新生成前端 API 契约 (openapi.json -> schema.ts)
# ============================================================
set -e

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
PG_DATA="$PROJECT_DIR/.postgres-data"
PG_SOCKET_DIR="$PROJECT_DIR/.pg-socket"
CONFIG_FILE="$PROJECT_DIR/.xuanmu/config.json"
LOG_FILE="/tmp/xuanmu-root.log"
PORT=8002

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'
ok()   { echo -e "  ${GREEN}✅${NC} $1"; }
info() { echo -e "  ${YELLOW}ℹ️ ${NC} $1"; }
err()  { echo -e "  ${RED}❌${NC} $1"; }

# ---------- 检查 PostgreSQL 是否运行 ----------
# 用端口检测（pg_isready 在 PG 停止/异常时会误判）
pg_running() {
    ss -tln 2>/dev/null | grep -q "127.0.0.1:5432"
}

# ---------- 自动探测 PostgreSQL 二进制路径 ----------
# 兼容 Arch (/usr/bin/postgres) 与 Debian/Kali (/usr/lib/postgresql/<ver>/bin/postgres)
detect_pg_bin() {
    local BIN
    BIN=$(command -v postgres 2>/dev/null) && { echo "$BIN"; return 0; }
    BIN=$(ls /usr/lib/postgresql/*/bin/postgres 2>/dev/null | sort -V | tail -n 1) && { echo "$BIN"; return 0; }
    BIN=$(ls /usr/local/pgsql/bin/postgres 2>/dev/null | tail -n 1) && { echo "$BIN"; return 0; }
    err "未找到 postgres 可执行文件，请确认已安装 PostgreSQL"
    exit 1
}

# ---------- 探测 PG 配套工具 (initdb / psql) ----------
detect_pg_tool() {
    local TOOL="$1"
    command -v "$TOOL" 2>/dev/null && return 0
    ls "/usr/lib/postgresql/"*/bin/"$TOOL" 2>/dev/null | sort -V | tail -n 1 && return 0
    ls "/usr/local/pgsql/bin/$TOOL" 2>/dev/null | tail -n 1 && return 0
    return 1
}

# ---------- 读取 config.json 数据库配置 (不硬编码凭据) ----------
# config.json 的 database 凭据由 setup.sh 随机生成后写入，这里统一从这里读取
read_db_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        err "未找到配置文件 $CONFIG_FILE，请先运行 ./setup.sh 初始化"
        exit 1
    fi
    local PY
    if [ -x "$VENV_DIR/bin/python" ]; then
        PY="$VENV_DIR/bin/python"
    else
        PY=python3
    fi
    local OUT
    OUT=$("$PY" - "$CONFIG_FILE" <<'PYEOF' 2>/dev/null
import json
import shlex
import sys
cfg = json.load(open(sys.argv[1]))["database"]
for k, name in (("host", "HOST"), ("port", "PORT"), ("database", "NAME"),
                ("username", "USER"), ("password", "PASS")):
    v = cfg.get(k, "")
    print(f"DB_{name}={shlex.quote(str(v))}")
PYEOF
)
    if [ $? -ne 0 ]; then
        err "无法解析 $CONFIG_FILE 的 database 配置"
        exit 1
    fi
    eval "$OUT"
    [ -n "$DB_USER" ] || { err "config.json 缺少 database.username"; exit 1; }
    [ -n "$DB_PASS" ] || { err "config.json 缺少 database.password"; exit 1; }
}

# ---------- 启动 PostgreSQL ----------
start_pg() {
    echo "[1/4] 检查 PostgreSQL..."
    read_db_config
    if pg_running; then
        ok "PostgreSQL 已在运行"
        ensure_local_trust
        ensure_pg_user
        return
    fi
    mkdir -p "$PG_SOCKET_DIR"
    if [ ! -d "$PG_DATA" ]; then
        init_pg
    fi
    echo "  ⏳ 启动项目专属 PostgreSQL..."
    # PG 数据目录归当前用户所有，必须以该用户身份启动，root 会被 PG 拒绝
    # 清理可能由 root 创建的旧日志（当前用户无权删时用 sudo 兜底）
    rm -f /tmp/xuanmu-pg.log 2>/dev/null || sudo rm -f /tmp/xuanmu-pg.log 2>/dev/null || true
    PG_BIN=$(detect_pg_bin)
    info "使用 PostgreSQL: $PG_BIN"
    setsid nohup "$PG_BIN" -D "$PG_DATA" -h 127.0.0.1 -p 5432 -k "$PG_SOCKET_DIR" \
        > /tmp/xuanmu-pg.log 2>&1 < /dev/null &
    sleep 3
    if pg_running; then
        ok "PostgreSQL 启动成功"
        ensure_local_trust
        ensure_pg_user
    else
        err "PostgreSQL 启动失败，查看 /tmp/xuanmu-pg.log"
        exit 1
    fi
}

# ---------- 确保本地 socket 走 trust (管理员免密) ----------
# 项目专属实例的 local 认证需为 trust，否则 psql 会卡在密码提示
# 仅影响本地 socket，TCP (127.0.0.1) 仍走 scram-sha-256 密码认证
ensure_local_trust() {
    local HBA="$PG_DATA/pg_hba.conf"
    [ -f "$HBA" ] || return 0
    if grep -qE '^local\s+all\s+all\s+(scram-sha-256|md5|peer)\s*$' "$HBA"; then
        cp "$HBA" "$HBA.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
        sed -i -E 's/^local\s+all\s+all\s+scram-sha-256/local all all trust/' "$HBA"
        sed -i -E 's/^local\s+all\s+all\s+md5/local all all trust/' "$HBA"
        sed -i -E 's/^local\s+all\s+all\s+peer/local all all trust/' "$HBA"
        local MASTER_PID="$PG_DATA/postmaster.pid"
        if [ -f "$MASTER_PID" ]; then
            kill -HUP "$(head -n 1 "$MASTER_PID")" 2>/dev/null || true
            sleep 1
        fi
        info "已将 pg_hba.conf local 认证改为 trust (已备份)"
    fi
}

# ---------- 初始化项目专属 PostgreSQL (initdb) ----------
init_pg() {
    echo "  ⏳ 首次运行，初始化项目专属 PostgreSQL..."
    local INITDB
    INITDB=$(detect_pg_tool initdb) || {
        err "未找到 initdb，请确认已安装 PostgreSQL"
        exit 1
    }
    info "使用 initdb: $INITDB"
    "$INITDB" -D "$PG_DATA" -U postgres --auth-local=trust --auth-host=scram-sha-256 \
        > /tmp/xuanmu-initdb.log 2>&1 || {
        err "initdb 失败，查看 /tmp/xuanmu-initdb.log"
        exit 1
    }
    ok "数据目录初始化完成: .postgres-data"
}

# ---------- 确保数据库用户/库存在 (幂等，凭据来自 config.json) ----------
ensure_pg_user() {
    local PSQL SUPERUSER
    PSQL=$(detect_pg_tool psql) || {
        err "未找到 psql，请确认已安装 PostgreSQL"
        exit 1
    }
    # 探测可用的超级用户：优先 postgres，不存在则回退到 config.json 的 DB_USER
    # （兼容手动初始化的旧数据目录，其超级用户可能是 root）
    SUPERUSER="postgres"
    if ! "$PSQL" -w -h "$PG_SOCKET_DIR" -U postgres -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
        SUPERUSER="$DB_USER"
        info "管理员角色 postgres 不存在，改用 $SUPERUSER"
    fi
    # 通过项目专属 socket 以 trust 免密连接 postgres 管理员
    # 幂等创建/修正用户与数据库，凭据全部来自 config.json（不硬编码）
    # 注意：psql -c 不进行变量替换，必须用 heredoc 交互模式 + \gexec 条件执行
    "$PSQL" -w -h "$PG_SOCKET_DIR" -U "$SUPERUSER" -d postgres \
        -v db_user="$DB_USER" -v db_pass="$DB_PASS" <<'SQL'
SELECT 'CREATE USER ' || quote_ident(:'db_user') || ' WITH PASSWORD ' || quote_literal(:'db_pass') || ' SUPERUSER' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'db_user')\gexec
SELECT 'ALTER USER ' || quote_ident(:'db_user') || ' WITH PASSWORD ' || quote_literal(:'db_pass') || ' SUPERUSER' WHERE EXISTS (SELECT FROM pg_roles WHERE rolname = :'db_user')\gexec
SQL
    "$PSQL" -w -h "$PG_SOCKET_DIR" -U "$SUPERUSER" -d postgres \
        -v db_name="$DB_NAME" -v db_user="$DB_USER" <<'SQL'
SELECT 'CREATE DATABASE ' || quote_ident(:'db_name') || ' OWNER ' || quote_ident(:'db_user') WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'db_name')\gexec
SQL
    ok "数据库用户 $DB_USER / 库 $DB_NAME 已就绪 (凭据来自 config.json)"
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
        LATEST_SRC=$(find "$PROJECT_DIR/web/src" "$PROJECT_DIR/web" -maxdepth 1 \
            \( -name '*.ts' -o -name '*.tsx' -o -name '*.vue' -o -name '*.html' \
               -o -name '*.css' -o -name 'package.json' -o -name 'vite.config.*' \) \
            -newer "$DIST" -print 2>/dev/null | head -n 1)
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

# ---------- 启动后端 (root) ----------
start_backend() {
    echo "[4/4] 启动 XuanMu 后端服务 (root)..."
    local PID
    PID=$(pgrep -f "python main.py" | head -n 1 || true)
    if [ -n "$PID" ]; then
        info "后端已在运行 (PID $PID)，跳过"
        return
    fi

    sudo bash -c "cd '$PROJECT_DIR' && setsid nohup .venv/bin/python main.py > '$LOG_FILE' 2>&1 < /dev/null & echo \$!"
    sleep 4

    if pgrep -f "python main.py" >/dev/null; then
        ok "后端启动成功 (root, PID $(pgrep -f 'python main.py' | head -n 1))"
    else
        err "后端启动失败，查看 $LOG_FILE"
        exit 1
    fi
}

# ---------- 停止 ----------
stop_all() {
    echo "停止 XuanMu 服务..."
    local BACKEND_PIDS
    BACKEND_PIDS=$(pgrep -f "python main.py" || true)
    if [ -n "$BACKEND_PIDS" ]; then
        echo "  ⏳ 停止后端进程 (root)..."
        sudo pkill -f "python main.py" 2>/dev/null || true
        sleep 1
        if pgrep -f "python main.py" >/dev/null; then
            info "后端进程仍在，强制结束..."
            sudo pkill -9 -f "python main.py" 2>/dev/null || true
        fi
        ok "后端已停止"
    else
        info "后端未在运行"
    fi

    # 可选: 停止项目专属 PG
    if pg_running; then
        echo "  ⏳ 停止 PostgreSQL..."
        # 用完整路径匹配，兼容 /usr/bin/postgres -D /abs/path
        pkill -f "postgres -D.*postgres-data" 2>/dev/null || true
        sleep 3
        if pg_running; then
            pkill -9 -f "postgres -D.*postgres-data" 2>/dev/null || true
            sleep 2
        fi
        if pg_running; then
            err "PostgreSQL 未能停止"
        else
            ok "PostgreSQL 已停止"
        fi
    else
        info "PostgreSQL 未在运行"
    fi
}

# ---------- 状态 ----------
show_status() {
    echo "=== XuanMu 服务状态 ==="
    local BPID
    BPID=$(pgrep -f "python main.py" | head -n 1 || true)
    if [ -n "$BPID" ]; then
        local BUSER
        BUSER=$(ps -o user= -p "$BPID" 2>/dev/null)
        echo "  后端: ${GREEN}运行中${NC} (PID $BPID, 用户 $BUSER)"
    else
        echo "  后端: ${RED}未运行${NC}"
    fi

    if pg_running; then
        echo "  PostgreSQL: ${GREEN}运行中${NC}"
    else
        echo "  PostgreSQL: ${RED}未运行${NC}"
    fi

    if ss -tln 2>/dev/null | grep -q ":$PORT "; then
        echo "  端口 $PORT: ${GREEN}监听中${NC}"
    else
        echo "  端口 $PORT: ${RED}未监听${NC}"
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
case "${1:-start}" in
    start)
        start_pg
        load_venv
        build_frontend
        start_backend
        echo ""
        echo "  🌐 API:     http://127.0.0.1:$PORT"
        echo "  📖 Docs:    http://127.0.0.1:$PORT/docs"
        echo "  👤 管理员账号: admin@admin.com（密码见 .xuanmu/config.json）"
        echo "  日志: $LOG_FILE"
        ;;
    stop)
        stop_all
        ;;
    restart)
        echo "===== 重启 XuanMu 后端 ====="
        # 只重启后端，保留 PostgreSQL（避免竞态）
        BPID=$(pgrep -f "python main.py" | head -n 1 || true)
        if [ -n "$BPID" ]; then
            echo "  ⏳ 停止后端 (PID $BPID)..."
            sudo pkill -f "python main.py" 2>/dev/null || true
            sleep 1
            sudo pkill -9 -f "python main.py" 2>/dev/null || true
            sleep 1
        fi
        start_pg
        load_venv
        build_frontend
        start_backend
        echo ""
        echo "  🌐 API:     http://127.0.0.1:$PORT"
        echo "  📖 Docs:    http://127.0.0.1:$PORT/docs"
        echo "  日志: $LOG_FILE"
        ;;
    status)
        show_status
        ;;
    gen-api)
        gen_api
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status|gen-api}"
        exit 1
        ;;
esac
