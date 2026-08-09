# Codex 分析工作流

系统负责可复核的事实与硬约束；Codex 负责在这些事实之上做链路推演、比较与排序。
不要把实时取数、价格计算、风险否决或下单交给模型。

## 1. 市场与链路

先导出盘前包：

```bash
ashare-ds preopen-research --format json --output /tmp/preopen.json
```

交给 Codex 的任务：

```text
只依据输入 facts 与 overseas_snapshot，判断今日 A 股风险偏好、政策/地缘/商品/海外市场
到行业的传导链。区分已发生事实、市场预期和推演。给出 1-3 条需要竞价或开盘确认的
观察链，以及每条的失效条件。不得编造价格、资金流、政策或公司数据；没有数据时直接说明。
```

输出是情景与观察条件，不是买卖指令。

## 2. 板块主线

盘前主链路完成后导出事实：

```bash
ashare-ds morning --explain
ashare-ds facts --no-live --output /tmp/facts.json
```

交给 Codex 的任务：

```text
只依据盘前主链路、板块排序、轮动、成交额迁移、宽基对照、新闻覆盖与筛选漏斗，
把板块分为主线、观察、回避三组。每组说明：催化、价格结构、广度/轮动证据、
仍需确认的条件和失效条件。不得仅按单日涨幅排序；新闻覆盖不足或数据跨日时必须降级。
不要修改系统的仓位上限、情绪周期限制或风险否决。
```

系统候选池是事实筛选后的集合；Codex 可以重排和解释，但不应把池外标的当成已核验候选。

## 3. 个股比较

交易时段内按需要导出实时事实：

```bash
ashare-ds facts --symbols 600519.SH 000001.SZ --output /tmp/live-facts.json
```

交给 Codex 的任务：

```text
只比较 facts 中出现的标的。按打法是否成立、趋势与量价结构、派发/洗盘判别、
公告/业绩风险、盘口可成交性、风险收益比和当前时点排序。每只票必须给出：
支持证据、阻断项、等待的触发条件、失效条件。价格只能引用 facts 中已有的
entry/stop/target/candidate 值；没有实时数据或已跨日时，不得给即时操作建议。
```

模型不得创造价格区间、止损、仓位比例或胜率。最终交易动作由用户结合券商实时行情和自身纪律独立决定。

## 4. 动态买卖点裁决

系统不会把买入、止损、目标写死为一组不可改变的数字。每次实时分析都会从当时的分时
均价、日内高低点、日线结构与盘口生成候选集；Codex 可以从中改选更合适的组合，或明确
弃权。候选价格随新 facts 重新计算，不能跨时段复用。

```bash
ashare-ds facts --symbols 600519.SH --output /tmp/live-facts.json
ashare-ds codex-adjudication-packet --facts /tmp/live-facts.json --output /tmp/packet.json
```

将 `packet.json` 和以下要求交给 Codex：

```text
仅针对 packet.symbols 中的标的作答。每只标的只返回一条 decisions 项：
action 为 select 或 abstain；select 必须从 candidates 的 key 中选择 entry、stop，
target 可为 null；abstain 时三者均为 null。不得输出价格、仓位、下单或候选外标的。
reason 不超过 120 字，数字必须原样来自 packet。信息不足、风险警告或盘口失真时应 abstain。
```

将返回内容保存为 `decision.json` 后校验：

```bash
ashare-ds codex-adjudication-validate \
  --packet /tmp/packet.json --decision decision.json
```

校验成功只说明候选选择在当前事实与时效边界内，**不是交易授权，也不生成订单**。任何
系统不生成订单，也不连接券商。

## 5. 边界

- 系统不下单、不撤单、不接管账户。
- `as_of` 与 `session_date` 不一致时，旧数据只能用于结构回顾，不能当实时行情。
- 缺失字段为 `null` 或空集合，表示未知，不是零或否定。
- 基本面、估值、真实筹码分布、融资融券余额等尚无稳定数据源，不能由 Codex 补造。
