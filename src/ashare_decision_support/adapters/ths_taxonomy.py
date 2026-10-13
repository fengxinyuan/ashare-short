"""同花顺行业与概念的静态画像，保留股票和板块的对应关系。

数据来自第三方托管的 industries.json / concepts.json，并非同花顺官方成分接口。
行业数组的第二项与当前同花顺行业指数名称对应；只将这一项导入 industry，
不保留大类、细分层级或提供层级切换。名称匹配不代表已核验官方成分完全一致。

两份文件按股票代码取并集。缺少行业的股票仍保留概念，行业缺失不向其他层级
回填。概念数量随上游变化，排序时过滤无效标签及规模过小的概念。

缓存默认每天刷新一次，读取旧版本时投影为单个行业。刷新失败可继续用旧缓存
并记录年龄；无可用缓存则标记不可用，由板块调用方明确报错。
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.http import BROWSER_USER_AGENT, fetch_text
from ashare_decision_support.jsoncache import read_stamped, write_stamped
from ashare_decision_support.schedule.timezone import beijing_today
from ashare_decision_support.state.paths import state_home
from ashare_decision_support.symbols import normalize_symbol

# 分类与 SecurityMaster 共用相同端点，保证板块和标的画像来自同一份快照。
BASE_URL = "https://files.688798.xyz/ths/"
INDUSTRY_FILE = "industries.json"
CONCEPT_FILE = "concepts.json"

TIMEOUT_SECONDS = 30

CACHE_DIR_NAME = "taxonomy"
CACHE_NAME = "ths_profiles.json"
# 新版本只保存一个行业；旧版本的数组仅在读取时转换。
CACHE_SCHEMA = "ths-profiles-v2"
LEGACY_CACHE_SCHEMA = "ths-profiles-v1"

# 只将当天快照视为新鲜；刷新失败可回退旧缓存并说明年龄。
MAX_AGE_DAYS = 0

# 概念里**不是主题**的那些标签。它们混进板块排序是纯噪声——一只票属于「融资融券」
# 说明的是它的交易资格，不是它今天为什么涨。

# 分两类，都按**精确名**匹配而不是子串：`一体化压铸`、`光伏建筑一体化` 是真主题，
# 子串匹配 `一体化` 会把它们一起误杀。
_MECHANISM_TAGS = frozenset(
    {
        # 交易机制与资格
        "融资融券",
        "深股通",
        "沪股通",
        "同花顺果指数",
        # 上市阶段与状态
        "ST板块",
        "摘帽",
        "新股与次新股",
        "注册制次新股",
        "科创次新股",
    }
)
_SLOGAN_TAGS = frozenset(
    {
        # 区域与政策口号。它们的成分股是按注册地或政策文件圈的，不按业务共振，
        # 所以「这个板块今天强」在它们身上没有可解释的含义。
        "国企改革",
        "央企国企改革",
        "上海国企改革",
        "深圳国企改革",
        "一带一路",
        "西部大开发",
        "乡村振兴",
        "新疆振兴",
        "粤港澳大湾区",
        "京津冀一体化",
        "长三角一体化",
        "新型城镇化",
        "新型工业化",
        "海峡两岸",
        "自由贸易港",
        "专精特新",
        "上海自贸区",
        "天津自贸区",
        "广东自贸区",
        "福建自贸区",
        "海南自贸区",
        "黑龙江自贸区",
        "中韩自贸区",
    }
)
CONCEPT_BLOCKLIST = _MECHANISM_TAGS | _SLOGAN_TAGS

# 带年份的财报事件标签（`2026中报预增`、`2026一季报预增`）。它们每年换名，
# 写进黑名单会年年失效，所以按模式挡。
_DATED_EVENT = re.compile(r"^20\d{2}(年)?(一季报|中报|三季报|年报|季报)")

# 排除上游将空值字符串化后产生的伪概念。
_NULL_TAGS = frozenset({"nan", "NaN", "None", "null", "-", "--"})

# 概念规模下限。少于这么多成分股，中位数由个别票决定，排名翻上翻下全是噪声。
# 只是**不进板块排序**，标签仍然留在 `SymbolProfile.concepts` 上照常印进简报。
CONCEPT_MIN_SIZE = 15

# 概念不设规模上限；盘中按超额涨幅排名，避免用成分数量代替实际强弱。


@dataclass(frozen=True)
class SymbolProfile:
    """一只票的静态画像。全部来自文件，不含任何行情。"""

    symbol: str
    name: str
    industry: str | None = None
    concepts: tuple[str, ...] = ()

    def describe(self, max_concepts: int = 3) -> str:
        """将证券名称、行业与概念标签组合成一行。"""
        parts = [self.name or self.symbol]
        if self.industry:
            parts.append(self.industry)
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
            return (beijing_today() - date.fromisoformat(self.fetched_on)).days
        except ValueError:
            return None

    def names(self) -> dict[str, str]:
        return {symbol: profile.name for symbol, profile in self.profiles.items() if profile.name}

    def industry_groups(self) -> dict[str, list[str]]:
        """同花顺行业名 → 成分股；未知归属保持缺失。"""
        groups: dict[str, list[str]] = {}
        for symbol, profile in self.profiles.items():
            name = profile.industry
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
            markets[symbol.partition(".")[2] or "?"] = (
                markets.get(symbol.partition(".")[2] or "?", 0) + 1
            )
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
        # 行业与概念共用任务内快照，避免重复读取，并保证两个维度一致。
        self._loaded: ThsTaxonomySnapshot | None = None

    # public

    def load(self, force_refresh: bool = False) -> ThsTaxonomySnapshot:
        """缓存够新就用缓存，否则重取；重取失败继续用旧的并报出年龄。"""
        if not force_refresh and self._loaded is not None:
            return self._loaded

        cached = self._read_cache()
        if not force_refresh and cached is not None and _is_fresh(cached, self.max_age_days):
            cached.notes.append(cached.coverage_note())
            self._loaded = cached
            return self._loaded

        fetched, errors = self._fetch()
        if fetched is not None:
            self._write_cache(fetched)
            fetched.notes.append(fetched.coverage_note())
            self._loaded = fetched
            return self._loaded

        if cached is not None:
            age = cached.age_days
            cached.notes.append(
                f"同花顺静态分类重取失败（{'；'.join(errors)}），"
                f"继续使用 {cached.fetched_on} 的缓存"
                + (f"，已过期 {age} 天" if age is not None else "")
                + "——新上市、摘帽、行业调整的标的可能归错板块"
            )
            cached.notes.append(cached.coverage_note())
            self._loaded = cached
            return self._loaded

        self._loaded = ThsTaxonomySnapshot(
            ok=False,
            notes=[f"同花顺静态分类不可用且无缓存：{'；'.join(errors)}"],
        )
        return self._loaded

    # io

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
                industry=industry_from_path(industry_tags),
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
                fetched_on=str(beijing_today()),
                from_cache=False,
                ok=True,
                notes=notes,
            ),
            errors,
        )

    def _get_json(self, filename: str, errors: list[str]) -> Any:
        try:
            payload = fetch_text(
                self._opener,
                f"{self.base_url}{filename}",
                timeout=self.timeout,
                user_agent=BROWSER_USER_AGENT,
            )
            return json.loads(payload)
        except Exception as exc:
            errors.append(f"{filename}: {type(exc).__name__}")
            return None

    # cache

    def _cache_path(self) -> Path:
        root = self._cache_dir if self._cache_dir is not None else state_home() / CACHE_DIR_NAME
        return root / CACHE_NAME

    def _read_cache(self) -> ThsTaxonomySnapshot | None:
        payload = read_stamped(self._cache_path(), CACHE_SCHEMA) or read_stamped(
            self._cache_path(), LEGACY_CACHE_SCHEMA
        )
        if payload is None:
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
                industry=(
                    industry_from_path(row.get("industries"))
                    if payload.get("schema") == LEGACY_CACHE_SCHEMA
                    else normalize_industry(row.get("industry"))
                ),
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
        # 落盘失败只降速，不影响正确性——调用方此时已经拿到了数据。
        return write_stamped(
            self._cache_path(),
            {
                "fetched_on": snapshot.fetched_on,
                "profiles": [
                    {
                        "symbol": profile.symbol,
                        "name": profile.name,
                        "industry": profile.industry,
                        "concepts": list(profile.concepts),
                    }
                    for profile in snapshot.profiles.values()
                ],
            },
            CACHE_SCHEMA,
        )


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
        values = row.get(tag_key)
        tags = (
            tuple(item.strip() if isinstance(item, str) else "" for item in values)
            if isinstance(values, list)
            else ()
        )
        if tag_key == "concepts":
            tags = tuple(item for item in tags if item)
        out[symbol] = (str(row.get("name") or "").strip(), tags)
    return out


def industry_from_path(values: Any) -> str | None:
    """导入旧画像及上游数组中的同花顺行业；不把大类或细分名补成行业。

    静态文件的第二项与同花顺行业指数名称对应。保留空项的位置，避免缺字段时
    将第三级细分误当成行业。其余层级只在导入边界出现，不进入新的分类画像。
    """
    if not isinstance(values, (list, tuple)) or len(values) < 2:
        return None
    return normalize_industry(values[1])


def normalize_industry(value: Any) -> str | None:
    name = value.strip() if isinstance(value, str) else ""
    return name if name and name not in _NULL_TAGS else None


def _is_fresh(snapshot: ThsTaxonomySnapshot, max_age_days: int) -> bool:
    if not snapshot.fetched_on:
        return False
    try:
        fetched = date.fromisoformat(snapshot.fetched_on)
    except ValueError:
        return False
    return fetched > beijing_today() - timedelta(days=max_age_days + 1)
