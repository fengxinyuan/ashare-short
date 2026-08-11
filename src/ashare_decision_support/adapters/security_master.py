"""标的元数据：中文名 · 同花顺三级行业 · 概念标签。

数据源是一个 miniserve 目录（`ASHARE_DS_SECURITY_MASTER_URL`，默认
`https://files.688798.xyz/ths/`），两个 JSON，每天早上 08:00 前后更新一次：

```text
industries.json   5446 只   {"symbol","name","industries":[一级,二级,三级]}
concepts.json     5546 只   {"symbol","name","concepts":[...]}
```

2026-08-04 实测：`industries` 每条**恰好三级**，`concepts` 每只中位 7–8 个标签；
两份的标的集合是包含关系（concepts ⊇ industries），代码已经是 `NNNNNN.XX` 规范形式，
覆盖 SH 2280 / SZ 2837 / BJ 329。

## 这个源**没有英文名**

原始需求里提到「中英文名称」，但两份文件的字段只有 `symbol` / `name` / `industries`
或 `concepts`——`name` 是中文简称，**没有任何英文字段**。所以 `name_en` 一律为 None，
不用拼音、不用代码、不去别处凑一个。缺字段就是缺字段，编一个出来会让下游以为它是
真的（同 `adapters/mapping.py` 的契约：缺必需字段就报错，可选字段默认 None）。

## 概念为什么不进打分

概念的粒度和申万行业不是一回事：一只票可以同时挂 11 个概念，而板块打分的分母是
成分股数量。硬融进去会造出一个看起来精确的错数——`jobs/morning.py` 对同花顺行业指数
已经做过同样的判断（并列呈现，刻意不参与打分），这里沿用。

它先只做两件事：**简报/告警里显示**，和**问答检索**（「半导体有哪些票」「这只票
属于哪些概念」）。超短线看题材，而申万行业分类里没有「题材」这个东西。

## 不是每个「概念」都是题材

2026-08-04 实测 389 个概念标签里，`融资融券` 挂了 **3841/5546** 只——它是一个
**可交易属性**，不是题材。同类的还有 `深股通`/`沪股通`/`证金持股` 这种。一个覆盖
七成市场的标签在题材轮动里没有任何信息量，混进去只会让「今天哪个题材在动」永远
指向它。

所以 `themes()` 按成分占比设了一道上限（`MAX_THEME_SHARE`）。**这不是把它们删掉**
——`get(symbol).concepts` 仍然原样返回全部标签，因为「这只票能不能融资」是个真实
且有用的事实。只是在「哪个题材在动」这个问题上，它们不参与。

## 缓存

参考数据变动很慢（一天一次），每次跑都拉一遍是浪费，且上游挂了会拖垮整条链路。
落 `~/.ashare-ds/reference/security_master.json`，带 `fetched_at` 与 schema 版本戳。
过期只是**重新拉一次的理由**，不是拒绝使用的理由——拉不到就继续用旧的并标注，
因为一份三天前的行业分类仍然基本正确，而没有分类是彻底不能用。
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

from ashare_decision_support.jsoncache import read_stamped, write_stamped
from ashare_decision_support.state.paths import state_path
from ashare_decision_support.symbols import normalize_symbol

ENV_URL = "ASHARE_DS_SECURITY_MASTER_URL"
DEFAULT_BASE_URL = "https://files.688798.xyz/ths/"

INDUSTRIES_FILE = "industries.json"
CONCEPTS_FILE = "concepts.json"

# 缓存 schema 版本。结构变了之后老缓存里按旧口径写的行会静默参与新口径的查询，
# 所以版本不匹配时重建而不是猜——同 `news/store.py:SCHEMA_VERSION`。
SCHEMA_VERSION = 1
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
    industries: tuple[str, ...] = ()
    concepts: tuple[str, ...] = ()

    @property
    def industry(self) -> str | None:
        """一级行业。"""
        return self.industries[0] if self.industries else None

    @property
    def industry_leaf(self) -> str | None:
        """最细一级（实测恒为三级）。"""
        return self.industries[-1] if self.industries else None

    def describe(self) -> str:
        """简报里跟在代码后面的那一小段。"""
        parts = [item for item in (self.name, self.industry_leaf) if item]
        if self.concepts:
            parts.append("、".join(self.concepts[:3]) + ("…" if len(self.concepts) > 3 else ""))
        return "　".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "name_en": self.name_en,
            "industries": list(self.industries),
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
        with_industry = sum(1 for info in self.entries.values() if info.industries)
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
        raw = base_url or os.environ.get(ENV_URL) or DEFAULT_BASE_URL
        self.base_url = raw if raw.endswith("/") else raw + "/"
        self.cache_path = Path(cache_path) if cache_path else state_path(*CACHE_RELATIVE_PATH)
        self.timeout = timeout
        # 注入点，测试用。默认走 urllib，不引入 requests 依赖。
        self._opener = opener or _urlopen

    # ---- public ----------------------------------------------------------

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

    # ---- fetch -----------------------------------------------------------

    def _fetch(self) -> tuple[SecurityMaster | None, list[str]]:
        notes: list[str] = []
        industries, note = self._fetch_one(INDUSTRIES_FILE)
        if note:
            notes.append(note)
        concepts, note = self._fetch_one(CONCEPTS_FILE)
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
                    industries=existing.industries,
                    concepts=parsed.concepts,
                )

        if not entries:
            return None, [*notes, "元数据文件解析后为空，按不可用处理"]

        master = SecurityMaster(
            entries=entries, fetched_at=time.time(), source=self.base_url
        )
        # 覆盖率必须报出来。一份少了几百只的分类外观上完全正常——
        # `sectors/taxonomy.py` 的缺片教训就是这么来的。
        missing_industry = sum(1 for info in entries.values() if not info.industries)
        if missing_industry:
            master.notes.append(
                f"{missing_industry}/{len(entries)} 只没有行业分类（概念文件独有的标的）"
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

    # ---- cache -----------------------------------------------------------

    def _fresh(self, master: SecurityMaster, max_age_seconds: float) -> bool:
        if master.fetched_at is None:
            return False
        return (time.time() - master.fetched_at) < max_age_seconds

    def _read_cache(self) -> SecurityMaster | None:
        payload = read_stamped(self.cache_path, SCHEMA_VERSION)
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
                industries=tuple(row.get("industries") or ()),
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
    tags = tuple(str(item).strip() for item in values if str(item).strip()) if isinstance(values, list) else ()
    name = row.get("name")
    return SecurityInfo(
        symbol=symbol,
        name=str(name).strip() if isinstance(name, str) and name.strip() else None,
        industries=tags if key == "industries" else (),
        concepts=tags if key == "concepts" else (),
    )


def _urlopen(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "ashare-ds/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")
