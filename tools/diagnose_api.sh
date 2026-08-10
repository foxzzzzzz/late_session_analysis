#!/bin/bash
# 云服务器 API 连通性诊断
# 用法: bash diagnose_api.sh
set -e

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$APP_DIR/.venv/bin/python3"
[ -f "$PYTHON" ] || PYTHON=python3  # fallback to system python if no venv

echo "========================================"
echo "  API 连通性诊断"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================"

# 1. 东财 push2 分钟线
echo ""
echo "[1/5] 东财 push2 分钟线..."
$PYTHON -c "
import urllib.request, ssl
try:
    ctx = ssl.create_default_context()
    url = 'https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid=0.000001&fields1=f1,f2,f3,f7&fields2=f51,f52&klt=1&lmt=10'
    r = urllib.request.urlopen(url, timeout=15, context=ctx)
    print(f'  东财push2 OK: {len(r.read())} bytes')
except Exception as e:
    print(f'  东财push2 FAIL: {e}')
"

# 2. 东财 push2his 历史
echo ""
echo "[2/5] 东财 push2his 历史资金流..."
$PYTHON -c "
import urllib.request, ssl
try:
    ctx = ssl.create_default_context()
    url = 'https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?secid=0.000001&fields1=f1,f2,f3,f7&fields2=f51,f52&lmt=10'
    r = urllib.request.urlopen(url, timeout=15, context=ctx)
    print(f'  东财push2his OK: {len(r.read())} bytes')
except Exception as e:
    print(f'  东财push2his FAIL: {e}')
"

# 3. 新浪资金流
echo ""
echo "[3/5] 新浪资金流..."
$PYTHON -c "
import urllib.request, ssl
try:
    ctx = ssl.create_default_context()
    url = 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=5&sort=opendate&asc=20260722&daima=000001'
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'Mozilla/5.0')
    r = urllib.request.urlopen(req, timeout=15, context=ctx)
    print(f'  新浪资金流 OK: {len(r.read())} bytes')
except Exception as e:
    print(f'  新浪资金流 FAIL: {e}')
"

# 4. 腾讯行情
echo ""
echo "[4/5] 腾讯行情..."
$PYTHON -c "
import urllib.request
try:
    url = 'https://qt.gtimg.cn/q=sh000001'
    req = urllib.request.Request(url)
    req.add_header('User-Agent', 'Mozilla/5.0')
    r = urllib.request.urlopen(req, timeout=10)
    print(f'  腾讯行情 OK: {len(r.read())} bytes')
except Exception as e:
    print(f'  腾讯行情 FAIL: {e}')
"

# 5. 东财 datacenter (限售解禁用)
echo ""
echo "[5/5] 东财 datacenter..."
$PYTHON -c "
import urllib.request, ssl
try:
    ctx = ssl.create_default_context()
    url = 'https://datacenter-web.eastmoney.com/api/data/v1/get?pageSize=1&pageNumber=1&reportName=RPT_LIFT_STAGE&columns=SECURITY_CODE'
    r = urllib.request.urlopen(url, timeout=15, context=ctx)
    print(f'  东财datacenter OK: {len(r.read())} bytes')
except Exception as e:
    print(f'  东财datacenter FAIL: {e}')
"

echo ""
echo "========================================"
echo "  诊断完成"
echo "========================================"
