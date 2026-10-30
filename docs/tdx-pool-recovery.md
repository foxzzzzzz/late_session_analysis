# TDX服务器池无法恢复问题

## 现象与分析

2026-10-22指数成功，服务器池有8台；23、26、27、28日每轮扫描135台均为0，指数回退中性，S0恢复固定1841只。同期5分钟线指标均为0，S2全部淘汰。本地同版本已知服务器共143台，143-8=135，与可用节点被长期排除的推断一致，但云端完整名单和首次失败原因仍未核实。

代码确认：`mark_failed()`移除可用节点并加入进程级`_failed`，`scan()`过滤这些节点，`refresh()`只有找到可用节点才清空失败集合。该机制可导致临时失败节点即使已恢复，也永远不能重新入池。模拟测试已复现。

## 修复范围

全量扫描重新探测所有已知服务器，包括此前失败者；仍有可用节点时直接返回，不新增扫描。新增日志展示全量节点数及此前失败数。不增加等待、不增加重试轮次、不引入定时器或新的配置参数。

本次只修复服务器池恢复缺陷，未调整5分钟线并发客户端、数据回退或筛选阈值。即使修复后，真实网络/节点不可用时仍可能扫描0台，不能保证云端恢复。

## 验证

- 修复前：3个新增测试中2个恢复测试失败，健康节点不触发扫描测试通过。
- 修复后：服务器池、指数判定、K线与相关管线测试共76项通过。
- 2026-10-28本地独立进程探测117.34.114.13和117.34.114.17失败，未取得有效K线。不能把本地结果当作腾讯云测试结果。

## Ubuntu非交易时段探测

历史指数日线和已完成5分钟K线可在非交易时段查询。以下命令无需应用修复、不重启服务、不运行交易管线、不修改服务器池缓存；独立连接可绕开旧服务进程的失败集合。每个接口单独记录结果，即使指数失败也继续验证5分钟线。

```bash
cd ~/late_session_analysis
mkdir -p tmp
.venv/bin/python - <<'PY' > tmp/tdx-direct-check.log 2>&1
from pytdx.hq import TdxHq_API
for host in ('117.34.114.13', '117.34.114.17'):
    api = TdxHq_API(auto_retry=False, raise_exception=True)
    try:
        connected = api.connect(host, 7709, time_out=3)
        print(host, 'connected=', bool(connected), flush=True)
        if not connected:
            continue
        for kind in ('index_daily', 'stock_5min'):
            try:
                rows = (api.get_index_bars(9, 1, '000001', 0, 50)
                        if kind == 'index_daily'
                        else api.get_security_bars(0, 0, '000001', 0, 48))
                print(host, kind, 'rows=', len(rows or []),
                      'latest=', rows[-1].get('datetime') if rows else None,
                      flush=True)
            except Exception as exc:
                print(host, kind, type(exc).__name__, str(exc), flush=True)
    except Exception as exc:
        print(host, 'CONNECT', type(exc).__name__, str(exc), flush=True)
    finally:
        try:
            api.disconnect()
        except Exception as exc:
            print(host, 'CLOSE', type(exc).__name__, str(exc), flush=True)
PY
cat tmp/tdx-direct-check.log
```

若独立进程成功而旧服务扫描0台，支持失败集合问题，但两者测试时点不同，不能排除时变网络。若仍失败，应进一步检查服务器端出站网络、节点状态及协议请求，不能通过继续扩大候选池解决。需要时再扫描完整名单，避免将两台节点的失败推广为所有节点失败。

代码部署后须重启服务载入修复。验收日志应显示全量节点重新参与探测，恢复后指数/5分钟线返回有效数据；不以S0必须改变或必须产生推荐作为成功标准。

## 2026-10-29 全量双客户端诊断

新增独立工具，不改动生产服务器池、交易管线或依赖。诊断参数及含义位于 `tools/tdx_diagnostic_config.json`；默认12个节点并发、单次网络操作3秒超时，三种查询使用独立连接。查询失败前捕获响应正文长度、前64字节十六进制及按现有协议解读的记录数；这些字段仅供诊断，不视为有效行情。报告中的ok表示解析返回了记录，是否新鲜、是否足够根数仍须结合latest及rows判断。

```bash
cd ~/late_session_analysis
mkdir -p tmp
.venv/bin/python tools/diagnose_tdx_access.py --output tmp/tdx-cloud-diagnostic.json > tmp/tdx-cloud-diagnostic.log 2>&1
cat tmp/tdx-cloud-diagnostic.log
```

默认使用完整已知节点名单（不读失败集合），可用 `--hosts IP:端口 IP:端口` 限定节点。结果包含时间、依赖版本、配置、逐节点TCP状态、pytdx/tdxpy的三类查询结果和统计。扫描完成后提供JSON和日志文件；不要在14:35选股管线并行执行全量诊断。

测试：新增5项诊断测试全部通过，与服务器池及市场状态测试合计22项通过。覆盖短包原始证据、正常记录、连接异常、查询隔离、两种后端加载。

本地2026-10-29 14:19完成完整143台诊断：100台TCP失败，43台TCP成功。pytdx的129次查询全部解析失败；tdxpy的129次查询中128次解析失败、1次连接失败。257次解析失败捕获的正文均只有2字节`2003`，没有有效K线。该结果仅代表本地网络与该时点，不替代腾讯云完整诊断，也不能把2003直接解释为某个服务端状态码。

当前证据不支持仅替换pytdx为已安装tdxpy即可解决问题；也不能据此认定整个TDX协议变化。先完成腾讯云全量对照，再决定更换节点、协议兼容处理或独立数据源回退。

2026-10-30 10:43云端完整扫描143台，约25秒完成：44台TCP成功、99台失败；两种客户端各132次查询均解析失败，264次正文都是2字节2003。未取得有效K线。交易时段复测已否定仅因休市失败的解释，但未确定短包的服务端含义；后续采用HTTP备用源恢复取数，TDX根因继续调查。
