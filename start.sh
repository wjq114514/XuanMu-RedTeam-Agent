#!/usr/bin/env bash
# ============================================================
# XuanMu RedTeam Agent - 一键启动脚本
# 启动 PostgreSQL + 后端 API + 前端（构建后自动托管）
# ============================================================
set -e

cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

echo "========================================"
echo "  XuanMu RedTeam Agent"
echo "  Version 0.2.1"
echo "========================================"

# 1. 检查 PostgreSQL 是否运行
echo "[1/3] 检查 PostgreSQL..."
if pg_isready -q 2>/dev/null; then
    echo "  ✅ PostgreSQL 已在运行"
else
    echo "  ⏳ 启动 PostgreSQL..."
    sudo pg_ctlcluster 18 main start 2>/dev/null || sudo service postgresql start 2>/dev/null || {
        echo "  ❌ 启动失败，请手动运行: sudo pg_ctlcluster 18 main start"
        exit 1
    }
    sleep 2
    if pg_isready -q 2>/dev/null; then
        echo "  ✅ PostgreSQL 启动成功"
    else
        echo "  ❌ PostgreSQL 启动失败"
        exit 1
    fi
fi

# 2. 激活虚拟环境
echo "[2/4] 加载 Python 虚拟环境..."
if [ ! -d "$VENV_DIR" ]; then
    echo "  ⏳ 创建虚拟环境..."
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
fi
source "$VENV_DIR/bin/activate"
echo "  ✅ 虚拟就绪: $(python3 --version)"

# 3. 构建前端（仅当源码有变动时）
echo "[3/4] 检查前端是否需要构建..."
if [ ! -d "$PROJECT_DIR/web/node_modules" ]; then
    echo "  ⏳ 安装前端依赖 (npm install)..."
    (cd "$PROJECT_DIR/web" && npm install)
fi

# 判断是否需要重新构建：dist-app 不存在，或源码比 dist-app 新
NEED_BUILD=0
DIST="$PROJECT_DIR/web/dist-app"
if [ ! -d "$DIST" ]; then
    NEED_BUILD=1
    echo "  ⏳ 未检测到 dist-app，需要首次构建"
else
    # 比较源码目录与 dist-app 的最近修改时间
    LATEST_SRC=$(find "$PROJECT_DIR/web/src" "$PROJECT_DIR/web" -maxdepth 1 \
        \( -name '*.ts' -o -name '*.tsx' -o -name '*.vue' -o -name '*.html' \
           -o -name '*.css' -o -name 'package.json' -o -name 'vite.config.*' \) \
        -newer "$DIST" -print 2>/dev/null | head -n 1)
    if [ -n "$LATEST_SRC" ]; then
        NEED_BUILD=1
        echo "  ⏳ 检测到源码变动: $(basename "$LATEST_SRC")"
    fi
fi

if [ "$NEED_BUILD" -eq 1 ]; then
    echo "  ⏳ 运行 npm run build..."
    (cd "$PROJECT_DIR/web" && npm run build)
    if [ -d "$PROJECT_DIR/web/dist-app" ]; then
        echo "  ✅ 前端构建完成: web/dist-app"
    else
        echo "  ❌ 前端构建失败，未生成 dist-app"
        exit 1
    fi
else
    echo "  ✅ 前端无变动，跳过构建（使用现有 dist-app）"
fi

# 4. 启动后端
echo "[4/4] 启动 XuanMu 后端服务..."
echo ""
echo "  🌐 API 地址:     http://127.0.0.1:8000"
echo "  📖 API 文档:     http://127.0.0.1:8000/docs"
echo "  🖥️  前端界面:    http://127.0.0.1:8000"
echo "  👤 管理员账号:   admin@admin.com（密码见 .xuanmu/config.json）"
echo ""
echo "  按 Ctrl+C 停止服务"
echo "========================================"
echo ""

exec python main.py
