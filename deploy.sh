#!/bin/bash
# ============================================================
# 尾盘分析系统 — 阿里云一键部署脚本
# 执行方式: chmod +x deploy.sh && ./deploy.sh
# ============================================================
set -e

APP_DIR="/home/admin/late_session_analysis"
REPO_URL="https://github.com/foxzzzzzz/late_session_analysis.git"
PORT=5000

echo "============================================"
echo "  尾盘分析系统 Web Dashboard 部署"
echo "============================================"

# ── 1. 基础检查 ──────────────────────────────────────────
echo ""
echo "[1/6] 检查 Python 环境..."
python3 --version || { echo "请先安装 python3"; exit 1; }

# ── 2. 克隆代码 ──────────────────────────────────────────
echo ""
echo "[2/6] 下载代码..."
if [ -d "$APP_DIR" ]; then
    echo "  目录已存在，执行 git pull 更新..."
    cd "$APP_DIR"
    git pull origin main
else
    git clone "$REPO_URL" "$APP_DIR"
    cd "$APP_DIR"
fi

# ── 3. 安装依赖 ──────────────────────────────────────────
echo ""
echo "[3/6] 安装 Python 依赖..."
pip3 install -r requirements.txt -r web/requirements-web.txt -q

# ── 4. 配置环境变量 ──────────────────────────────────────
echo ""
echo "[4/6] 配置 .env..."
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
    else
        cat > .env << 'ENVEOF'
LLM_API_KEY=sk-your-key-here
LLM_API_BASE=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
ENVEOF
    fi
    echo "  已创建 .env 文件，请编辑填入 LLM_API_KEY:"
    echo "  nano $APP_DIR/.env"
else
    echo "  .env 已存在，跳过"
fi

# ── 5. 创建必要目录 ──────────────────────────────────────
echo ""
mkdir -p web_instance reports backtest_reports backtest_cache live_snapshots
echo "  数据目录已创建"

# ── 6. 配置 systemd 开机自启 ─────────────────────────────
echo ""
echo "[5/6] 配置 systemd 服务..."
SERVICE_FILE="/etc/systemd/system/late-session.service"

sudo tee "$SERVICE_FILE" > /dev/null << SERVEOF
[Unit]
Description=Late Session Analysis Web Dashboard
After=network.target

[Service]
Type=simple
User=admin
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 -m web.app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=10
Environment=TZ=Asia/Shanghai
Environment=WEB_INSTANCE_DIR=$APP_DIR/web_instance

[Install]
WantedBy=multi-user.target
SERVEOF

sudo systemctl daemon-reload
sudo systemctl enable late-session
sudo systemctl start late-session

# ── 完成 ──────────────────────────────────────────────────
echo ""
echo "[6/6] 部署完成!"
echo "============================================"
echo "  服务状态:"
sudo systemctl status late-session --no-pager -l 2>/dev/null || echo "  请运行: sudo systemctl status late-session"
echo ""
echo "  访问地址: http://$(curl -s ifconfig.me 2>/dev/null || echo '<服务器IP>'):$PORT"
echo ""
echo "  后续操作:"
echo "  1. 编辑 API Key:  nano $APP_DIR/.env"
echo "  2. 重启服务:      sudo systemctl restart late-session"
echo "  3. 查看日志:      sudo journalctl -u late-session -f"
echo "  4. 停止服务:      sudo systemctl stop late-session"
echo "  5. 阿里云安全组:  开放 TCP $PORT 端口"
echo "============================================"
