# PRD

## Product definition

An A-share ultra-short analysis and decision support platform. It produces
structured suggestions before and during the session. It does not submit orders
or manage accounts.

## Goals

Four working needs, in the user's words:

1. 开市前分析和预测整体和板块趋势
2. 开市后咨询单支或者多支股票的买卖点
3. 开盘后预测后续走势
4. 持续分析和不断对话

See `docs/SOLUTION.md` for what each one maps to and what data backs it.

## Non-goals

- Automatic order placement
- Account balance and position control
- Broker order state management
- LLM inference on the critical path, or LLM-authored numbers anywhere
- Auction microstructure inference — impossible on every available source
- Probability, win-rate, or expected-return output without a fitted model

## Users

One trader, who reads the output and executes manually. Every design tradeoff
resolves in favour of that person understanding *why* a number is what it is.

## Workflows

| 时段 | 动作 | 命令 |
|---|---|---|
| 07:00-09:15 | 舆情 + 量价 + 资金 → 板块排序 → 板块内选股 | `sector-scan` |
| 09:30-15:00 | 就手上或候选的标的问买卖点与后续情景 | `advise` / `chat` |
| 15:30-17:00 | 自选股打分、写盘后简报与次日交接矩阵 | `post-close-brief` |
| 次日 08:30 | 把矩阵复述成执行卡 | `pre-open-brief` |

## Success criteria

Free tier — what must hold today:

- 四条主线的命令都能在无付费 key、无 QMT 的条件下跑完
- 所有挂单价位使用不复权口径；复权口径无法对齐时拒绝给价而不是给错价
- 数据缺失一律置空并标注，不用低分辨率数据顶替
- LLM 输出中的每个数字可追溯到规则引擎的计算结果
- 每份输出都写明它是在什么数据边界下产生的

Paid tier / QMT — unlocked by data, not by code changes:

- 板块与个股在盘中实时刷新，而非昨收
- 五档盘口进入买卖点计算
- 执行侧接 QMT（系统仍不自动下单）

## Open question

新浪分钟线在**盘中**的时效未验证（写作时为周六，测不了）。这是免费档能否支撑需求 2/3
的唯一未知数。用 `intraday-check` 在交易时段实测后决定是否开通付费档。
