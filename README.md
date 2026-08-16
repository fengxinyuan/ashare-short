# A-share Ultra-Short AI Decision Support

A 股超短线事实采集与分析辅助系统。只做采集、整理、提醒和复盘，不下单、不撤单、不接管账户。

默认工作方式已经收敛成两层：

- 系统负责：舆情、股价、分时、板块、公告、轮动等事实采集与结构化输出
- 你负责：在 Codex / Claude 这类交互窗口里读取这些事实，再做总结和分析

系统默认**不自动调用模型**。模型相关能力都改成显式开关。

**标的由你给出。** 自选股和候选池已经删掉：`advise` / `ask` / `chat` 只看你在
命令里写出来的代码，省略时才回落到持仓表。系统替你记着的名单只剩持仓一份——
它要回答的是「该不该卖」，那个问题不该要求你每次重新敲一遍代码。

从这里开始：[`docs/USAGE.md`](docs/USAGE.md)

## 能做什么

| 场景 | 命令 | 结果 |
|---|---|---|
| 趋势分析 | `ashare-ds morning` / `ashare-ds ask "今天板块趋势怎么样"` | 盘前大盘、板块排序、当日候选 |
| 参数化选股 | `ashare-ds strategy-screen --profile all` | TickFlow 全市场日线筛选，多策略结果供 AI 比较 |
| 股价分析 | `ashare-ds advise 600519.SH` / `ashare-ds ask "600519 现在怎么看"` | 单票结构、分时、成交量、盘口、买卖点 |
| 盘中监控 | `ashare-ds monitor --once` | 板块状态 + 持仓卖点；不传代码就只盯持仓 |
| facts 导出 | `ashare-ds facts` | 导出当前事实、宏观联动和近期验证窗口，供 Codex / Claude 使用 |
| 外部 AI 工具 | `ashare-ds-mcp` | 通过 MCP 向 Codex / Claude Code 提供只读事实与研究资料包 |
| 全板块龙头 | `ashare-ds sector-leaders --refresh` | 每个行业/概念的量价龙头候选，不等同产业龙头或上涨预测 |
| 月度研究包 | `ashare-ds monthly-context` | 本地事实 + 历史记录 + 事件核验任务，交给外部 AI 做月度推演 |
| facts 对话 | `ashare-ds chat` | 多轮查看同一份事实包；默认不调模型 |
| 显式模型分析 | `ashare-ds ask --llm "600519 现在怎么看"` | 把事实包交给模型组织语言，数字仍过校验闸门 |
| 买卖点裁决 | `ashare-ds advise --adjudicate --writer llm 600519.SH` | 在规则候选里做取舍，可弃权；仅在显式开启时调用模型 |

## 快速开始

```bash
cd a-share-ultra-short-ai-system
python3 -m pip install -e ".[tickflow,akshare]"

cp .env.tickflow.example .env.tickflow
cp .env.feishu.example .env.feishu   # 可选，填 webhook 后 `--feishu` 自动推送
```

常用命令：

```bash
ashare-ds position open 600519.SH --cost 1350.6 --stop 1330 --target 1430
ashare-ds position list

ashare-ds morning
ashare-ds monitor --once
ashare-ds monitor --feishu
ashare-ds advise 600519.SH
ashare-ds facts
ashare-ds ask "600519 现在趋势和买点怎么样"
ashare-ds ask --llm "600519 现在趋势和买点怎么样"
ashare-ds ask --deep "这几只低开了，结合成交量怎么处理"
ashare-ds chat
ashare-ds review

# 全市场策略筛选
ashare-ds strategy-screen --profile high-elasticity --price-max 60 --limit 30
ashare-ds strategy-screen --profile all --price-max 40 --min-amount-yi 3 --limit 50
ashare-ds strategy-screen --profile all --all-a --price-max 40
```

筛选结果默认落在 `briefs/strategy_screen_latest.json`，`ask` / `facts` 会自动带入最近一份结果。ST/退市风险标的默认纳入，并在候选的 `warnings` 中明示；需要排除时显式加 `--exclude-st`。批量模式会输出各策略的诊断漏斗、跨策略多样化短名单和重复命中标的；`setup_rank` 只用于同一策略内排序，不是胜率、收益率或次日收益承诺。筛选不生成或执行订单。

如果你想让系统保持完全确定性，直接用默认值即可：`writer` 默认就是 `template`，
`ask` / `chat` 默认就是 facts-only。只有显式加 `--llm` 或 `--writer llm` 才会走模型。

## 推荐工作流

1. 用 `morning` / `advise` / `monitor` 先把事实和结构化结论跑出来。
2. 用 `facts` 导出统一 bundle，直接在 Codex / Claude 窗口里粘贴或引用这些 facts / briefs 做后续分析。包内同时有当前事实和最近 20/60 个交易日的验证结果，避免只根据单次晨报下结论。JSON 包带有 `schema: "ashare-facts-v1"` 和 `contract` 字段：`as_of` 是统一数据时点，缺失值保持为 `null`/空集合，不能由分析者补猜；数值由确定性规则层产生，模型只负责解释和取舍。
3. 如果只是想在命令行里让模型代写，再显式加 `--llm` 或 `--writer llm`。最终交易判断和执行由你自行决定。

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/USAGE.md`](docs/USAGE.md) | 场景到命令的对应表，优先看这个 |
| [`docs/CODEX_WORKFLOW.md`](docs/CODEX_WORKFLOW.md) | Codex 做链路、板块与个股分析的事实包工作流与边界 |
| [`docs/EXTERNAL_AI_TOOLS.md`](docs/EXTERNAL_AI_TOOLS.md) | Codex / Claude Code 的 MCP 接入、月度展望与全板块龙头工具 |
| [`docs/SOLUTION.md`](docs/SOLUTION.md) | 总览 · 需求拆解 · 运行链路 · 设计约束 |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 模块地图 · 调度 · 状态 · 失败模式 |
| [`docs/INVENTORY.md`](docs/INVENTORY.md) | **功能接线清单**：哪个模块今天真的跑、哪些建成但未接线 |
| [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) | 数据源实测矩阵与安装说明 |
| [`docs/PIPELINE.md`](docs/PIPELINE.md) | 舆情 → 板块 → 选股 → 买卖点的计算细节 |
| [`docs/KNOWLEDGE_VOLUME_PRICE.md`](docs/KNOWLEDGE_VOLUME_PRICE.md) | 量价与形态知识库：白皮书提炼、尾盘打法、K 线词汇与各自的落点 |
| [`docs/PROMPT_TEMPLATES.md`](docs/PROMPT_TEMPLATES.md) | **交给外部模型的提示词都在这里**：每日盘前固定版、研究版、多 Agent 编排与数字闸门口径 |
| [`docs/PEER_TRADINGAGENTS.md`](docs/PEER_TRADINGAGENTS.md) | 同类开源项目对照：哪些做法值得抄、哪些不适用 |
| [`docs/PEER_DAILY_STOCK_ANALYSIS.md`](docs/PEER_DAILY_STOCK_ANALYSIS.md) | 同类开源项目对照（daily_stock_analysis）：护栏 · 降级读数 · 组合层 |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | 设计取舍、被否决方案、已删除内容 |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | **下一轮**：体检结论、七个真缺口、量价形态/轮动/记忆层设计与实施顺序 |
| [`docs/REWORK.md`](docs/REWORK.md) | **本轮改造方案**：主链路清理 · 量价策略 · 筹码与元数据 · 行情记忆 |

## 约束

- 只做分析辅助，不接管证券账户。
- 数字只在确定性层产生；模型只负责组织语言和取舍，而且默认关闭。
- 盘口、成交量、趋势、买卖点都可以问，但都必须标明数据时点。

## 目录

```text
docs/      设计、链路、数据源与使用指南
scripts/   审计脚本
src/       源码
tests/     单元测试
```

## 开发

```bash
python3 -m pip install -e ".[dev]"

python3 scripts/check.py     # ruff + 接线审计 + 文档审计 + pytest
python3 scripts/check.py --fast  # 跳过 pytest，只跑三道静态闸门

python3 -m pytest -q         # 单元测试，不打外网
ruff check .                 # 静态检查
ruff check . --fix           # 只应用安全修复

python3 scripts/inventory_audit.py   # 六条接线口径，见 docs/INVENTORY.md
python3 scripts/docs_audit.py        # 文档里的每条 `ashare-ds` 命令都还跑得通
```

`ruff` 的规则族与每一条忽略的理由写在 `pyproject.toml` 的 `[tool.ruff.lint]`
里。裸跑默认规则会报几千条中文标点告警，那个信噪比下没人会看第二次——所以规则
是显式列出来的，加规则请连同理由一起加。
