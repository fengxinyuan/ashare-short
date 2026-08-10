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

## 安装与 MCP

```bash
python3 -m pip install -e ".[tickflow]"
```

仓库已包含 Codex 的 `.codex/config.toml` 和 Claude Code 的 `.mcp.json`，两者都会启动
同一个 STDIO 服务：`ashare-ds-mcp`。重新进入项目会话后，客户端可看到三个工具：

| 工具 | 是否联网/写盘 | 用途 |
|---|---|---|
| `get_facts` | 否 | 读取盘前、盘中、盘后、台账和策略扫描事实 |
| `refresh_sector_leaders` | 是 | 扫全市场日线，生成每个行业/概念的量价龙头候选 |
| `monthly_outlook_context` | 否 | 组合月度事实、龙头、事件窗口和外部研究任务 |

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

月度包的 `historical_validation` 是台账统计和近期兑现核验的显式入口；它只用于校准
“过去类似规则发生过什么”，样本不足或窗口重叠时不能解读为未来收益概率。
