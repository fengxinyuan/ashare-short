"""按行业与概念聚合全市场实时行情，生成盘中板块截面。

以板块涨幅中位数减全场中位数计算超额，行业和概念分别排名。
上涨占比只统计有报价成分股；概念重叠，amount_share 之和不保证为 1。
成交额为双边交易量，不解释为净流入。未达总成交额门槛时标记 tradable=False。
截面追加到 JSONL，供板块跃迁告警读取。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from ashare_decision_support.numeric import finite
from ashare_decision_support.schedule.timezone import (
    beijing_naive_now,
    beijing_today,
    to_beijing_naive,
)
from ashare_decision_support.sectors.base import (
    LEVEL_CONCEPT,
    LEVEL_INDUSTRY,
    SectorDefinition,
    SectorTaxonomyError,
)
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.state.store import append_jsonl, file_lock, read_jsonl

LEVEL_LABEL = {LEVEL_INDUSTRY: "行业", LEVEL_CONCEPT: "概念"}

# 一个板块至少要有这么多只**有报价**的成分股才给读数。少于这个数，中位数由个别
# 票决定，排名翻上翻下全是噪声。分类层已经按成分股总数过了一道闸（行业 5、
# 概念 15），这一道管的是当轮实际取到报价的只数。
MIN_COVERED = 5

# 全板块合计成交额闸门（亿）。开盘头几分钟总量太小，横截面排名没有意义。
MIN_TOTAL_AMOUNT_YI = 50.0

# 领涨成分股取几只。带上它们是因为「有色金属排名 12→3」本身没法执行——
# 要知道是谁在拉，而这份数据同一次扫描里已经有了，不额外取数。
LEADER_COUNT = 3

# 涨停判定的容差（百分点），覆盖四舍五入与最后一笔的微小偏差。
LIMIT_TOLERANCE = 0.5


@dataclass(frozen=True)
class SectorRead:
    """一个板块在某一轮的截面读数。"""

    name: str
    level: str
    median_pct: float
    excess_pct: float
    advance_ratio: float
    amount_yi: float
    amount_share: float
    covered: int
    total: int
    limit_up: int = 0
    # (名称, 涨跌幅)，按涨跌幅降序。
    leaders: tuple[tuple[str, float], ...] = ()
    # 这个板块里你自己盯着的票（持仓，或本轮显式传进来的那几只），(代码, 名称)。

    # **在归并时算，不在告警时算。** 告警层拿到的是 `SectorRead`，里面没有成分股
    # 名单；要在那里做交集就得把 1215 只的成分表一路带过去，落盘也会跟着涨一个
    # 量级。这里只留命中的那几只，绝大多数板块是空的。
    watch_hits: tuple[tuple[str, str], ...] = ()

    @property
    def tone(self) -> str:
        """强 / 中 / 弱。**判据是超额，不是涨幅**——理由见模块开头第 1 条。"""
        if self.excess_pct >= 1.0 and self.advance_ratio >= 0.7:
            return "强"
        if self.excess_pct <= -1.0 and self.advance_ratio <= 0.3:
            return "弱"
        return "中"

    def leaders_text(self) -> str:
        return "、".join(f"{name} {pct:+.1f}%" for name, pct in self.leaders)

    def watch_text(self) -> str:
        return "、".join(f"{name}({code})" for code, name in self.watch_hits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "level": self.level,
            "median_pct": self.median_pct,
            "excess_pct": self.excess_pct,
            "advance_ratio": self.advance_ratio,
            "amount_yi": self.amount_yi,
            "amount_share": self.amount_share,
            "covered": self.covered,
            "total": self.total,
            "limit_up": self.limit_up,
            "leaders": [list(item) for item in self.leaders],
            "watch_hits": [list(item) for item in self.watch_hits],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SectorRead | None:
        """读回一行历史。**缺字段返回 None 而不是补默认值**——补出来的 0 会被
        当成真读数参与「较上轮」的差分，凭空造出一次跃迁。

        NaN 走同一条退路，理由也是同一条：`json.loads` 认 `NaN` 字面量，收进来
        的不是「读数是 NaN」而是「这行坏了」。它和 0 的区别在于连报错都不会——
        NaN 参与差分得到 NaN，比较永远是 False，于是这个板块**再也不会跃迁**。
        """
        readings = {
            key: finite(payload.get(key))
            for key in ("median_pct", "excess_pct", "advance_ratio", "amount_yi")
        }
        if any(value is None for value in readings.values()):
            return None
        try:
            return cls(
                name=str(payload["name"]),
                level=str(payload.get("level") or LEVEL_INDUSTRY),
                **readings,  # type: ignore[arg-type]
                amount_share=finite(payload.get("amount_share")) or 0.0,
                covered=int(payload["covered"]),
                total=int(payload["total"]),
                limit_up=int(payload.get("limit_up") or 0),
                leaders=tuple(
                    (str(item[0]), value)
                    for item in payload.get("leaders") or []
                    if isinstance(item, (list, tuple))
                    and len(item) >= 2
                    and (value := finite(item[1])) is not None
                ),
                watch_hits=tuple(
                    (str(item[0]), str(item[1]))
                    for item in payload.get("watch_hits") or []
                    if isinstance(item, (list, tuple)) and len(item) >= 2
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class SectorSnapshot:
    """一轮全市场归并的结果。`tradable=False` 时排名不该被解读。"""

    as_of: datetime
    reads: list[SectorRead] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    breadth_advance: float | None = None
    market_median_pct: float | None = None
    market_amount_yi: float = 0.0
    tradable: bool = True

    def of_level(self, level: str) -> list[SectorRead]:
        return [item for item in self.reads if item.level == level]

    def ranked(self, level: str | None = None) -> list[SectorRead]:
        """按超额降序。**同层内排名**——跨层比名次没有含义。"""
        rows = self.reads if level is None else self.of_level(level)
        return sorted(rows, key=lambda item: item.excess_pct, reverse=True)

    def rank_map(self, level: str) -> dict[str, int]:
        return {item.name: index for index, item in enumerate(self.ranked(level), start=1)}

    def levels(self) -> list[str]:
        """本轮有读数的层级。**行业在前、概念在后**，不按字母序。

        先展示行业定位，再展示可跨行业的题材，避免数量更多的概念占据简报开头。
        """
        present = {item.level for item in self.reads}
        ordered = [level for level in (LEVEL_INDUSTRY, LEVEL_CONCEPT) if level in present]
        return ordered + sorted(present - set(ordered))

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(timespec="seconds"),
            "breadth_advance": self.breadth_advance,
            "market_median_pct": self.market_median_pct,
            "market_amount_yi": self.market_amount_yi,
            "tradable": self.tradable,
            "reads": [item.to_dict() for item in self.reads],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SectorSnapshot | None:
        raw = str(payload.get("as_of") or "")
        try:
            moment = to_beijing_naive(datetime.fromisoformat(raw))
        except ValueError:
            return None
        raw_reads = payload.get("reads")
        reads = (
            [
                item
                for item in (
                    SectorRead.from_dict(row) for row in raw_reads if isinstance(row, dict)
                )
                if item is not None
            ]
            if isinstance(raw_reads, list)
            else []
        )
        return cls(
            as_of=moment,
            reads=reads,
            notes=list(payload["notes"]) if isinstance(payload.get("notes"), list) else [],
            breadth_advance=finite(payload.get("breadth_advance")),
            market_median_pct=finite(payload.get("market_median_pct")),
            market_amount_yi=finite(payload.get("market_amount_yi")) or 0.0,
            tradable=bool(payload.get("tradable", True)),
        )


# 归并


def measure(
    rows: dict[str, Any],
    definitions: list[SectorDefinition],
    market_median: float,
    total_amount_yi: float,
    min_covered: int = MIN_COVERED,
    watched_symbols: set[str] | None = None,
) -> tuple[list[SectorRead], int]:
    """行情行 × 板块定义 → 截面读数。纯计算，不碰网络。

    `rows` 是 `MarketScan.rows`，键是**裸 6 位代码**；板块定义里是 `NNNNNN.XX`。
    在这里对齐一次，而不是在归并循环里每次 split ——那是 5 万次字符串切分。

    `watched_symbols` 是裸代码集合（持仓，或本轮显式传进来的那几只），命中的记进
    `watch_hits`。
    """
    reads: list[SectorRead] = []
    watched = watched_symbols or set()
    thin = 0

    for definition in definitions:
        pcts: list[float] = []
        scored: list[tuple[str, float]] = []
        hits: list[tuple[str, str]] = []
        amount = 0.0
        advances = 0
        limit_up = 0

        for symbol in definition.symbols:
            code = symbol.partition(".")[0]
            row = rows.get(code)
            if row is None:
                continue
            pct = finite(getattr(row, "change_pct", None))
            if pct is None:
                continue  # 停牌/无报价：不计入分母，而不是当成 0% 拉平中位数
            name = str(getattr(row, "name", "") or code)
            pcts.append(pct)
            scored.append((name, pct))
            if code in watched:
                hits.append((code, name))
            if pct > 0:
                advances += 1
            if _at_limit_up(pct, code, bool(getattr(row, "is_st", False))):
                limit_up += 1
            amt = finite(getattr(row, "amount_yi", None))
            if amt is not None:
                amount += amt

        if len(pcts) < min_covered:
            thin += 1
            continue

        scored.sort(key=lambda pair: pair[1], reverse=True)
        centre = median(pcts)
        reads.append(
            SectorRead(
                name=definition.name,
                level=definition.level,
                median_pct=round(centre, 3),
                excess_pct=round(centre - market_median, 3),
                advance_ratio=round(advances / len(pcts), 4),
                amount_yi=round(amount, 2),
                amount_share=round(amount / total_amount_yi, 5) if total_amount_yi > 0 else 0.0,
                covered=len(pcts),
                total=definition.size,
                limit_up=limit_up,
                leaders=tuple((name, round(pct, 2)) for name, pct in scored[:LEADER_COUNT]),
                watch_hits=tuple(sorted(hits)),
            )
        )

    return reads, thin


def _at_limit_up(pct: float, code: str, is_st: bool) -> bool:
    """涨停判定。**是一条带子，不是一个下限。**

    上市首日的新股没有涨跌幅限制：2026-08-09 实测 `N展芯 +396.9%`。用「≥ 涨停价」
    判定会把它算成涨停，于是「国防军工涨停 1 家」这条读数里那一家其实是个新股，
    而涨停家数正是情绪判断里最不该掺假的那个计数。封在板上的票是**贴着**涨停价的，
    所以两边都要卡。
    """
    cap = _limit_cap(code, is_st)
    return cap - LIMIT_TOLERANCE <= pct <= cap + LIMIT_TOLERANCE


def _limit_cap(code: str, is_st: bool) -> float:
    """这只票今天的涨停幅度，**只用来数涨停家数**，不参与任何价格计算。

    北交所 30%、创业板与科创板 20%、其余 10%；ST 只在 10cm 的板上降到 5%——
    20cm 的板对 ST 不另设更低的限制，在那里判 5% 会把一批 +6% 的票错数成涨停。
    """
    if code.startswith(("92", "83", "87", "88", "43")):
        return 30.0
    if code[:3] in ("300", "301", "688", "689"):
        return 20.0
    return 5.0 if is_st else 10.0


def take_snapshot(
    taxonomies: dict[str, Any],
    adapter: Any | None = None,
    now: datetime | None = None,
    min_covered: int = MIN_COVERED,
    min_total_amount_yi: float = MIN_TOTAL_AMOUNT_YI,
    watched_symbols: set[str] | None = None,
) -> SectorSnapshot:
    """扫一次全市场，按每个分类各归并一遍。

    `taxonomies` 的键为 `LEVEL_INDUSTRY` 与 `LEVEL_CONCEPT`。
    分类对象自带缓存，所以多轮之间只有第一次会读文件。

    **一个分类挂了不影响另一个。** 概念文件不可用时行业排名照出，反之亦然——
    把两者绑成一次成败，等于让次要的那个否决主要的。同 `adapters/ths_taxonomy.py`。
    """
    moment = now or beijing_naive_now()
    scan = (adapter or _default_adapter()).fetch()
    rows = scan.rows
    notes = list(scan.notes or [])

    quoted = [
        value
        for value in (finite(getattr(row, "change_pct", None)) for row in rows.values())
        if value is not None
    ]
    if not quoted:
        return SectorSnapshot(
            as_of=moment,
            notes=[*notes, "全市场扫描没有任何有效报价，本轮无板块读数"],
            tradable=False,
        )

    market_median = median(quoted)
    breadth = round(sum(1 for value in quoted if value > 0) / len(quoted), 4)
    total_amount = sum(
        value
        for value in (finite(getattr(row, "amount_yi", None)) for row in rows.values())
        if value is not None
    )

    reads: list[SectorRead] = []
    for level, taxonomy in taxonomies.items():
        try:
            definitions = taxonomy.list_sectors()
        except SectorTaxonomyError as exc:
            notes.append(f"{level} 分类不可用，本轮没有这一层的板块读数：{exc}")
            continue
        except Exception as exc:  # 分类源的任何意外都不该中断这一轮
            notes.append(f"{level} 分类读取异常（{type(exc).__name__}），本轮跳过这一层")
            continue
        level_reads, thin = measure(
            rows,
            definitions,
            market_median,
            total_amount,
            min_covered=min_covered,
            watched_symbols=watched_symbols,
        )
        reads.extend(level_reads)
        if thin:
            notes.append(f"{level}：{thin} 个板块有报价成分股不足 {min_covered} 只，本轮不给读数")

    tradable = total_amount >= min_total_amount_yi
    if not tradable:
        notes.append(
            f"全场成交额仅 {total_amount:.0f} 亿，低于 {min_total_amount_yi:.0f} 亿闸门，"
            "本轮排名视为噪声，仅记录不解读"
        )

    return SectorSnapshot(
        as_of=moment,
        reads=reads,
        notes=notes,
        breadth_advance=breadth,
        market_median_pct=round(market_median, 3),
        market_amount_yi=round(total_amount, 1),
        tradable=tradable,
    )


def _default_adapter() -> Any:
    # 局部导入：这一层的纯计算部分（`measure`）不该因为拉不起网络适配器就 import 失败。
    from ashare_decision_support.adapters.market_scan import MarketScanAdapter

    return MarketScanAdapter()


def default_taxonomies(source: Any | None = None) -> dict[str, Any]:
    """盘中与盘前共用同一行业、概念分类工厂。"""
    from ashare_decision_support.sectors.ths_taxonomy import static_taxonomies

    industry, concept = static_taxonomies(source)
    return {LEVEL_INDUSTRY: industry, LEVEL_CONCEPT: concept}


# 序列落盘


def store_path(trade_date: str, root: Path | None = None) -> Path:
    name = f"intraday_sector_{str(trade_date).replace('-', '')}.jsonl"
    if root is not None:
        base = Path(root)
        base.mkdir(parents=True, exist_ok=True)
        return base / name
    return state_path("market", name)


# 板块序列保留多少天。实测一天 30 轮 × 377 个板块 ≈ 3.1MB，是状态目录里最大的
# 一类文件（其余日更文件都在几十 KB），一年不清就是 750MB。

# 取 30 天，因为这份数据存在的理由是让 `review` 有朝一日能回答「盘中排名跃升的
# 板块选出来的票兑现率高多少」——那需要一个月量级的样本，不需要一年。
HISTORY_KEEP_DAYS = 30


def prune_history(
    keep_days: int = HISTORY_KEEP_DAYS,
    root: Path | None = None,
    protected_dates: set[str] | None = None,
) -> list[str]:
    """删掉过期的板块序列文件，返回删掉的文件名。

    **按文件名里的日期删，不按 mtime。** mtime 会被备份、rsync、容器镜像重建
    改掉，于是「保留 30 天」在换一次部署方式之后可能变成删光或者一个不删。
    文件名里的那个日期是这份数据自己说的它是哪天的。
    """
    base = Path(root) if root is not None else state_path("market", "x").parent
    if not base.is_dir():
        return []
    cutoff = beijing_today() - timedelta(days=max(0, keep_days))
    protected = {str(item).replace("-", "") for item in protected_dates or set()}
    removed: list[str] = []
    for path in base.glob("intraday_sector_*.jsonl"):
        stamp = path.stem.rsplit("_", 1)[-1]
        if stamp in protected:
            continue
        try:
            when = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
        except (ValueError, IndexError):
            continue  # 名字对不上格式的不碰——宁可留着也不误删
        if when < cutoff:
            try:
                path.unlink()
            except OSError:
                continue  # 删不掉只是没省下空间，不是错误
            removed.append(path.name)
    return removed


def append(snapshot: SectorSnapshot, path: Path) -> None:
    """在锁内追加，公共写入器隔离残行并在失败时回滚本次追加。"""
    with file_lock(path):
        append_jsonl(path, snapshot.to_dict())


def load_history(path: Path, notes: list[str] | None = None) -> list[SectorSnapshot]:
    """读回当日全部快照，按时间升序。

    半行（进程被杀在写入中途）跳过而不是抛错——一次崩溃不该让当天剩下的
    序列全部读不出来。

    **告警层不用这个，用 `load_first`。** 那边只要基线一条，而整天下来这里是
    二三十条 × 377 个板块读数。
    """
    out: list[SectorSnapshot] = []
    for row in read_jsonl(path, notes):
        snapshot = SectorSnapshot.from_dict(row)
        if snapshot is not None:
            out.append(snapshot)
        elif notes is not None:
            notes.append(f"{path.name} 有无法解析的板块截面，已跳过")
    out.sort(key=lambda item: item.as_of)
    return out


def load_first(path: Path) -> SectorSnapshot | None:
    """当日第一条快照——「较开盘」的基线。

    **单独一个函数而不是 `load_history(path)[0]`。** 板块会话每轮都要它，而整天
    下来序列有二三十条、每条 377 个板块读数；整份解析一遍只为了取第一条，成本
    随当天已跑的轮数线性涨，到收盘时是开盘时的三十倍。这里读到第一条能解析的
    就停。

    **从文件读而不是记在内存里**，因为盘中重启是常事：13:00 重启的进程要接着和
    09:35 的基线比，不是和 13:00 比——否则「较开盘」在重启后悄悄变成「较重启」。
    """
    if not path.exists():
        return None
    with path.open("rb") as handle:
        for line in handle:
            snapshot = _parse_line(line)
            if snapshot is not None:
                return snapshot
    return None


def _parse_line(line: str | bytes) -> SectorSnapshot | None:
    text = line.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return SectorSnapshot.from_dict(payload) if isinstance(payload, dict) else None
