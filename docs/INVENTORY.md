# 功能接线清单

**这份文档回答一个问题：某个模块今天到底跑不跑。**

它存在的理由写在 `docs/ROADMAP.md` §2——一个被导出、有测试、有 `health_check`，
唯独没有任何调用方的模块，会制造「系统有这项能力」的假象。假象的代价不是多几行
代码，是**下判断的时候把它算进去了**。所以接线状态必须是一个能查的事实，而不是
读完全仓 import 才能拼出来的印象。

- 生成日期：**2026-08-11**
- 口径：`src/` 共 **171 个模块**（CLI 与 MCP 两个入口）
- 判定方法：从 `cli.py` 与 `mcp_server.py` 出发做 import 可达性，加上公开符号、dataclass 字段、
  类上公开方法、私有函数/方法这四层的实际引用检查。**五条都要过**——只被
  `__init__.py` 转出口而没人用它的符号、算了没人读的字段、没有任何调用点的
  方法与私有辅助，都算未接线。五条全在 `scripts/inventory_audit.py` 里，见 §4。

---

## 0. 一句话结论

| 状态 | 模块数 | 含义 |
|---|---|---|
| **活** | 171 | 至少有一条 CLI 或 MCP 入口能走到，且符号被真正调用 |
| **建成·未接线** | **0** | 代码写完了、大多有测试，但没有任何调用方 |
| **已删** | 4 | 见 §3 |

当前没有已建未接线模块，**五条口径全部为 0**。新增模块仍需按本文档的可达性口径
复核，避免未来重新积累死代码。

**2026-08-11 一次替换 + 一次删除。** `state/watchlist.py`（自选股 + 候选池）换成
`state/positions.py`（只有持仓），判据是使用方式变了而不是代码写得不好，理由与
连锁影响见 DECISIONS §十八；`signals/daily_technical.py` 里的 `DailyKChartRenderer`
删掉，见 §19.3。模块数 171 不变（删一个加一个）。

**2026-08-09 补一轮符号级复核。** 模块级早就是 0，但本文档的口径有**两条**，
第二条（「只被 `__init__.py` 转出口而没人用它的符号」）此前没有真正跑过。跑完
命中 3 个——都不是模块，是模块里没人调的公开符号，已删：

| 符号 | 原位置 | 判定 |
|---|---|---|
| `collect_notes` | `agents/base.py` | 把各 agent 的 notes 拼平；调用方各自内联做了这件事 |
| `NewsBundle` | `news/base.py` | 一天舆情的打包类型，**从未被构造过**——收集层直接传 `(docs, notes)` 二元组 |
| `next_task` | `schedule/timetable.py` | 「下一个任务是什么」；`daemon` 走的是自己的循环，不查表 |

三个都只有「定义 + `__init__` 转出口」两处引用，删除后 `ruff` 与 1111 个测试全过。
**这三个的量级（几十行）不是重点**，重点是第二条口径以前是句空话——写在文档里、
没人跑。复核脚本见 §4，下次改动后照跑。

2026-08-09 新增两个模块：`fanout.py`（独立取数的并发与失败隔离收口，调用方横跨
`news/` 五处与 `jobs/sources.py` 的龙虎榜席位，所以放在包根，理由同 `numeric.py`）
与 `news/health.py`（舆情源可达性探针，口径对齐 `adapters/` 那十个 `health_check`）。
两个都在主链路上。

同日新增三个模块，都在 `monitor` 主链路上，构成盘中板块监控：
`sectors/intraday.py`（全市场扫描按行业+概念归并成截面）、
`monitor/sector_triggers.py`（板块跃迁判定与噪声预算）、
`monitor/sector_session.py`（一轮的编排 + 投递收口）。三个都由 `run_monitor`
默认启用，`--no-sectors` 关闭；`scripts/intraday_sector_monitor.py` 是同一套代码的
命令行外壳，**不含第二份口径**。

---

## 1. 建成 · 未接线（0 个）

**这一节是本文档的重点。** 下面每个模块都能 import、都能单独调用、大多有测试
通过，但**没有任何 job / signal / CLI 路径调用它们**。

> **模块级早就是 0，方法级是 2026-08-11 才归零的。** 第四条口径（§4.2）在**已接线
> 的模块内部**找到过 3 处同样形状的东西：`NewsStore.sector_heat` / `recent` 与
> `DailyKChartRenderer.render_svg`——完整功能、有测试、零生产调用方。前两个已接进
> 盘前 facts，第三个已删（DECISIONS §19.2、§19.3）。整模块死码清干净之后，剩下的
> 都藏在活模块里，这一节的口径按定义看不见它们。

| 模块 | 行数 | 测试 | 属于哪件事 | 接上它就能拿到什么 |
|---|---|---|---|---|
暂无。`adapters/lhb_seats.py` 已于 2026-08-08 接入 `post_close`：只把已披露上榜标的的
机构/北向/营业部席位构成写入盘后卡，不进入评分或仓位。

### 1.1 已接入：静态标的元数据

`adapters/ths_taxonomy.py` + `sectors/ths_taxonomy.py` + `adapters/security_master.py`
是同一件事的三段，分别服务于板块分类与单票展示。

`morning` 与 `daemon` 注入 `ThsIndustryTaxonomy`，优先走静态分类；`advise`、
`post-close`、`facts` 通过 `SecurityMasterAdapter` 补入中文名、三级行业与概念。
静态源不可用时 `morning` 才回退 TickFlow SW1，且会留下明确 note。

**概念这一维已于 2026-08-09 接线**：`ThsConceptTaxonomy` 此前只用来在简报里印标签，
从没进过任何排序——现在它和 `ThsIndustryTaxonomy` 并排喂给 `sectors/intraday.py`，
是盘中板块监控的两个层级之一。同日补上了 taxonomy 单元测试
（`tests/test_intraday_sectors.py`，35 条）。

剩下的缺口是**盘前**那一侧：`morning` 的板块排序仍然只用行业，不用概念。
接它要改候选的产出口径，不是纯增量，所以单列在 ROADMAP 而不是顺手做掉。

### 1.2 已接入：相对宽基强度

`relative_strength` 已于 2026-08-08 接入 `advise`：与个股日线并行取得所需宽基日线，
按共同交易日计算 1/5/20 日超额收益。它只作为 facts 与简报读数，不进入评分、筛选或买卖点。
`signals/distribution.py` 已于 2026-08-08 接入 `jobs/advise.py`；`signals/breakeven.py` 同日接入持仓监控：前者写入单票 facts 和简报，后者只在浮亏告警中给出回本位与阻力位的重合结构。两者都不进入硬过滤，避免把经验性分类误当成买卖门槛。

### 1.3 为什么这轮不删它们

删除的判据是「**这件事已经决定不做了**」，不是「现在没跑」。上面 2 个都在
ROADMAP 里有明确的去向，`git rm` 之后再捡回来的成本不为零。

**taxonomy 那条「没有测试」的理由 2026-08-09 已经消掉**：`sectors/ths_taxonomy.py`
补了 `tests/test_static_taxonomy.py`（10 条，覆盖率 64% → 96%），钉住的是这一层
最容易静默出错的三件事——分类取不到时**抛错而不是返回空列表**（返回空在下游读
起来是「今天没有板块」，会得出一份没有板块值得做的结论）、成分股顺序稳定可 diff、
以及 `reconcile` 那个「第三方文件是不是已经陈旧」的探针。

同日 `jobs/daily_cycle.py` 从 33% 补到 85%（`tests/test_daily_cycle_wiring.py`）。
它是**唯一**用那套关键字去调五条主链路的地方，`cli.py` 有它自己的调用点——两边
签名一旦漂移，CLI 那条路照常绿，daemon 这条路要到某个交易日 09:15 无人值守跑到
那一步才炸。用例按**真实签名**绑定实参，漂移当场失败。

对照 §3 删掉的那两类，区别很清楚：那两类是**已经被替代**或**作者自己标了墓碑**。

---

## 2. 活链路：按 CLI 命令看

每个模块至少被一条命令走到。下表按命令列出它**独占**或**主导**的子系统，
共用的底座（`numeric` / `symbols` / `models` / `state` / `notify` / `writer`）不重复列。

| 命令 | 主链路 | 主要子系统 |
|---|---|---|
| `morning` | `jobs/morning.py` | `sectors/`（除 ths_taxonomy）、`news/` 全部、`screens/`、`scoring/`、`strategy/select·regime·playbook`、`signals/`（除 §1 三个）、`agents/` |
| `advise` | `jobs/advise.py` | `advice/`（engine·candidates·adjudicate·view）、`adapters/sina_intraday`、`tickflow_intraday` |
| `monitor` | `jobs/monitor.py` | `monitor/`（feed·holdings·loop·session·triggers·**sector_session·sector_triggers**）、`sectors/intraday`、`signals/stabilise`、`adapters/freshness·market_scan` |
| `postclose` | `jobs/post_close.py` | `strategy/exits`、`signals/chips`、`adapters/tencent_detail` |
| `preopen` | `jobs/pre_open.py` | 复用 postclose + advise 的读数 |
| `review` | `jobs/review.py` | `journal/`（records·stats·store·verify） |
| `daemon` | `jobs/daily_cycle.py` | `schedule/`（calendar·timetable·daemon），串起上面全部 |
| `chat` / `ask` | `chat/session.py`、`ask/runner.py` | `ask/`（context·plan·question·report·verdict·visual） |

### 2.1 在 CLI 层注入、不被 job 直接 import 的模块

这几个从 job 模块出发的 import 图里看不到，但它们是**活的**——`cli.py` 构造好
之后作为参数传进去（依赖注入）。列在这里免得下次审计把它们误判成死码：

`adapters/tickflow_adapter`（主行情源）、`adapters/akshare_adapter`、
`adapters/qmt_xtdata`、`adapters/ratelimit`、
`notify/feishu`、`chat/repl`、`envfile`、`schedule/clock`。

---

## 3. 已删（5 类）

| 模块 | 行数 | 删除理由 |
|---|---|---|
| `state/watchlist.py`（`Watchlist` / `CandidatePool` / `WatchEntry`） | ~284 | 自选股与候选池：**没人维护却仍是所有命令的默认输入**。换成 `state/positions.py`（只有持仓），见 DECISIONS §十八 |
| `DailyKChartRenderer` + `DailyTechnicalSnapshot.chart_bars` | ~110 | SVG K 线渲染器，零生产调用方。这套系统的产物是 JSON 与 Markdown，见 DECISIONS §19.3 |
| `research.py` + `tests/test_research_pipeline.py` | 17 | **文件自己的 docstring 写着「已删除，等待 `git rm`」。** `ResearchPipeline` 那条矩阵管线已被 `strategy/select.py` 取代 |
| `adapters/chip_distribution.py` + 测试 | ~200 | 被 `signals/chips.py`（换手衰减法估算）取代，且本机东财 CYQ 实测不可达。ROADMAP §7 快赢 #1 点名要删 |
| `simulation/` + `tests/test_paper_simulation.py` | ~1,400 | 纸上交易与决策卡订单校验不属于分析主链路，已由 facts 导出与人工判断替代 |

`signals/chips.py` 是**活的**（`morning` / `postclose` / `preopen` / `daemon` 都走到），
它才是今天系统里真正的筹码读数——命名为 `chip_*_est` 是刻意的，见该模块 docstring。

---

## 4. 怎么复核这份清单

判定不是手写的，可以重跑——**六条口径都在脚本里**，退出码非 0 就是有未接线的
模块、符号、方法、私有辅助或常量：

```bash
python3 scripts/inventory_audit.py                  # 六条都跑
python3 scripts/inventory_audit.py --symbols        # 只跑「仅转出口」那条
python3 scripts/inventory_audit.py --fields         # 只跑「算了没人读」那条
python3 scripts/inventory_audit.py --methods        # 只跑「没有调用点」那条
python3 scripts/inventory_audit.py --private        # 只跑「私有符号没人引用」那条
python3 scripts/inventory_audit.py --constants      # 只跑「模块级常量没人引用」那条
python3 scripts/inventory_audit.py --strict-fields  # 把第三条也计入退出码
```

本文档的数字就是这个脚本的输出：`171 个模块 / 171 个可达 / 482 个公开符号 /
0 个仅转出口 / 1015 个 dataclass 公开字段 / 0 个没有读取点 / 452 个公开方法 /
0 个零引用 / 0 个只有测试在调 / 742 个私有函数与方法 / 0 个零引用 /
644 个模块级公开常量 / 0 个零引用`。

**2026-08-11 起六条全部归零**，包括此前只报告不计分的那两档（「算了没人读的
字段」与「只有测试在调的方法」）。归零本身不是目标——目标是**清单上没有悬而
未决的条目**：每一条要么接上了，要么删了，要么在本文里写明了为什么留着。

写成脚本而不是留在 git 历史里，是因为第二条口径此前正是这么丢的——文档里写着
「可以重跑」，脚本却只存在于某次会话，于是那条检查两个月没跑过，攒出 3 个死符号
（见 §0）。**一条不能一行命令跑出来的口径，等于没有这条口径。**

### 4.1 第三条口径：算了但没人读的字段（2026-08-09 新增）

前两条都抓不到这一类——**字段既不是模块也不是顶层符号**。而代价是一样的：

| 字段 | 症状 |
|---|---|
| `SectorIndexReading.volume_ratio` | 近 5 日均额 / 近 20 日均额，上线起就在算。**没有任何调用方**，于是「这个板块的反弹有没有量能跟上」在简报里根本没有答案——而它正是 2026-08-07 分辨小金属 1.29 与半导体 0.85 的那个数 |
| `LimitStock.seal_fund` / `first_seal` / `sector` | 涨停池每天解析，74/74 只有值。简报却只印得出「涨停 74 只」，答不了「封得结不结实」和「扎堆在哪」 |

两处都已在同日接上（见 `adapters/ths_sector_index.py:describe`、
`adapters/limit_pool.py:_seal_quality_lines`）。

**这条默认只报告、不计入退出码**，与前两条不同，理由写在 `check_fields` 的
docstring 里：前两条的判据是二值的（模块要么有调用方要么没有），而一个字段完全
可以是**有意留着**的（留给台账 payload、留给下一轮要接的那一层）。让它直接把 CI
变红，只会逼人加一份豁免清单，而豁免清单会在三个月内长成第二份没人读的文档——
正是这份脚本存在的理由所要避免的。所以它是**看板**：把候选列出来，接、删还是
写明理由，是一次人工判断。

当时 **7 个**候选待分诊。同日已分诊掉 5 个，两批的处置正好示范了这条口径的两种
结局——**接上**或**说明为什么留着**，而不是让清单一直挂着：

| 字段 | 处置 |
|---|---|
| `PositionSizingPlan.suggested_factor` / `capped_suggested_factor` / `manual_factor` | **接进台账**（`jobs/morning.py:_market_state`）。此前 `review` 回答不了「系统建议多少、上限有没有真的压住、是不是手工覆盖」——三种决策的 `position_factor` 可以完全一样，而它们对「仓位上限这条规则有没有用」是相反的证据 |
| `Stabilisation.bars_since_low` | **接进 `describe()`**。三种止跌形态本来都算了这个数，却只有最弱的「缩量止跌」写进了 reason，放量反包与二次探底不破一个字都没有——恰恰是这两个会被拿去当买点 |
| `PositionSizingPlan.cycle_cap` | **留着，理由在此**：与台账里的 `regime_position_cap` 同源同值，重复存一遍只会在两者漂移时制造一个假的交叉验证 |

**2026-08-09 第二批分诊，清单归零。** 上一版说「下一轮再看」的 6 个已经全部有
结论——3 个接上、剩下的写明理由：

| 字段 | 处置 |
|---|---|
| `IntradaySnapshot.day_change_pct` | **接上**（`signals/intraday.py` + `advice/engine.py:_facts` + `ask/verdict.py`）。它此前**声明了但从未被赋值**，注释却写着「尾盘打法的第一道筛直接读它」——永远是 `None`。代价不只是少一个数：事实包里只有跳空与开盘后涨幅两截，模型要说「今天涨了 3.3%」得自己乘，而乘出来的数不在事实里，`unverified_numbers` 会把**整段论证**丢掉 |
| `Finding.elapsed_ms` | **接上**（`agents/base.py:timing_note`，用在 `jobs/morning.py` 与 `jobs/advise.py`）。每个 Finding 都记着自己跑了多久，调用方却只 `extend(finding.notes)`——「盘前哪一段慢」在生产环境里没有答案，只能临时加计时器重跑。并行跑批里最慢那条腿决定整轮墙钟时间，所以按耗时降序原样列出，不设阈值 |
| `IntradaySnapshot.minutes_since_open` | **接进 `_facts`**。上面几个比率的可信度全取决于它：10:00 的量比 1.8 和 14:50 的量比 1.8 是两回事，只给比率等于把「才走了 30 分钟」藏起来 |
| `SectorIndexReading.amount_5d` | **接上**（`ths_sector_index.py:describe`）。比值说「比自己以前放量了吗」，绝对额说「这个板块有没有足够容量」 |
| `TradePrint.change` / `volume_lot` | ~~留着~~ → **2026-08-11 删掉**。「如实承载源数据那一行」这条理由撑不住两个月都没有消费者的事实：真需要时从解析函数改回来是十几行，而挂在清单上的每一行都在稀释这份清单的意义 |
| `PassResult.polled` | ~~留着~~ → **2026-08-11 删掉**，同上 |

**2026-08-11 这条口径归零。** 上表里判「留着」的 4 个（`TradePrint.change` /
`volume_lot`、`PassResult.polled`、`PositionSizingPlan.cycle_cap`）已经全部删掉。
`cycle_cap` 那条尤其干净：§4.1 给它写的「留着」理由（与台账里的
`regime_position_cap` 同源同值，重复存一遍只会在两者漂移时制造一个假的交叉验证）
本身就是删它的理由——**一个不该被读的字段，和一个没人读的字段，在这条口径下
是同一件事**。`HeatPoint.bucket_start` 不再是孤儿——它唯一的生产者
`NewsStore.sector_heat` 已经接进盘前 facts（§4.2）。

这条检查**仍然没有豁免清单**，那是有意的：豁免清单会在三个月内长成第二份没人读的
文档。归零的正确做法是清单上不留悬案，不是给悬案发免死金牌。

### 4.2 第四条口径：没有调用点的公开方法（2026-08-10 新增）

前三条**合起来**仍有一个盲区，而且是最容易攒的一个：类上的公开方法。它不是模块
（第一条看不见），不是模块顶层符号（第二条只扫 `tree.body`），也不是 dataclass
字段（第三条只看 `AnnAssign`）；`F401` 那类告警更管不着——一个没人调的方法在
ruff 眼里和被调 100 次的方法长得一模一样。2026-08-09 临时扫过一次，命中 13 处，
单是 `dump_snapshot` × 3 就是 150 行（见 DECISIONS §十二、§十三）。

现在它是 `check_methods`，分两档：

| 档 | 判据 | 处置 |
|---|---|---|
| **零引用** | 定义之外全仓一个引用点都没有 | **进退出码**。和模块、符号一样是二值事实。当前 **0 个**（6 个已删，见 DECISIONS §14.1） |
| **仅测试在调** | `src/` 里没有调用点，只有 `tests/` 在调 | 只报告。**这一档就是 §1「建成·未接线」的自动版**——测试证明它能用，不证明系统在用 |

第二档必须把测试单独摘出来数，不能沿用第二条那句「测试也是调用方」：竞价采集层
（DECISIONS §十二，950 行）当年正是**有测试、全绿、零生产调用方**。

**2026-08-11：这一档也归零了**，三类各有各的结局——三种结局本身就是这条口径的
全部用法：

| 方法 | 判定 |
|---|---|
| ~~`SecurityMaster.name_of` / `by_concept` / `by_industry` / `concepts_of`、`SectorIndexSnapshot.by_name`、`RegimeVerdict.permits`、`RateLimitedClient.unwrapped`~~ | 数据持有者上的小查询器，各 2–7 行。上一版判「留着」，这一版**删掉**：小不是留下的理由，没有调用方才是判据，而「哪天会用上」两个月没有兑现 |
| ~~`Watchlist.unheld`~~ | 随 `Watchlist` 整个删掉，见 DECISIONS §十八 |
| ~~`NewsStore.sector_heat`（53 行）+ `recent`~~ | **已接线**：盘前 facts 的 `sector_news_memory`，见 DECISIONS §19.2。语料库跨日累积的那一维此前没有任何生产读取点 |
| ~~`DailyKChartRenderer.render_svg`（97 行）~~ | **已删**，见 DECISIONS §19.3。这套系统的产物是 JSON 与 Markdown，没有一条链路会消费一张图 |
| ~~`Recommendation.from_advice`~~ | **2026-08-10 已接线**，见 DECISIONS §14.2。它不是查询器也不是待定功能，是一根**断了的线**：台账字段、压缩函数、读它的统计全都写好了，唯独没有生产写入方 |

`from_advice` 那一条是这条口径到目前为止最贵的一次命中，值得记住它的形状——
**「仅测试在调」不等于「可以删」，也不等于「等产品决定」**，第三种可能是某条
主链路本来就该调它而漏了，而那种情况下前三条口径与全部单测都是绿的。

### 4.3 第五条口径：没人引用的私有符号（2026-08-10 新增）

前四条查的全是**公开面**。而私有面——模块级 `_func` 与类上 `_method`——
**一条自动告警都没有**：`F401` 只管未使用的导入，一个没人调的私有函数在 ruff
眼里和被调 100 次的函数长得一模一样。DECISIONS §十二 删竞价那一层时连锁清掉的
14 个私有辅助（`_build_auction_payload` 101 行、`_build_open_payload` 59 行……）
**不是扫出来的，是删公开方法时顺手撞见的**。没有那次删除，它们会一直躺着。

私有面反而能查得**准**：`_foo` 按约定只在自己模块里被调，所以引用只在定义它的
那个模块里数就够了，没有跨模块重名顶账的问题——那正是前四条不得不「宁可漏报」
的原因。这条是五条里唯一可以要求精确的。

**它迭代到不动点**，这一点是必须的而不是锦上添花：删掉一个私有函数常常会让它
调用的另一个私有函数也失去唯一调用方，一轮只能看见第一层。落地当天命中的 3 处
正好演示了两层——

| 私有符号 | 怎么掉下来的 |
|---|---|
| `QmtXtDataAdapter._call_optional` | §14.1 删 `get_l2_quote_raw` 之后失去唯一调用方 |
| `QmtXtDataAdapter._extract_source_dt`（21 行） | 同上，跟着 `get_full_tick_raw` 掉下来。**只有测试在调**——那条测试测的是一段生产代码已经碰不到的逻辑 |
| `QmtXtDataAdapter._first_value` | 只被 `_extract_source_dt` 调，**第二层**。一轮扫描看不见它 |

`_extract_source_dt` 那条测试连同方法一起删了：它钉的是「`isinstance(raw, (int,
float))` 会放 `True` 和 NaN 过去」这条浮点卫生，而那条判据的正主是
`numeric.finite`，`tests/test_numeric.py` 已经独立钉住（`finite(True) is None`、
NaN/inf 全挡）。教训留在有调用方的那一份里，不留在没有调用方的这一份里。

**「仅测试在调」这一档在私有面上比在公开面上更刺眼。** 私有辅助不是对外契约，
没有「留给下一个消费者」这种理由，所以它基本只有一个含义：**某次删除留下的连锁，
而测试还绿着**。

**下次改动后要重跑的时机**：新增模块、删除模块、把某个未接线模块接进主链路、
**给 dataclass 加字段**、**给类加公开方法**，以及——最容易忘的——**删掉任何
一个函数之后**，因为它调用的那些私有辅助会一起变成孤儿。

接线之后记得把它从 §1 移到 §2——**这份文档过期就没有价值**，它唯一的用处是
「查得到、且查到的是真的」。

### 4.4 第六条口径：没人引用的模块级常量（2026-08-11 新增）

前五条一条都看不见它。第二条（公开符号）**只收模块顶层的 `def` 与 `class`**，
赋值语句根本不进它的表；字段那条查的是 dataclass 内部；方法与私有那两条按定义
也不覆盖。于是模块顶层的 `UPPER_SNAKE` 是全仓**唯一一类既没有 ruff 告警、也不
落在任何一条审计口径里**的公开面。

首次跑，643 个常量里命中 5 个，**全是被删掉的实现留下的锚**：

| 常量 | 怎么掉下来的 |
|---|---|
| `tickflow_adapter.VOLUME_UNIT_BANDS` | 量纲推断搬去 `validate.infer_volume_unit` 之后没跟着删。那张表连同头上三行注释还在，读起来像是活的 |
| `tickflow_adapter.DAILY_PROXY_LOOKBACK_DAYS` / `DAILY_PROXY_MAX_ROWS` | 降级日线代理整段没了，两个参数还留着 |
| `seesaw.POSITIVE_THRESHOLD` / `LABEL_AGAINST` | 对应 `explain_divergence` 里**从未实现**的第三个分支，见下 |

**常量比函数更容易攒成这一形状**：删实现的时候它不报错，而它头上那段注释还在
替它说明「我是干什么的」。`VOLUME_UNIT_BANDS` 的注释写着「used to infer the
volume unit」——那件事早就在别的文件里做了。

**引用只认 AST，不数 token。** 这是这条与第二条唯一的口径分歧，而且是被
`POSITIVE_THRESHOLD` 逼出来的：它在正上方的注释里被提到过一次（「见
`POSITIVE_THRESHOLD` 上方」），按全文 token 计数就算「有人引用」，这条口径会
当场瞎掉。**一个只在散文里被提到的常量仍然是死的**——注释解释不了任何运行时
行为。所以只认裸名读取、属性读取（`mod.CONST`）与**完全相等**的字符串字面量
（`getattr` 与分发表）。

`seesaw` 那两个不只是死码，它们指出了一处**说了没做**：`POSITIVE_THRESHOLD` 的
注释声称自己「只用于 `explain_divergence` 判断今天两个板块同涨同跌是不是常态」，
而那个函数只分得开「与历史关系一致」和「没有稳定关系」两种。今天背着走、历史上
却是正相关的那一对，会被判成后者。已按「删掉残留」处理：常量删掉，并在
`explain_divergence` 的 docstring 里写明它只分两种情况、以及要认出第三种得让
`read_seesaw` 同时保留正相关对——**那是另一个读数，不是这个函数少写了一个分支**。

---

## 5. 第二道闸门：文档里的命令还跑不跑得通

这份文档管的是「代码接没接上」。它有一个对称的盲区：**文档写的用法，代码还认不认**。
2026-08-11 撞上一次——README 与 `docs/USAGE.md` 从第一版起写的都是
`ashare-ds advise 600519.SH`，而解析器只认 `--symbols`，照文档敲直接退出。
文档、代码、测试**三边各自都是绿的**，没有任何一条现有口径会去看两者对不对得上。

```bash
python3 scripts/docs_audit.py        # 用法文档里的每条 `ashare-ds` 命令都过一遍解析器
```

只解析不执行，不打网络、不碰状态目录。`ROADMAP.md` / `REWORK.md` 不在范围内：
它们写的是还没做的设计，里面的命令解析失败是正确结果。范围按文档的**用途**划，
不按一份「这几行豁免」的清单——理由同 §4.1。
