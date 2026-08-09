# 功能接线清单

**这份文档回答一个问题：某个模块今天到底跑不跑。**

它存在的理由写在 `docs/ROADMAP.md` §2——一个被导出、有测试、有 `health_check`，
唯独没有任何调用方的模块，会制造「系统有这项能力」的假象。假象的代价不是多几行
代码，是**下判断的时候把它算进去了**。所以接线状态必须是一个能查的事实，而不是
读完全仓 import 才能拼出来的印象。

- 生成日期：**2026-08-09**
- 口径：`src/` 共 **163 个模块 / 42013 行**
- 判定方法：从 `cli.py` 出发做 import 可达性 + 公开符号的实际引用检查。
  **两条都要过**——只被 `__init__.py` 转出口而没人用它的符号，算未接线。

---

## 0. 一句话结论

| 状态 | 模块数 | 含义 |
|---|---|---|
| **活** | 163 | 至少有一条 CLI 命令能走到，且符号被真正调用 |
| **建成·未接线** | **0** | 代码写完了、大多有测试，但没有任何调用方 |
| **已删** | 2 | 本轮删除，见 §3 |

当前没有已建未接线模块。新增模块仍需按本文档的可达性口径复核，避免未来重新积累死代码。

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
接它要改候选池的产出口径，不是纯增量，所以单列在 ROADMAP 而不是顺手做掉。

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
`adapters/qmt_xtdata`、`adapters/mapping`、`adapters/ratelimit`、
`notify/feishu`、`chat/repl`、`envfile`、`schedule/clock`。

---

## 3. 本轮已删（3 类）

| 模块 | 行数 | 删除理由 |
|---|---|---|
| `research.py` + `tests/test_research_pipeline.py` | 17 | **文件自己的 docstring 写着「已删除，等待 `git rm`」。** `ResearchPipeline` 那条矩阵管线已被 `strategy/select.py` 取代 |
| `adapters/chip_distribution.py` + 测试 | ~200 | 被 `signals/chips.py`（换手衰减法估算）取代，且本机东财 CYQ 实测不可达。ROADMAP §7 快赢 #1 点名要删 |
| `simulation/` + `tests/test_paper_simulation.py` | ~1,400 | 纸上交易与决策卡订单校验不属于分析主链路，已由 facts 导出与人工判断替代 |

`signals/chips.py` 是**活的**（`morning` / `postclose` / `preopen` / `daemon` 都走到），
它才是今天系统里真正的筹码读数——命名为 `chip_*_est` 是刻意的，见该模块 docstring。

---

## 4. 怎么复核这份清单

判定不是手写的，可以重跑——**两条口径都在脚本里**，退出码非 0 就是有未接线的
模块或符号：

```bash
python3 scripts/inventory_audit.py             # 两条都跑
python3 scripts/inventory_audit.py --symbols   # 只跑「仅转出口」那条
```

本文档的数字就是这个脚本的输出：`163 个模块 / 163 个可达 / 454 个公开符号 / 0 个仅转出口`。

写成脚本而不是留在 git 历史里，是因为第二条口径此前正是这么丢的——文档里写着
「可以重跑」，脚本却只存在于某次会话，于是那条检查两个月没跑过，攒出 3 个死符号
（见 §0）。**一条不能一行命令跑出来的口径，等于没有这条口径。**

**下次改动后要重跑的时机**：新增模块、删除模块、把某个未接线模块接进主链路。
接线之后记得把它从 §1 移到 §2——**这份文档过期就没有价值**，它唯一的用处是
「查得到、且查到的是真的」。
