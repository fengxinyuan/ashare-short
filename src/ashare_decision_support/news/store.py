"""持久舆情语料库：把「今天采到了什么」变成「这条消息是什么时候第一次出现的」。

`news/cache.py` 解决的是**重复拉取**——同一个早上跑两次不要再打一遍上游，TTL 一小时、
按天分目录、放在输出目录里，跑完即弃。它不解决、也不该解决下面这件事：

**对超短线，同一条利好「10 分钟前第一次出现」和「已经发酵三天」是完全不同的两件事。**
前者是机会，后者往往是接盘位置。而在临时缓存里两者长得一模一样，因为它只存
「今天见到的」——昨天见过没见过，明天就查不到了。

`published_at` 顶不上这个位置，两个原因都是实测出来的：

1. **大量源不给时间戳**（新闻联播只给日期），`dedup.py` 对这类文档刻意不折减。
   拿一个一半为空的字段当「第一次出现」的依据，等于给这些源随机赋新鲜度。
2. **转发会带上新的 published_at。** 财联社与东财大量转发同一条内容，标题常常
   一字不差。按发布时间看，一条隔夜消息会在第二天早上「重新变新」。

所以语料库自己记 `first_seen_at`——**系统什么时候第一次知道这件事**。这个时钟是
可靠的，因为它由本地采集动作定义，不依赖上游给不给、给得准不准。

## 为什么这里用 SQLite，而全仓其余地方用 JSON/JSONL

这是系统里**唯一需要查询**的数据：按板块、按标的、按时间窗、按 first_seen 回查，
而且会长期累积。JSONL 要回答「这个板块过去一周的舆情热度曲线」只能全量扫，且扫的量
每天都在涨。台账用 JSONL 是因为它纯追加、按月分片、每次统计本来就要全读——那是另一种
访问模式。

`sqlite3` 在标准库里，所以这不引入依赖。

## 失败契约

**任何一步失败都不抛异常，只返回计数并记 note。** 和 `NewsCache` 同一条约定：调用方
此时已经拿到了实时文档，语料库写不进去是分析能力的损失，绝不能变成链路中断。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ashare_decision_support.news.base import NewsDoc
from ashare_decision_support.news.dedup import dedup_key, parse_published
from ashare_decision_support.state.paths import state_path

# 语料库的 schema 版本。和 `sectors/taxonomy.py` 的缓存戳同一个理由——结构变了之后，
# 老库里按旧口径写的行会静默参与新口径的统计。版本不匹配时建新表而不是猜。
SCHEMA_VERSION = 1

DEFAULT_RELATIVE_PATH = ("news", "corpus.sqlite3")

# 热度曲线的默认分桶。1 小时是盘中问「这个板块最近在被说什么」的自然粒度；
# 更细会被单条消息的多源转发主导，更粗看不出盘中的起量。
DEFAULT_BUCKET_HOURS = 1


@dataclass(frozen=True)
class StoredDoc:
    """语料库里的一条，比 `NewsDoc` 多出三个只有持久化才能给的字段。"""

    key: str
    title: str
    summary: str
    source: str
    published_at: str
    url: str
    # 这三个是这个模块存在的理由。
    first_seen_at: datetime
    last_seen_at: datetime
    seen_count: int
    # 跨源覆盖数。一条被 4 个源同时转的消息和只有 1 个源提的消息不是一回事，
    # 而去重之后这个信息在 `NewsDoc` 里就没了。
    source_count: int
    sectors: tuple[str, ...] = ()
    direction: str | None = None
    event_type: str | None = None

    def age_hours(self, now: datetime) -> float:
        """距**第一次被看到**多久，不是距发布多久。"""
        return round(max(0.0, (now - self.first_seen_at).total_seconds() / 3600.0), 2)

    def is_fresh(self, now: datetime, within_hours: float = 1.0) -> bool:
        return self.age_hours(now) <= within_hours

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "source": self.source,
            "published_at": self.published_at,
            "first_seen_at": self.first_seen_at.isoformat(timespec="seconds"),
            "last_seen_at": self.last_seen_at.isoformat(timespec="seconds"),
            "seen_count": self.seen_count,
            "source_count": self.source_count,
            "sectors": list(self.sectors),
            "direction": self.direction,
            "event_type": self.event_type,
        }


@dataclass
class IngestResult:
    """一次入库的结果。`ok=False` 时其余计数无意义，但链路照跑。"""

    ok: bool = True
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.inserted + self.updated


@dataclass(frozen=True)
class HeatPoint:
    bucket_start: datetime
    fresh_docs: int
    sources: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    key            TEXT PRIMARY KEY,
    title          TEXT NOT NULL DEFAULT '',
    summary        TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT '',
    published_at   TEXT NOT NULL DEFAULT '',
    url            TEXT NOT NULL DEFAULT '',
    trade_date     TEXT NOT NULL DEFAULT '',
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    seen_count     INTEGER NOT NULL DEFAULT 1,
    direction      TEXT,
    event_type     TEXT,
    mechanism      TEXT
);
CREATE INDEX IF NOT EXISTS idx_docs_first_seen ON docs(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_docs_trade_date ON docs(trade_date);

-- 一条消息可以归到多个板块，也可以一个都不归（模型弃权），所以拆表而不是塞字段。
CREATE TABLE IF NOT EXISTS doc_sectors (
    key     TEXT NOT NULL,
    sector  TEXT NOT NULL,
    PRIMARY KEY (key, sector)
);
CREATE INDEX IF NOT EXISTS idx_doc_sectors_sector ON doc_sectors(sector);

-- 同一条消息被哪些源带过。跨源覆盖数要从这里数，不能在 docs 上累加——
-- 同一个源重复推送会把它刷高，而那不是「传播广」。
CREATE TABLE IF NOT EXISTS doc_sources (
    key     TEXT NOT NULL,
    source  TEXT NOT NULL,
    PRIMARY KEY (key, source)
);

CREATE TABLE IF NOT EXISTS doc_symbols (
    key     TEXT NOT NULL,
    symbol  TEXT NOT NULL,
    PRIMARY KEY (key, symbol)
);
CREATE INDEX IF NOT EXISTS idx_doc_symbols_symbol ON doc_symbols(symbol);

CREATE TABLE IF NOT EXISTS meta (
    name   TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""


class NewsStore:
    """持久语料库。构造与所有方法都不抛异常。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else state_path(*DEFAULT_RELATIVE_PATH)
        self.ok = False
        self.error: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
                self._check_schema(conn)
            self.ok = True
        except Exception as exc:  # sqlite3.Error, OSError, ...
            self.error = f"{type(exc).__name__}: {exc}"

    # ---- 写 --------------------------------------------------------------

    def ingest(
        self,
        docs: list[NewsDoc],
        trade_date: date | None = None,
        now: datetime | None = None,
    ) -> IngestResult:
        """把一批文档并入语料库，已存在的只更新「又见到一次」。

        去重键复用 `dedup.dedup_key`——**必须和批内去重用同一个函数**。两边口径
        一旦分叉，同一条消息会在批内被合并、在库里被当成新的，`first_seen_at`
        就变成了「最近一次换标题的时间」，这个字段的全部意义都没了。
        """
        result = IngestResult()
        if not self.ok:
            result.ok = False
            result.notes.append(f"语料库不可用（{self.error}），本次舆情未入库，不影响本轮分析")
            return result
        if not docs:
            return result

        stamp = (now or datetime.now()).replace(microsecond=0)
        day = str(trade_date) if trade_date else ""

        try:
            with self._connect() as conn:
                for doc in docs:
                    key = dedup_key(doc)
                    if not key:
                        result.skipped += 1
                        continue
                    if self._upsert(conn, key, doc, day, stamp):
                        result.inserted += 1
                    else:
                        result.updated += 1
        except Exception as exc:
            result.ok = False
            result.notes.append(f"舆情入库失败（{type(exc).__name__}），不影响本轮分析")
            return result

        if result.inserted or result.updated:
            result.notes.append(
                f"舆情入库：新增 {result.inserted} 条、重复 {result.updated} 条"
                f"（重复的只更新 last_seen，first_seen 保持第一次见到的时刻）"
            )
        return result

    def label(
        self,
        doc: NewsDoc | str,
        sectors: list[str] | None = None,
        direction: str | None = None,
        event_type: str | None = None,
        mechanism: str | None = None,
    ) -> bool:
        """写入模型归因结果。标签可以后补，也可以整批重写。

        分开成一个方法而不是塞进 `ingest`，是因为归因发生在采集之后：`morning`
        先把文档入库，再送给 `llm_classifier` 归因，归因还可能整批作废退回词典。
        入库不该等归因，否则归因一失败连原文都没留下——而原文正是事后核对
        「当时到底看到了什么」的唯一依据。
        """
        if not self.ok:
            return False
        key = doc if isinstance(doc, str) else dedup_key(doc)
        if not key:
            return False
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE docs SET direction=COALESCE(?,direction), "
                    "event_type=COALESCE(?,event_type), mechanism=COALESCE(?,mechanism) "
                    "WHERE key=?",
                    (direction, event_type, mechanism, key),
                )
                for sector in sectors or []:
                    if sector:
                        conn.execute(
                            "INSERT OR IGNORE INTO doc_sectors(key, sector) VALUES (?, ?)",
                            (key, sector),
                        )
        except Exception:
            return False
        return True

    # ---- 读 --------------------------------------------------------------

    def recent(
        self,
        within_hours: float = 24.0,
        sector: str | None = None,
        symbol: str | None = None,
        now: datetime | None = None,
        limit: int = 50,
        by: str = "first_seen",
    ) -> list[StoredDoc]:
        """最近的文档，按 `first_seen_at` 倒序（新故事在最上面）。

        `by` 选的是**窗口按哪个时钟切**，两个问题不一样，都得答得了：

        - `first_seen`（默认）——「有什么**新**消息」。三天前进来、今天还在转的
          那条不算新，落在窗口外。
        - `last_seen`——「现在还在被说的是什么」。上面那条会回来，而且它的
          `age_hours` 会如实报出 72 小时，不会伪装成刚出现。

        默认选 first_seen 是因为盘前盘中问得最多的是前者；但板块研判要的往往是
        后者——一个两天前爆出、至今仍在发酵的故事，正是现在推动板块的那个，把它
        滤掉会让「这个板块最近什么消息」答出一片空白。
        """
        if not self.ok:
            return []
        column = "last_seen_at" if by == "last_seen" else "first_seen_at"
        stamp = now or datetime.now()
        floor = (stamp - timedelta(hours=within_hours)).isoformat(timespec="seconds")

        sql = ["SELECT d.* FROM docs d"]
        params: list[Any] = []
        if sector:
            sql.append("JOIN doc_sectors s ON s.key = d.key AND s.sector = ?")
            params.append(sector)
        if symbol:
            sql.append("JOIN doc_symbols y ON y.key = d.key AND y.symbol = ?")
            params.append(symbol)
        sql.append(f"WHERE d.{column} >= ? ORDER BY d.first_seen_at DESC LIMIT ?")
        params.extend([floor, max(1, int(limit))])

        try:
            with self._connect() as conn:
                rows = conn.execute(" ".join(sql), params).fetchall()
                return [self._hydrate(conn, row) for row in rows]
        except Exception:
            return []

    def sector_heat(
        self,
        sector: str,
        within_hours: float = 24.0,
        bucket_hours: int = DEFAULT_BUCKET_HOURS,
        now: datetime | None = None,
    ) -> list[HeatPoint]:
        """板块热度时间序列：每个时间桶里**第一次出现**的文档数。

        数 first_seen 而不是数全部提及，理由和 `dedup.py` 丢重复是同一条——
        一条消息被四个源转发不代表热度翻四倍，那只是同一个故事。热度要测的是
        **有多少新故事进来**。跨源覆盖数单独作为第二列给出，它测的是另一件事：
        这批新故事传播得有多广。
        """
        if not self.ok:
            return []
        stamp = now or datetime.now()
        bucket = max(1, int(bucket_hours))
        floor = stamp - timedelta(hours=within_hours)

        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT d.first_seen_at, d.key FROM docs d "
                    "JOIN doc_sectors s ON s.key = d.key AND s.sector = ? "
                    "WHERE d.first_seen_at >= ? ORDER BY d.first_seen_at",
                    (sector, floor.isoformat(timespec="seconds")),
                ).fetchall()
                if not rows:
                    return []
                sources = dict(conn.execute(
                        "SELECT key, COUNT(*) FROM doc_sources GROUP BY key"
                    ).fetchall())
        except Exception:
            return []

        buckets: dict[datetime, list[str]] = {}
        for raw_stamp, key in rows:
            seen = _parse_iso(raw_stamp)
            if seen is None:
                continue
            start = seen.replace(minute=0, second=0, microsecond=0)
            start = start.replace(hour=(start.hour // bucket) * bucket)
            buckets.setdefault(start, []).append(key)

        return [
            HeatPoint(
                bucket_start=start,
                fresh_docs=len(keys),
                sources=sum(sources.get(key, 1) for key in keys),
            )
            for start, keys in sorted(buckets.items())
        ]

    def first_seen(self, doc: NewsDoc | str) -> datetime | None:
        """这条消息第一次被看到是什么时候。库里没有就是 None（即「全新」）。"""
        if not self.ok:
            return None
        key = doc if isinstance(doc, str) else dedup_key(doc)
        if not key:
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT first_seen_at FROM docs WHERE key = ?", (key,)
                ).fetchone()
        except Exception:
            return None
        return _parse_iso(row[0]) if row else None

    def annotate_freshness(
        self, docs: list[NewsDoc], now: datetime | None = None
    ) -> tuple[list[NewsDoc], list[str]]:
        """标出哪些是全新的、哪些已经在库里躺了一阵。不改权重，只回报。

        刻意不动 `NewsDoc.weight`：时效折权归 `dedup.py` 按发布时间算，两套折减
        叠在一起会把同一件事扣两遍，而且事后拆不开是哪一层扣的。这里只产出给人看
        的读数——要不要因为「这条已经播了三天」而回避，是人的判断。
        """
        if not self.ok or not docs:
            return docs, []
        stamp = now or datetime.now()
        brand_new = 0
        stale = 0
        for doc in docs:
            seen = self.first_seen(doc)
            if seen is None:
                brand_new += 1
            elif (stamp - seen).total_seconds() > 24 * 3600:
                stale += 1

        notes: list[str] = []
        if brand_new:
            notes.append(f"{brand_new} 条为语料库中首次出现")
        if stale:
            notes.append(f"{stale} 条已在语料库中超过 24 小时，属重复播报而非新增信息")
        return docs, notes

    def stats(self) -> dict[str, Any]:
        if not self.ok:
            return {"ok": False, "error": self.error, "path": str(self.path)}
        try:
            with self._connect() as conn:
                docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
                sectors = conn.execute(
                    "SELECT COUNT(DISTINCT sector) FROM doc_sectors"
                ).fetchone()[0]
                span = conn.execute(
                    "SELECT MIN(first_seen_at), MAX(first_seen_at) FROM docs"
                ).fetchone()
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "path": str(self.path)}
        return {
            "ok": True,
            "path": str(self.path),
            "docs": docs,
            "sectors_labelled": sectors,
            "earliest": span[0],
            "latest": span[1],
        }

    # ---- 内部 ------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # WAL：守护进程在写的同时 `ask` / `chat` 会读，默认的 rollback journal
        # 会让读方拿到 "database is locked"。
        conn = sqlite3.connect(self.path, timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def _check_schema(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT value FROM meta WHERE name = 'schema'").fetchone()
        if row is None:
            conn.execute(
                "INSERT OR REPLACE INTO meta(name, value) VALUES ('schema', ?)",
                (str(SCHEMA_VERSION),),
            )
            return
        if row[0] != str(SCHEMA_VERSION):
            # 老库按旧口径写的行会静默参与新口径的统计。整库重建，不猜。
            for table in ("doc_sectors", "doc_sources", "doc_symbols", "docs"):
                conn.execute(f"DELETE FROM {table}")
            conn.execute(
                "INSERT OR REPLACE INTO meta(name, value) VALUES ('schema', ?)",
                (str(SCHEMA_VERSION),),
            )

    def _upsert(
        self,
        conn: sqlite3.Connection,
        key: str,
        doc: NewsDoc,
        trade_date: str,
        stamp: datetime,
    ) -> bool:
        """返回 True 表示这是新插入的一条。"""
        iso = stamp.isoformat(timespec="seconds")
        cursor = conn.execute(
            "INSERT INTO docs(key, title, summary, source, published_at, url, trade_date,"
            " first_seen_at, last_seen_at, seen_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)"
            " ON CONFLICT(key) DO UPDATE SET"
            "   last_seen_at = excluded.last_seen_at,"
            "   seen_count = docs.seen_count + 1,"
            # 摘要与 URL 只在原来为空时补，绝不覆盖——先到的那条通常是原发，
            # 后到的转发常常把正文砍成一句话。
            "   summary = CASE WHEN docs.summary = '' THEN excluded.summary ELSE docs.summary END,"
            "   url = CASE WHEN docs.url = '' THEN excluded.url ELSE docs.url END",
            (
                key,
                doc.title or "",
                doc.summary or "",
                doc.source or "",
                doc.published_at or "",
                doc.url or "",
                trade_date,
                iso,
                iso,
            ),
        )
        if doc.source:
            conn.execute(
                "INSERT OR IGNORE INTO doc_sources(key, source) VALUES (?, ?)",
                (key, doc.source),
            )
        for symbol in doc.symbols or ():
            conn.execute(
                "INSERT OR IGNORE INTO doc_symbols(key, symbol) VALUES (?, ?)", (key, symbol)
            )
        # rowcount == 1 且 first_seen == last_seen 才是真新插入；ON CONFLICT 分支
        # 同样报 rowcount 1，所以不能只看它。
        return bool(cursor.rowcount) and self._is_new(conn, key, iso)

    @staticmethod
    def _is_new(conn: sqlite3.Connection, key: str, iso: str) -> bool:
        row = conn.execute(
            "SELECT first_seen_at, seen_count FROM docs WHERE key = ?", (key,)
        ).fetchone()
        return bool(row) and row[0] == iso and row[1] == 1

    @staticmethod
    def _hydrate(conn: sqlite3.Connection, row: tuple) -> StoredDoc:
        key = row[0]
        sectors = tuple(
            item[0]
            for item in conn.execute(
                "SELECT sector FROM doc_sectors WHERE key = ? ORDER BY sector", (key,)
            ).fetchall()
        )
        source_count = conn.execute(
            "SELECT COUNT(*) FROM doc_sources WHERE key = ?", (key,)
        ).fetchone()[0]
        return StoredDoc(
            key=key,
            title=row[1],
            summary=row[2],
            source=row[3],
            published_at=row[4],
            url=row[5],
            first_seen_at=_parse_iso(row[7]) or datetime.min,
            last_seen_at=_parse_iso(row[8]) or datetime.min,
            seen_count=row[9],
            source_count=max(1, source_count),
            sectors=sectors,
            direction=row[10],
            event_type=row[11],
        )


def _parse_iso(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        # 库里存的一律是 isoformat，走到这里说明有人手改过或版本迁移出了岔子。
        # 退回通用解析器，它认得各源那些花样格式。
        return parse_published(str(value))
