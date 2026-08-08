# 使用指南

这份项目的核心不是“会不会算”，而是“你能不能用命令直接拿到你要的分析”。
下面按场景整理，优先回答六件事：趋势分析、股价分析、自选股监控、facts 导出、
外部 AI 分析。

默认情况下，`morning` / `advise` 用 `template` 输出，`ask` / `chat` 只导出 facts
或 facts 上下文，不会自动调模型。只有显式加 `--writer llm` 或 `--llm` 才会走模型。

## 场景到命令

| 你想做的事 | 推荐命令 | 说明 |
|---|---|---|
| 看大盘和板块趋势 | `ashare-ds morning` | 盘前主链路：舆情 → 大盘趋势 → 板块排序 → 候选池 |
| 问单只股票怎么走 | `ashare-ds ask "600519 现在趋势和买点怎么样"` | 单入口问答，按时段自动装上下文 |
| 看单只股票的结构、量能和买卖点 | `ashare-ds advise 600519.SH` | 结合日线、分时、成交量、盘口生成买卖点 |
| 做全市场参数化选股 | `ashare-ds strategy-screen --profile all` | 比较多种形态，输出确定性候选事实 |
| 监控自选股是否触发告警 | `ashare-ds monitor --feishu` | 持续轮询；首轮会补报已成立的买卖点，后续按跃迁提醒 |
| 导出统一事实包 | `ashare-ds facts --output facts.json` | 给 Codex / Claude 的结构化输入，不调模型 |
| 让模型多轮追问同一份事实 | `ashare-ds chat` | 适合连续问“为什么”“如果跌破怎么办”；回答会先给判断，再给证据 |
| 让模型在候选价里做取舍 | `ashare-ds advise --adjudicate 600519.SH` | 用 LLM 做买卖点裁决，可弃权 |

## 推荐用法

### 1. 盘前先看趋势

```bash
ashare-ds watchlist add 600519.SH 000001.SZ --note "核心仓"
ashare-ds morning
ashare-ds morning --explain   # 同时打印筛选漏斗
```

这一步关注的是大盘、板块和候选池，不是逐票追问。
运行时会先打印几行阶段进度，再输出完整晨报。

晨报里有四节是**只出读数、不进打分**的（打分权重未经回测，这几层连权重都还没有）：

| 小节 | 回答什么 | 关掉 |
|---|---|---|
| 大盘怎么读里的宽基那行 | 市值加权的指数在涨还是跌，与等权宽度是否背离 | `--no-broad-index` |
| 轮动到哪了 | 月度趋势有没有反转（长窗口）、这周资金有没有掉头（等长窗口） | 随 `--no-sector-index` 的序列走 |
| 成交额挪到哪了 | 钱挪去了哪个板块，以及那是在买还是在跑；全场总量是涨是缩 | 同上 |
| 候选行的「昨日逐笔」 | 上一交易日的主动买占比、大单净额、尾盘净额 | `--no-trade-flow`（省约 2 分钟） |

**赶时间就加 `--no-trade-flow`**——它是盘前链路里最慢的一段，一只候选要翻约 67 页
成交明细。其余几层都是秒级或纯磁盘读。

### 1.1 主题雷达与社媒线索

`morning` 的 facts 会附带主题雷达。内置范围包括 AI 高速互连、PCB 关键材料、光芯片、
先进封装、战略金属、铜、电网储能、机器人、低空商业航天、核电、锂电、创新药/CRO、
消费家电更新和航运。它只把主题标记为“社媒线索”“事件预警”或“价格确认观察”，不改变
板块分数，也不生成买卖指令。

可以通过合规渠道导入小红书、抖音等平台的每日热点快照，系统不会绕过登录、验证码或
平台反爬机制。设置 `ASHARE_DS_SOCIAL_HEAT_PATH=/data/social-heat` 后，目录中当天的
`*YYYYMMDD*.json` 文件会在下一次 `morning` 被读取：

```json
{
  "platform": "xiaohongshu",
  "items": [
    {
      "title": "电子布供需观察",
      "summary": "可选，保留原始摘要",
      "published_at": "2026-08-07 18:00",
      "url": "https://approved-source.example/topic"
    }
  ]
}
```

社媒数据只用于发现线索；要升级为“价格确认观察”，仍需财经/官方消息与关联板块的趋势或
广度共同确认。新题材可通过 `ASHARE_DS_THEME_CATALOG_PATH=/data/themes.json` 增加：每项包含
`name`、`terms`、`sectors`、`transmission`、`invalidation`，格式为 JSON 数组或带 `themes` 数组的对象。

### 2. 盘中盯自选股

```bash
ashare-ds watchlist hold 600519.SH --cost 1350.6 --stop 1330 --target 1430
ashare-ds monitor --once
ashare-ds monitor --feishu
```

有成本价时，系统会同时盯卖点；没有成本价时，只盯买点。启动时已经成立的可执行买卖点会补报一次，普通均价震荡不补报，避免刷屏。

### 3. 单票做股价分析

```bash
ashare-ds advise 600519.SH 000001.SZ
ashare-ds advise --adjudicate 600519.SH
```

这里会看日线结构、分时位置、成交量、量比、五档盘口和风险过滤。
`--adjudicate` 会让模型在规则候选里做取舍，但不会让它编新价位。
`ask` 也能直接问单票，问题里写出代码会自动识别。

### 4. 用对话追问

```bash
ashare-ds chat
```

在对话里可以直接问：

- `600519 现在趋势怎么样`
- `今天量能放大但没突破，该怎么理解`
- `自选股里谁先触发止损`
- `如果明天低开，哪个价位先看`

`chat` 适合连续追问同一份事实；`ask` 适合一次性问清楚。
要问新票，先 `/add` 或 `/watch` 把它放进上下文。

### 5.1 参数化策略筛选

筛选使用 TickFlow 的分类和日线数据。默认只看沪深主板、价格 3 到 60 元、当日成交额至少 5 亿元，并纳入名称含 ST/退的标的；系统会在候选 `warnings` 中标明风险：

```bash
ashare-ds strategy-screen --profile high-elasticity --price-max 60 --limit 30
ashare-ds strategy-screen --profile balanced-pullback --price-min 5 --price-max 30 --min-amount-yi 3
ashare-ds strategy-screen --profile all --price-max 40 --min-amount-yi 3 --limit 50
```

`--profile all` 会一次运行三种策略，输出各自的诊断漏斗、跨策略多样化短名单和重复命中标的；由于策略位置区间可能互斥，不再把空交集误读成“没有机会”。需要扩大范围时使用 `--all-a`；需要排除 ST/退市风险标的时显式使用 `--exclude-st`（`--allow-st` 仅为旧命令兼容）。除了价格和成交额，还可用 `--min-history-bars`、`--min-atr-pct`、`--max-atr-pct`、`--min-range-position`、`--max-range-position`、`--min-pullback-pct`、`--max-pullback-pct`、`--min-volume-ratio`、`--max-volume-ratio` 等参数细化策略。结果写入 `briefs/strategy_screen_latest.json`，并自动进入 `ask` / `facts` 上下文。这里的形态标签不是收益预测，不能据此承诺每日 1% 收益。

### 5. 研究式问答

```bash
ashare-ds ask --deep "这几只低开了，结合成交量怎么处理"
```

`--deep` 会拆成计划、逐步论证、决策卡和价位轴，适合要一个结构化答案的时候用。

### 6. 让 Codex / Claude 先看 facts

```bash
ashare-ds facts --output facts.json
```

推荐顺序是：

- 先用 `facts` 导出统一 bundle，把舆情、价位、宏观联动、近期验证窗口一次性给外部 AI。
- 外部 AI 只能解释 facts 中已有的数字、价位和条件，不能补造实时数据。
- 任何交易动作由你根据券商的实时行情、交易规则和自身风险承受能力独立决定。

## 什么时候用哪个入口

- 想看“今天市场怎么了”，用 `morning`。
- 想看“这只票怎么走”，用 `advise` 或 `ask`。
- 想看“自选股有没有触发动作”，用 `monitor`。
- 想把事实交给 Codex / Claude 再继续分析，用 `facts`。
- 想连续追问同一批事实，用 `chat`。
- 想让模型参与取舍但不参与算价，用 `advise --adjudicate`。

## 约束

- 系统不下单、不撤单、不接管账户。
- 价格、趋势、成交量和买卖点都必须带数据时点。
- LLM 只负责组织语言和取舍，数字还是来自规则层；系统不生成或执行订单。
