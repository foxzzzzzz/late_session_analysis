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
LLM_MAX_TOKENS=512
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
LLM_MAX_TOKENS=512
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
LLM_MAX_TOKENS=512
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
LLM_MAX_TOKENS=512
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
LLM_MAX_TOKENS=512
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
LLM_MAX_TOKENS=512
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
LLM_MAX_TOKENS=512
LLM_TEMPERATURE=0.3
```

无需 Key，本地已拉取的模型即可。参考: https://ollama.com/

---

## 参数说明

| 参数 | 说明 | 建议值 |
|------|------|--------|
| `LLM_PROVIDER` | 提供商标识 | `deepseek` / `openai` / `anthropic` |
| `LLM_MODEL` | 模型名称 | 轻量模型即可，单次分析只需20+字段判断 |
| `LLM_API_KEY` | API 密钥 | 从对应平台获取 |
| `LLM_API_BASE` | API 端点地址 | 兼容 OpenAI 格式的均可 |
| `LLM_MAX_TOKENS` | 最大输出 Token | `512` (系统仅需简短JSON输出) |
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

如果 LLM 调用失败，系统会自动降级为**纯规则评分模式**，不影响报告产出。报告顶部会显示降级警告。

## 禁用 LLM（纯规则模式）

两种方式：

```bash
# 方式1: 命令行参数
python main.py --test --no-llm

# 方式2: .env 中不填 LLM_API_KEY
# LLM_API_KEY=
```
