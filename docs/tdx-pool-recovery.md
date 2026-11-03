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

## 2026-11-03 上游调研与新节点对照

本次只分析和独立联网诊断，不修改生产代码、依赖、服务器缓存或部署。下面的实时测试均在本地Windows执行；云端结论沿用上面的10月30日记录，未在本次重新登录云端。

### 判断

目前最直接的证据指向：候选节点名单老化，以及其中节点的标准K线服务不可用。不能把“现有143台候选中没有通过K线验证的节点”等同于“全部TDX服务器断网”。同一本地出口、同一套已安装客户端，新增名单外节点可以成功取数，旧节点仍返回短包。因此，客户端API全面失效、仅腾讯云IP被单独封禁，都不是当前首要解释；云端访问新节点仍待对照。

`2003`是正文十六进制字节`20 03`，按旧K线记录数格式解读为800，但后面没有记录。它不是已被确认的十进制错误码2003，不能据此宣称权限不足或IP封禁。

### 本地验证用例及结果

复用`tools/diagnose_tdx_access.py`，测试前定义四类查询（深圳个股日线、上海个股日线、上证指数日线、深圳个股5分钟线），每类分别请求5、100、800根；两种客户端各使用独立连接。

| 用例 | 结果 |
|------|------|
| 原名单117.34.114.13、180.153.18.170，pytdx和tdxpy，四类查询×三种根数 | TCP可达，48次查询均失败，正文只有`2003`；增加根数未解决 |
| 名单外103.251.85.94:7709，同一查询矩阵 | pytdx 12/12成功；tdxpy 11/12成功、1次连接失败；成功查询根数均与请求一致 |
| 使用生产函数`_probe_server('103.251.85.94', 7709)` | True；确认该地址不在当前143台候选中 |
| 新节点独立补测沪深日线各5根、上证指数50根、沪深5分钟线各48根 | 5/5成功；日线日期为2026-11-03，分钟线末条10:05；日线15:00是当日日bar时间标签，不表示当日已收盘 |
| 另外两个新节点139.9.52.158、103.221.142.82 | 本地TCP失败；不能照搬上游所有标记可用的地址 |

依赖保持pytdx 1.72、tdxpy 0.2.7、mootdx 0.11.7。原始结果分别保存在`tmp/tdx-local-20261103.json`、`tmp/tdx-count-20261103.json`、`tmp/tdx-new-hosts-20261103.json`、`tmp/tdx-new-host-samples-20261103.json`；临时查询矩阵配置为`tmp/tdx-count-config.json`。这些文件在忽略目录中。新节点存在一次连接失败，尚不能据此承诺持续稳定或批量吞吐。

### GitHub对照

- [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis)：本地停留2026-06-11，远端主线最新提交为2026-11-02 UTC。直接读取远端`data_provider/pytdx_fetcher.py`仍使用旧`get_security_bars`接口；该文件最近提交为2026-05-23，没有找到近期K线协议修复。其冷却和多源回退可作工程参考，不能作为TDX已恢复的证据。
- [TradingAgents-astock](https://github.com/simonlin1212/TradingAgents-astock)：本地为v0.2.13时期，远端已到2026-10-31北京时间发布的v0.5.22。远端`a_stock.py`及CHANGELOG增加真实取K线验活、失败客户端丢弃、5分钟负缓存、候选表扩展及BESTIP保护；仍使用mootdx，不是新的协议实现。主线源码与更新日志已下载到`tmp/tdx-research-*`供本次核对。
- 本地qlib与RD-Agent核心代码搜索未发现TDX接入，远端最新提交分别为2026-10-08和2026-09-23 UTC；没有进一步把无关更新当作行情协议修复研究。
- [mootdx issue #157](https://github.com/mootdx/mootdx/issues/157)：2026-07起有多人报告节点空返回、刷新bestip也无法解决，9月仍有失败反馈。讨论中的“协议变化”是用户判断，不是通达信官方结论。
- [pytdxdata协议路由说明](https://openbot-coder.github.io/pytdxdata/internals/routing/)记录标准K线两字节空壳、部分节点转为MAC-only；[协议说明](https://openbot-coder.github.io/pytdxdata/internals/protocol/)还记录部分节点小于100根时空返回。本次已用5/100/800根矩阵排查后一个解释，未能解释所测旧节点失败。MAC-only解释尚未在本项目实测。
- 从[pytdxdata节点清单](https://github.com/openbot-coder/pytdxdata/blob/main/src/pytdxdata/servers.json)发现三个未纳入当前候选表的标准通道节点，其中103.251.85.94已用现有客户端实测成功。这是本次新增的直接证据，无需先整体迁移到该项目。

### 尚待验证及处理顺序

先在云端使用现有工具对新节点做小规模对照：

```bash
.venv/bin/python tools/diagnose_tdx_access.py --hosts 103.251.85.94:7709 117.34.114.13:7709 --output tmp/tdx-cloud-host-compare-20261103.json
```

新节点云端同样成功后，才据此实施最小节点配置更新，并验收生产mootdx路径、指数、沪深分钟线、批量覆盖率与耗时；HTTP回退继续保留。如果本地成功但云端新节点失败，进一步按TCP失败、协议握手失败、K线短包分别调查出口网络或节点访问策略。后续扩大可用名单及MAC通道实验应独立评估，不以本次单节点成功代替稳定性验收。

此外，这次日志在约86秒内进行了四次约21秒的全表扫描。`get_servers()`在空池时每次都会调用`refresh()`，当前没有失败冷却。这会放大等待时间，但不能解释节点为何返回无效K线，应与节点恢复问题分开处理。

## 2026-11-03 云端对照确认与配置修复

用户提供的`tdx-cloud-host-compare-20261103.json`记录云端10:10:51至10:10:54的对照：两台TCP均成功，103.251.85.94在pytdx和tdxpy下的个股日线5根、指数日线50根、5分钟线48根均成功；旧节点117.34.114.13的同六次查询均失败，正文只有`2003`。新节点分钟线末条10:15是当前5分钟bar的时间标签，不表示该bar已结束。该证据排除云端全面不能访问TDX或这两个客户端全面失效的解释；未确认旧节点服务端短包的具体成因。

新增`data_provider/tdx_servers.json`，将验证成功的新节点纳入补充候选；`_collect_servers()`每次收集重新读取配置，再合并原有候选并去重。配置参数有说明，不修改依赖、不绕过K线健康检查、不清除用户缓存。名单从143台增加为144台。配置的顺序是提交探测的顺序，健康池仍按现有并发探测完成顺序收集，不承诺覆盖已有健康缓存或改变节点选用顺序。

实施前新增配置优先合并/去重、下次收集读取更新、配置节点进入健康池且重启复用缓存3项用例；旧实现3失败、原有3通过。修复后服务器池、诊断、市场状态、K线、HTTP回退及相关管线测试合计110项通过。Windows默认pytest临时目录不可访问，验收显式使用仓库`tmp`下独立basetemp。

本地使用真实完整名单、独立健康缓存及隔离mootdx配置完成生产路径验收：144台中1台通过；`KlineProvider.client`选中103.251.85.94，沪深日线各5根、上证指数50根、沪深5分钟线各48根均成功，分钟线末条10:20。结果位于`tmp/tdx-config-live-20261103/result.json`。本次仅验证有限请求，批量吞吐和持续可用性仍需生产观察。

部署时须同步`data_provider/tdx_server_pool.py`及新增`data_provider/tdx_servers.json`，重启服务加载代码。Docker通过现有`COPY . .`包含该文件，需重新构建镜像。核对日志出现扫描144台且可用至少1台、选中103.251.85.94、指数及分钟线TDX成功。实际选股管线覆盖率与耗时待部署后验收；仍保留HTTP降级，扫描冷却另列任务。本次未commit、push、打tag或部署。
