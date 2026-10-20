# 市场状态识别数据链路修复

## 问题与证据

2026-10-20 腾讯云 Ubuntu 运行日志显示，两次自动判定均出现“上证指数数据不可用，使用中性阈值”。随后固定8个板块从 baostock 缓存匹配出1841只候选。这是数据失败后的中性回退，不能解释为真实行情中性。

原指数获取直接创建 mootdx 默认客户端，绕过项目的健康服务器池；空返回不记录服务器及失败原因，也没有逐台切换。

此前怀疑代码999999有误，但本地对日志中的117.34.114.17:7709实测，999999和000001均返回50根相同的上证日线。因此不能将旧代码值单独认定为此次故障根因。改用上游示例的 `index(symbol='000001', frequency=9)`，关键修复是接入健康池并验证指数返回值。上游参考：https://github.com/mootdx/mootdx#使用说明 。

## 解决方案

- 指数获取复用 `get_default_pool()`，按顺序尝试服务器。
- 指数接口固定使用上证代码000001和日线frequency=9，避免使用个股bars接口。
- 拒绝空返回、缺少close、少于25根、非数字/非正数/无穷收盘价，并切换下一台。
- 记录失败服务器、具体原因及成功根数；所有尝试失败时明确标记回退中性。指数失败不将服务器从共享的个股服务器池移除。
- 连接在尝试结束后关闭；禁用客户端内部重试，由本层控制切换次数。
- 行业预加载后市场状态改变时，同时更新S1 K线阈值。

`.env` 可配置以下参数；未设置时使用相同默认值，不需要覆盖服务器已有配置：

| 参数 | 默认值 | 含义 |
|------|--------|------|
| MARKET_INDEX_BARS | 50 | 请求日线根数，应至少25根 |
| MARKET_INDEX_TIMEOUT | 5 | 单次网络操作超时秒数 |
| MARKET_INDEX_MAX_SERVERS | 3 | 指数获取最多尝试服务器数 |

最大尝试次数不限制共享服务器池首次无缓存时的扫描耗时。仍保留现有数据全失败时的中性回退策略；不扩大全市场候选池、不调整策略评分阈值。

## 验证

新增测试在修复前复现：指数链路11项失败；预加载后S1阈值同步测试失败。修复后执行：

```bash
python -m pytest tests/test_market_regime.py tests/test_pipeline_snapshots.py tests/test_pipeline_enrich.py tests/test_pipeline_stage2.py tests/test_kline_provider.py -q
```

结果：73项通过。覆盖正常获取、空/异常/不足/无效数据换台、配置生效、全失败回退、上涨/下跌判定及S1阈值同步。

本地联网验证修复后的 `_fetch_sh_index()`，使用仅包含日志中可用服务器的临时池：成功取得50根上证日线，末条2026-10-20 15:00，收盘值3735.37。该结果只证明本地网络和此服务器的获取链路；Ubuntu实际运行仍需部署验收。

## Ubuntu部署与验收

修复推送后，在服务器项目根目录拉取更新：

```bash
git pull --ff-only origin main
```

若服务器此前已手动应用补丁，先检查 `git status`，不要强制覆盖已有修改。

也可使用补丁部署：将补丁上传至项目根目录的tmp文件夹后，依次执行以下两条命令（与拉取更新二选一）：

```bash
git apply --check tmp/market-regime-fix-20261020.patch
git apply tmp/market-regime-fix-20261020.patch
```

更新后执行测试：

```bash
.venv/bin/python -m pytest tests/test_market_regime.py tests/test_pipeline_snapshots.py tests/test_pipeline_enrich.py tests/test_pipeline_stage2.py tests/test_kline_provider.py -q
```

若虚拟环境没有pytest，先在现有虚拟环境中安装pytest。补丁检查失败时停止，不使用强制覆盖；可能是服务器源码版本与本地不同。

随后只验证数据获取，不执行交易管线、不改变市场防抖状态：

```bash
.venv/bin/python - <<'PY'
import logging
from dotenv import load_dotenv
load_dotenv('.env')
logging.basicConfig(level=logging.INFO)
from screening.market_regime import _fetch_sh_index
df = _fetch_sh_index()
if df is None:
    raise SystemExit('FAIL: 上证指数数据仍不可用，请提供本条命令完整日志')
print(df.tail(3).to_string())
print('PASS: 上证指数有效日线', len(df), '根；请确认末条交易日期符合预期')
PY
```

成功后重启服务加载代码：

```bash
sudo systemctl restart late-session
sudo systemctl is-active late-session
```

下次正常运行检查“上证指数日线获取成功”和包含score、20日收益率、MA20及行业宽度的市场判定日志，不以S0是否变化作为修复成功标准：真实中性或牛市仍使用固定板块。

## 后续独立问题

原日志S2五分钟线仅成功1/13只，量条件失败13只，导致13→0。指数修复不等于此问题解决；不通过放宽阈值掩盖缺数据。先完成Ubuntu指数链路验收，再定位五分钟线问题、评估全股票池。
