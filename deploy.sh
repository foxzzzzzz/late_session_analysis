#!/bin/bash
# ============================================================
# 尾盘分析系统 — 阿里云一键部署脚本
# 使用方式:
#   curl -sSL https://raw.githubusercontent.com/foxzzzzzz/late_session_analysis/main/deploy.sh | bash
#   或: chmod +x deploy.sh && ./deploy.sh
# ============================================================
set -e

APP_DIR="/home/admin/late_session_analysis"
VENV_DIR="$APP_DIR/.venv"
REPO_URL="https://github.com/foxzzzzzz/late_session_analysis.git"
PORT=5000
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
echo "  尾盘分析系统 Web Dashboard 部署"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
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

# 检查/创建虚拟环境
if [ ! -f "$VENV_DIR/bin/python" ]; then
    log_info "创建 Python 虚拟环境..."
    python3 -m venv "$VENV_DIR" 2>&1 || {
        log_error "创建虚拟环境失败，尝试安装 python3-venv: sudo apt install python3-venv"
        exit 1
    }
fi
PIP="$VENV_DIR/bin/pip"
PYTHON="$VENV_DIR/bin/python"
log_info "虚拟环境: $VENV_DIR"
log_info "当前用户: $(whoami)"
log_info "工作目录: $APP_DIR"

# ── 2. 下载/更新代码 ─────────────────────────────────────
log_step "2/6 下载代码"

if [ -d "$APP_DIR/.git" ]; then
    log_info "仓库已存在，执行 git pull..."
    cd "$APP_DIR"
    git fetch origin main 2>&1 || log_warn "git fetch 失败，继续尝试 pull"
    git pull origin main 2>&1 && log_info "代码更新成功" || {
        log_error "git pull 失败，请检查网络或手动处理"
        exit 1
    }
else
    if [ -d "$APP_DIR" ]; then
        log_warn "目录已存在但不是 git 仓库，备份后重新克隆..."
        mv "$APP_DIR" "${APP_DIR}.bak.$(date +%Y%m%d%H%M%S)"
    fi
    git clone "$REPO_URL" "$APP_DIR" 2>&1 && log_info "代码克隆成功" || {
        log_error "git clone 失败，请检查网络和仓库地址: $REPO_URL"
        exit 1
    }
    cd "$APP_DIR"
fi

log_info "当前 commit: $(git log --oneline -1 2>/dev/null || echo 'unknown')"

# ── 3. 安装依赖 ──────────────────────────────────────────
log_step "3/6 安装 Python 依赖"

cd "$APP_DIR"

# 检查关键依赖是否已安装 (使用虚拟环境)
MISSING_DEPS=""
$PYTHON -c "import flask" 2>/dev/null || MISSING_DEPS="$MISSING_DEPS flask"
$PYTHON -c "import apscheduler" 2>/dev/null || MISSING_DEPS="$MISSING_DEPS apscheduler"

if [ -z "$MISSING_DEPS" ]; then
    log_info "Web 依赖已安装，跳过"
else
    log_info "在虚拟环境中安装依赖..."
    $PIP install --upgrade pip -q 2>&1
    $PIP install -r requirements.txt 2>&1 && log_info "基础依赖安装 OK" || {
        log_error "基础依赖安装失败"
        exit 1
    }
    $PIP install -r web/requirements-web.txt 2>&1 && log_info "Web 依赖安装 OK" || {
        log_error "Web 依赖安装失败"
        exit 1
    }
fi

# ── 4. 检测 TDX 服务器 ───────────────────────────────────
log_step "4/7 检测 TDX 服务器 (mootdx)"

MOOTDX_CFG="$HOME/.mootdx/config.json"
if [ -f "$MOOTDX_CFG" ]; then
    log_info "mootdx 配置已存在，跳过检测"
else
    log_info "扫描可用的 TDX 服务器..."
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
fi

# ── 5. 配置环境变量 ──────────────────────────────────────
log_step "5/7 配置环境变量"

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
    log_warn "⚠  请编辑 $APP_DIR/.env 填入 LLM_API_KEY 后重启服务:"
    log_warn "   sudo systemctl restart late-session"
else
    log_info ".env 已存在，跳过创建"
fi

# ── 5. 创建数据目录 ──────────────────────────────────────
log_step "6/7 创建数据目录"

for d in web_instance reports backtest_reports backtest_cache live_snapshots; do
    mkdir -p "$APP_DIR/$d" && log_info "  $d/"
done

# 修复权限: 确保 admin 用户可读写所有文件
if id admin &>/dev/null; then
    chown -R admin:admin "$APP_DIR" 2>/dev/null && log_info "权限已修正 (admin:admin)" || log_warn "chown 失败，请手动执行: sudo chown -R admin:admin $APP_DIR"
    chown -R admin:admin "$VENV_DIR" 2>/dev/null
else
    log_warn "admin 用户不存在，跳过 chown"
fi

# ── 6. systemd 服务 ──────────────────────────────────────
log_step "7/7 配置 systemd"

SERVICE_NAME="late-session"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [ -f "$SERVICE_FILE" ]; then
    log_info "systemd 服务已存在，更新配置..."
else
    log_info "创建 systemd 服务..."
fi

sudo tee "$SERVICE_FILE" > /dev/null << SERVEOF
[Unit]
Description=Late Session Analysis Web Dashboard
After=network.target

[Service]
Type=simple
User=admin
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

sudo systemctl daemon-reload && log_info "systemctl daemon-reload OK" || {
    log_error "systemctl daemon-reload 失败，请检查 systemd"
    exit 1
}

sudo systemctl enable "$SERVICE_NAME" 2>&1 && log_info "已设置开机自启" || log_warn "enable 失败"

# 如果服务已在运行则重启，否则启动
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    sudo systemctl restart "$SERVICE_NAME" 2>&1 && log_info "服务已重启" || log_error "重启失败"
else
    sudo systemctl start "$SERVICE_NAME" 2>&1 && log_info "服务已启动" || log_error "启动失败"
fi

sleep 2

# ── 验证 ──────────────────────────────────────────────────
echo ""
echo "============================================"
echo "  部署完成!"
echo "============================================"

if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    echo -e "  服务状态: ${GREEN}运行中 ✓${NC}"
else
    echo -e "  服务状态: ${RED}未运行 ✗${NC}"
    echo "  排查命令: sudo journalctl -u $SERVICE_NAME -n 50 --no-pager"
fi

# 端口检查
if ss -tlnp 2>/dev/null | grep -q ":$PORT "; then
    echo -e "  端口 $PORT: ${GREEN}监听中 ✓${NC}"
else
    echo -e "  端口 $PORT: ${RED}未监听 ✗${NC}"
    echo "  排查命令: sudo journalctl -u $SERVICE_NAME -n 30"
fi

echo ""
echo "  后续操作:"
echo "  1. 编辑 API Key:    nano $APP_DIR/.env"
echo "  2. 重启服务:        sudo systemctl restart $SERVICE_NAME"
echo "  3. 查看实时日志:    sudo journalctl -u $SERVICE_NAME -f"
echo "  4. 查看服务状态:    sudo systemctl status $SERVICE_NAME"
echo "  5. 阿里云安全组:    入方向 允许 TCP $PORT"
echo ""
echo "  访问: http://<服务器IP>:$PORT"
echo "============================================"

exit $HAS_ERROR
