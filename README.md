# A股尾盘分析系统 (Late Session Analysis)

基于实时数据扫描+4层漏斗筛选+LLM辅助决策的A股尾盘交易分析系统。

## 核心定位

14:30-15:00期间渐进式扫描全市场5000+股票，识别尾盘存在交易潜力的标的，14:58前给出买入建议，实现T+0尾盘买入、T+1开盘卖出获利的交易策略。

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置LLM (可选)
cp .env.example .env
# 编辑 .env 填入 LLM_API_KEY

# 3. 快速测试 (用当前行情跑完整流程)
python main.py --test

# 4. 实时模式 (仅14:25-15:05有效)
python main.py

# 5. 定时调度 (14:29自动启动)
python main.py --schedule

# 6. 仅数据拉取 (不分析)
python main.py --test --dry-run

# 7. 禁用LLM，纯规则评分
python main.py --test --no-llm
```

## 架构

```
数据采集层 → 筛选漏斗引擎 → [缓存层] → LLM/规则分析 → 报告生成
  (efinance     L1→L2→L3→L4        (LLM并行+规则兜底)   (Jinja2+imgkit)
   →akshare)
```

## 项目结构

```
late_session_analysis/
  main.py                    # CLI入口
  data_provider/             # 数据采集 (多源降级)
  screening/                 # 4层筛选漏斗
  analysis/                  # LLM + 规则评分
  report/                    # 报告模板+渲染
  orchestration/             # 编排+配置+调度
  tests/                     # 单元测试
```

## 运行测试

```bash
pytest tests/ -v
```
