# API Connectivity Diagnostic (Windows Local)
Write-Host "========================================"
Write-Host "  API Connectivity Diagnostic (Local)"
Write-Host "  Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "========================================"

# 1. Eastmoney push2 (minute flow)
Write-Host "`n[1/5] Eastmoney push2 (minute flow)..."
try {
    $r = Invoke-WebRequest -Uri "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?secid=0.000001&fields1=f1,f2,f3,f7&fields2=f51,f52&klt=1&lmt=10" -TimeoutSec 15 -UseBasicParsing
    Write-Host "  push2 OK: $($r.Content.Length) bytes"
} catch {
    Write-Host "  push2 FAIL: $($_.Exception.Message)"
}

# 2. Eastmoney push2his (daily flow)
Write-Host "`n[2/5] Eastmoney push2his (daily flow)..."
try {
    $r = Invoke-WebRequest -Uri "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?secid=0.000001&fields1=f1,f2,f3,f7&fields2=f51,f52&lmt=10" -TimeoutSec 15 -UseBasicParsing
    Write-Host "  push2his OK: $($r.Content.Length) bytes"
} catch {
    Write-Host "  push2his FAIL: $($_.Exception.Message)"
}

# 3. Sina fund flow
Write-Host "`n[3/5] Sina fund flow..."
try {
    $r = Invoke-WebRequest -Uri "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs?page=1&num=5&sort=opendate&asc=20260722&daima=000001" -TimeoutSec 15 -UseBasicParsing -Headers @{"User-Agent"="Mozilla/5.0"}
    Write-Host "  Sina OK: $($r.Content.Length) bytes"
} catch {
    Write-Host "  Sina FAIL: $($_.Exception.Message)"
}

# 4. Tencent quote
Write-Host "`n[4/5] Tencent quote..."
try {
    $r = Invoke-WebRequest -Uri "https://qt.gtimg.cn/q=sh000001" -TimeoutSec 10 -UseBasicParsing -Headers @{"User-Agent"="Mozilla/5.0"}
    Write-Host "  Tencent OK: $($r.Content.Length) bytes"
} catch {
    Write-Host "  Tencent FAIL: $($_.Exception.Message)"
}

# 5. Eastmoney datacenter
Write-Host "`n[5/5] Eastmoney datacenter..."
try {
    $r = Invoke-WebRequest -Uri "https://datacenter-web.eastmoney.com/api/data/v1/get?pageSize=1&pageNumber=1&reportName=RPT_LIFT_STAGE&columns=SECURITY_CODE" -TimeoutSec 15 -UseBasicParsing
    Write-Host "  Datacenter OK: $($r.Content.Length) bytes"
} catch {
    Write-Host "  Datacenter FAIL: $($_.Exception.Message)"
}

Write-Host "`n========================================"
Write-Host "  Done"
Write-Host "========================================"
