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

系统不下单、不撤单、不接管账户。下面三个工具里只有 `refresh_sector_leaders` 会
联网并写一个 JSON 文件，其余两个只读已落盘的结果。系统替你记着的名单只有**持仓**
（`ashare-ds position open <代码> --cost <成本价>`）——自选股与候选池已删，事实包里的候选是当天晨报的
结论，不是一份持续维护的清单。

## 安装与 MCP

```bash
python3 -m pip install -e ".[tickflow]"
```

仓库已包含 Codex 的 `.codex/config.toml` 和 Claude Code 的 `.mcp.json`。两份配置都
用 `python3 -m ashare_decision_support.mcp_server`（`PYTHONPATH=src`）拉起同一个
STDIO 服务，装过包之后 `ashare-ds-mcp` 是同一个入口。重新进入项目会话后，客户端
可看到三个工具：

| 工具 | 是否联网/写盘 | 用途 |
|---|---|---|
| `get_facts` | 否 | 读取盘前、盘中、盘后、台账和策略扫描事实 |
| `refresh_sector_leaders` | 是 | 扫全市场日线，生成每个行业/概念的量价龙头候选 |
| `monthly_outlook_context` | 否 | 组合月度事实、龙头、轮动记忆、事件窗口和外部研究任务 |

每个参数在 `inputSchema` 里都带 description，且 `additionalProperties: false`——
拼错的键会当场被拒，而不是被静默忽略之后返回一份「看起来正常」的结果。同理，
`phase` 只认四个值（`pre_open` / `intraday` / `post_close` / `off_hours`），
拼错会报错而不是产出一份自称 `phase: banana` 的事实包。

## 体积：一次 `get_facts` 有多大

实测（2026-08-11，一份跑过盘前主链路 + 全市场筛选的目录）：

| 部分 | 字符数 |
|---|---|
| `盘前主链路` | 57K |
| `参数化策略扫描`（已按体积截断，见下） | 25K |
| `最近一次买卖点` | 7K |
| 其余（持仓、台账统计、近期验证…） | 2K |
| `rendered`（同一份事实的可读版） | 38K |

两件事因此是刻意的：

- **`content` 里放的是 `rendered`，不是 payload 的第二份 JSON。** 机器要的那份在
  `structuredContent` 里，两份都塞 JSON 等于让同一件事占两遍上下文。
- **筛选结果按体积截断**：每个策略保留前 5 只、多样化短名单保留前 10 只，省略了
  多少行写在 `notes` 里，完整结果在 `briefs/strategy_screen_latest.json`。

只要其中几块时用 `sections`：

```json
{"name": "get_facts", "arguments": {"sections": ["盘前主链路", "持仓"]}}
```

省掉的块名同样写进 `notes`——**少给数据可以，让你以为「就这些」不行**。

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
| `sector_rotation_memory` | 已落盘的申万行业主线延续/轮动摘要 | 只描述**系统自己的排名**稳不稳；没跑过 `morning` 时会是「样本不足」 |
| `sector_leaders` | 每个行业/概念的量价龙头候选 | 先调 `refresh_sector_leaders` 才有；`leadership_rank` 不是胜率 |
| `event_window` | 按日历规则可确定的事件 | 移动日期与临时事件不在这里，见下一行 |
| `external_research_tasks` | 必须由你联网核验的事项 | 项目不会用一张会过期的硬编码表冒充实时信息 |

前两项与 `sector_rotation_memory` 都只用于校准“过去类似规则发生过什么”，不能
解读为未来收益概率。
