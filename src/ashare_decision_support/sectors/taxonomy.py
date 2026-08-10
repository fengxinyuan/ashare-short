"""申万行业分类 sourced from the TickFlow universe list.

TickFlow shards each 申万 level across many universe ids — level 1 arrives as
335 ids that collapse to 31 distinct sector names — so the shards have to be
merged by name before anything downstream can use them.

This is available on the free tier and needs no key, which is what makes the
whole sector chain workable without a data subscription.

**分片取数走批量接口。** 逐个 `universes.get()` 要打 335 次，在免费档 60 次/分钟
的闸门下被节流成 **6 分钟**冷启动——这是整条链路最大的一笔固定开销。付费档的
`universes.batch(ids)` 实测 **200 个 / 0.27 秒**，335 个分片折成 2 次调用、约
0.5 秒。批量接口不可用（老 SDK、或免费档不放行）时自动退回逐个取，行为不变。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ashare_decision_support.jsoncache import read_stamped, write_stamped
from ashare_decision_support.sectors.base import SectorDefinition, SectorTaxonomy

LEVEL_PREFIXES: dict[str, str] = {
    "SW1": "CN_Equity_SW1_",
    "SW2": "CN_Equity_SW2_",
    "SW3": "CN_Equity_SW3_",
}

TAXONOMY_CACHE_NAME = "sector_taxonomy_{level}.json"

# 缓存格式戳。**只有确认零分片失败的结果才带戳**，读取时无戳即重建。
# 戳不是为了版本演进，是为了让「修复之前写下的、可能缺片的缓存」自动失效——
# 那种文件从外观上和好文件毫无区别。
TAXONOMY_SCHEMA = "sw-complete-v1"

# `universes.batch` 的单次 id 数。实测 10/50/100/200 个的耗时都在 0.23~0.27 秒，
# 也就是说开销几乎全在往返而不在条数上，取 200 只是留一截安全边际。
BATCH_SIZE = 200


class SectorTaxonomyError(RuntimeError):
    """分类取数不完整。缺片的分类会静默污染之后所有的板块排序，所以不许落盘。"""


class TickFlowSwTaxonomy(SectorTaxonomy):
    name = "tickflow-sw"

    def __init__(
        self,
        client: Any,
        level: str = "SW1",
        cache_dir: str | Path | None = None,
        min_constituents: int = 5,
    ) -> None:
        if level not in LEVEL_PREFIXES:
            raise ValueError(f"unsupported level {level!r}; expected one of {sorted(LEVEL_PREFIXES)}")
        self.client = client
        self.level = level
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.min_constituents = min_constituents
        self._cached: list[SectorDefinition] | None = None
        self.last_shard_failures = 0

    def list_sectors(self) -> list[SectorDefinition]:
        if self._cached is not None:
            return self._cached

        from_disk = self._read_cache()
        if from_disk is not None:
            self._cached = from_disk
            return from_disk

        merged: dict[str, list[str]] = {}
        prefix = LEVEL_PREFIXES[self.level]

        # 先把分片清单收齐，再一次性批量取明细——逐个取会打 335 次。
        wanted: list[tuple[str, str]] = []
        for entry in self._universe_list():
            universe_id = str(entry.get("id", ""))
            if not universe_id.startswith(prefix):
                continue
            sector = str(entry.get("name", "")).replace(self.level, "", 1).strip()
            if not sector:
                continue
            wanted.append((universe_id, sector))

        details = self._universe_details([uid for uid, _ in wanted])
        shards = len(wanted)
        failed = 0
        for universe_id, sector in wanted:
            detail = details.get(universe_id)
            if detail is None:
                failed += 1
                continue
            symbols = detail.get("symbols") if isinstance(detail, dict) else None
            if not isinstance(symbols, list):
                continue
            merged.setdefault(sector, []).extend(str(symbol) for symbol in symbols)

        # 分片取数失败会让行业少掉成分股，而下游只看得到「这个行业成分股少」，
        # 看不到「有 N 个分片没取到」。缺片的结果绝不能落盘——缓存是永久的，
        # 一次限流会让之后每一次盘前扫描都用这份残缺分类。
        self.last_shard_failures = failed
        if failed:
            raise SectorTaxonomyError(
                f"{failed}/{shards} 个 {self.level} 分片取数失败，分类不完整，已放弃写缓存。"
                "免费档限流是最常见的原因，重试即可。"
            )

        definitions = [
            SectorDefinition(
                name=sector,
                level=self.level,
                # dict.fromkeys keeps first-seen order, so the constituent list
                # is stable across runs and the cache stays diffable.
                symbols=tuple(dict.fromkeys(symbols)),
            )
            for sector, symbols in sorted(merged.items())
        ]
        definitions = [item for item in definitions if item.size >= self.min_constituents]
        self._write_cache(definitions)
        self._cached = definitions
        return definitions

    # ---- io --------------------------------------------------------------

    def _universe_list(self) -> list[dict]:
        universes = getattr(self.client, "universes", None)
        list_fn = getattr(universes, "list", None) if universes is not None else None
        if not callable(list_fn):
            return []
        payload = list_fn()
        return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []

    def _universe_get(self, universe_id: str) -> dict | None:
        """成分股明细。``None`` = 取数失败（与「空成分股」区分开）。"""
        universes = getattr(self.client, "universes", None)
        get_fn = getattr(universes, "get", None) if universes is not None else None
        if not callable(get_fn):
            return {}
        try:
            payload = get_fn(universe_id)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else {}

    def _universe_details(self, universe_ids: list[str]) -> dict[str, dict | None]:
        """id → 明细。值为 ``None`` 表示这一片取数失败。

        优先走批量接口；不可用时逐个取，语义与耗时之外的行为完全一致。
        """
        if not universe_ids:
            return {}
        batched = self._universe_batch(universe_ids)
        if batched is not None:
            return batched
        return {uid: self._universe_get(uid) for uid in universe_ids}

    def _universe_batch(self, universe_ids: list[str]) -> dict[str, dict | None] | None:
        """批量明细。``None`` = 这个 client 没有批量接口，调用方应退回逐个取。"""
        universes = getattr(self.client, "universes", None)
        batch_fn = getattr(universes, "batch", None) if universes is not None else None
        if not callable(batch_fn):
            return None

        out: dict[str, dict | None] = {}
        for start in range(0, len(universe_ids), BATCH_SIZE):
            chunk = universe_ids[start : start + BATCH_SIZE]
            payload = self._batch_call(batch_fn, chunk)
            if payload is None:
                # 第一批就调不通 = 接口形态不对，整体退回逐个取；后面的批次失败
                # 只补这一批，已经批量拿到的部分不浪费。
                if not out:
                    return None
                for uid in chunk:
                    out[uid] = self._universe_get(uid)
                continue
            for uid in chunk:
                item = payload.get(uid)
                out[uid] = item if isinstance(item, dict) else None
        return out

    @staticmethod
    def _batch_call(batch_fn: Any, chunk: list[str]) -> dict | None:
        for args, kwargs in (((), {"ids": chunk}), ((chunk,), {})):
            try:
                payload = batch_fn(*args, **kwargs)
            except TypeError:
                continue
            except Exception:
                return None
            return payload if isinstance(payload, dict) else None
        return None

    def _cache_path(self) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / TAXONOMY_CACHE_NAME.format(level=self.level.lower())

    def _read_cache(self) -> list[SectorDefinition] | None:
        # 无戳的缓存是 `complete` 语义生效之前写下的，可能缺片——2026-08-01 现场那
        # 份 SW1 缓存少了 469/4430 只成分股，而文件本身完全正常，肉眼与代码都看不
        # 出来。缓存是永久的，所以宁可重建一次，也不能继续用一份不知道完不完整的。
        payload = read_stamped(self._cache_path(), TAXONOMY_SCHEMA)
        if payload is None:
            return None
        sectors = payload.get("sectors")
        if not isinstance(sectors, list) or not sectors:
            return None
        definitions = [
            SectorDefinition(
                name=str(item["name"]),
                level=self.level,
                symbols=tuple(str(symbol) for symbol in item.get("symbols", [])),
            )
            for item in sectors
            if isinstance(item, dict) and item.get("name")
        ]
        return definitions or None

    def _write_cache(self, definitions: list[SectorDefinition]) -> bool:
        if not definitions:
            return False
        return write_stamped(
            self._cache_path(),
            {
                "level": self.level,
                "sectors": [
                    {"name": item.name, "symbols": list(item.symbols)} for item in definitions
                ],
            },
            # 只有走完整性检查、一个分片都没失败的结果才配带这个戳。
            TAXONOMY_SCHEMA,
            indent=2,
        )
