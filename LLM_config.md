# LLM 配置指南

## 配置方式

复制 `.env.example` 为 `.env`，填入对应提供商的配置即可。

```bash
cp .env.example .env
```

## 支持的 LLM 提供商

系统基于 [LiteLLM](https://docs.litellm.ai/) 统一接口，支持以下提供商的 API Key 方式接入：

### DeepSeek (推荐，性价比高)

```ini
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat
LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LLM_API_BASE=https://api.deepseek.com/v1
LLM_MAX_TOKENS=2048
LLM_TEMPERATURE=0.3
```

获取 Key: https://platform.deepseek.com/

---

### OpenAI

```ini
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LLM_API_BASE=https://api.openai.com/v1
LLM_MAX_TOKENS=2048
LLM_TEMPERATURE=0.3
```

获取 Key: https://platform.openai.com/api-keys

---

### Anthropic Claude

```ini
LLM_PROVIDER=anthropic
LLM_MODEL=claude-3-haiku-20240307
LLM_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LLM_API_BASE=https://api.anthropic.com
LLM_MAX_TOKENS=2048
LLM_TEMPERATURE=0.3
```

获取 Key: https://console.anthropic.com/

---

### 阿里通义千问 (Qwen)

```ini
LLM_PROVIDER=openai
LLM_MODEL=qwen-turbo
LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LLM_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MAX_TOKENS=2048
LLM_TEMPERATURE=0.3
```

获取 Key: https://dashscope.console.aliyun.com/

---

### 智谱 GLM

```ini
LLM_PROVIDER=openai
LLM_MODEL=glm-4-flash
LLM_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx.xxxxxxxx
LLM_API_BASE=https://open.bigmodel.cn/api/paas/v4
LLM_MAX_TOKENS=2048
LLM_TEMPERATURE=0.3
```

获取 Key: https://open.bigmodel.cn/

---

### Moonshot (月之暗面 Kimi)

```ini
LLM_PROVIDER=openai
LLM_MODEL=moonshot-v1-8k
LLM_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LLM_API_BASE=https://api.moonshot.cn/v1
LLM_MAX_TOKENS=2048
LLM_TEMPERATURE=0.3
```

获取 Key: https://platform.moonshot.cn/

---

### 本地模型 (Ollama)

```ini
LLM_PROVIDER=openai
LLM_MODEL=qwen2.5:7b
LLM_API_KEY=ollama
LLM_API_BASE=http://localhost:11434/v1
LLM_MAX_TOKENS=2048
LLM_TEMPERATURE=0.3
```

无需 Key，本地已拉取的模型即可。参考: https://ollama.com/

---

## 参数说明

| 参数 | 说明 | 建议值 |
|------|------|--------|
| `LLM_PROVIDER` | 提供商标识 | `deepseek` / `openai` / `anthropic` |
| `LLM_MODEL` | 模型名称 | 用于公告原文理解及结构化证据分析，效果需前向验收 |
| `LLM_API_KEY` | API 密钥 | 从对应平台获取 |
| `LLM_API_BASE` | API 端点地址 | 兼容 OpenAI 格式的均可 |
| `LLM_MAX_TOKENS` | 最大输出 Token | `2048`（示例值，需容纳事件/引用/冲突/未知项；运行默认4096） |
| `LLM_TEMPERATURE` | 随机性 0-2 | `0.3` (分析任务需要稳定一致) |

## 模型选择建议

系统对模型能力要求不高（输入约200 tokens，输出约50 tokens），轻量模型即可胜任：

| 场景 | 推荐模型 | 预估成本/千次 |
|------|---------|--------------|
| 最佳性价比 | DeepSeek-V3 | < ¥0.5 |
| 最高质量 | Claude 3 Haiku / GPT-4o-mini | ~$0.5 |
| 国内合规 | Qwen-Turbo / GLM-4-Flash | < ¥1 |
| 数据不出本机 | Ollama + Qwen2.5:7B | 免费 |

## 验证配置

```bash
# 测试LLM是否正常连接
python main.py --test --stages 4
```

正式推荐始终使用规则评分。LLM调用失败或JSON校验失败只标记旁路“未评估”，不改变评分、排序、推荐等级或阈值；报告及Web日志保留状态。

## 禁用 LLM（纯规则模式）

两种方式：

```bash
# 方式1: 命令行参数
python main.py --test --no-llm

# 方式2: .env 中不填 LLM_API_KEY
# LLM_API_KEY=
```

## 公告证据旁路配置

LLM不生成交易分数/买卖指令，而是分析公告事件、影响对象、事实/传闻/推断、引用、信号冲突及未知项。引用仅可忽略空白/换行差异，其他字符变化一律不放行；原始引用与exact/whitespace_only匹配方式均保留。匹配不等于事实已经独立核实。提示词要求区分计划上限与执行结果、保留已知年度期限，无可证实冲突时输出空conflicts。

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `LLM_EVIDENCE_MAX_ITEMS` | 3 | 每股最多尝试获取的公告原文条数（失败也计入） |
| `LLM_EVIDENCE_MAX_CHARS` | 4000 | 每条公告摘录字符上限，超出标记截断 |
| `LLM_EVIDENCE_TIMEOUT` | 5 | 列表/正文每次HTTP超时秒数 |
| `LLM_EVIDENCE_LOOKBACK_DAYS` | 3 | 证据回看自然日数，仅作用于旁路 |
| `LLM_MAX_WORKERS` | 4 | 旁路并发数 |

未提供精确发布时间、晚于分析时点或缺少正文的公告不作为可引用证据；没有获取到不代表无风险。后台独立`.llm.json`保留全部候选的输入提示词、模型标识、原始响应及证据；规则报告链接到补充文件，不等待旁路完成。

既有.env不会自动调整。若此前使用512输出token，可参照示例检查输出预算并观察JSON完整性；诊断不会自动覆盖密钥。真实调用与质量记录见[旁路设计](docs/llm-sidecar-analysis.md)，本轮耗时验收保持现有8196预算。

## 旁路耗时配置（2026-10-30）

| 参数 | 默认值 | 含义 |
|------|--------|------|
| `LLM_THINKING_ENABLED` | false | 仅DeepSeek，通过extra_body发送thinking.type=disabled；true启用思考。其他提供商忽略该参数 |
| `LLM_MAX_RETRIES` | 0 | 客户端额外重试次数；SDK隐藏重试固定关闭，避免叠加。显式启用后总耗时可能超过单次请求超时 |
| `LLM_TIMEOUT` | 30 | 单次模型HTTP请求上限；后台任务还会取整批剩余预算的较小值 |
| `LLM_BATCH_TIMEOUT` | 30 | 整批旁路从规则发布后起算，含等待后台启动、排队、限流、公告和模型；不按股票数放大 |

官方文档说明DeepSeek默认思考开启，可用[thinking开关](https://api-docs.deepseek.com/guides/thinking_mode/)关闭。代码默认已关闭，现有.env没有该项时也生效，不必改写密钥文件。实际HTTP体及返回思考内容均已核对。

本批公告最终方案连续3次4.897～5.768秒，13/13引用通过。未降低原有8196输出预算，也未截短输入证据；精简的是重复事件/表述和不必要的法规长引文。先前仅关闭思考仍出现长引用内容变化，第一版精简仍出现年度语义问题，均未判通过。完整结果及适用范围见[耗时专项](docs/llm-latency-optimization.md)。

当前正式推荐不再等待旁路：推荐、模拟交易和完成状态先以单事务发布，旁路初始化/启动失败独立回滚。单次HTTP和客户端可选重试都受整批剩余预算约束；默认仍零重试。公告HTTP读取超时不等于总耗时限制，因此带截止的请求使用spawn子进程；启动开销也计入整批预算，到期终止并回收进程后释放共享门控。未带截止的旧请求仍使用原Session，限流频率不变；代价是旁路逐次请求不复用连接且有进程启动开销，实际覆盖率需生产观察。模型请求仍取剩余超时，迟到结果不采纳。新建的pipeline_sidecars状态表与旧推荐表分离；进程重启不恢复任务，过期项显示未评估。详情见[发布隔离](docs/llm-sidecar-publication.md)。
