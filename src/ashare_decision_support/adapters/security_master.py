"""标的元数据：中文名、一个同花顺行业和概念标签。

默认读取第三方托管的同花顺分类快照，地址可由 ASHARE_DS_SECURITY_MASTER_URL
覆盖。行业与板块分类共用导入规则，只保留上游 industries 数组的第二项；其他
层级不进入画像和报告。行业、概念文件按股票代码取并集，缺行业时仍保留概念。
这个源不提供英文名，name_en 保持 None。

concepts 保留原始标签；themes() 在持仓归并时排除覆盖过广的标签，板块排序的
概念筛选由 sectors/ths_taxonomy.py 负责。

缓存带 fetched_at 和版本戳，兼容读取旧的行业数组。刷新失败时继续使用缓存
并标注；只需本地画像的问答和持仓查询使用 cached()，不隐式发起网络请求。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.http import BROWSER_USER_AGENT, fetch_text
from ashare_decision_support.adapters.ths_taxonomy import (
    BASE_URL,
    CONCEPT_FILE,
    INDUSTRY_FILE,
    industry_from_path,
    normalize_industry,
)
from ashare_decision_support.jsoncache import read_stamped, write_stamped
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.symbols import normalize_symbol

# 地址与两个文件名从 `adapters/ths_taxonomy.py` 取，那边是按这个源命名的模块，
# 理由写在它的常量上方：同一台主机的同两份 JSON 被两个适配器各读一遍。

# **可被 env 覆盖的只有这一边，这是有意的**：`ENV_URL` 指的是「标的元数据从哪
# 取」，换镜像时板块分类那边并不跟着换。两边共用的是**默认地址**，不是同一个
# 开关——共用默认值让「上游换地址」只需改一处，各自的开关仍然各归各。
ENV_URL = "ASHARE_DS_SECURITY_MASTER_URL"

# 新缓存只保存单个行业；旧版本只在读取边界投影，不混用层级。
SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
CACHE_RELATIVE_PATH = ("reference", "security_master.json")

# 超过这个时长就该重新拉。**不是过期即失效**，见模块 docstring。
DEFAULT_MAX_AGE_SECONDS = 20 * 3600

DEFAULT_TIMEOUT = 30.0

# 覆盖超过这个比例的「概念」按可交易属性处理，不算题材。实测 `融资融券` 占
# 3841/5546 ≈ 69%，`深股通`/`证金持股` 同类。见模块 docstring。
MAX_THEME_SHARE = 0.25


@dataclass(frozen=True)
class SecurityInfo:
    """一只票的静态属性。全部可能缺失——缺了就是 None / 空，不猜。"""

    symbol: str
    name: str | None = None
    # 这个源没有英文名。字段留着是因为契约需要它，值恒为 None。
    name_en: str | None = None
    industry: str | None = None
    concepts: tuple[str, ...] = ()

    def describe(self) -> str:
        """简报里跟在代码后面的那一小段。"""
        parts = [item for item in (self.name, self.industry) if item]
        if self.concepts:
            parts.append("、".join(self.concepts[:3]) + ("…" if len(self.concepts) > 3 else ""))
        return "　".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "name_en": self.name_en,
            "industry": self.industry,
            "concepts": list(self.concepts),
        }


@dataclass
class SecurityMaster:
    """代码 → 静态属性的查表，外加反向的行业/概念检索。"""

    entries: dict[str, SecurityInfo] = field(default_factory=dict)
    fetched_at: float | None = None
    source: str | None = None
    notes: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def ok(self) -> bool:
        return bool(self.entries)

    def get(self, symbol: str) -> SecurityInfo | None:
        return self.entries.get(normalize_symbol(symbol))

    def themes(self, max_share: float = MAX_THEME_SHARE) -> dict[str, int]:
        """概念 → 成分数，**剔除覆盖过广的可交易属性**。

        `融资融券` 覆盖 69% 的市场，它是「能不能融资」不是「在炒什么」。把它留在
        题材榜里，「今天哪个题材在动」的答案会永远是它。

        剔除只发生在这里；`get(symbol).concepts` 仍然原样返回全部标签——「这只票
        能不能融资」是个真实的事实，只是回答不了题材问题。
        """
        if not self.entries:
            return {}
        counts: dict[str, int] = {}
        for info in self.entries.values():
            for tag in info.concepts:
                counts[tag] = counts.get(tag, 0) + 1
        ceiling = len(self.entries) * max_share
        return {tag: size for tag, size in counts.items() if size <= ceiling}

    def stats(self) -> dict[str, Any]:
        with_concepts = sum(1 for info in self.entries.values() if info.concepts)
        with_industry = sum(1 for info in self.entries.values() if info.industry)
        themes = self.themes()
        return {
            "symbols": len(self.entries),
            "with_industry": with_industry,
            "with_concepts": with_concepts,
            "concepts_total": len({tag for info in self.entries.values() for tag in info.concepts}),
            "themes": len(themes),
            "fetched_at": self.fetched_at,
            "source": self.source,
            "provides_english_name": False,
            "notes": list(self.notes),
        }


class SecurityMasterAdapter:
    """拉取 + 缓存。**任何一步失败都不抛异常**，返回空表并记 note。

    元数据缺失只该让简报少显示一个行业名，不该让盘前链路停下来。
    """

    name = "ths-security-master"

    def __init__(
        self,
        base_url: str | None = None,
        cache_path: str | Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        opener: Any = None,
    ) -> None:
        raw = base_url or os.environ.get(ENV_URL) or BASE_URL
        self.base_url = raw if raw.endswith("/") else raw + "/"
        self.cache_path = Path(cache_path) if cache_path else state_path(*CACHE_RELATIVE_PATH)
        self.timeout = timeout
        # 注入点，测试用。默认走 urllib，不引入 requests 依赖。
        self._opener = opener or _urlopen

    # public

    def load(
        self, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS, force: bool = False
    ) -> SecurityMaster:
        """缓存新鲜就用缓存，否则拉一次；拉失败**继续用旧的**并标注。"""
        cached = self._read_cache()
        if not force and cached is not None and self._fresh(cached, max_age_seconds):
            return cached

        fetched, notes = self._fetch()
        if fetched is not None:
            fetched.notes.extend(notes)
            self._write_cache(fetched)
            return fetched

        if cached is not None:
            age_hours = (time.time() - (cached.fetched_at or 0)) / 3600.0
            cached.notes.extend(notes)
            cached.notes.append(
                f"元数据拉取失败，继续使用 {age_hours:.1f} 小时前的缓存"
                "——旧分类基本仍然正确，没有分类才是彻底不能用"
            )
            return cached

        return SecurityMaster(notes=[*notes, "标的元数据不可用，本次不含行业与概念标签"])

    def cached(self) -> SecurityMaster:
        """只读缓存，**一次网络都不打**。

        `load()` 的降级顺序（缓存新鲜就用、否则拉、拉不到再退回旧缓存）在跑批链路
        上是对的：那时慢几秒换一份新分类值得。装配事实包与盘中循环不是那个场景
        ——`ask` / `facts` / `chat` 随时会被调用，一次同步的外网往返会让「导出当前
        事实」这个动作变成一件不确定要等多久的事，而这一层要的只是「这几只票分别
        属于哪个行业和题材」，三天前的分类和今天的没有区别。

        缓存不在就是空表加一条说明，不静默返回一份「没有任何集中度」的读数——
        `strategy/portfolio.py` 靠 `ok` 分得开「没有分类」和「不集中」。
        """
        cached = self._read_cache()
        if cached is not None:
            return cached
        return SecurityMaster(notes=["标的元数据缓存为空，本次不含行业与题材归并"])

    def health_check(self) -> dict[str, Any]:
        master = self.load()
        payload = master.stats()
        payload.update(
            {
                "source": self.name,
                "base_url": self.base_url,
                "reachable": master.ok,
                "cache_path": str(self.cache_path),
            }
        )
        return payload

    # fetch

    def _fetch(self) -> tuple[SecurityMaster | None, list[str]]:
        notes: list[str] = []
        industries, note = self._fetch_one(INDUSTRY_FILE)
        if note:
            notes.append(note)
        concepts, note = self._fetch_one(CONCEPT_FILE)
        if note:
            notes.append(note)

        if industries is None and concepts is None:
            return None, notes

        entries: dict[str, SecurityInfo] = {}
        for row in industries or []:
            info = _parse_row(row, "industries")
            if info is not None:
                entries[info.symbol] = info
        for row in concepts or []:
            parsed = _parse_row(row, "concepts")
            if parsed is None:
                continue
            existing = entries.get(parsed.symbol)
            if existing is None:
                entries[parsed.symbol] = parsed
            else:
                # 两份文件的 name 应当一致；以行业那份为准，概念只补 concepts。
                entries[parsed.symbol] = SecurityInfo(
                    symbol=existing.symbol,
                    name=existing.name or parsed.name,
                    industry=existing.industry,
                    concepts=parsed.concepts,
                )

        if not entries:
            return None, [*notes, "元数据文件解析后为空，按不可用处理"]

        master = SecurityMaster(entries=entries, fetched_at=time.time(), source=self.base_url)
        # 缺行业不应被误读为不存在这只股票。
        missing_industry = sum(1 for info in entries.values() if not info.industry)
        if missing_industry:
            master.notes.append(
                f"{missing_industry}/{len(entries)} 只缺少同花顺行业，已保留名称与概念"
            )
        return master, notes

    def _fetch_one(self, filename: str) -> tuple[list | None, str | None]:
        url = self.base_url + filename
        try:
            raw = self._opener(url, self.timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return None, f"{filename} 拉取失败（{type(exc).__name__}）"
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            return None, f"{filename} 不是合法 JSON（{type(exc).__name__}）"
        if not isinstance(payload, list):
            return None, f"{filename} 顶层不是数组（实为 {type(payload).__name__}），拒绝解析"
        return payload, None

    # cache

    def _fresh(self, master: SecurityMaster, max_age_seconds: float) -> bool:
        if master.fetched_at is None:
            return False
        return (time.time() - master.fetched_at) < max_age_seconds

    def _read_cache(self) -> SecurityMaster | None:
        payload = read_stamped(self.cache_path, SCHEMA_VERSION) or read_stamped(
            self.cache_path, LEGACY_SCHEMA_VERSION
        )
        if payload is None:
            return None
        rows = payload.get("entries")
        if not isinstance(rows, list):
            return None

        entries: dict[str, SecurityInfo] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = normalize_symbol(row.get("symbol", ""))
            if not symbol:
                continue
            entries[symbol] = SecurityInfo(
                symbol=symbol,
                name=row.get("name") or None,
                industry=(
                    industry_from_path(row.get("industries"))
                    if payload.get("schema") == LEGACY_SCHEMA_VERSION
                    else normalize_industry(row.get("industry"))
                ),
                concepts=tuple(row.get("concepts") or ()),
            )
        if not entries:
            return None
        return SecurityMaster(
            entries=entries,
            fetched_at=payload.get("fetched_at"),
            source=payload.get("source"),
        )

    def _write_cache(self, master: SecurityMaster) -> None:
        written = write_stamped(
            self.cache_path,
            {
                "fetched_at": master.fetched_at,
                "source": master.source,
                "entries": [info.to_dict() for info in master.entries.values()],
            },
            SCHEMA_VERSION,
        )
        if not written:
            master.notes.append("元数据缓存写入失败，下次仍会重新拉取")


def _parse_row(row: Any, key: str) -> SecurityInfo | None:
    if not isinstance(row, dict):
        return None
    symbol = normalize_symbol(row.get("symbol", ""))
    if not symbol:
        return None
    values = row.get(key)
    tags = (
        tuple(str(item).strip() for item in values if str(item).strip())
        if isinstance(values, list)
        else ()
    )
    name = row.get("name")
    return SecurityInfo(
        symbol=symbol,
        name=str(name).strip() if isinstance(name, str) and name.strip() else None,
        industry=industry_from_path(values) if key == "industries" else None,
        concepts=tags if key == "concepts" else (),
    )


def _urlopen(url: str, timeout: float) -> str:
    # 与 `adapters/ths_taxonomy.py` 打的是同一台主机、同两份文件，请求头也用同一套。
    return fetch_text(
        urllib.request.urlopen,
        url,
        timeout=timeout,
        user_agent=BROWSER_USER_AGENT,
        errors="strict",
    )
