# 本轮改造：主链路清理 · 量价策略 · 筹码与元数据 · 行情记忆

写于 2026-08-03。这份文档只定**方向与判据**，不含代码。落地按 §二 的顺序推进，
每一步做完回来把状态标上。

> **实现状态约定**同 `SOLUTION.md`：未建的一律标「⬜ 未建」，在建标「🔨 在建」，
> 建成标「✅」。**把设计当现状是这份文档最需要防的事**——上一版文档正是因此重写过一次。

四件事，按确定的顺序：

| # | 事 | 为什么排这个位置 | 状态 |
|---|---|---|---|
| 1 | 主链路清理 | 后面三件都要往链路上挂东西。链路是两套的时候，挂哪一套都错 | ✅ **已建**（§三.4），697 tests 全绿 |
| 2 | 量价策略深化 | 「放量上涨/缩量下跌」的判据，买点与**卖点/止损**同时补 | ✅ **已建**（§四.4） |
| 3 | 筹码 + 标的元数据 | 依赖外部源探测，可与 2 并行 | ✅ **已建**（§五.3） |
| 4 | 行情记忆与日报/周报/月报 | 需要 1 产出的口径稳定之后才有意义 | ⬜ 未建 |

---

## 一、先说清楚现状

### 1.1 「选股没有明确策略」——策略有，但只挂在一条链上

`strategy/playbook.py` 已经把四种打法写成了显式判据链，这正是「放量上涨/缩量下跌」
那一类东西：

```text
BREAKOUT   突破买    60日分位 ≥0.85 × 量比 1.2–4.0 × 未从20日高回落
PULLBACK   回踩买    回调到位 × 缩量 ≤0.90（门槛，不是权重）× 收在 MA20 上方
RANGE_LOW  区间下沿  60日分位 ≤0.35 × 下沿不放量 × 下方有支撑
REVERSAL   超跌反转  deep 档 × 必须有反转触发（Spring / 放量反包 / 踩回成本收回）
```

`strategy/regime.py` 再加一道大盘闸门：情绪周期决定今天**允许**哪几种打法、仓位
上限多少，冰点档直接不出候选。

**真问题是它只接了一条链。** 实测四条链路的打法门槛覆盖情况：

| 链路 | 入口 | 过 `evaluate_playbooks` | 过 `regime` 闸门 | 你看到的是什么 |
|---|---|:---:|:---:|---|
| 盘前主链路 | `jobs/morning.py` | ✅ | ✅ | 有打法标签、有判据、有周期约束 |
| 个股买卖点 | `jobs/advise.py` → `advice/engine.py` | ❌ | ❌ | **只有加权分数与价位，没有「凭哪条打法买」** |
| 盘后矩阵 | `jobs/post_close.py` | ❌ | ❌ | 走的是被 morning 明确否定过的旧逻辑 |
| 盘中卖点 | `monitor/holdings.py` | ❌ | ❌ | 五类卖点齐全，但**没有一条是量价触发** |

`advise` 是人主动问一只票时走的路，也是最常被读的输出。它现在回答不了
「这只票凭哪条打法可以买」——这就是「感觉太简单、没有明确策略」的来源。

### 1.2 「量价关系太简单」——这条完全成立

整套系统对量能只有**两个**读数：

| 读数 | 出处 | 局限 |
|---|---|---|
| `volume_ratio_20d` | `signals/daily_technical.py:_volume_ratio` | 当日量 / 前20日均量。单日快照，看不出「连续放量」还是「单日脉冲」 |
| `dip.volume_contraction` | `signals/dipbuy.py` | 回调段的缩量程度，只在回调时有定义 |

没有的东西，逐条列出来：

- **换手率**。量比是自己跟自己比，跨票不可比；换手率（成交量/流通股本）才可比。
  流通股本 `instruments.batch` 全 A 100% 覆盖、0.97 秒，数据就在手上没用起来。
- **量价背离**。价创 N 日新高而量未创新高（顶背离）、价创新低而量萎缩（底背离）。
- **量能趋势**。近 5 日量能斜率——连续三天放量和今天突然放量是两回事。
- **上涨量/下跌量比**。近 N 日上涨日成交量之和 ÷ 下跌日成交量之和。这是**不用 L2
  时「主动买卖」最诚实的替代**。
- **天量/地量**。天量滞涨是顶部特征，地量是底部特征，两个都没有判据。
- **量价四象限**本身没有被表达成一个标签。

### 1.3 主链路不清晰——因为有两套并行管线

```text
链路 A（真链路，新）
  morning → regime 闸门 → 板块排序 → playbook 门槛 → rule_based 打分
          → 候选池 → journal → monitor

链路 B（遗留，旧）
  post_close → research.py:ResearchPipeline → DecisionMatrix → pre_open
```

链路 B 自带**一整套与 A 重复且口径不同**的东西：

| 概念 | 链路 A | 链路 B | 冲突 |
|---|---|---|---|
| 仓位系数 | `regime.position_cap` + `predict.suggested_position_factor`，取 min | `research._position_band_factor`（阶跃） | **两套算法算同一个数** |
| 大盘状态 | `EmotionCycle` 七档（冰点/启动/发酵/高潮/分歧/退潮/未知） | `_market_regime` 四档字符串（strong/balanced/cautious/defensive） | 两套分类，互不映射 |
| 板块选择 | `sectors/predict.py:SectorPredictor` | `research._select_sectors` + `Route(MAINLINE/PULSE/HEDGE)` | 两套排序 |
| 阈值形状 | 斜坡（`_ramp`），项目已明确否定阶跃 | 阶跃（`_blowup_factor` 等） | **B 用的正是 A 修掉的那种** |

`research.py` 208 行 + `models/schemas.py` 里约 150 行的 blueprint/matrix 系
schema，全部服务于链路 B。链路 B 的产物 `DecisionMatrix` 只被 `pre_open` 和
`build-matrix` 命令消费。

**这是「代码混乱、主链路不够清晰」最大的一块。** 不是命名问题，是同一个决策被两
套不同的规则算了两遍，而两遍的结果没有任何地方对账。

其余小块（**2026-08-05 全部核实完毕**，原文的三条「待核实」是在 shell 受限、
跑不成 grep 的环境下写的，现补上实测结论）：

- `src/ashare_decision_support/screening/` 是**空目录**（commit `f6950aa` revert 后残留）
  → **已不存在**，无需处理。
- `adapters/chip_distribution.py` 只被 `adapters/__init__.py` 和一个测试引用，**没有任何生产调用方**
  → **已删**（2026-08-05）。取代它的是 `signals/chips.py`（换手衰减法估算），
  那个是活的，`morning` / `postclose` / `preopen` / `daemon` 都走到。
- `models/enums.py:Route` → **已不存在**。全仓唯一残留是一句注释里的名字，不是引用。
- `schemas.py:AuctionFeatures / OpenFeatures` → **判断有误，这两个是活的，不要删。**
  它们是**适配器协议的一部分**：`adapters/base.py` 的 `get_auction_features` /
  `get_open_features` 加五个适配器实现都按它们出参，`adapters/mapping.py` 的归一层
  拿它们当券商字段的落点。**策略层确实不消费**（竞价那条线没建起来，见
  `docs/DECISIONS.md`），但「没有策略层消费」不等于「无引用」——这正是
  `schemas.py:16` 的类 docstring 已经写明的事。

### 1.4 筹码与「明盘暗盘」——先说数据边界

- **L2 逐笔不做**（已确认）。
- **筹码分布**：`docs/DATA_SOURCES.md` §十一记录的实测结论——TickFlow 无 chip/cost/
  winner 接口；`akshare.stock_cyq_em`（东财 CYQ）函数存在但本机 `RemoteDisconnected`，
  与 `push2` 系被出口代理拒绝同因；同花顺未发现个股筹码接口。

  **但有一条自算的路没走过**，见 §五.1——换手衰减成本分布，它需要的换手率和流通股本
  今天就都有。这比「成交成本代理」强一个量级，且是真正的筹码分布算法。

- **标的中英文与所属领域**：`https://files.688798.xyz`。**本次会话网络受限，未能探测**，
  格式与字段未知。落地第一步是探测，不是写代码。

### 1.5 行情学习——完全没有

现在系统的记忆只有两处，都不回答「最近是什么行情」：

| 已有 | 记的是 | 答不了 |
|---|---|---|
| `journal/` 台账 | 每条建议 T+1..T+5 的兑现 | 单条建议的对错，不是市场状态 |
| `news/corpus.sqlite3` | 每条消息的 first_seen / 板块标签 | 消息，不是价格行为 |

没有任何地方存过：板块每天的排名、轮动、相关性、主线更替、风格切换。
所以「银行和半导体是不是跷跷板」这个问题，系统今天**连数据都没有**，
更谈不上回答。日报/周报/月报也不存在。

---

## 二、改造顺序与验收

```text
第 1 步  主链路清理        删链路 B，三套口径并一套，四条链共用同一个打法门槛
第 2 步  量价策略          signals/volume_price.py → 进 playbook 门槛 + 进卖点触发
第 3 步  筹码与元数据      探测 → 能接就接，接不到就诚实标注（与第 2 步可并行）
第 4 步  行情记忆          落库 → 轮动/跷跷板 → 日报 → 周报/月报 → 回喂盘前
```

每一步的验收都是同一条：**现有测试全绿 + 新增判据有测试钉住 + 端到端跑通一次**。
不满足就不算做完，不往下走。

---

## 三、第一步：主链路清理

### 3.1 删除清单

| 删什么 | 行数 | 理由 |
|---|---:|---|
| `src/ashare_decision_support/research.py` | 208 | 链路 B 的全部逻辑 |
| `models/schemas.py`：`Candidate` `DecisionMatrix` `MarketContext` `SectorBlueprint` `CandidateBlueprint` `ResearchInput` `MatrixBuildConfig` `BlacklistEntry` `CapitalFlowItem` `NewsItem` `AnnouncementItem` | ~150 | 只服务链路 B |
| `models/enums.py:Route` | 4 | 只服务链路 B |
| CLI `build-matrix` 子命令 | ~8 | 链路 B 的唯一手工入口 |
| `src/ashare_decision_support/screening/`（空目录） | 0 | revert 残留 |
| `tests/test_research_pipeline.py` | — | 随链路 B 一起 |
| ~~`schemas.py:AuctionFeatures` `OpenFeatures`~~ | — | ❌ **不删。核实结果是它们还活着**：`adapters/base.py` 的 `get_auction_features` / `get_open_features` 是适配器协议的一部分，五个适配器（base · akshare · mapping · tickflow · qmt）都在实现它。策略层不消费只是因为竞价那条线还没建（QMT 补不上撮合量，见 `DECISIONS.md`），不是因为它们是死代码 |

合计约 400 行，加上 `post_close`/`pre_open`/`notify/render.py` 的瘦身，预计净减 600+ 行。

**已核实的一处连带影响：** `ask/context.py:49` 定义了
`MATRIX_LATEST_NAME = "decision_matrix_latest.json"`——问答链路**确实在读**盘后矩阵。
删链路 B 时必须同步把它改成读 `post_close` 的新 payload，否则「盘后决策矩阵」那一块
上下文会**静默变空**（`ask/context.py` 每块独立守卫，读不到不报错，只是那一节消失）。
`chat/session.py` 不读，已核实。

### 3.2 三套口径并成一套

| 概念 | 唯一出处 | 契约 |
|---|---|---|
| 仓位 | `strategy/regime.py:position_cap`（上限）× `sectors/predict.py:suggested_position_factor`（建议值） | **取 min。上限不是目标值。** 手工值仍然优先，但超过上限要出显式警告——这条 `morning.py` 已经做对了，推广到全链路 |
| 大盘状态 | `models/enums.py:EmotionCycle` 七档 | 删掉 strong/balanced/cautious/defensive 那一套。**不做映射**——两套分类的切点不同，映射出来的是一个看起来精确的错数 |
| 打法门槛 | `strategy/playbook.py:evaluate_playbooks` | 四条链路统一走它 |

### 3.3 一条链路

抽出共用的选股段，让 morning / post_close / advise 调同一个函数：

```python
# 新增 pipeline.py（或放进 advice/），签名大意：
def evaluate_universe(
    bars_by_symbol: dict[str, list[dict]],
    regime: RegimeVerdict | None,      # None = 不施加周期约束（advise 单票用）
    news: dict[str, SymbolSentiment] | None = None,
) -> list[ScoredCandidate]:
    """日线 → 技术快照 → 威科夫 → 低吸画像 → 量价 → 打法门槛 → 打分。

    三条链路的差别只在**标的从哪来**和**结果怎么渲染**，中间这一段必须是同一份
    代码。现在 morning 和 post_close 各写了一遍，而两遍已经漂移到用不同的规则。
    """
```

改造后：

| 链路 | 标的来源 | regime | 输出 |
|---|---|---|---|
| `morning` | 板块成分股（全市场自上而下） | 生效 | 候选池 + 台账 + 盘前简报 |
| `post_close` | 自选股 + 持仓 | 生效 | 次日执行卡（替代 DecisionMatrix） |
| `advise` | 手工指定 / 自选股 | **不生效**（人问一只票时该看到完整判断） | 买卖点 + 打法判据 |

`advise` 不施加周期约束这一条要保留——`playbook.py` 的 docstring 已经写明理由：
人工问一只票时不该被今天的大盘状态静默截断。但**要把打法判据显示出来**，包括
「今天周期不允许这条打法」这句话本身。

### 3.4 已落地的改动（2026-08-03）

| 文件 | 改动 |
|---|---|
| `strategy/select.py` | **新增。** `analyze_universe`（结构读数，贵，算一次）+ `select_from`（过门槛、打分，可多次）。`SelectedSymbol.to_row()` 是台账/候选池/渲染共用的那一行 |
| `strategy/playbook.py` | `PlaybookMatch.to_dict()` 补上 `rejected`，新增 `reject_reasons` 属性。**「为什么不买」此前算出来了但没被序列化**，于是台账、卡、问答三处都看不到 |
| `jobs/morning.py` | `_rank_candidates` 改调 `select_from`，两遍打分共用一份结构读数；`_note_setup_funnel` 移进 `select.py` |
| `jobs/post_close.py` | **重写。** 走同一段选股逻辑，并**第一次过情绪周期闸门**；产物由 `DecisionMatrix` 改为 `post_close_latest.json`；自选股只标注不剔除（`require_playbook=False`），分成「明天可以做的 / 只能看，不能做」两段 |
| `jobs/pre_open.py` | 改读新 payload，`--matrix` → `--payload` |
| `jobs/advise.py` + `advice/engine.py` | **接上打法判据**：`StockAdvice` 新增 `setup` / `playbook` / `has_playbook`；无可用打法时拒绝理由进 `warnings` 而不是 `reasons` |
| `notify/render.py` | 两个渲染器改吃 dict；价位地图与候选表加「打法」列；逐只看法加「今日无可用打法：……」 |
| `ask/context.py` | 读 `post_close_latest.json`，块名「次日决策矩阵」→「次日执行卡」 |
| `models/schemas.py` · `enums.py` · `cli.py` | 删链路 B 的 schema 与 `Route`；删 `build-matrix` 子命令 |
| `tests/test_daily_jobs.py` · `tests/test_strategy_select.py` | 圆桌测试改新 payload；新增选股段测试 |

**两个文件只做了墓碑，等 `git rm`**：`src/ashare_decision_support/research.py` 与
`tests/test_research_pipeline.py`。改动时 shell 被安全分类器挡住，删不掉文件，
所以改成了 import-safe 的说明性 docstring。

### 3.5 一处判断修正

方案初稿把 `schemas.py:AuctionFeatures` / `OpenFeatures` 列进了删除清单，标注
「疑似 commit `306b806` 漏删」。**核实结果是它们还活着**——`adapters/base.py` 的
`get_auction_features` / `get_open_features` 是适配器协议，五个适配器都在实现它。
策略层不消费只是因为竞价那条线还没建（QMT 补不上撮合量，见 `DECISIONS.md`），
不是死代码。删掉后已恢复。

---

## 四、第二步：量价策略

### 4.1 新增 `signals/volume_price.py`（L1 事实层，只出读数不出结论）

全部从**已有日线 + 流通股本**算，零新增依赖、零新增取数：

| 读数 | 定义 | 答什么 |
|---|---|---|
| `turnover_rate` | `volume / float_shares` | 换手率。跨票可比，量比不可比 |
| `vp_quadrant` | 涨跌 × 放缩量的四象限标签 | 放量上涨 / 缩量上涨 / 放量下跌 / 缩量下跌 |
| `vol_trend_5d` | 近 5 日量比序列的斜率 | 连续放量 vs 单日脉冲 |
| `up_down_vol_ratio` | 近 20 日上涨日成交量和 ÷ 下跌日成交量和 | **不用 L2 时「主动买卖」的替代读数** |
| `price_vol_divergence` | 价创 N 日新高/新低 而量未同步 | 顶背离 / 底背离 |
| `climax_volume` | 量比 ≥ 3 且 60日分位 ≥ 0.9 | 天量见顶 |
| `dry_up` | 量比 ≤ 0.6 连续 ≥ 3 日 | 地量见地 |

三条必须写进代码的约束，否则量价读数会被当成比它实际更有力的证据：

1. **同一个量比在不同位置意义相反。** 回调段的放量是出货，启动段的放量是确认。
   `scoring/rule_based.py:_score_volume` 已经做对了这一条，新模块必须沿用：
   **标签只出事实，含义由 playbook 的判据决定。**
2. **`up_down_vol_ratio` 不是主动买卖单。** 它是日线粒度的方向性成交量代理。
   命名、注释、简报文案三处都不能写成「主力资金」「主动买入」。有分钟线时可以
   升级到「收在分时均价上方的分钟成交量占比」，那更接近但仍然不是逐笔。
3. **换手率要区分全流通与非全流通。** 有限售股的票，流通股本口径下的换手率会
   系统性偏高。`instruments` 返回的是流通股本，先用它，并在 note 里写明口径。

### 4.2 接进 playbook —— 做**门槛**，不做权重

这是 `playbook.py` 已经确立的原则，本次继续：*门槛不对时调权重调的是噪声。*

| 打法 | 新增判据 | 为什么 |
|---|---|---|
| BREAKOUT | `vol_trend_5d > 0`（连续放量而非单日脉冲）<br>否决 `price_vol_divergence == 顶背离` | 「放量上涨」要的是持续的量，不是一根量柱 |
| PULLBACK | `up_down_vol_ratio ≥ 1.0` | 「缩量下跌」要的是下跌日的量确实小于上涨日，不只是今天缩量 |
| RANGE_LOW | `dry_up` 加分标注（不设门槛） | 区间下沿的地量是好事，但不该成为硬条件 |
| REVERSAL | 现有三触发之外，接受 `dry_up` 后的首根放量阳线 | 地量见地 → 放量启动，是经典反转序列 |

### 4.3 卖出与止损：把「有依据」补齐

`monitor/holdings.py` 现有五类触发（止损/目标/结构破坏/时间止损/浮盈回吐）质量很高，
但**没有一条是量价触发，也没有一条是环境触发**。补两类：

| 新增触发 | 判据 | 说的是 |
|---|---|---|
| **天量滞涨** | `climax_volume` 且当日收在日内区间下半段 | 放巨量却没能收在高位——买盘被吃完了 |
| **放量破位** | `vp_quadrant == 放量下跌` 且跌破 MA20 或跌破 30 日成交成本 | 与「缩量回调」正好相反的那一面，是最该无条件走的形态 |
| **顶背离** | `price_vol_divergence == 顶背离` 且已有浮盈 | 价还在创新高但量跟不上，减仓而非清仓 |
| **环境降档** | 情绪周期切到 `EBB` / `FREEZING` | 「今天行情很差」这件事必须能直接触发降仓，而不是只影响明天的选股 |

最后一条是用户明确点名的场景（「比如今天行情很差」）。现在 `regime` 只在盘前选股时
生效，**盘中情绪转差不会对已有持仓说任何话**——这是一个真实的缺口。

环境降档触发的口径要写死：**它是提示降仓，不是清仓信号**，且必须带上当时的
炸板率/连板/跌停家数三个原始读数，让人能自己复核。

### 4.4 已落地的改动

| 文件 | 改动 |
|---|---|
| `signals/volume_price.py` | **新增。** 换手率 · 量价四象限 · 量能趋势 · 涨跌量比 · 量价背离 · 天量/地量 · 放量破位 |
| `signals/dipbuy.py` | `_normalised_volume` 提升为公开的 `normalised_volume`，量纲反推不再有第二份 |
| `strategy/playbook.py` | 四条判据链接上量价门槛；`evaluate_playbooks` 多一个 `volume_price` 入参（缺失时**放行并标注**，不判负） |
| `strategy/exits.py` | **新增。** 卖出判据：天量滞涨（减）· 放量跌破 MA20（离场）· 顶背离（减，且要求有浮盈）· 情绪周期降档（EBB 减 / FREEZING 离场） |
| `strategy/select.py` | `analyze_universe` 多算一份量价画像；`SelectedSymbol` 带 `volume_price` 与 `exits`，两者都进 `to_row()` 落台账 |
| `notify/render.py` | 盘后卡在买入判据下方并列「⚠️ 卖出判据同时成立」 |
| `tests/test_volume_price_and_exits.py` | **新增**，17 条 |

**为什么卖出判据不进盘中监控。** 盘中告警走分钟线，而这些判据全部算自日线——盘中
拿到的日线最后一根通常还是昨天那根。把日线口径的「天量滞涨」当盘中告警发出去，说的
是昨天的事却带着此刻的时间戳，比不报更糟。它们的位置在日线新鲜的那几个时刻：
`post_close`（收盘后）、`advise`（你主动问）、`morning`（盘前定处置计划）。
`monitor/holdings.py` 那五类（止损/目标/结构破坏/时间止损/浮盈回吐）本来就是分钟线
口径，保持不动。

**实现时被测试抓出来的一个错。** `expanding`（量能是否持续放大）第一版写成
`vol_trend_5d > 0`，即近 5 日量能的最小二乘斜率为正。**这个判据答不了它要答的问题**：
序列 `[1,1,1,1,3]`（单日脉冲）的斜率和 `[1.4,1.8,2.2,2.6,3.0]`（持续放量）同号——
短窗口的最小二乘会被末端离群值整个拽正，而区分这两者正是它唯一的用途。改成看
**今天之前那几天**相对 20 日均量的位置，today 完全不参与判断。斜率作为原始读数保留。

---

## 五、第三步：筹码与标的元数据

### 5.1 筹码：三条路，按可靠性排序

**路 1（推荐，今天就能走通）：换手衰减成本分布，自算。**

这是通达信 CYQ 的公开算法，输入只需要日线 OHLCV + 流通股本，两样都有：

```text
初始：把最早一根 bar 的成交量按价格分布铺开
逐日：t = 当日换手率 = volume / float_shares
      chips *= (1 - t)                    # 老筹码按换手率衰减
      chips[当日价格区间] += volume * t    # 今日成交成为新筹码
分布假设：三角分布（低点→均价→高点），或均匀分布，取一个并写进文档
```

派生：`avg_cost`（加权平均成本）、`winner_ratio`（获利盘比例 = 成本低于现价的筹码占比）、
`concentration_90`（90% 筹码的价格区间宽度）。

为什么这条最该先做：

- **零外部依赖**，不受本机网络策略影响
- 它是**真的筹码分布算法**，不是「成交成本代理」的换皮
- 可以逐日回算，因此**可回测、可进台账**
- 分布之和恒等于 1，这是一条可以写成测试的守恒律

命名必须诚实：叫 `chip_distribution_est`，简报里写「**估算**筹码分布（换手衰减法）」。
它与真实 CYQ 的差别在于分布假设，不在于算法本体——这句话要写进模块 docstring。

**路 2：外部真实筹码源探测。** 扩展 `scripts/source_audit.py` 加一段筹码探测，
把下面这些一次跑完并记录 http 码与列名：

```text
akshare.stock_cyq_em                     东财 CYQ（已知本机失败，复测确认）
d.10jqka.com.cn / basic.10jqka.com.cn    同花顺（d.10jqka 已知可达）
vip.stock.finance.sina.com.cn            新浪
akshare 其余 THS/新浪系筹码相关函数        逐个试
```

探到什么接什么。**探不到就明说没有**，不能用路 1 的估算值冒充真实筹码——
`DATA_SOURCES.md` §十一已定过这条闸门，继续执行。

**路 3：已有的 `turnover_cost_15/30` 成交成本代理**（`signals/dipbuy.py`）保留不动。
路 1 建成后它仍然有用：它便宜、无分布假设、且已经进了打分。

**三个值永远分三个字段名**：`chip_avg_cost`（路2真实）/ `chip_avg_cost_est`（路1估算）/
`turnover_cost_30`（路3代理）。混用一个名字，事后没有任何办法查出某个数当时是从哪来的。

### 5.2 标的元数据

新增 `adapters/security_master.py`。源的实际格式见 §五.3（已探测）。

设计契约（与 `adapters/mapping.py` 同一条原则——**策略层从不直接读上游字段名**）：

| 规范字段 | 用途 |
|---|---|
| `symbol` | 主键 |
| `name_cn` / `name_en` | 简报、告警、问答里的显示名 |
| `industry` / `sector` | 与申万分类**并列**的第二套归类 |
| `concepts` | 题材标签。**超短线看题材，不只看行业** |
| `board` / `listed_on` | 主板/创业板/科创板 → 涨跌停幅度；上市日 → 次新判定 |

三条设计约束：

1. **落本地缓存**（`~/.ashare-ds/reference/securities.json`），带 `fetched_at` 与
   schema 版本戳。理由同 `sectors/taxonomy.py`：这类参考数据变动很慢，每次跑都拉
   一遍是浪费，且上游挂了会拖垮整条链路。
2. **缺字段不猜。** 拿不到英文名就是空，不用拼音顶。
3. **概念标签不进板块打分。** 至少这一轮不进——概念的粒度和申万行业不同，硬融进
   板块分数会造出一个看起来精确的错数（`morning.py` 对同花顺行业指数已经做过同样的
   判断，理由一致）。它先只做**显示**与**问答检索**。

### 5.3 已落地的改动与实测结果

**元数据源探测（2026-08-04）。** `https://files.688798.xyz/` 是一个 miniserve 目录，
下面只有 `ths/`，两个文件，每天 08:00 前后更新：

| 文件 | 条数 | 字段 |
|---|---|---|
| `industries.json` | 5446 | `symbol` · `name` · `industries`（**恰好三级**） |
| `concepts.json` | 5546 | `symbol` · `name` · `concepts`（中位 7–8 个） |

实测：代码已是 `NNNNNN.XX` 规范形式，覆盖 SH 2280 / SZ 2837 / BJ 329；两份是包含
关系（concepts ⊇ industries，100 只只有概念没有行业）；31 个一级行业；389 个概念。

> ⚠️ **这个源没有英文名。** 原始需求提到「中英文名称」，但两份文件的字段只有
> `symbol` / `name`（中文简称）/ `industries` 或 `concepts`，**没有任何英文字段**。
> 所以 `SecurityInfo.name_en` 恒为 None，不用拼音、不用代码、不去别处凑。要英文名
> 得另找源。

> ⚠️ **不是每个「概念」都是题材。** `融资融券` 挂了 **3841/5546**（69%）只——它是
> 可交易属性不是题材，同类的还有 `深股通`/`证金持股`。一个覆盖七成市场的标签在
> 题材轮动里没有信息量，混进去会让「今天哪个题材在动」永远指向它。`themes()` 按
> 成分占比设了上限（`MAX_THEME_SHARE = 0.25`）；`concepts_of()` 仍返回全部标签。

**筹码源复测（2026-08-04）：仍然拿不到。** `akshare.stock_cyq_em` 与
`stock_zh_a_hist` 双双 `RemoteDisconnected`，`push2.eastmoney.com` 探测 `http=000`
——与 `DATA_SOURCES.md` §三记录的出口代理策略完全一致。**所以走的是自算那条路。**

| 文件 | 改动 |
|---|---|
| `adapters/security_master.py` | **新增。** 拉取 + 合并 + 原子写缓存（`~/.ashare-ds/reference/`）；`by_concept` / `by_industry` 反查；`themes()` 剔除宽标签。拉取失败时**继续用旧缓存并标注**——三天前的行业分类基本仍然正确，没有分类才是彻底不能用 |
| `signals/chips.py` | **新增。** 换手衰减法估算筹码分布（通达信 CYQ 的公开算法），三角分布假设，200 档价格网格。派生 `chip_avg_cost_est` / `chip_winner_ratio_est` / `chip_concentration_90_est` |
| `adapters/base.py` | 新增 `float_shares()`：换手率与筹码衰减速度都靠它。**只认流通股本，不拿总股本顶替** |
| `strategy/select.py` | `analyze_universe` 多算一份估算筹码；`chips` 进 `to_row()` 落台账 |
| `jobs/morning.py` · `jobs/post_close.py` | 取流通股本并传进选股段；取不到时明确写进 notes |

**实测（600519 / 000001 / 688981，267 根日线）：**

| 标的 | 换手 | 估算平均成本 | 现价偏离 | 估算获利盘 | 90% 筹码宽度 |
|---|---:|---:|---:|---:|---:|
| 贵州茅台 | 0.29% | 1345.44 | +1.0% | 44% | 19.5% |
| 平安银行 | 0.55% | 10.96 | +6.1% | 92% | 12.4% |
| 中芯国际 | 3.27% | 143.60 | −18.9% | 3% | 40.4% |

换手率量级与三只票的真实特征吻合（茅台低换手、中芯高换手），获利盘与各自走势
一致。**这是估算不是真实筹码**，命名一律带 `_est`，简报每次都印那一行免责。

**接线时发现的一个真实缺口。** `analyze_universe` 接受 `float_shares`，但两个调用方
一开始都没传——于是换手率和筹码在**生产里恒为 None**，而所有单元测试照样全绿，因为
它们都直接调 `estimate_chips`。缺的不是算法，是那一根线。已补，并加了一条接线测试
（`test_pipeline_actually_receives_float_shares`）专门钉它。

---

## 六、第四步：行情记忆与日报/周报/月报

目标（用户原话）：*不断学习过去的市场情况、行情、趋势，每个板块的涨跌要分析总结出
原因，供后续分析和参考；上下文不宜太长。* 主要给机器用，人也要看得懂。

### 6.1 存储：新增 `market/` 模块 + SQLite

用 SQLite 的理由与 `news/store.py` 完全一致，也是全仓第二个（也是最后一个）该用它的
地方：**这是需要按维度查询、且长期累积的数据。** JSONL 回答不了「银行和半导体过去
60 天的相关系数」，只能全量扫，而扫的量每天都在涨。

`~/.ashare-ds/market/history.sqlite3`：

```sql
-- 每个交易日一行：大盘环境。字段全部来自 morning 已经在算的东西
daily_market(
  trade_date PRIMARY KEY, regime, position_cap,
  blowup_rate, max_streak, limit_up_count, limit_down_count,
  breadth, median_ret_1d, median_ret_5d, above_ma20,
  direction, score
)

-- 每个交易日 × 每个板块一行：板块行为
daily_sector(
  trade_date, sector, level,
  ret_1d, ret_5d, ret_20d, breadth, volume_ratio, strong_count,
  close_position,          -- 来自同花顺行业指数，答「收在日内哪个分位」
  net_inflow, rank,
  PRIMARY KEY(trade_date, sector, level)
)

-- 归因：这个板块这天为什么动
sector_attribution(
  trade_date, sector, driver, evidence_doc_keys, confidence, source
)
```

**写入点：`morning` 跑完顺手落一次，不新增任何取数。** 这三张表要的字段，
`_facts()` 里已经全都有了——现在只是算完就丢。

### 6.2 派生分析（纯计算，不经模型）

| 分析 | 算法 | 答什么 |
|---|---|---|
| **轮动** | `rank` 时间序列的差分 | 谁在爬升、谁在掉队、当前领先板块已经领先了几天 |
| **跷跷板** | 板块日收益矩阵 → 滚动 20/60 日相关系数 → 取显著负相关对 | **「银行和半导体是不是跷跷板」的直接答案** |
| **板块状态机** | 板块自身的 ret/breadth/close_position/rank 变化 | 每个板块打一个标签：启动中/发酵中/高位分歧/退潮/沉寂。与 `EmotionCycle` 同构，作用在板块层 |
| **持续性** | 领先板块在前 N 名停留的天数分布 | 「这条主线还能走几天」的历史基准 |
| **风格** | 申万一级按金融/消费/科技/周期分组的收益差 | 权重 vs 题材、大盘 vs 小盘 |

跷跷板这一条要写清楚它**不是因果**：相关系数只说两个板块历史上反向走，不说明为什么，
也不保证明天继续。它的正确用法是**风险提示**——「你选的主攻板块，它 60 日相关系数
−0.4 的对手今天在涨」——不是选股信号。

### 6.3 三层报表

| 报表 | 触发 | 内容 | 长度上限 |
|---|---|---|---|
| **日报** | 15:40 `review` 之后 | 今日情绪档 · 板块涨跌前后各 5 · 轮动变化（谁进前 5、谁掉出）· 今日归因 1–3 条链 · 明日观察点 | 60 行 |
| **周报** | 周五收盘后 | 本周情绪周期路径 · 板块排名热力 · 本周主线与持续天数 · 跷跷板对更新 · 本周台账兑现 | 120 行 |
| **月报** | 月末 | 风格切换 · 主线更替时间线 · 月度相关性矩阵 · 规则表现（入选 vs 对照）· 建议重标定的阈值 | 200 行 |

每份报表**分两段产出**：

- **机读段**：结构化 JSON，落库。这是「供后续分析和参考」真正依赖的东西。
- **人读段**：markdown。结论先行，表格为主，每个数字都能在机读段里查到。

归因段（「为什么涨」）走 LLM，但**只出标签与文字，不出数字**——与
`news/llm_classifier.py` 同一条约定，且必须引用语料库原文（`evidence_doc_keys`）。
数字全部来自上面的确定性计算。LLM 挂了，报表照出，归因段写「本期无归因」。

### 6.4 怎么回喂盘前而不撑爆上下文

**这是这一块最关键的设计决定**，直接回应「上下文不宜太长」。

盘前链路**永远不读报表全文**。新增：

```python
# market/memory.py
def recent_context(days: int = 5) -> dict:
    """给盘前链路的定长市场记忆。**上限 2KB**，超了就截并在 notes 里说明截了什么。

    只带三样：
      1. 最近 N 日的情绪周期路径与板块 rank 变化（谁在爬、谁在掉）
      2. 当前生效的跷跷板对（|corr| ≥ 阈值的前若干对）
      3. 每个候选板块的状态机标签与已持续天数

    **不带**：报表正文、归因文字、历史 K 线、任何超过 N 日的东西。
    需要更多的时候，人去读周报/月报，或者用 `ask` 单独问——那是另一条路径，
    有自己的上下文预算。
    """
```

定长是硬约束，不是建议。市场记忆一旦可以按需增长，它会在几周内挤掉盘前 facts 里
所有别的东西，而挤掉的顺序是不可控的。

### 6.5 分阶段落地

| 阶段 | 做什么 | 前置 |
|---|---|---|
| 4a | 建表 + `morning` 落库 + **日报** | 无（字段都已在算） |
| 4b | 轮动 + 跷跷板 + 板块状态机 + **周报** | 4a 攒够 ≥ 20 个交易日 |
| 4c | 归因（接语料库标签）+ **月报** + `recent_context` 回喂盘前 | 4b |

4a 几乎零成本，先做。4b 需要样本——**没有 20 天数据时相关系数是噪声**，这一点
和 `journal/stats.py:MODEL_READY_SAMPLES` 是同一条纪律：样本不足时算出来的数
不报告，或者报告并显式标注不可信。

---

## 七、明确不做的事

| 不做 | 理由 |
|---|---|
| L2 逐笔 / 明盘暗盘 | 已确认不做。用 `up_down_vol_ratio` 与分时均价上方成交量占比替代，且**不改名冒充** |
| 让 LLM 出数字 | 全仓既有约定，本次不破 |
| 概念标签进板块打分 | 粒度与申万不同，硬融会造出看起来精确的错数。先只做显示与检索 |
| 跷跷板当选股信号 | 相关系数不是因果。只做风险提示 |
| 按新统计回写打分权重 | `journal/stats.py:MODEL_READY_SAMPLES` = 200 条已兑现记录，还没到 |
| 保留 `DecisionMatrix` 做兼容 | 留着就还是两套口径。要删就删干净 |

---

## 八、验收清单

第 1 步（主链路清理）：

- [ ] `research.py` 与链路 B 的 schema 删净，全仓 grep 无残留引用
- [ ] `ask` / `chat` 的上下文装配不因删 `decision_matrix_latest.json` 而变空
- [ ] `post_close` 与 `morning` 共用同一个选股函数，仓位口径只剩一套
- [ ] `advise` 输出里能看到「凭哪条打法可以买 / 为什么今天不能买」
- [ ] 现有测试全绿

第 2 步（量价）：

- [ ] `signals/volume_price.py` 七个读数各有测试
- [ ] 四条打法的新判据有测试，且**拒绝理由**可读（拒绝理由本身就是「这只票为什么不买」的答案）
- [ ] 新增四类卖出触发有测试
- [ ] 端到端：`ashare-ds advise --symbols ...` 输出里有量价形态标签

第 3 步（筹码与元数据）：

- [ ] 换手衰减成本分布：分布之和守恒律有测试
- [ ] `chip_avg_cost` / `chip_avg_cost_est` / `turnover_cost_30` 三个名字在全仓不混用
- [ ] `source_audit.py --probe-chips` 跑得出一张可达性表
- [ ] `security_master` 探测报告：字段清单 + 覆盖率 + 缓存策略

第 4 步（行情记忆）：

- [ ] 建表 + `morning` 落库，跑三天有三行
- [ ] 日报能出，且每个数字都能在机读段查到
- [ ] `recent_context()` 输出 ≤ 2KB，超限有截断说明
- [ ] 样本不足时相关系数标注为不可信而不是照报
