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
import json, os
from mootdx.quotes import Quotes

# 优先用 mootdx 内置 bestip 选择 (真正测试可用性，非裸TCP)
cfg_dir = os.path.expanduser('~/.mootdx')
os.makedirs(cfg_dir, exist_ok=True)

# 尝试标准 factory (会用 bestip 选最快服务器并验证数据)
try:
    q = Quotes.factory(market='std', bestip=True)
    ip, port = q.server
    # 验证真的能拉数据
    d = q.bars(symbol='000001', category=4, offset=5)
    if d is not None and len(d) > 0:
        cfg = {'SERVER': {'HQ': [['bestip', ip, port]]}, 'BESTIP': {'HQ': [ip, port], 'EX': '', 'GP': ''}}
        json.dump(cfg, open(os.path.join(cfg_dir, 'config.json'), 'w'))
        print(f'{ip}:{port}')
    else:
        raise Exception('server OK but returned 0 rows')
except Exception:
    # bestip 可能选中了可达但不返数据的服务器，用已知可用IP兜底
    for fallback_ip in ['115.238.56.198', '110.41.147.114', '8.129.13.54']:
        try:
            q = Quotes.factory(market='std', server=(fallback_ip, 7709), bestip=False)
            d = q.bars(symbol='000001', category=4, offset=5)
            if d is not None and len(d) > 0:
                cfg = {'SERVER': {'HQ': [['fallback', fallback_ip, 7709]]}, 'BESTIP': {'HQ': [fallback_ip, 7709], 'EX': '', 'GP': ''}}
                json.dump(cfg, open(os.path.join(cfg_dir, 'config.json'), 'w'))
                print(f'{fallback_ip}:7709')
                break
        except Exception:
            pass
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

# 确定运行用户及其 HOME (保持一致, 避免 sudo 执行时 $HOME 指向 /root)
RUN_USER="${SUDO_USER:-$USER}"
if [ "$RUN_USER" = "root" ]; then
    RUN_HOME="/root"
else
    RUN_HOME="/home/$RUN_USER"
fi

sudo tee "$SERVICE_FILE" > /dev/null << SERVEOF
[Unit]
Description=Late Session Analysis Web Dashboard
After=network.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
ExecStart=$VENV_DIR/bin/python -m web.app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=10
Environment=TZ=Asia/Shanghai
Environment=HOME=$RUN_HOME
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
