# TDD

主文档是 `docs/SOLUTION.md`。本文只写模块职责与设计约束。

## Architecture

```text
adapters/   数据接入与能力探测
  tickflow_adapter   日线（批量）· 申万行业 · 标的信息        free 可用
  sina_intraday      分钟线 1/5/15/30/60，不复权              free 可用
  akshare_adapter    现货/分钟代理（push2 系在本机不可用）
  qmt_xtdata         券商侧通道，待开通
  freshness          实测分钟线盘中时效，决定是否需要付费档
  mapping            券商 raw payload 字段归一

news/       采集（财联社/东财快讯/新闻联播/个股新闻/公告/龙虎榜）+ 词典情绪 + 按日缓存
sectors/    申万分类合并 · 成分股动量聚合 · 动量/舆情/资金三读数融合排序
screens/    ST、退市、非可交易板块过滤；致命公告否决
signals/    daily_technical · wyckoff · intraday
scoring/    规则打分（默认）；LightGBM provider 接口预留
advice/     买卖点位 + 情景推演
chat/       fact 上下文 + 多轮对话
writer/     模板（默认）· LLM + 数字闸门
notify/     渲染 + 控制台/文件（飞书骨架）
engines/    open_follow（开盘后跟随判定）
jobs/       sector_scan · advise · post_close · pre_open
research.py DecisionMatrix 构建
```

## 五条设计约束

### 1. 能力探测驱动降级

走哪条路由由 `adapter.health_check()["capabilities"]` 决定，不靠改代码。
付费 key 到位即解锁盘中链路。**能力缺失时置空并标注，绝不用低分辨率数据顶替。**

### 2. 价格口径必须显式

日线前复权、分钟线不复权。所有对外输出的**挂单价**取自不复权序列；日线位置经
复权因子换算后才允许引用。因子由两条序列在同一交易日的收盘价之比反推，落在
`[0.2, 5.0]` 之外即判定对不上，宁可不给也不给错的（`advice/engine.py`）。

### 3. 筛选与打分分离

screen 直接踢出标的，不是扣分。ST、退市、立案调查这类风险无法被好走势补偿，
做成扣分迟早会被其他因子的正分盖过。

### 4. 没数据 ≠ 中性

覆盖率不足的板块 `trusted=False`，永远不进候选池。给中性默认分会让「无知」
压过「测量出的走弱」（`sectors/predict.py`）。

### 5. LLM 只组织语言，且被强制执行

- 数字闸门：生成文本的每个数字必须可追溯到 facts（逐字 / 按精度舍入 / 万亿% 换算），
  否则整段丢弃回退模板（`writer/llm.py:unverified_numbers`）
- 合规文本生成后程序化追加，不交给模型
- 对话层走同一套闸门

## 状态机

```text
UNIVERSE -> 板块排序 -> 入选板块 -> ST/公告筛除 -> CANDIDATE
CANDIDATE -> 日线结构 + 分时结构 -> 价位 + 情景
情景 -> 强势延续 | 均价震荡 | 转弱回落
```

竞价相关状态（`AUC_FOCUS` / `AVOID_NOFILL` / `AVOID_FAKE_STRENGTH`）已随
`engines/auction.py` 一并删除——任何数据源都拿不到集合竞价撮合量与未匹配量，
且 09:20–09:25 不接受撤单。见 `docs/PLAN_REVIEW.md`。

## 降级规则

| 缺失 | 行为 |
|---|---|
| 分钟线 | 只给日线结构判断，明确标注价位为前复权口径、不可挂单 |
| 日线 | 只给分时结构，不引用历史支撑压力 |
| 舆情源 | 该源计 0，简报里写明哪个源挂了 |
| 全部舆情源 | 板块排序仅依据量价动量，显式标注 |
| 复权因子对不上 | 不引用日线位置，标注原因 |
| LLM 不可用或输出越界 | 回退确定性模板，标注原因 |
| 非当日分钟数据 | 简报顶部横幅 + 每只标的的 warning |
