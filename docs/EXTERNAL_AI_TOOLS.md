# Codex / Claude Code 工具接入

## 边界

本项目只负责可复核的 A 股事实与历史统计：行情、行业/概念分类、量价结构、板块内
龙头候选、台账结果和已知事件窗口。Codex / Claude Code 负责：

- 联网查未来一个月的新事件、政策、选举、央行会议和公司公告；
- 核对中秋、春节等移动日期，以及事件是否已经被市场定价；
- 推演“事件 → 海外资产 → A 股行业/概念 → 个股”的传导链；
- 给出情景、确认条件和失效条件，而不是把候选排名写成上涨概率。

项目内的 `leadership_rank` 只表示同一板块内的相对量价与流动性名次。它不是产业
地位、胜率、预期收益，也不是买入建议。产业龙头身份必须由外部一手资料核验。

读事实包时另有三条不能越的线：

- `as_of` 与 `session_date` 不一致时，那份数据只能用于结构回顾，不能当实时行情；
- 缺失字段是 `null` 或空集合，表示**未知**，不是零、也不是否定；
- 基本面、估值、真实筹码分布、两融余额尚无稳定数据源，模型不得代为补造。

各工具的联网与写盘行为见下表。系统登记的名单只有**持仓**
（`ashare-ds position open <代码> --cost <成本价>`）。事实包里的候选是当天晨报的分析结果，
不会自动成为后续默认名单。

## 安装与 MCP

```bash
python3 -m pip install -e ".[tickflow]"
```

仓库中的 `.codex/config.toml` 和 `.mcp.json` 分别用于 Codex 与 Claude Code。
两份配置通过 `python3 -m ashare_decision_support.mcp_server` 拉起同一个 STDIO 服务；
它们包含当前工作区的绝对路径，迁移机器或目录时需要调整 `cwd` 与 `PYTHONPATH`，
并让 `command` 指向已安装本项目的 Python。安装后也可使用 `ashare-ds-mcp` 入口。
CLI 与 MCP 启动时使用相同的[配置加载顺序](DATA_SOURCES.md#配置)。重新进入项目会话后，客户端
可看到八个工具：

| 工具 | 是否联网/写盘 | 用途 |
|---|---|---|
| `get_facts` | 否 | 读取盘前、盘中、盘后、台账和策略扫描事实 |
| `daily_sector_analysis` | 否 | 读取最近一次 `morning` 生成的每日板块分析 |
| `sector_forecast` | 否 | 输出主题簇情景、拥挤度、校准状态和盘中证伪协议 |
| `sector_confirmation` | 否 | 用当天已落盘板块截面确认或否决最新盘前情景 |
| `sector_baseline_backtest` | 否 | 用本地行业指数验证四种固定价格基线的1/3/5日相对收益 |
| `preopen_research_context` | 可能 | 组合 A 股落盘事实与隔夜外盘研究资料；不生成交易指令 |
| `refresh_sector_leaders` | 是 | 扫全市场日线，生成每个行业/概念的量价龙头候选 |
| `monthly_outlook_context` | 否 | 组合月度事实、龙头、轮动记忆、事件窗口和外部研究任务 |

参数按 `inputSchema` 校验类型、枚举、数组成员和数值范围，未知字段在取数或写入前拒绝。
`phase` 只接受 `pre_open` / `intraday` / `post_close` / `off_hours`。
无请求编号的通知不执行工具，也不产生回复；请求编号必须为字符串或整数。
坏消息和工具异常分别报告，并保留有效请求编号；标准输出仅包含协议消息，SDK 日志写入标准错误。

## 按需读取事实

MCP 的 `content` 返回可读的 `rendered`，`structuredContent` 提供结构化结果。
策略扫描摘要每个策略保留前 5 只，多样化短名单保留前 10 只；省略行数写在 `notes`，
完整结果在 `briefs/strategy_screen_latest.json`。

只需要部分事实时，用 `sections` 选择分区：

```json
{"name": "get_facts", "arguments": {"sections": ["盘前主链路", "持仓"]}}
```

省略的分区名称同样写入 `notes`。

盘中和盘后调用 `get_facts` 时，统一事实包会自动增加 `板块预测盘中验证`：它把最新
盘前主题与当天已落盘板块截面对齐。只要先运行过 `monitor --once`，外部模型无需再
拼接两个工具结果；没有有效截面时该块明确返回“等待数据”。

Codex 也可以用官方 CLI 手工登记：

```bash
codex mcp add ashare-analysis -- ashare-ds-mcp
codex mcp list
```

## CLI 等价入口

```bash
# 第一次或需要更新时调用；会访问行情和分类源
ashare-ds sector-leaders --refresh --level all --top 3

# 之后只读已落盘结果
ashare-ds sector-leaders

# 生成交给外部 AI 的 31 天资料包
ashare-ds monthly-context --horizon-days 31 --output briefs/monthly_context.json

# 当前时段完整事实
ashare-ds facts --no-live --output briefs/facts.json
```

## 推荐提问

让客户端先调用 `refresh_sector_leaders`，再调用 `monthly_outlook_context`，然后要求：

```text
只把工具返回内容当作 A 股本地事实。联网核验未来 31 天国内外事件，优先使用官方
日历、监管机构、交易所、央行和公司一手公告。区分：已发生事实、已确认未来事件、
市场预期、你的推演。

按行业和概念分别比较量价龙头候选；再核验真实主营、产业地位、订单和公告风险。
输出基准/乐观/风险三种情景，每种写清传导链、确认条件、失效条件和证据来源。
不得把 leadership_rank 当胜率，不得声称某只股票一个月内必然上涨。
```

月度包会列出国庆、劳动节、618、双十一和偶数年的美国联邦选举日等可按日历规则
确定的窗口。中秋、春节、政策会议、央行议息和临时事件会明确列为“外部核验任务”，
项目不会用一张容易过期的硬编码表冒充实时信息。

## 月度包里有什么

| 键 | 内容 | 读的时候要注意 |
|---|---|---|
| `a_share_facts` | 当前时段的完整事实包（`ashare-facts-v1`） | `as_of` 是统一数据时点；`null` 保持为 `null`，不能补猜 |
| `historical_validation` | 台账统计 + 近期兑现核验 | 样本不足或滚动窗口重叠时不是收益概率 |
| `sector_rotation_memory` | 已落盘的同花顺行业主线延续/轮动摘要 | 只描述**系统自己的排名**稳不稳；新口径样本不足时单独积累，不混入旧行业排名 |
| `sector_leaders` | 每个行业/概念的量价龙头候选 | 先调 `refresh_sector_leaders` 才有；`leadership_rank` 不是胜率 |
| `event_window` | 按日历规则可确定的事件 | 移动日期与临时事件不在这里，见下一行 |
| `external_research_tasks` | 必须由你联网核验的事项 | 项目不会用一张会过期的硬编码表冒充实时信息 |

前两项与 `sector_rotation_memory` 都只用于校准“过去类似规则发生过什么”，不能
解读为未来收益概率。

## 盘前与盘中分析提示词

将事实包交给外部 AI 时可以使用以下模板；模板本身不调用模型，也不改变项目规则：

```text
先检查 as_of、data_status 和数据健康，再说明本次资料能支持哪些判断。
区分数据事实、系统规则结论和你的推演；缺失字段保留未知，数字和价位引用原始事实。

盘前：梳理市场环境、主要行业与概念、关键新闻、已有候选的触发和失效条件。
盘中：先看持仓风险，再用当天价格、量能与板块截面确认或否决盘前情景；说明哪些条件仍未满足。
给出证据来源、数据时点和需要后续核验的问题。排名不是胜率，条件触发不等于成交。
```

月度分析使用上面的月度模板。模型只能解释本地已有数字；外部事件另查一手来源，并注明来源和日期。

## 外部买卖点裁决

先导出当前事实，再生成只包含已有候选的裁决包：

```bash
ashare-ds facts --symbols 600519.SH --output /tmp/live-facts.json
ashare-ds codex-adjudication-packet --facts /tmp/live-facts.json --output /tmp/packet.json
```

把裁决包和以下要求交给外部 AI：

```text
仅针对 packet.symbols 中的标的作答，每只返回一条 decisions 项。
action 为 select 或 abstain；select 必须从 candidates 的 key 中选择 entry、stop，
target 可为 null；abstain 时三者均为 null。不得添加价格、仓位、订单或候选外标的。
reason 不超过 120 字，数字必须原样来自 packet；证据不足或风险无法排除时使用 abstain。
```

将结果保存为 `decision.json` 后验证：

```bash
ashare-ds codex-adjudication-validate --packet /tmp/packet.json --decision decision.json
```

`advice/external.py` 检查候选键、数字、措辞及数据时效。校验通过表示符合当前事实包的边界，
不会生成订单；候选随新事实重算，旧裁决不能跨时段复用。
模型文本与项目内裁决的校验标准见 [架构：输出闸门](ARCHITECTURE.md#输出闸门)。
