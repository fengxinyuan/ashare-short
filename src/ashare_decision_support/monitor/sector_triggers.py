"""板块跃迁告警：只在**板块状态发生变化**时说话。

和 `monitor/triggers.py` 同一条纪律，理由也一样：板块截面每几轮取一次，一整天
下来有二三十轮。一条写成「有色金属现在很强」的规则会响二三十次，而**同一条消息
说第二遍就已经没有信息了**——第三遍开始它在训练你忽略这个通道。所以这里每一条
都是跃迁：它**之前不在**前十、**现在进了**；它**之前是强**、**现在不是**。

## 为什么不塞进 `triggers.py`

那个 `TriggerEngine` 的每个入口都吃 `StockAdvice + IntradaySnapshot + HoldingView`。
板块没有止损、没有成本价、没有五档、没有持仓。硬塞进去要给一半参数传 None，
然后在每条规则里判一次「这是板块还是个股」。共用的那部分——跃迁判定、冷却、
每日一次——本来就不在 `TriggerEngine` 里，而在 `state/alerts.py`，这里直接复用它。

## 状态存在哪

**另开一个文件**，不与个股共用。`AlertState` 是「读进来 → 改 → 整份写回」，
两个引擎各持一个实例指向同一个路径的话，后写的那个会把前一个的记录整片抹掉——
而那正是「这条今天已经发过了」的唯一凭证。分开之后 `/alerts` 里也不会混进
一堆看起来像代码的板块名。

## 噪声预算

行业 31 个 + 概念 346 个 = 377 个板块。**普涨的早盘里，几十个板块会同时「转强」。**
四道闸，数的都是**推送条数**（同一板块的多条触发器先合并成一条）：

1. **只看各自层级的前 `ATTENTION_RANK` 名。** 排在第 120 的概念转强不是新闻。
2. **同一板块一轮只发一条**（`_merge_by_sector`）。一次跃迁经常同时命中四条规则，
   而它们贴的是同一个证据块。
3. **每轮最多 `MAX_PER_PASS` 条**，并把被截掉的条数写进 notes——**静默截断会让
   「只推了 3 条」读起来像「只发生了 3 件事」**。
4. **每小时最多 `MAX_PER_HOUR` 条 + 全天最多 `MAX_PER_SESSION` 条。**

第 4 条里「每小时」那一层是实测补上的。30 轮模拟里，只有全天上限时早盘半小时的
排名 churn 会把当天额度一次吃光，于是 13:00 一个九家涨停的真涨停潮**一条都推不
出来**——而被它挤掉额度的那些，恰恰是成交额基数最小、名次最不稳的时段推出来的。
一个把噪声挡在门外的机制反过来变成了漏报机制，比没有它更糟。按小时分配之后，
早盘再吵也吵不掉下午的份。

**预算数的是推送不是触发器**（`_PushLedger`）。按触发器数的话，一条「排名跃升
（同时触发：转强、涨停潮）」记 3 笔，「全天 20 条」实际上七八条就闭嘴。

> 上面这些阈值是**按噪声预算推的，没有回测**。同 `sectors/rotation.py`、五档
> 那几层的处置：先落台账观察，等 `review` 能回答「排名跃升的板块选出来的票
> 兑现率高多少」，再谈要不要动它们。目前 `rank_surge` 是最吵的一条。

## 不做的事

- **不预测。** 「排名跃升说明资金正在进场」这类话不写。每条告警都是对已经发生的
  截面变化的陈述句，与 `sectors/rotation.py` 的处置一致。
- **不打分、不进选股。** 板块告警是「先看哪里」，不是「买什么」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ashare_decision_support.monitor.triggers import LEVEL_ACTION, LEVEL_INFO, LEVEL_WARN
from ashare_decision_support.sectors.intraday import (
    LEVEL_LABEL,
    SectorRead,
    SectorSnapshot,
)
from ashare_decision_support.state.alerts import AlertState
from ashare_decision_support.state.paths import state_path

# 与个股告警同一套默认：ACTION + WARN 推，INFO 实现了但默认关闭。
DEFAULT_LEVELS: tuple[str, ...] = (LEVEL_ACTION, LEVEL_WARN)

# 只有排进本层前这么多名的板块才有资格触发 ACTION。
ATTENTION_RANK = 15

# 「退潮」只对曾经排进前这么多名的板块报——第 80 名转弱不是退潮，是本来就没起来。
FADE_RANK_LIMIT = 10

# 排名跃升：较当日首轮至少上升这么多名，且现在进了前 `ATTENTION_RANK`。
RANK_SURGE_STEPS = 8

# 涨停潮：涨停家数从 `≤ BEFORE` 跳到 `≥ AFTER`。
LIMIT_WAVE_BEFORE = 1
LIMIT_WAVE_AFTER = 3

# 成交额虹吸：占全场成交额的比重较当日首轮放大到这么多倍，且绝对占比过闸。
AMOUNT_SURGE_RATIO = 1.5
AMOUNT_SURGE_MIN_SHARE = 0.02

# 噪声预算，三层。数的都是**推送条数**，不是触发器条数——同一板块多条触发器
# 合并成一条推送后只算一条（见 `_PushLedger`）。
#
# 每轮上限：一条推送要能读完。一轮真出现三十条变化时，截断比全发有用。
MAX_PER_PASS = 5

# **每小时上限。** 这一条是为了防一个具体的失效：早盘前半小时排名churn 最剧烈
# （成交额基数小、名次每轮翻动），而那恰恰是信息量最低的时段。只有「全天上限」
# 的话，早盘噪声会把当天的额度一次性吃光，于是 13:00 一个九家涨停的真涨停潮
# **一条都推不出来**——实测 30 轮模拟里就是这个结果。按小时分配额度之后，
# 每个时段各有各的份，早盘再吵也吵不掉下午。
MAX_PER_HOUR = 4

# 全天上限，兜底。超了当天剩下的变化留给盘后复盘。
MAX_PER_SESSION = 20


@dataclass(frozen=True)
class SectorTriggerPolicy:
    key: str
    level: str
    label: str
    cooldown_minutes: int = 60
    once_per_session: bool = False


POLICIES: dict[str, SectorTriggerPolicy] = {
    policy.key: policy
    for policy in (
        # 开盘一条总览。它不是跃迁，是**基线**：盘中监控可能在 09:35 起，也可能
        # 在午后重启，无论哪种，第一件该说的事都是「此刻钱在哪」。发一条带前三名
        # 的总览，而不是给每个强势板块各发一条——后者在普涨日是二十条。
        SectorTriggerPolicy("session_open", LEVEL_WARN, "开盘板块概览", once_per_session=True),
        # 「资金正在往这里挪」的直接证据。once_per_session：一个板块一天升一次
        # 就够说明问题，反复报名次会退化成实况解说。
        SectorTriggerPolicy("rank_surge", LEVEL_ACTION, "排名跃升", once_per_session=True),
        # 超额与上涨占比同时过闸——**只有涨幅不算强**，那可能是两只权重股拉的。
        SectorTriggerPolicy("tone_strong", LEVEL_ACTION, "转强", cooldown_minutes=60),
        # 涨停潮是情绪的硬证据，不是估计值。
        SectorTriggerPolicy("limit_up_wave", LEVEL_ACTION, "涨停潮", once_per_session=True),
        # 注意力在往这里集中。给 WARN 不给 ACTION：成交额是双边的，它说的是
        # 「这里很热闹」，不是「有人在买」。
        SectorTriggerPolicy("amount_surge", LEVEL_WARN, "成交额虹吸", once_per_session=True),
        # **对超短最值钱的一条。** 上面几条都是「去哪找机会」，这条是「手上的
        # 该走了」：领涨板块从强转弱，是板块层面唯一能提前于个股止损的信号。
        SectorTriggerPolicy("fade", LEVEL_WARN, "领涨板块退潮", once_per_session=True),
        SectorTriggerPolicy("rank_slide", LEVEL_INFO, "排名下滑", once_per_session=True),
    )
}


@dataclass(frozen=True)
class SectorAlert:
    """一条板块告警。`sector` 为空表示这是一条跨板块的总览（`session_open`）。"""

    trigger: str
    level: str
    headline: str
    detail: list[str] = field(default_factory=list)
    # 只属于这条触发器的理由行（不含所有触发器共用的板块证据块）。
    # 合并同一板块的多条告警时折的是它——按下标切 `detail` 会因为各触发器的
    # 理由行数不同而切进共用部分。
    evidence: tuple[str, ...] = ()
    sector: str = ""
    sector_level: str = ""
    excess_pct: float | None = None
    rank: int | None = None
    as_of: datetime | None = None
    # 这个板块里命中的持仓（或本轮显式盯的）标的，用于排序时优先。
    watch_hits: tuple[tuple[str, str], ...] = ()

    @property
    def label(self) -> str:
        policy = POLICIES.get(self.trigger)
        return policy.label if policy is not None else self.trigger

    @property
    def touches_watched(self) -> bool:
        return bool(self.watch_hits)


def load_sector_state(session: date, path: Any | None = None) -> AlertState:
    """板块告警自己的状态文件。**不能与个股共用**——见模块开头「状态存在哪」。"""
    resolved = path if path is not None else state_path("alerts", f"sector-{session}.json")
    return AlertState.load(session, resolved)

# `AlertState.observed` 里放推送流水的保留键。借用 observed 而不是新开一个状态
# 文件，是因为它本来就是「随告警状态一起原子写回」的那份数据——预算账和
# 「已经发过了」必须同生共死，否则进程被杀在两次写之间会让额度凭空多出来。
PUSH_LEDGER_KEY = "__pushes__"

# 板块状态 key 的前缀与「跨板块总览」那个桶。收成常量是因为它们在四处被拼、
# 在一处被拆——拼的和拆的对不上时，读回路径会安静地少掉一整类告警
# （`session_open` 的 key 只有两段，此前正是这么被 `state` 漏掉的）。
_FIRE_KEY_PREFIX = "sector"
_ALL_SECTORS = "__all__"


class _PushLedger:
    """记录**推送**发生的时刻，用来算每小时与全天额度。

    **不能拿 `state.fired` 来数。** 那里的 key 是 `板块|触发器`，而
    `_merge_by_sector` 会把折进正文的触发器一并 `mark_fired`——一条「排名跃升
    （同时触发：转强、涨停潮）」的推送在那里记 3 笔。用它当分母，「全天最多
    20 条」实际上会在七八条推送之后就闭嘴，而且闭得悄无声息。
    """

    def __init__(self, state: AlertState) -> None:
        self._state = state
        raw = state.state_of(PUSH_LEDGER_KEY).get("at") or []
        self.stamps: list[datetime] = []
        for item in raw:
            try:
                self.stamps.append(datetime.fromisoformat(str(item)))
            except ValueError:
                continue

    def spent_today(self) -> int:
        return len(self.stamps)

    def spent_this_hour(self, now: datetime) -> int:
        return sum(1 for item in self.stamps if item.hour == now.hour and item.date() == now.date())

    def record(self, now: datetime, count: int) -> None:
        self.stamps.extend([now] * count)
        self._state.set_state(
            PUSH_LEDGER_KEY,
            {"at": [item.isoformat(timespec="seconds") for item in self.stamps]},
        )


def fired_sectors_today(session: date, path: Any | None = None) -> list[dict]:
    """今天推过哪些板块告警，最新在前。给 `chat` / `ask` / `state` 三处共用。

    板块告警的状态是**另一个文件**（见模块开头「状态存在哪」），所以那三处沿用
    `AlertState.load(today).fired_summary()` 时看到的永远是个股那一份——问
    「今天板块有什么异动」会得到「今天没有任何告警」，而实际上推过五条。

    顺带把 key 拆回可读形式：`fired_summary()` 的 symbol 是
    `SECTOR:SW1:有色金属`（`normalize_symbol` 会整体大写），直接印出来像个代码。
    """
    rows: list[dict] = []
    for row in load_sector_state(session, path).fired_summary():
        parts = str(row.get("symbol") or "").split(":", 2)
        if len(parts) < 2 or parts[0] != _FIRE_KEY_PREFIX.upper():
            continue
        # 跨板块的总览（`session_open`）没有层级和板块名，key 只有两段。
        # **不能因此把它跳过**——它是当天第一条推送，跳过之后 `state` 会在推过
        # 一条的日子里报「今天没有板块告警」，正是这条读回路径要修的那个问题。
        level, name = (parts[1], parts[2]) if len(parts) == 3 else ("", "")
        policy = POLICIES.get(str(row.get("trigger") or ""))
        rows.append(
            {
                "level": level,
                "sector": name,
                "sector_label": LEVEL_LABEL.get(level, level) if level else "全场",
                "trigger": row.get("trigger"),
                "label": policy.label if policy is not None else row.get("trigger"),
                "alert_level": policy.level if policy is not None else None,
                "at": row.get("at"),
                "count": row.get("count"),
            }
        )
    return rows


class SectorTriggerEngine:
    """把这一轮的截面与之前的截面对比，产出跃迁。"""

    def __init__(
        self,
        levels: tuple[str, ...] = DEFAULT_LEVELS,
        attention_rank: int = ATTENTION_RANK,
        max_per_pass: int = MAX_PER_PASS,
        max_per_hour: int = MAX_PER_HOUR,
        max_per_session: int = MAX_PER_SESSION,
    ) -> None:
        self.levels = tuple(levels)
        self.attention_rank = attention_rank
        self.max_per_pass = max_per_pass
        self.max_per_hour = max_per_hour
        self.max_per_session = max_per_session

    def evaluate(
        self,
        snapshot: SectorSnapshot,
        baseline: SectorSnapshot | None,
        state: AlertState,
        now: datetime,
    ) -> tuple[list[SectorAlert], list[str]]:
        """返回 (要推的告警, notes)。notes 里一定写清楚被预算截掉了多少条。

        `baseline` 是**当日第一条**截面，只用来算「较开盘」。「较上一轮」不从这里
        来——它在 `state.observed` 里，一个板块一条状态，不需要整段序列。
        这也是这个参数收成一条快照而不是一个 `history` 列表的原因：早先传整段，
        但用到的永远只有 `history[0]`，签名承诺的比实际用到的多一个量级。
        """
        notes: list[str] = []
        if not snapshot.tradable:
            # 排名本身已经被 `intraday.take_snapshot` 判为噪声，在噪声上做差分
            # 只会把噪声变成告警。
            return [], ["全场成交额未过闸，本轮不做板块告警"]
        if not snapshot.reads:
            return [], ["本轮没有任何板块读数"]

        candidates: list[SectorAlert] = []
        first = baseline

        for level in snapshot.levels():
            ranked = snapshot.ranked(level)
            ranks = {item.name: index for index, item in enumerate(ranked, start=1)}
            first_ranks = first.rank_map(level) if first is not None else {}
            first_reads = (
                {item.name: item for item in first.of_level(level)} if first is not None else {}
            )

            for read in ranked:
                rank = ranks[read.name]
                key = _state_key(read)
                previous = state.state_of(key)
                current = _derive_state(read, rank)
                state.set_state(key, current)
                if not previous:
                    continue  # 首见只立基线；开盘那条总览由 `_session_open` 负责
                candidates.extend(
                    self._sector_alerts(
                        read=read,
                        rank=rank,
                        previous=previous,
                        current=current,
                        first_rank=first_ranks.get(read.name),
                        first_read=first_reads.get(read.name),
                        now=now,
                    )
                )

        opener = self._session_open(snapshot, now)
        if opener is not None:
            candidates.insert(0, opener)

        emitted, dropped = self._apply_budget(candidates, state, now)
        notes.extend(dropped)
        return emitted, notes

    # ---- 单个板块的跃迁 ---------------------------------------------------

    def _sector_alerts(
        self,
        read: SectorRead,
        rank: int,
        previous: dict,
        current: dict,
        first_rank: int | None,
        first_read: SectorRead | None,
        now: datetime,
    ) -> list[SectorAlert]:
        alerts: list[SectorAlert] = []
        attention = rank <= self.attention_rank
        label = f"{LEVEL_LABEL.get(read.level, read.level)}·{read.name}"

        def make(trigger: str, headline: str, extra: list[str]) -> SectorAlert:
            policy = POLICIES[trigger]
            return SectorAlert(
                trigger=trigger,
                level=policy.level,
                headline=headline,
                detail=extra + _read_detail(read, rank, first_rank),
                evidence=tuple(extra),
                sector=read.name,
                sector_level=read.level,
                excess_pct=read.excess_pct,
                rank=rank,
                as_of=now,
                watch_hits=read.watch_hits,
            )

        # 1) 排名跃升——「资金正在往这里挪」
        if (
            attention
            and first_rank is not None
            and first_rank - rank >= RANK_SURGE_STEPS
        ):
            alerts.append(
                make(
                    "rank_surge",
                    f"{label} 排名跃升 {first_rank}→{rank}",
                    [f"开盘首轮排第 {first_rank}，现在第 {rank}，本层共 {read.total} 只成分股"],
                )
            )

        # 2) 转强——tone 的**变化**，不是 tone 本身
        if attention and current.get("tone") == "强" and previous.get("tone") != "强":
            alerts.append(
                make(
                    "tone_strong",
                    f"{label} 转强",
                    [
                        (
                            f"超额 {read.excess_pct:+.2f}% 且上涨占比 {read.advance_ratio:.0%}，"
                            f"由「{previous.get('tone') or '—'}」转「强」"
                        ),
                        "两个条件同时过闸才算强——只有涨幅可能是两只权重股拉的",
                    ],
                )
            )

        # 3) 涨停潮
        if (
            int(previous.get("limit_up") or 0) <= LIMIT_WAVE_BEFORE
            and read.limit_up >= LIMIT_WAVE_AFTER
        ):
            alerts.append(
                make(
                    "limit_up_wave",
                    f"{label} 涨停潮 {read.limit_up} 家",
                    [
                        (
                            f"涨停家数 {previous.get('limit_up')} → {read.limit_up}"
                            f"（有报价成分股 {read.covered} 只）"
                        )
                    ],
                )
            )

        # 4) 成交额虹吸
        if (
            first_read is not None
            and read.amount_share >= AMOUNT_SURGE_MIN_SHARE
            and first_read.amount_share > 0
            and read.amount_share >= first_read.amount_share * AMOUNT_SURGE_RATIO
        ):
            alerts.append(
                make(
                    "amount_surge",
                    f"{label} 成交额占比放大到 {read.amount_share:.1%}",
                    [
                        (
                            f"占全场成交额 {first_read.amount_share:.1%} → "
                            f"{read.amount_share:.1%}（{read.amount_yi:.0f} 亿）"
                        ),
                        "成交额是双边的，这说的是注意力在往这里集中，不是「净流入」",
                    ],
                )
            )

        # 5) 退潮——上面四条都在说去哪找机会，这条说手上的该不该走
        if (
            previous.get("tone") == "强"
            and current.get("tone") != "强"
            and int(previous.get("rank") or 999) <= FADE_RANK_LIMIT
        ):
            alerts.append(
                make(
                    "fade",
                    f"{label} 退潮：强 → {current.get('tone')}",
                    [
                        (
                            f"曾排本层第 {previous.get('rank')}，现第 {rank}；"
                            f"超额 {read.excess_pct:+.2f}%，上涨占比 {read.advance_ratio:.0%}"
                        ),
                        "板块层面的转弱通常早于个股跌破止损——这一条要求一个决定，不是通报",
                    ],
                )
            )

        # 6) 排名下滑（INFO，默认不推）
        if (
            first_rank is not None
            and rank - first_rank >= RANK_SURGE_STEPS
            and int(previous.get("rank") or 999) <= FADE_RANK_LIMIT
        ):
            alerts.append(
                make(
                    "rank_slide",
                    f"{label} 排名下滑 {first_rank}→{rank}",
                    [f"开盘首轮排第 {first_rank}，现在第 {rank}"],
                )
            )

        return alerts

    # ---- 开盘总览 ---------------------------------------------------------

    def _session_open(self, snapshot: SectorSnapshot, now: datetime) -> SectorAlert | None:
        detail: list[str] = []
        for level in snapshot.levels():
            top = snapshot.ranked(level)[:3]
            if not top:
                continue
            detail.append(
                f"{LEVEL_LABEL.get(level, level)}前三："
                + "、".join(
                    f"{item.name}（超额 {item.excess_pct:+.2f}%"
                    + (f"，涨停 {item.limit_up} 家" if item.limit_up else "")
                    + "）"
                    for item in top
                )
            )
        if not detail:
            return None
        market: list[str] = []
        if snapshot.market_median_pct is not None:
            market.append(f"全市场中位涨幅 {snapshot.market_median_pct:+.2f}%")
        if snapshot.breadth_advance is not None:
            market.append(f"上涨占比 {snapshot.breadth_advance:.0%}")
        market.append(f"成交额 {snapshot.market_amount_yi:.0f} 亿")
        detail.append("，".join(market))
        detail.append("排名按**超额涨幅**（板块中位 − 全市场中位），普涨日里绝对涨幅说明不了强弱")
        return SectorAlert(
            trigger="session_open",
            level=POLICIES["session_open"].level,
            headline="开盘板块概览",
            detail=detail,
            as_of=now,
        )

    # ---- 预算 -------------------------------------------------------------

    def _apply_budget(
        self, candidates: list[SectorAlert], state: AlertState, now: datetime
    ) -> tuple[list[SectorAlert], list[str]]:
        notes: list[str] = []
        allowed = [item for item in candidates if item.level in self.levels]
        level_filtered = len(candidates) - len(allowed)

        fresh: list[SectorAlert] = []
        for alert in sorted(allowed, key=_priority):
            policy = POLICIES.get(alert.trigger)
            if policy is None:
                continue
            key = _fire_key(alert)
            if policy.once_per_session and state.fire_count(key, alert.trigger) > 0:
                continue
            last = state.last_fired(key, alert.trigger)
            if last is not None and (now - last).total_seconds() < policy.cooldown_minutes * 60:
                continue
            fresh.append(alert)

        merged, folded = _merge_by_sector(fresh)

        ledger = _PushLedger(state)
        room, limit_note = self._room(ledger, now)
        if limit_note is not None and merged:
            notes.append(limit_note)
        if room <= 0:
            return [], notes

        emitted = merged[:room]

        # 折进正文的那些触发器也要记账：它们**确实发生了**，下一轮再单独报一遍
        # 不增加任何信息，只会让同一件事在通道里出现第二次。
        for alert in emitted:
            for trigger in folded.get(id(alert), (alert.trigger,)):
                state.mark_fired(_fire_key(alert), trigger, now)
        # 额度只扣**推送**条数，与上面记了几个触发器无关。
        ledger.record(now, len(emitted))

        dropped = len(merged) - len(emitted)
        if dropped:
            # **静默截断会让「只推了 3 条」读起来像「只发生了 3 件事」。**
            notes.append(
                f"本轮板块变化 {len(merged)} 条，按预算推 {len(emitted)} 条，"
                f"其余 {dropped} 条未推："
                + "、".join(f"{item.sector or '总览'}{item.label}" for item in merged[room:][:6])
            )
        if level_filtered:
            notes.append(f"另有 {level_filtered} 条低于推送级别，未计入")
        return emitted, notes

    def _room(self, ledger: _PushLedger, now: datetime) -> tuple[int, str | None]:
        """这一轮还能推几条，以及（如果被闸住了）是哪一层闸住的。

        三层取最小。**哪一层闸住必须说出来**——「今天没有板块告警」和「今天的
        板块告警被额度压掉了」是完全不同的两件事，而它们在通道里长得一模一样。
        """
        session_left = self.max_per_session - ledger.spent_today()
        hour_left = self.max_per_hour - ledger.spent_this_hour(now)
        room = min(self.max_per_pass, hour_left, session_left)
        if session_left <= 0:
            return 0, (
                f"板块告警已达当日上限 {self.max_per_session} 条，本轮变化不再推送"
                "（仍已落进当日板块序列，盘后可复盘）"
            )
        if hour_left <= 0:
            return 0, (
                f"板块告警已达本小时上限 {self.max_per_hour} 条，本轮变化留到下一小时"
                "（额度按小时分配，早盘的噪声吃不掉下午的份）"
            )
        return max(0, room), None


# ---- 内部 -----------------------------------------------------------------


def _merge_by_sector(
    alerts: list[SectorAlert],
) -> tuple[list[SectorAlert], dict[int, tuple[str, ...]]]:
    """一个板块一轮只发一条。返回 (合并后的告警, {id(告警): 折进去的触发器名})。

    实测同一次跃迁经常同时命中四条规则——有色金属从第 14 冲到第 1，会同时触发
    排名跃升、转强、涨停潮、成交额虹吸。它们**说的是同一件事的四个侧面**，而
    `_read_detail` 给每条都贴了一模一样的证据块。分四条推出去，等于让同一件事
    在通知栏里出现四次；四条一模一样的推送正是通道被静音的原因（同
    `monitor/triggers.py` 里 `stop_break` 用 once_per_session 的理由）。

    主条取优先级最高的那个，其余的标题折成正文里的一行。
    """
    groups: dict[str, list[SectorAlert]] = {}
    for alert in alerts:
        groups.setdefault(_fire_key(alert), []).append(alert)

    merged: list[SectorAlert] = []
    folded: dict[int, tuple[str, ...]] = {}
    for members in groups.values():
        primary = members[0]           # 调用方已按 `_priority` 排过序
        others = members[1:]
        if others:
            detail = [
                f"同时触发：{'、'.join(item.label for item in others)}",
                *[line for item in others for line in item.evidence],
                *primary.detail,
            ]
            primary = SectorAlert(
                trigger=primary.trigger,
                level=primary.level,
                headline=primary.headline,
                detail=detail,
                evidence=primary.evidence,
                sector=primary.sector,
                sector_level=primary.sector_level,
                excess_pct=primary.excess_pct,
                rank=primary.rank,
                as_of=primary.as_of,
                watch_hits=primary.watch_hits,
            )
        merged.append(primary)
        folded[id(primary)] = tuple(item.trigger for item in members)

    merged.sort(key=_priority)
    return merged, folded


def _state_key(read: SectorRead) -> str:
    return f"{_FIRE_KEY_PREFIX}:{read.level}:{read.name}"


def _fire_key(alert: SectorAlert) -> str:
    if not alert.sector:
        # 跨板块的总览没有板块名，用一个固定桶——它每天只发一条，不需要按板块分。
        return f"{_FIRE_KEY_PREFIX}:{_ALL_SECTORS}"
    return f"{_FIRE_KEY_PREFIX}:{alert.sector_level}:{alert.sector}"


def _derive_state(read: SectorRead, rank: int) -> dict:
    return {
        "rank": rank,
        "tone": read.tone,
        "excess": read.excess_pct,
        "limit_up": read.limit_up,
        "amount_share": read.amount_share,
    }


def _priority(alert: SectorAlert) -> tuple:
    """排序键：级别 → 是否命中你盯着的票 → 超额绝对值。

    命中持仓排在前面，是因为同样一条「退潮」，发生在你持仓所在的板块上和发生在
    一个你没碰过的板块上，紧急程度差一个量级。
    """
    level_order = {LEVEL_ACTION: 0, LEVEL_WARN: 1, LEVEL_INFO: 2}
    return (
        level_order.get(alert.level, 3),
        0 if alert.touches_watched else 1,
        -abs(alert.excess_pct or 0.0),
    )



def _read_detail(read: SectorRead, rank: int, first_rank: int | None) -> list[str]:
    """每条板块告警共用的证据行。"""
    lines = [
        f"本层第 {rank} 名"
        + (f"（开盘首轮第 {first_rank}）" if first_rank is not None else "")
        + f"　超额 {read.excess_pct:+.2f}%　中位 {read.median_pct:+.2f}%",
        (
            f"上涨占比 {read.advance_ratio:.0%}（{read.covered}/{read.total} 只有报价）　"
            f"涨停 {read.limit_up} 家　成交额 {read.amount_yi:.0f} 亿"
            f"（占全场 {read.amount_share:.1%}）"
        ),
    ]
    if read.leaders:
        lines.append("领涨：" + read.leaders_text())
    if read.watch_hits:
        # 「有色金属排名 12→3」本身没法执行。这一行把板块读数接回你手上的票。
        lines.append(f"⚑ 你盯着的票在这个板块里：{read.watch_text()}")
    return lines
