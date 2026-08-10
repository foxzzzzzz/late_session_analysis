#!/bin/bash
# ============================================================
# 尾盘分析系统 — 环境部署脚本
# 使用方式: 先 git clone/download 代码到服务器，然后:
#   cd late_session_analysis && chmod +x deploy.sh && ./deploy.sh
# ============================================================
set -e

# 自动检测项目根目录 (脚本所在目录)
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$APP_DIR/.venv"
PORT=${WEB_PORT:-5000}
HAS_ERROR=0

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC}  $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; HAS_ERROR=1; }
log_step()  { echo ""; echo -e "${GREEN}═══ $1 ═══${NC}"; }

trap 'log_error "脚本在第 $LINENO 行退出 (exit=$?)"; echo "请检查上方错误信息后重试"' ERR

echo "============================================"
echo "  Late Session Analysis 环境部署"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  目录: $APP_DIR"
echo "============================================"

# ── 1. 基础检查 ──────────────────────────────────────────
log_step "1/6 环境检查"

python3 --version 2>&1 && log_info "Python3: $(python3 --version 2>&1)" || {
    log_error "未找到 python3，请先安装: sudo apt install python3 python3-pip"
    exit 1
}

python3 -c "import ensurepip" 2>/dev/null && log_info "pip: OK" || {
    log_warn "pip 未安装，尝试安装..."
    sudo apt update && sudo apt install -y python3-pip || {
        log_error "pip 安装失败"
        exit 1
    }
}

log_info "当前用户: $(whoami)"

# ── 2. 创建虚拟环境 ──────────────────────────────────────
log_step "2/6 创建虚拟环境"

# 清除旧的损坏 venv (避免 pip 不存在等残留问题)
if [ -d "$VENV_DIR" ] && [ ! -f "$VENV_DIR/bin/pip" ]; then
    log_warn "检测到损坏的虚拟环境 (无 pip)，清除后重建..."
    rm -rf "$VENV_DIR"
fi

if [ -f "$VENV_DIR/bin/python" ] && [ -f "$VENV_DIR/bin/pip" ]; then
    log_info "虚拟环境已存在: $VENV_DIR"
else
    # 确保 python3-venv 已安装 (Ubuntu 新系统常见缺失)
    if ! python3 -m venv --help &>/dev/null; then
        log_warn "python3-venv 未安装，尝试安装..."
        sudo apt update -qq && sudo apt install -y python3-venv || {
            log_error "python3-venv 安装失败，请手动执行: sudo apt install python3-venv"
            exit 1
        }
    fi
    log_info "创建 Python 虚拟环境..."
    python3 -m venv "$VENV_DIR" 2>&1 || {
        log_error "创建虚拟环境失败"
        exit 1
    }
    log_info "虚拟环境创建完成"
fi
PIP="$VENV_DIR/bin/pip"
PYTHON="$VENV_DIR/bin/python"

# ── 3. 安装依赖 ──────────────────────────────────────────
log_step "3/6 安装 Python 依赖"

cd "$APP_DIR"

MISSING_DEPS=""
$PYTHON -c "import flask" 2>/dev/null || MISSING_DEPS="$MISSING_DEPS flask"
$PYTHON -c "import apscheduler" 2>/dev/null || MISSING_DEPS="$MISSING_DEPS apscheduler"

if [ -z "$MISSING_DEPS" ]; then
    log_info "Web 依赖已安装"
else
    log_info "安装依赖到虚拟环境..."
    $PIP install --upgrade pip -q 2>&1
    $PIP install -r requirements.txt 2>&1 && log_info "基础依赖 OK" || {
        log_error "基础依赖安装失败"
        exit 1
    }
    $PIP install -r web/requirements-web.txt 2>&1 && log_info "Web 依赖 OK" || {
        log_error "Web 依赖安装失败"
        exit 1
    }
fi

# ── 4. 检测 TDX 服务器 ───────────────────────────────────
log_step "4/6 检测 TDX 服务器 (mootdx)"

# 每次都重扫，避免旧配置指向不可达服务器
log_info "扫描可用的 TDX 服务器..."
rm -f "$MOOTDX_CFG"
TDX_OK=$($PYTHON -c "
import socket, json, os
from tdxpy.constants import hq_hosts
for name, addr, port in hq_hosts[:50]:
    try:
        s = socket.socket(); s.settimeout(2)
        s.connect((addr, port)); s.close()
        cfg = {'SERVER': {'HQ': [['auto', addr, port]]}, 'BESTIP': {'HQ': [addr, port], 'EX': '', 'GP': ''}}
        os.makedirs(os.path.expanduser('~/.mootdx'), exist_ok=True)
        json.dump(cfg, open(os.path.expanduser('~/.mootdx/config.json'), 'w'))
        print(f'{addr}:{port}')
        break
    except: pass
" 2>&1)
if [ -n "$TDX_OK" ]; then
    log_info "TDX 服务器可用: $TDX_OK"
else
    log_warn "所有 TDX 服务器不可达，K线将自动降级到 Sina HTTP"
fi

# ── 5. 配置文件 ──────────────────────────────────────────
log_step "5/6 配置文件"

# .env
if [ ! -f "$APP_DIR/.env" ]; then
    if [ -f "$APP_DIR/.env.example" ]; then
        cp "$APP_DIR/.env.example" "$APP_DIR/.env"
        log_info "从 .env.example 创建 .env"
    else
        cat > "$APP_DIR/.env" << 'ENVEOF'
LLM_API_KEY=sk-your-key-here
LLM_API_BASE=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
ENVEOF
        log_info "创建默认 .env"
    fi
    log_warn "请编辑 $APP_DIR/.env 填入 LLM_API_KEY"
else
    log_info ".env 已存在，跳过"
fi

# 数据目录
for d in web_instance reports backtest_reports backtest_cache live_snapshots; do
    mkdir -p "$APP_DIR/$d"
done
log_info "数据目录已就绪"

# ── 6. systemd 服务 ──────────────────────────────────────
log_step "6/6 配置 systemd"

SERVICE_NAME="late-session"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

sudo tee "$SERVICE_FILE" > /dev/null << SERVEOF
[Unit]
Description=Late Session Analysis Web Dashboard
After=network.target

[Service]
Type=simple
User=${SUDO_USER:-$USER}
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/python -m web.app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=10
Environment=TZ=Asia/Shanghai
Environment=WEB_INSTANCE_DIR=$APP_DIR/web_instance

[Install]
WantedBy=multi-user.target
SERVEOF

log_info "systemd 配置已写入 $SERVICE_FILE"

sudo systemctl daemon-reload && log_info "daemon-reload OK" || {
    log_error "daemon-reload 失败"
    exit 1
}

sudo systemctl enable "$SERVICE_NAME" 2>&1 && log_info "已设置开机自启" || log_warn "enable 失败"

if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    sudo systemctl restart "$SERVICE_NAME" 2>&1 && log_info "服务已重启" || log_error "重启失败"
else
    sudo systemctl start "$SERVICE_NAME" 2>&1 && log_info "服务已启动" || log_error "启动失败"
fi

sleep 2

# 修复权限 (确保运行用户可读写)
if [ "$(id -u)" -eq 0 ]; then
    TARGET_USER="${SUDO_USER:-root}"
    if [ "$TARGET_USER" != "root" ]; then
        chown -R "$TARGET_USER:$TARGET_USER" "$APP_DIR" 2>/dev/null || true
        chown -R "$TARGET_USER:$TARGET_USER" "$VENV_DIR" 2>/dev/null || true
        log_info "权限已修正: $TARGET_USER"
    fi
fi

# ── 验证 ──────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  部署完成!"
echo "============================================"

if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo -e "  服务状态: ${GREEN}运行中 ✓${NC}"
else
    echo -e "  服务状态: ${RED}未运行 ✗${NC}"
    echo "  排查: sudo journalctl -u $SERVICE_NAME -n 50 --no-pager"
fi

if ss -tlnp 2>/dev/null | grep -q ":$PORT "; then
    echo -e "  端口 $PORT: ${GREEN}监听中 ✓${NC}"
else
    echo -e "  端口 $PORT: ${RED}未监听 ✗${NC}"
fi

echo ""
echo "  后续操作:"
echo "  1. 编辑 API Key:    nano $APP_DIR/.env"
echo "  2. 重启服务:        sudo systemctl restart $SERVICE_NAME"
echo "  3. 查看日志:        sudo journalctl -u $SERVICE_NAME -f"
echo "  4. 云服务器安全组:   开放 TCP $PORT"
echo ""
echo "  访问: http://$(curl -s ifconfig.me 2>/dev/null || echo '<服务器IP>'):$PORT"
echo "============================================"

exit $HAS_ERROR
