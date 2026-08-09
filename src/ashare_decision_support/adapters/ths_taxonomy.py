"""同花顺静态分类文件：全 A 的名称、申万三级行业、概念标签。

**为什么换掉 TickFlow 的 universe 分类。** 那条路能跑通是因为免费档的
`universes.list()` 恰好带申万一级，但它有四个硬伤，全部由这份静态文件消掉：

| | TickFlow `universes` | 这份文件 |
|---|---|---|
| 覆盖 | 4430 / 5528，**北交所一只都没有** | **5550 只**（SH 2280 / SZ 2837 / **BJ 337**） |
| 层级 | 只有一级 31 个 | **一/二/三级 31 / 90 / 257** |
| 概念 | 无 | **390 个** |
| 冷启动 | 335 个分片 × 60 次/分钟闸门 = **约 6 分钟** | 两次 HTTP，**< 1 秒** |
| 配额 | 吃掉 60/分钟里的 45 次 | 不占 |

行业一级名与申万完全一致（机械设备 602、电子 513、医药生物 500 … 银行 42、
煤炭 34、综合 18），二三级带 `IT服务Ⅲ`、`游戏Ⅲ` 这类罗马数字后缀，是申万三级
分类的写法。以上数字都是 2026-08-09 实测。

**两个文件的覆盖不一样，所以按并集合并。** `industries.json` 5446 只、
`concepts.json` 5550 只，且前者是后者的真子集——有 104 只（华银电力、华大智造、
众生药业这些老票，不是新股）只在概念文件里。此前这里以行业文件为主循环，
那 104 只**整只消失**：既不进行业板块，也不进概念板块，简报里查不到它的画像。
行业缺失时 `industries=()`，它照样能进概念板块——**缺一个维度不该让整只票蒸发**。

**这份文件每天 00:00 UTC（北京 08:00）刷新一次**（2026-08-09 实测两个文件的
`last-modified` 都是当天 00:00:0x）。此前按「更新频率未知」按周缓存，于是概念
成分表可以整整落后一周——而概念成分是天天变的（新题材上榜、蹭概念的票被塞进来），
盘中板块监控读的正是这张表。所以 `MAX_AGE_DAYS = 0`：只认当天抓的。
两次 HTTP 约 2MB，一天一次，代价可以忽略。

过期的分类**外观完全正常**——新股不在里面、摘帽的还标着 ST，链路照跑不误。
这正是 2026-08-01 那次「缺 469 只成分股却一个行业不缺」的事故形态。所以：

1. 落盘带 `fetched_on`，超过 `MAX_AGE_DAYS` 天自动重取；
2. 重取失败**继续用旧的**（分类过期比没有分类好），但把**实际年龄**写进 notes；
3. 从来没取到过则 `ok=False`，由调用方决定回退还是中止——**不返回空分类**，
   空分类会让下游读成「今天没有板块」。

量纲与命名：`symbol` 一律过 `normalize_symbol`，与台账、自选股同一口径。
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ashare_decision_support.state.paths import state_home
from ashare_decision_support.symbols import normalize_symbol

BASE_URL = "https://files.688798.xyz/ths/"
INDUSTRY_FILE = "industries.json"
CONCEPT_FILE = "concepts.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT_SECONDS = 30

CACHE_DIR_NAME = "taxonomy"
CACHE_NAME = "ths_profiles.json"
# 只有走完整性检查的结果才配带这个戳，读取时无戳即重建。沿用
# `sectors/taxonomy.py:TAXONOMY_SCHEMA` 的做法。
CACHE_SCHEMA = "ths-profiles-v1"

# 超过这个天数自动重取。取 0 是因为**上游每天 00:00 UTC 刷新一次**（实测），
# 也就是「只认当天抓的」。此前取 7 天，理由是「新股上市、ST 摘帽、行业调整都是
# 周级别的事件」——那对行业分类成立，对概念分类不成立：概念成分表天天变，而盘中
# 板块监控就读它。重取失败仍然回落到旧缓存并报出年龄，所以激进一点没有下行风险。
MAX_AGE_DAYS = 0

# 行业层级名。对外沿用 SW1/SW2/SW3，因为 `--level` 参数、缓存文件名和台账字段
# 已经在用这套写法；一级名与申万逐字一致，二三级的申万对应关系**未逐条核对**。
LEVELS: dict[str, int] = {"SW1": 0, "SW2": 1, "SW3": 2}

# 概念里**不是主题**的那些标签。它们混进板块排序是纯噪声——一只票属于「融资融券」
# 说明的是它的交易资格，不是它今天为什么涨。
#
# 分两类，都按**精确名**匹配而不是子串：`一体化压铸`、`光伏建筑一体化` 是真主题，
# 子串匹配 `一体化` 会把它们一起误杀。
_MECHANISM_TAGS = frozenset({
    # 交易机制与资格
    "融资融券", "深股通", "沪股通", "同花顺果指数",
    # 上市阶段与状态
    "ST板块", "摘帽", "新股与次新股", "注册制次新股", "科创次新股",
})
_SLOGAN_TAGS = frozenset({
    # 区域与政策口号。它们的成分股是按注册地或政策文件圈的，不按业务共振，
    # 所以「这个板块今天强」在它们身上没有可解释的含义。
    "国企改革", "央企国企改革", "上海国企改革", "深圳国企改革",
    "一带一路", "西部大开发", "乡村振兴", "新疆振兴",
    "粤港澳大湾区", "京津冀一体化", "长三角一体化", "新型城镇化", "新型工业化",
    "海峡两岸", "自由贸易港", "专精特新",
    "上海自贸区", "天津自贸区", "广东自贸区", "福建自贸区", "海南自贸区",
    "黑龙江自贸区", "中韩自贸区",
})
CONCEPT_BLOCKLIST = _MECHANISM_TAGS | _SLOGAN_TAGS

# 带年份的财报事件标签（`2026中报预增`、`2026一季报预增`）。它们每年换名，
# 写进黑名单会年年失效，所以按模式挡。
_DATED_EVENT = re.compile(r"^20\d{2}(年)?(一季报|中报|三季报|年报|季报)")

# 上游漏出来的空值。2026-08-09 实测 `concepts.json` 里有 4 只票带着一个字面量
# 叫 `nan` 的「概念」——那是源头某一步把 NaN 直接字符串化了。它此前靠规模下限
# 被顺带挡住，但那是巧合而不是判断：下限一放宽它就会变成一个板块名。
_NULL_TAGS = frozenset({"nan", "NaN", "None", "null", "-", "--"})

# 概念规模下限。少于这么多成分股，中位数由个别票决定，排名翻上翻下全是噪声。
# 只是**不进板块排序**，标签仍然留在 `SymbolProfile.concepts` 上照常印进简报。
CONCEPT_MIN_SIZE = 15

# **没有上限。** 此前是 400，理由写的是「太大的概念不是轮动单位——『人工智能』
# 1069 只，它涨说明的是大盘涨」。那个担心是真的，但用成分股只数当判据是它的
# 粗糙代理，而且代价极高：2026-08-09 实测被 400 挡掉的 32 个概念，恰好是超短的
# 全部主战场——
#
#   机器人概念1215、人工智能1083、新能源汽车1061、华为概念1003、芯片概念911、
#   储能894、DeepSeek概念769、数据中心(AIDC)650、AI应用527、商业航天503、
#   低空经济498、无人机463、人形机器人461、AI智能体453、东数西算409 …
#
# 也就是说「监控机器人板块有没有异动」这件事，在旧闸门下**结构上不可能实现**。
#
# 精确的判据是**超额涨幅**（板块中位涨幅 − 全市场中位涨幅，见
# `sectors/intraday.py`）：占市场 20% 的「人工智能」跟着大盘涨时超额≈0，自己
# 排不上去；只有真的整体异动才拉得开。同一个担心，量出来而不是猜出来。


@dataclass(frozen=True)
class SymbolProfile:
    """一只票的静态画像。全部来自文件，不含任何行情。"""

    symbol: str
    name: str
    industries: tuple[str, ...] = ()   # (一级, 二级, 三级)，可能短于 3
    concepts: tuple[str, ...] = ()

    def industry(self, level: str = "SW1") -> str | None:
        index = LEVELS.get(level)
        if index is None or index >= len(self.industries):
            return None
        return self.industries[index] or None

    @property
    def industry_path(self) -> str:
        """`计算机·软件开发·横向通用软件`，给简报直接印。"""
        return "·".join(item for item in self.industries if item)

    def describe(self, max_concepts: int = 3) -> str:
        """`奥拓电子｜计算机·软件开发·横向通用软件｜AI应用、算力租赁`。

        候选表里此前只有一个裸代码 `300588.SZ`——读的人认不出这是什么公司、
        属于哪条线，于是整份排序看起来像是凭空来的。
        """
        parts = [self.name or self.symbol]
        if self.industry_path:
            parts.append(self.industry_path)
        tradable = [item for item in self.concepts if is_tradable_concept(item)]
        if tradable:
            parts.append("、".join(tradable[:max_concepts]))
        return "｜".join(parts)


@dataclass
class ThsTaxonomySnapshot:
    """一次分类读取的结果。`ok=False` 时 `profiles` 一定是空的。"""

    profiles: dict[str, SymbolProfile] = field(default_factory=dict)
    fetched_on: str | None = None
    from_cache: bool = False
    ok: bool = True
    notes: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.profiles)

    @property
    def age_days(self) -> int | None:
        if not self.fetched_on:
            return None
        try:
            return (date.today() - date.fromisoformat(self.fetched_on)).days
        except ValueError:
            return None

    def names(self) -> dict[str, str]:
        return {symbol: profile.name for symbol, profile in self.profiles.items() if profile.name}

    def industry_groups(self, level: str = "SW1") -> dict[str, list[str]]:
        """行业名 → 成分股。层级不存在时返回空 dict。"""
        if level not in LEVELS:
            return {}
        groups: dict[str, list[str]] = {}
        for symbol, profile in self.profiles.items():
            name = profile.industry(level)
            if name:
                groups.setdefault(name, []).append(symbol)
        return groups

    def concept_groups(self) -> dict[str, list[str]]:
        """概念名 → 成分股。**已剔除黑名单**，规模闸门留给调用方（要标 untrusted）。"""
        groups: dict[str, list[str]] = {}
        for symbol, profile in self.profiles.items():
            for concept in profile.concepts:
                if is_tradable_concept(concept):
                    groups.setdefault(concept, []).append(symbol)
        return groups

    def coverage_note(self) -> str:
        markets: dict[str, int] = {}
        for symbol in self.profiles:
            markets[symbol.partition(".")[2] or "?"] = markets.get(symbol.partition(".")[2] or "?", 0) + 1
        breakdown = " / ".join(f"{market} {count}" for market, count in sorted(markets.items()))
        source = "缓存" if self.from_cache else "远端"
        return (
            f"同花顺静态分类：{len(self.profiles)} 只（{breakdown}），"
            f"抓取日 {self.fetched_on or '未知'}（{source}）"
        )


def is_tradable_concept(name: str) -> bool:
    """这个概念能不能当板块用。黑名单、上游空值与带年份的财报事件都不算。"""
    text = str(name or "").strip()
    if not text or text in CONCEPT_BLOCKLIST or text in _NULL_TAGS:
        return False
    return not _DATED_EVENT.match(text)


class ThsTaxonomySource:
    """静态分类文件的取数与落盘。**不做任何板块计算**，那是 `sectors/` 的事。"""

    name = "ths-static-taxonomy"

    def __init__(
        self,
        base_url: str = BASE_URL,
        cache_dir: str | Path | None = None,
        opener: Any | None = None,
        timeout: int = TIMEOUT_SECONDS,
        max_age_days: int = MAX_AGE_DAYS,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        # 默认落在状态目录而不是 `--output-dir`：后者常按日期命名，那样每天会重下
        # 一次，而这份文件是周级别才变的。理由同 `jobs/morning.py` 里语料库那段。
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.max_age_days = max_age_days

    # ---- public ----------------------------------------------------------

    def load(self, force_refresh: bool = False) -> ThsTaxonomySnapshot:
        """缓存够新就用缓存，否则重取；重取失败继续用旧的并报出年龄。"""
        cached = self._read_cache()
        if not force_refresh and cached is not None and _is_fresh(cached, self.max_age_days):
            cached.notes.append(cached.coverage_note())
            return cached

        fetched, errors = self._fetch()
        if fetched is not None:
            self._write_cache(fetched)
            fetched.notes.append(fetched.coverage_note())
            return fetched

        if cached is not None:
            age = cached.age_days
            cached.notes.append(
                f"同花顺静态分类重取失败（{'；'.join(errors)}），"
                f"继续使用 {cached.fetched_on} 的缓存"
                + (f"，已过期 {age} 天" if age is not None else "")
                + "——新上市、摘帽、行业调整的标的可能归错板块"
            )
            cached.notes.append(cached.coverage_note())
            return cached

        return ThsTaxonomySnapshot(
            ok=False,
            notes=[f"同花顺静态分类不可用且无缓存：{'；'.join(errors)}"],
        )

    # ---- io --------------------------------------------------------------

    def _fetch(self) -> tuple[ThsTaxonomySnapshot | None, list[str]]:
        """行业文件是必需的，概念文件缺了只降级。

        分开处理是因为两者的失效后果不同：没有行业就没有板块链路，
        没有概念只是少一个维度。把它们绑成一次成败，等于让次要的那个否决主要的。

        **合并按并集，不按行业文件的行。** 实测 `concepts.json` 比
        `industries.json` 多 104 只（华银电力、华大智造、众生药业……都是老票，
        不是还没归类的新股），以行业文件为主循环会让它们整只消失——连概念板块
        都进不去。行业缺失只该少一个维度，不该让这只票从系统里蒸发。
        """
        errors: list[str] = []
        industries = self._get_json(INDUSTRY_FILE, errors)
        if not isinstance(industries, list) or not industries:
            return None, errors or [f"{INDUSTRY_FILE} 返回的不是非空数组"]

        industry_rows = _index_rows(industries, "industries")
        concepts = self._get_json(CONCEPT_FILE, errors)
        concept_rows = _index_rows(concepts, "concepts") if isinstance(concepts, list) else {}

        profiles: dict[str, SymbolProfile] = {}
        for symbol in industry_rows.keys() | concept_rows.keys():
            industry_name, industry_tags = industry_rows.get(symbol, ("", ()))
            concept_name, concept_tags = concept_rows.get(symbol, ("", ()))
            profiles[symbol] = SymbolProfile(
                symbol=symbol,
                # 两份文件都带名字，取先有的那个——它们实测一致，不一致时行业
                # 文件优先，因为板块链路的其余部分都以它为准。
                name=industry_name or concept_name,
                industries=industry_tags,
                concepts=concept_tags,
            )

        if not profiles:
            return None, [*errors, f"{INDUSTRY_FILE} 解析后没有任何可用标的"]

        notes: list[str] = []
        if not concept_rows:
            notes.append("同花顺概念文件不可用，本次只有行业分类，无概念板块")
        else:
            concept_only = len(concept_rows.keys() - industry_rows.keys())
            if concept_only:
                notes.append(
                    f"{concept_only} 只标的只在概念文件里有行业文件没有，"
                    "已按并集保留（它们没有行业归属，只进概念板块）"
                )
        return (
            ThsTaxonomySnapshot(
                profiles=profiles,
                fetched_on=str(date.today()),
                from_cache=False,
                ok=True,
                notes=notes,
            ),
            errors,
        )

    def _get_json(self, filename: str, errors: list[str]) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}{filename}", headers={"User-Agent": USER_AGENT}
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                payload = response.read().decode("utf-8", "ignore")
            return json.loads(payload)
        except Exception as exc:
            errors.append(f"{filename}: {type(exc).__name__}")
            return None

    # ---- cache -----------------------------------------------------------

    def _cache_path(self) -> Path:
        root = self._cache_dir if self._cache_dir is not None else state_home() / CACHE_DIR_NAME
        return root / CACHE_NAME

    def _read_cache(self) -> ThsTaxonomySnapshot | None:
        try:
            payload = json.loads(self._cache_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("schema") != CACHE_SCHEMA:
            return None
        rows = payload.get("profiles")
        if not isinstance(rows, list) or not rows:
            return None

        profiles: dict[str, SymbolProfile] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = normalize_symbol(str(row.get("symbol") or ""))
            if not symbol:
                continue
            profiles[symbol] = SymbolProfile(
                symbol=symbol,
                name=str(row.get("name") or ""),
                industries=tuple(str(item) for item in row.get("industries") or []),
                concepts=tuple(str(item) for item in row.get("concepts") or []),
            )
        if not profiles:
            return None
        return ThsTaxonomySnapshot(
            profiles=profiles,
            fetched_on=str(payload.get("fetched_on") or "") or None,
            from_cache=True,
            ok=True,
        )

    def _write_cache(self, snapshot: ThsTaxonomySnapshot) -> bool:
        path = self._cache_path()
        payload = {
            "schema": CACHE_SCHEMA,
            "fetched_on": snapshot.fetched_on,
            "profiles": [
                {
                    "symbol": profile.symbol,
                    "name": profile.name,
                    "industries": list(profile.industries),
                    "concepts": list(profile.concepts),
                }
                for profile in snapshot.profiles.values()
            ],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            # 落盘失败只降速，不影响正确性——调用方此时已经拿到了数据。
            return False
        return True


def _index_rows(rows: Any, tag_key: str) -> dict[str, tuple[str, tuple[str, ...]]]:
    """`[{symbol, name, <tag_key>: [...]}, ...]` → `{symbol: (name, tags)}`。

    两个文件的行结构只差一个键名，索引一次比在合并循环里分两路解析清楚，
    也让「按并集合并」写起来就是一次字典 union。
    """
    out: dict[str, tuple[str, tuple[str, ...]]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol(str(row.get("symbol") or ""))
        if not symbol:
            continue
        tags = tuple(
            str(item).strip() for item in (row.get(tag_key) or []) if str(item).strip()
        )
        out[symbol] = (str(row.get("name") or "").strip(), tags)
    return out


def _is_fresh(snapshot: ThsTaxonomySnapshot, max_age_days: int) -> bool:
    if not snapshot.fetched_on:
        return False
    try:
        fetched = date.fromisoformat(snapshot.fetched_on)
    except ValueError:
        return False
    return fetched > date.today() - timedelta(days=max_age_days + 1)
