"""同花顺行业指数：指数行情与成分股宽度分别呈现。

直接读取 d.10jqka.com.cn 的日线，不占用 TickFlow 配额。指数涨跌、日内收位和
量比用于观察板块趋势；成分股中位涨跌、上涨占比、涨停家数仍由成分股行情计算。
两者的统计方法不同，不能互相替代。

行业名称与静态分类对齐，但静态成分来自第三方快照，不能据此认定官方成分完全
相同。last.js 提供当年日线，历史序列在缓存中累积；这些读数不代表分钟级行情。
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from statistics import fmean
from typing import Any

from ashare_decision_support.adapters.http import BROWSER_USER_AGENT, fetch_text, jsonp_payload
from ashare_decision_support.fanout import gather
from ashare_decision_support.jsoncache import (
    merge_series,
    read_series,
    read_stamped,
    series_bars,
    write_stamped,
)
from ashare_decision_support.numeric import finite
from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.state.paths import state_home

BASE_URL = "https://d.10jqka.com.cn/v6/line/bk_{code}/01/last.js"
# 按年的那一路。**与 `last.js` 逐字同格式**（11 列、含成交额），只是 payload 里
# 没有 `name` 字段——见 `backfill` 的 docstring，那里写了为什么走这条路而不是
# 一次就能回全部 4600 根的 `all.js`。
YEAR_URL = "https://d.10jqka.com.cn/v6/line/bk_{code}/01/{year}.js"
REFERER = "https://q.10jqka.com.cn/"

# 同花顺板块指数最早一根。2026-08-20 实测 881101（种植业与林业）起于 20070801，
# `all.js` 自报 `start=20070801`、`total=4600`。回填从这一年开始扫，早于它的年份
# 返回空，多打的那几次是一次性成本。
BACKFILL_START_YEAR = 2007

# 同花顺行业板块代码区间。2026-08-02 实测 881101..881419 里有 90 个有效板块，
# 其余返回空。扫一遍就把有效码缓存下来，之后只打这 90 个。
CODE_START = 881101
CODE_END = 881419

CACHE_NAME = "ths_sector_codes.json"
CACHE_SCHEMA = "ths-bk-v1"

# 日线序列的落盘位置与格式。

# **取数已经把整条序列拿回来了，这里只是别再扔掉它。** `last.js` 一次回当年全部
# 日线（2026 年 140 根），而 `_measure` 只留最新一根——板块轮动、跷跷板、相对强弱
# 排名要的历史因此每天被丢一次，尽管它一分钱额外成本都没有。

# 落在状态目录而不是 `--output-dir`：后者常按日期命名，那样序列会散成一天一份、
# 永远拼不起来，而这份数据的全部价值就在于跨日累积。同 `jobs/morning.py` 里
# 语料库那段的理由。
HISTORY_DIR_NAME = "sector_index"
HISTORY_SCHEMA = "ths-bk-series-v1"

TIMEOUT_SECONDS = 12
MAX_WORKERS = 16

# 窗口。20 根用于「距 20 日高点」与量比基线，5 根用于近周成交额。
HIGH_WINDOW = 20
VOLUME_WINDOW = 5
MIN_BARS = 25

# 收盘位置的高低分界（收盘落在当日区间的百分位）。收在上沿 = 尾盘有承接，
# 收在下沿 = 卖压在兑现。`shape` 与 `describe()` 两处都要用，写死两遍必然漂。
HIGH_CLOSE = 80.0
LOW_CLOSE = 20.0


def _yi(amount: float | None) -> str:
    """成交额 → 「日均 N 亿」。取不到就明说，不印一个 0 亿冒充读数。"""
    return "日均额缺失" if amount is None else f"日均 {amount / 1e8:.0f} 亿"


@dataclass(frozen=True)
class SectorIndexReading:
    """一个行业指数的当日读数。"""

    code: str
    name: str
    trade_date: str
    close: float
    ret_1d: float | None = None
    ret_5d: float | None = None
    ret_20d: float | None = None
    # 收盘落在当日区间的分位，0 = 收在最低，100 = 收在最高。
    # 「涨了多少」和「谁在被卖」是两个问题，这个字段答第二个。
    close_position: float | None = None
    off_20d_high: float | None = None
    # 近 5 日均额 / 近 20 日均额。>1 放量，<1 缩量。
    volume_ratio: float | None = None
    amount_5d: float | None = None

    @property
    def shape(self) -> str:
        """一句话形态。只用于措辞，不参与任何打分。"""
        if self.close_position is None or self.ret_1d is None:
            return "数据不足"
        if self.ret_1d > 0 and self.close_position <= LOW_CLOSE:
            return "涨但收在日内低位（冲高回落，卖压在兑现）"
        if self.ret_1d > 0 and self.close_position >= HIGH_CLOSE:
            return "涨且收在日内高位（有承接）"
        if self.ret_1d < 0 and self.close_position >= HIGH_CLOSE:
            return "跌但收在日内高位（尾盘有买盘）"
        if self.ret_1d < 0 and self.close_position <= LOW_CLOSE:
            return "跌且收在日内低位（无承接）"
        return "日内震荡"


@dataclass
class BackfillResult:
    """一次历史回填的战果。产物是序列文件，这里只交代覆盖到了什么。"""

    codes: int = 0
    years: tuple[int, ...] = ()
    bars_after: int = 0
    earliest: str | None = None
    latest: str | None = None
    failures: int = 0
    notes: list[str] = field(default_factory=list)
    write_failures: int = 0

    @property
    def ok(self) -> bool:
        return self.bars_after > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "codes": self.codes,
            "years": list(self.years),
            "bars_after": self.bars_after,
            "earliest": self.earliest,
            "latest": self.latest,
            "failures": self.failures,
            "write_failures": self.write_failures,
            "notes": list(self.notes),
        }


@dataclass
class SectorIndexSnapshot:
    readings: list[SectorIndexReading] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ok: bool = True

    def __len__(self) -> int:
        return len(self.readings)

    def top(self, key: str, n: int = 10, reverse: bool = True) -> list[SectorIndexReading]:
        scored = [r for r in self.readings if getattr(r, key) is not None]
        return sorted(scored, key=lambda r: getattr(r, key), reverse=reverse)[:n]

    def describe(self, n: int = 5) -> list[str]:
        """给简报用的几行摘要。"""
        if not self.readings:
            return []
        lines: list[str] = []
        gainers = self.top("ret_1d", n)
        losers = self.top("ret_1d", n, reverse=False)
        if gainers:
            lines.append(
                "同花顺行业涨幅前列：" + "、".join(f"{r.name} {r.ret_1d:+.2%}" for r in gainers)
            )
        if losers:
            lines.append(
                "同花顺行业跌幅前列：" + "、".join(f"{r.name} {r.ret_1d:+.2%}" for r in losers)
            )
        # 「涨了但收在最低」是这一层独有的读数，单独点名。
        lines.extend(self._close_position_lines(n))
        lines.extend(self._volume_position_lines(n))
        return lines

    # describe 的两条独有读数

    def _close_position_lines(self, n: int) -> list[str]:
        """收盘位置的横截面分布。

        **原来只在出现冲高回落时才有一行**，于是最强的那种收盘反而是沉默的：
        2026-08-07 实测 90 个板块涨了 78 个、冲高回落 **0 个**，简报上一个字都没有。
        沉默在这里不是中性的——读的人会把「没写」读成「没测出什么」，而当天的事实
        是全场尾盘承接极强，这正是本层 docstring 说它独有、别处拿不到的那个读数。

        所以分布**总是**报：分母（有收位读数的板块数）跟着出，读的人才能自己判断
        「0 个冲高回落」是全场强，还是当天压根没几个板块在涨。
        """
        scored = [r for r in self.readings if r.close_position is not None]
        if not scored:
            return []
        risers = [r for r in scored if r.ret_1d is not None and r.ret_1d > 0]
        held = [r for r in scored if (r.close_position or 0.0) >= HIGH_CLOSE]
        faded = sorted(
            (r for r in risers if (r.close_position or 0.0) <= LOW_CLOSE),
            key=lambda r: r.close_position or 0.0,
        )
        line = (
            f"收盘位置：{len(held)}/{len(scored)} 个板块收在日内高位"
            f"（收位 ≥{HIGH_CLOSE:.0f}%）；当日上涨的 {len(risers)} 个里"
            f"有 {len(faded)} 个收在日内低位"
        )
        if faded:
            line += "（冲高回落，卖压在兑现）：" + "、".join(
                f"{r.name}（{r.ret_1d:+.2%}，收位 {r.close_position:.0f}%）" for r in faded[:n]
            )
        elif risers:
            line += "——**没有一个冲高回落**，尾盘承接强。"
        else:
            line += "。"
        return [line]

    def _volume_position_lines(self, n: int) -> list[str]:
        """上涨板块按量能分组，展示近 5 日 / 20 日均额比及绝对成交额。

        比值和金额并列，避免将小板块的高量比误读为更大的交易规模。
        """
        risers = [
            r
            for r in self.readings
            if r.ret_1d is not None and r.ret_1d > 0 and r.volume_ratio is not None
        ]
        if not risers:
            return []
        expanding = sorted(risers, key=lambda r: -(r.volume_ratio or 0.0))
        shrinking = [r for r in risers if (r.volume_ratio or 0.0) < 1.0]
        lines = [
            f"上涨板块的量能位置（近 5 日均额 / 近 20 日均额，括号内为日均额）：放量 "
            f"{len(risers) - len(shrinking)}/{len(risers)} 个，"
            + "、".join(
                f"{r.name} {r.volume_ratio:.2f}（{_yi(r.amount_5d)}）"
                for r in expanding[:n]
                if (r.volume_ratio or 0.0) >= 1.0
            )
        ]
        if shrinking:
            shrinking.sort(key=lambda r: r.volume_ratio or 0.0)
            lines.append(
                "⚠️ 涨但缩量（涨幅在榜、量能没跟上，更像空头回补而不是新钱进场）："
                + "、".join(
                    f"{r.name}（{r.ret_1d:+.2%}，量能 {r.volume_ratio:.2f}，{_yi(r.amount_5d)}）"
                    for r in shrinking[:n]
                )
            )
        return lines


class ThsSectorIndexAdapter:
    """同花顺行业指数日线。无限流，不占 TickFlow 配额。"""

    name = "ths-sector-index"

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        opener: Callable[..., Any] | None = None,
        timeout: int = TIMEOUT_SECONDS,
        max_workers: int = MAX_WORKERS,
        history_dir: str | Path | None = None,
        persist_history: bool = True,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.max_workers = max_workers
        self._history_dir = Path(history_dir) if history_dir else None
        self.persist_history = persist_history
        self.history_written = 0

    # public

    def fetch(self, codes: Iterable[str] | None = None) -> SectorIndexSnapshot:
        self.history_written = 0
        notes: list[str] = []
        wanted = list(dict.fromkeys(codes if codes is not None else self._codes(notes)))
        if not wanted:
            return SectorIndexSnapshot(notes=[*notes, "同花顺行业指数：没有可用板块代码"], ok=False)

        parsed: list[tuple[str, str, list[dict]]] = []
        done, errors = gather(wanted, self._fetch_one, max_workers=self.max_workers)
        failures = len(errors)
        for code, result in done:
            if result is None:
                continue
            name, bars = result
            if name and bars:
                parsed.append((code, name, bars))

        readings = [r for r in (_measure(c, n, b) for c, n, b in parsed) if r is not None]
        # 落盘在 `_measure` 之后、闸门之前：即使某个板块的最新交易日对不齐、
        # 当日读数被排除，它的历史序列仍然是好的，没有理由跟着丢。
        written = self._persist_history(parsed)
        if self.persist_history and written < len(parsed):
            notes.append(
                f"同花顺行业历史 {len(parsed) - written}/{len(parsed)} 个板块保存失败，"
                "当日读数仍可用，原有历史保留"
            )
        if failures:
            kinds = "、".join(sorted({type(exc).__name__ for _, exc in errors}))
            notes.append(
                f"同花顺行业指数 {failures}/{len(wanted)} 个板块取数失败（{kinds}），其余照常"
            )
        if not readings:
            return SectorIndexSnapshot(notes=[*notes, "同花顺行业指数全部取数失败"], ok=False)

        # 各板块最新交易日必须一致，否则是在拿不同天的读数排名次。
        stamps = {r.trade_date for r in readings}
        if len(stamps) > 1:
            latest = max(stamps)
            dropped = [r for r in readings if r.trade_date != latest]
            readings = [r for r in readings if r.trade_date == latest]
            notes.append(
                f"同花顺行业指数有 {len(dropped)} 个板块最新交易日不是 {latest}，已排除"
                "（不同交易日的读数不可比）"
            )

        notes.append(f"同花顺行业指数 {len(readings)} 个板块，最新交易日 {readings[0].trade_date}")
        return SectorIndexSnapshot(readings=readings, notes=notes, ok=True)

    def backfill(
        self,
        codes: Iterable[str] | None = None,
        since_year: int = BACKFILL_START_YEAR,
        until_year: int | None = None,
    ) -> BackfillResult:
        """按年把整段历史并进序列文件。**这是基准率层样本量的唯一来源。**

        日常跑批走 `last.js`，它只回**最近 140 根**（实测 2026-01-21..08-19）。
        140 根算得了 20 日窗口，算不了「过去费半跌 2% 之后电子板块怎么走」——
        那种条件一年只成立十几次，样本量得按年攒。

        **为什么不用 `all.js`。** 它一次就回全部 4600 根（2007 年起），但那是
        另一种编码：价格按 `priceFactor=1000` 缩放并做了增量存储，而且
        **没有成交额那一列**。成交额是 `sectors/turnover.py` 与量比读数的输入，
        缺了它这段历史只有一半能用；至于自己解那段增量编码——解错一位不会报错，
        只会让 18 年的价格静默偏掉，那正是这套系统最怕的一类错。

        按年的 `{year}.js` 与 `last.js` **逐字同格式**（11 列、含成交额），
        于是这条路复用同一个解析器（`_parse_bars`），多付的只是请求数：
        90 个板块 × 20 年 = 1800 次。同花顺不限流，而且这是一次性成本——
        回填过一次之后，日常那条 `last.js` 每天自动往上并。

        失败按 (板块, 年) 计。**某一年取不到不影响别的年份落盘**：序列本来就允许
        有洞，而「2015 年缺失」和「整个板块没有历史」在下游是两件事——前者由
        基准率层的样本量报出来，后者是取数彻底失败。
        """
        wanted = list(dict.fromkeys(codes if codes is not None else self._codes([])))
        if not wanted:
            return BackfillResult(notes=["同花顺行业指数：没有可用板块代码，无法回填"])

        last_year = until_year if until_year is not None else beijing_today().year
        years = tuple(range(max(since_year, BACKFILL_START_YEAR), last_year + 1))
        if not years:
            return BackfillResult(notes=[f"回填年份区间为空（{since_year}..{last_year}）"])

        jobs = [(code, year) for code in wanted for year in years]
        done, errors = gather(jobs, self._fetch_year, max_workers=self.max_workers)

        by_code: dict[str, list[dict]] = {code: [] for code in wanted}
        for (code, _year), bars in done:
            if bars:
                by_code[code].extend(bars)

        root = self.history_dir()
        written = 0
        write_failed: list[str] = []
        for code, bars in by_code.items():
            if not bars:
                continue
            # 年线没有板块名；在更新锁内读取最新名称，避免覆盖同时完成的日常刷新。
            if _write_series(root, code, None, bars):
                written += 1
            else:
                write_failed.append(code)

        bars_after, earliest, latest = _history_coverage(root, wanted)
        result = BackfillResult(
            codes=written,
            years=years,
            bars_after=bars_after,
            earliest=earliest,
            latest=latest,
            failures=len(errors),
            write_failures=len(write_failed),
        )
        result.notes.append(
            f"回填 {written}/{len(wanted)} 个板块，{years[0]}..{years[-1]} 共 {len(jobs)} 次取数，"
            f"落盘后合计 {result.bars_after} 根（{result.earliest}..{result.latest}）"
        )
        if write_failed:
            result.notes.append(
                f"板块历史保存失败：{'、'.join(write_failed)}；原有历史保留，可重试"
            )
        if errors:
            kinds = "、".join(sorted({type(exc).__name__ for _, exc in errors})[:4])
            result.notes.append(
                f"{len(errors)}/{len(jobs)} 次按年取数失败（{kinds}）——"
                "序列允许有洞，缺哪几年由基准率层的样本量报出来"
            )
        return result

    def _codes(self, notes: list[str]) -> list[str]:
        cached = self._read_cache()
        if cached:
            return cached
        discovered = self._discover(notes)
        if discovered:
            self._write_cache(discovered)
        return discovered

    def _discover(self, notes: list[str]) -> list[str]:
        """扫一遍代码区间，留下有数据的。只在没有缓存时发生。"""
        candidates = [str(c) for c in range(CODE_START, CODE_END + 1)]
        done, _errors = gather(candidates, self._fetch_one, max_workers=self.max_workers)
        # 探测阶段没有「失败」这回事：这个区间里本来就大部分代码不存在，取不到
        # 就是不存在。所以只看取到了什么，不记 note。
        alive = [code for code, result in done if result is not None and result[0] and result[1]]
        notes.append(
            f"同花顺行业代码首次探测：{CODE_START}..{CODE_END} 共 {len(candidates)} 个，"
            f"{len(alive)} 个有数据，已缓存"
        )
        return alive

    def _fetch_one(self, code: str) -> tuple[str, list[dict]] | None:
        text = fetch_text(
            self._opener,
            BASE_URL.format(code=code),
            timeout=self.timeout,
            referer=REFERER,
            user_agent=BROWSER_USER_AGENT,
        )
        return _parse_line_js(text)

    def _fetch_year(self, job: tuple[str, int]) -> list[dict]:
        """一个 (板块, 年份) 的日线。上市之前的年份返回空列表，不算失败。"""
        code, year = job
        text = fetch_text(
            self._opener,
            YEAR_URL.format(code=code, year=year),
            timeout=self.timeout,
            referer=REFERER,
            user_agent=BROWSER_USER_AGENT,
        )
        return _parse_year_js(text) or []

    def _cache_path(self) -> Path | None:
        return None if self.cache_dir is None else self.cache_dir / CACHE_NAME

    # 日线序列落盘

    def history_dir(self) -> Path:
        return (
            self._history_dir if self._history_dir is not None else state_home() / HISTORY_DIR_NAME
        )

    def _persist_history(self, parsed: list[tuple[str, str, list[dict]]]) -> int:
        """把整条日线按日期并进已有序列。**失败只降速，不影响当日读数。**

        并而不是覆盖：`last.js` 只回当年的数据，跨年之后覆盖会把去年整段抹掉，
        而那正是 60 日以上窗口要用的部分。
        """
        self.history_written = 0
        if not self.persist_history:
            return 0
        written = 0
        for code, name, bars in parsed:
            if _write_series(self.history_dir(), code, name, bars):
                written += 1
        self.history_written = written
        return written

    def _read_cache(self) -> list[str]:
        payload = read_stamped(self._cache_path(), CACHE_SCHEMA)
        if payload is None:
            return []
        codes = payload.get("codes")
        return [str(c) for c in codes] if isinstance(codes, list) else []

    def _write_cache(self, codes: list[str]) -> bool:
        if not codes:
            return False
        return write_stamped(self._cache_path(), {"codes": codes}, CACHE_SCHEMA)


# 日线序列的读写


def _series_path(root: Path, code: str) -> Path:
    return root / f"{code}.json"


def _write_series(root: Path, code: str, name: str | None, bars: list[dict]) -> bool:
    """在更新锁内合并历史和名称；锁或保存失败时保留旧文件并返回 False。

    回填那一路没有板块名（`_parse_year_js` 的说明），所以名字要从旧文件里续用。
    """

    def header(stored: dict[str, Any] | None) -> dict[str, Any]:
        stored_name = str((stored or {}).get("name") or "").strip()
        return {"code": code, "name": name or stored_name or code}

    return merge_series(_series_path(root, code), HISTORY_SCHEMA, bars, header)


def load_series(code: str, root: str | Path | None = None) -> list[dict]:
    """读回一个板块的日线序列，按日期升序。没有就返回空列表。

    这是板块轮动那一层（主线持续度、跷跷板相关性、相对强弱排名）唯一的输入口。
    """
    directory = Path(root) if root is not None else state_home() / HISTORY_DIR_NAME
    return read_series(_series_path(directory, str(code)), HISTORY_SCHEMA)


def _history_coverage(root: Path, codes: Iterable[str]) -> tuple[int, str | None, str | None]:
    """逐个汇总所选板块，避免为三个统计量同时驻留全部多年日线。"""
    count = 0
    starts: list[str] = []
    ends: list[str] = []
    for code in codes:
        bars = load_series(code, root)
        if bars:
            count += len(bars)
            starts.append(bars[0]["date"])
            ends.append(bars[-1]["date"])
    return count, min(starts) if starts else None, max(ends) if ends else None


def load_all_series(root: str | Path | None = None) -> dict[str, tuple[str, list[dict]]]:
    """读回全部已落盘的板块序列，`{code: (板块名, 日线)}`。

    板块名只在文件里，`load_series` 不带它出来——而轮动层报的是「电力设备排名
    上升最快」，不是「881155 排名上升最快」，所以这里单独走一遍 payload。

    目录不存在就返回空字典：`morning` 第一次跑之前本来就没有历史，那是预期状态
    而不是错误。**第一次跑完就有 140 根**（`last.js` 一次回当年全部日线），
    所以轮动层不需要等着攒够天数才能用。
    """
    directory = Path(root) if root is not None else state_home() / HISTORY_DIR_NAME
    result: dict[str, tuple[str, list[dict]]] = {}
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return result
    for path in paths:
        payload = read_stamped(path, HISTORY_SCHEMA)
        usable = series_bars(payload)
        if not payload or not usable:
            continue
        code = str(payload.get("code") or path.stem)
        result[code] = (str(payload.get("name") or code), usable)
    return result


def _parse_line_js(text: str) -> tuple[str, list[dict]] | None:
    """``quotebridge_v6_line_bk_881101_01_last({...})`` -> (板块名, 日线)。

    自己解析而不是用现成封装：上游改字段时这里是**解析失败**，不是静默错值。
    """
    payload = _parse_callback(text)
    if payload is None:
        return None

    name = str(payload.get("name") or "").strip()
    raw = payload.get("data")
    if not name or not isinstance(raw, str):
        return None
    return name, _parse_bars(raw)


def _parse_year_js(text: str) -> list[dict] | None:
    """按年那一路。**只有 `data` 一个键**，没有 `name`。

    与 `_parse_line_js` 分开而不是把 name 判空放宽：板块名缺失在**当日读数**那条
    路上是解析失败（排行榜要报名字），在回填这条路上是正常的——序列文件里的名字
    由 `last.js` 那次写入，回填只往里并日线。同一个判断在两条路上语义相反，
    合成一个带开关的函数只会让下次改动挑错分支。
    """
    payload = _parse_callback(text)
    if payload is None:
        return None
    raw = payload.get("data")
    return _parse_bars(raw) if isinstance(raw, str) else None


def _parse_callback(text: str) -> dict[str, Any] | None:
    """剥掉 JSONP 外壳，拿到里面那个对象。"""
    payload = jsonp_payload(text)
    return payload if isinstance(payload, dict) else None


def _parse_bars(raw: str) -> list[dict]:
    """``20260819,4453.911,4540.915,...;...`` -> 日线列表。"""
    bars: list[dict] = []
    for row in raw.split(";"):
        cells = row.split(",")
        if len(cells) < 7:
            continue
        # 一格收不住就整根丢掉。半根日线（有收盘没成交额）比没有这根更糟——
        # 它会让 `_measure` 的窗口计数够了，而算出来的量比少一天的分母。
        values = [finite(cell) for cell in cells[1:7]]
        if any(value is None for value in values):
            continue
        open_, high, low, close, volume, amount = values
        bars.append(
            {
                "date": cells[0],
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "amount": amount,
            }
        )
    return bars


def _measure(code: str, name: str, bars: list[dict]) -> SectorIndexReading | None:
    if len(bars) < MIN_BARS:
        return None
    last = bars[-1]
    close = last["close"]
    if close <= 0:
        return None

    highs = [b["high"] for b in bars[-HIGH_WINDOW:]]
    amounts = [b["amount"] for b in bars if b["amount"] > 0]
    span = last["high"] - last["low"]

    return SectorIndexReading(
        code=code,
        name=name,
        trade_date=str(last["date"]),
        close=round(close, 3),
        ret_1d=_pct(bars[-2]["close"], close),
        ret_5d=_pct(bars[-6]["close"], close) if len(bars) >= 6 else None,
        ret_20d=_pct(bars[-21]["close"], close) if len(bars) >= 21 else None,
        close_position=(round((close - last["low"]) / span * 100, 1) if span > 0 else 50.0),
        off_20d_high=_pct(max(highs), close) if highs and max(highs) > 0 else None,
        volume_ratio=(
            round(fmean(amounts[-VOLUME_WINDOW:]) / fmean(amounts[-HIGH_WINDOW:]), 4)
            if len(amounts) >= HIGH_WINDOW and fmean(amounts[-HIGH_WINDOW:]) > 0
            else None
        ),
        amount_5d=(
            round(fmean(amounts[-VOLUME_WINDOW:]), 2) if len(amounts) >= VOLUME_WINDOW else None
        ),
    )


def _pct(previous: float, current: float) -> float | None:
    if previous <= 0:
        return None
    return round(current / previous - 1.0, 6)
