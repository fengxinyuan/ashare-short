# 使用指南

这份项目的核心不是“会不会算”，而是“你能不能用命令直接拿到你要的分析”。
下面按场景整理，优先回答五件事：趋势分析、股价分析、盘中监控、facts 导出、
外部 AI 分析。

默认情况下，`morning` / `advise` 用 `template` 输出，`ask` / `chat` 只导出 facts
或 facts 上下文，不会自动调模型。只有显式加 `--writer llm` 或 `--llm` 才会走模型。

**要看哪几只由你说了算。** 自选股与候选池已经删掉，`advise` / `ask` / `chat`
只算你写在命令里的代码；省略时才回落到持仓表。持仓是系统唯一还替你记着的名单，
因为「该不该卖」这个问题必须有成本价、计划止损和持有天数才答得了。

## 场景到命令

| 你想做的事 | 推荐命令 | 说明 |
|---|---|---|
| 看大盘和板块趋势 | `ashare-ds morning` | 盘前主链路：舆情 → 大盘趋势 → 板块排序 → 当日候选 |
| 复看最近一次板块分析 | `ashare-ds sector-analysis` | 只读 `morning` 的落盘事实，不重复访问行情源 |
| 生成板块情景预测 | `ashare-ds sector-forecast` | 主题重叠去重，并列出09:35后的确认/失效条件 |
| 盘中验证板块预测 | `ashare-ds sector-confirmation` | 读取 `monitor` 已落盘截面，返回已确认/部分确认/已否决 |
| 检查价格信号是否有效 | `ashare-ds sector-backtest` | 本地历史、固定基线、时间序列后30%留出验证 |
| 问单只股票怎么走 | `ashare-ds ask "600519 现在趋势和买点怎么样"` | 单入口问答，按时段自动装上下文 |
| 看单只股票的结构、量能和买卖点 | `ashare-ds advise 600519.SH` | 结合日线、分时、成交量、盘口生成买卖点 |
| 做全市场参数化选股 | `ashare-ds strategy-screen --profile all` | 比较多种形态，输出确定性候选事实 |
| 盯持仓与板块异动 | `ashare-ds monitor --feishu` | 不传代码就盯持仓；首轮补报已成立的买卖点，后续按跃迁提醒；**板块监控与名单无关，空仓也有话说** |
| 导出统一事实包 | `ashare-ds facts --output facts.json` | 给 Codex / Claude 的结构化输入，不调模型 |
| 维护历史缓存 | `ashare-ds history-refresh` | 回填同花顺板块指数与海外指数日线，供外盘映射历史基准率读取 |
| 让模型多轮追问同一份事实 | `ashare-ds chat` | 适合连续问“为什么”“如果跌破怎么办”；回答会先给判断，再给证据 |
| 让模型在候选价里做取舍 | `ashare-ds advise --adjudicate 600519.SH` | 用 LLM 做买卖点裁决，可弃权 |

## 推荐用法

### 1. 盘前先看趋势

```bash
ashare-ds morning
ashare-ds morning --explain   # 同时打印筛选漏斗
ashare-ds morning --top-sectors 5 --top-concepts 3  # 行业、概念分开取名额
ashare-ds sector-analysis     # 复看最近一次每日板块分析
ashare-ds sector-analysis --format text      # markdown/text 等价
ashare-ds sector-analysis --format json --output sector-analysis.json
ashare-ds sector-forecast    # 情景排序；评分不是上涨概率
ashare-ds monitor --once     # 09:35后采集一轮盘中板块截面
ashare-ds sector-confirmation
ashare-ds sector-backtest --output briefs/sector_backtest_latest.json
```

这一步关注的是大盘、板块和当日候选，不是逐票追问。
运行时会先打印几行阶段进度，再输出完整晨报。
行业和概念默认并排计算、各自排名；概念不会参与全市场宽度，避免一只股票因多个
题材标签被重复计权。只想看行业时使用 `--no-concepts`。

`sector-analysis` 是只读查看入口：它读取 `briefs/morning_latest.json`，汇总主攻板块、
行业指数位置、价格轮动、成交额迁移和情绪旁证，不会重新跑 `morning`，也不会消耗行情源配额。
如果文件不存在，先运行一次 `ashare-ds morning`；如果数据日期早于今天，报告顶部会标出滞后天数，
这时它只能当历史复看，不能当作“现在”的板块判断。

`sector-forecast` 把高度重叠的行业和概念折成主题簇，避免“芯片、电子、存储”被误读成
三条独立证据。输出明确区分预测目标日与信息截止日，并要求盘中广度、相对收益和成交额
共同确认。`sector-backtest` 比较5日动量、超跌反转、趋势回调、强弱加速四个不调参基线；
它的滚动3/5日窗口会重叠，因此只能用于识别明显失效，不能直接解释为独立胜率样本。
`sector-confirmation` 不重写盘前判断，只附加盘中证据；广度低于45%，或放量但超额转负，
会明确标记为“已否决”。没有09:35后的有效截面时只返回“等待数据”，不会猜测结果。
CLI 每次运行会按“交易日 + 主题”去重写入确认台账，并保留状态变化序列；MCP 与
`get_facts` 仍然完全只读，不会因为模型查看数据而修改台账。

`--max-constituents` 只用于快速诊断链路，会在 summary 和 `morning_latest.json` 中标记
`diagnostic_mode=true`。诊断模式下的覆盖率、主攻板块和候选都不能视为正式全量结论。

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

### 2. 盘中盯持仓

```bash
ashare-ds position open 600519.SH --cost 1350.6 --stop 1330 --target 1430
ashare-ds position list
ashare-ds monitor --once
ashare-ds monitor --feishu

ashare-ds monitor --symbols 600519.SH 000001.SZ   # 只盯这两只，不读持仓
ashare-ds position close 600519.SH                # 卖了就删掉，历史留在台账里
```

不传 `--symbols` 就盯持仓表里的票，每轮重读——盘中 `position open` 登记的新仓位
立刻被盯上，不用重启监控。有成本价时盯的是卖点（止损/目标/结构破坏/时间止损/
浮盈回吐），显式传进来的票没有成本价，盯的是买点。启动时已经成立的可执行买卖点
会补报一次，普通均价震荡不补报，避免刷屏。

`position list` 除了逐笔明细，还给一份**组合层读数**（`portfolio` 字段）：每只占
多少、按行业与题材归并之后最大的一块有多大、哪几笔还没有计划止损。三只票分属三个
代码但挂在同一个题材上时，它不是三笔分散的仓位，是一笔重仓——这句话逐行看是看不
出来的。归并用本地缓存的标的元数据，不打网络；缓存为空时只有权重，没有归并，并
明确写出「没有分类」而不是给一张空表。

它**不报总仓位**：仓位上限说的是占账户总资金的比例，而这套系统只知道持仓表，不
知道你账户里还有多少钱。

### 2b. 盘中盯板块趋势

`monitor` 默认**同时**在跑板块监控，不用额外开：每 5 轮（约 10 分钟）做一次全市场
扫描，按申万行业（31 个）和同花顺概念（321 个，含机器人、人形机器人、低空经济
这类）各归并一次，只在板块**状态发生变化**时推送。

```bash
ashare-ds monitor --feishu                  # 个股 + 板块，两条链路
ashare-ds monitor --no-sectors              # 只要个股告警
ashare-ds monitor --sector-every 10         # 板块看得稀一点（约 20 分钟一次）
python3 scripts/intraday_sector_monitor.py  # 只看板块，不跑个股监控
```

推送的七种板块变化：

| 触发 | 级别 | 说的是 |
|---|---|---|
| 开盘板块概览 | WARN | 此刻钱在哪（每天一条基线，不是跃迁） |
| 排名跃升 | ACTION | 资金正在往这里挪：较开盘首轮升 8 名以上且进了前 15 |
| 转强 | ACTION | 超额 ≥ 1% **且**上涨占比 ≥ 70%——只有涨幅可能是两只权重股拉的 |
| 涨停潮 | ACTION | 涨停家数从 ≤1 跳到 ≥3 |
| 成交额虹吸 | WARN | 占全场成交额较开盘放大 1.5 倍且绝对占比过 2% |
| 领涨板块退潮 | WARN | **手上的该不该走**：曾进前 10 且从「强」转弱 |
| 排名下滑 | INFO | 默认不推 |

两条要点：

- **排的是超额涨幅**（板块中位 − 全市场中位），不是绝对涨幅。普涨日里 +1.5% 是落后，
  阴跌日里 −0.5% 是强势，绝对涨幅排出来的榜没有信息。
- **一个板块一轮只推一条。** 四条规则同时命中时折成一条，其余进正文——四条一模一样的
  推送正是通道被静音的原因。额度**按小时分配**（每小时 4 条、全天 20 条），所以早盘的
  噪声吃不掉下午的份；超了写进 notes 而不是静默丢掉。

你盯着的票（持仓，或本轮 `--symbols` 传进来的）落在哪个板块里会直接标在告警上
（`⚑ 你盯着的票在这个板块里`），
并且这类告警排在最前——同一条「退潮」，发生在你有仓位的方向上紧急程度高一个量级。

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
- `持仓里谁先触发止损`
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

一份完整事实包是 130KB 量级，只要其中几块时：

```bash
ashare-ds facts --sections 盘前主链路 持仓
```

需要在终端快速查看时使用文本模式；默认是精简摘要，完整文本加 `--full`。JSON 模式
仍然保留完整结构，并在顶层提供 `data_status`（`正常` / `降级` / `未记录`）。

```bash
ashare-ds facts --format text --no-live
ashare-ds facts --format text --full --no-live
```

省掉的块名会写进 `notes`——**少给数据可以，让你以为「就这些」不行**。
MCP 那一侧的 `get_facts` 有同名参数，两条路是同一份实现。
CLI 也可用 `facts --phase pre_open|intraday|post_close|off_hours` 固定装配时段，适合
回放和自动化测试；省略时仍按北京时间与交易日历自动判断。

盘前研报包要看外盘映射的历史基准率时，先维护本地历史缓存：

```bash
ashare-ds history-refresh
ashare-ds history-refresh --only overseas --keys sox kospi taiex
```

这条命令只填样本：同花顺板块指数 + 海外指数日线。`preopen-research` 会只读这些
缓存，把「过去类似外盘条件出现后，相关 A 股板块怎么反应」放进
`sector_base_rates`，但它不生成预测、不改变板块评分。

包里有一块叫 `数据健康`，回答的是「这份包里每一块是什么时候的」：数据日期、落后了
几个**交易日**、判定（当日 / 上一交易日 / 陈旧 / 缺失 / 读取失败），以及整份包是
正常还是降级。落后按交易日数而不是自然日——上周五的盘前简报在周一读，是上一个
交易日的数据，不是「三天前」。**先看这一块，再看排序**：周一早上还没跑
`morning` 时，上周五那份板块排序仍然会照常出现在包里。

推荐顺序是：

- 先用 `facts` 导出统一 bundle，把舆情、价位、宏观联动、近期验证窗口一次性给外部 AI。
- 外部 AI 只能解释 facts 中已有的数字、价位和条件，不能补造实时数据。
- 任何交易动作由你根据券商的实时行情、交易规则和自身风险承受能力独立决定。

## 什么时候用哪个入口

- 想看“今天市场怎么了”，用 `morning`。
- 想看“这只票怎么走”，用 `advise` 或 `ask`。
- 想看“持仓有没有触发动作”，用 `monitor`。
- 想把事实交给 Codex / Claude 再继续分析，用 `facts`。
- 想连续追问同一批事实，用 `chat`。
- 想让模型参与取舍但不参与算价，用 `advise --adjudicate`。

## 约束

- 系统不下单、不撤单、不接管账户。
- 价格、趋势、成交量和买卖点都必须带数据时点。
- LLM 只负责组织语言和取舍，数字还是来自规则层；系统不生成或执行订单。
