"""SQLite 舆情语料库，按板块、标的和时间检索原文及归因。

first_seen_at 和 last_seen_at 记录本地首次及最近观察时间，与上游 published_at 分开。
重复传播只更新观察记录。存储失败通过计数、状态与 notes 报告，保留本轮已采集文档。
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
from ashare_decision_support.schedule.timezone import beijing_naive_now, to_beijing_naive
from ashare_decision_support.state.paths import state_path

# 语料库的 schema 版本。结构变更时避免旧字段静默参与新口径统计。
SCHEMA_VERSION = 1

DEFAULT_RELATIVE_PATH = ("news", "corpus.sqlite3")

# 热度曲线的默认分桶。1 小时是盘中问「这个板块最近在被说什么」的自然粒度；
# 更细会被单条消息的多源转发主导，更粗看不出盘中的起量。
DEFAULT_BUCKET_HOURS = 1

# 一次 `IN (...)` 里塞多少个 key。SQLite 的变量上限旧版是 999，取 500 留足余量——
# 这个数只影响分几批，不影响结果。
_KEY_QUERY_CHUNK = 500


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
        elapsed = (to_beijing_naive(now) - to_beijing_naive(self.first_seen_at)).total_seconds()
        return round(max(0.0, elapsed / 3600.0), 2)

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


@dataclass(frozen=True)
class DocAttribution:
    """一条消息的归因：属于哪几个板块，以及（模型给了的话）方向与事件类型。

    单独一个类型而不是三个并列的字典，是因为这三样**必须一起写**：一条消息的
    方向脱离它归属的板块就没有意义，而分开传的话调用方少传一个不会有任何症状。
    """

    sectors: tuple[str, ...] = ()
    direction: str | None = None
    event_type: str | None = None
    mechanism: str | None = None


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
        # 最近一次**读**失败的原因。构造失败记在 `error` 里，而读失败此前谁也不知道：
        # 每个读方法都是 `except Exception: return []`，于是一个锁住的、损坏的或
        # 权限不对的库，在调用方看来和「这个板块最近没有新消息」一模一样。
        # 而「没数据」与「读不到」在这套系统里从来不是同一件事。
        self.read_error: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
                self._check_schema(conn)
            self.ok = True
        except Exception as exc:  # sqlite3.Error, OSError, ...
            self.error = f"{type(exc).__name__}: {exc}"

    # 写

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

        stamp = to_beijing_naive(now or beijing_naive_now()).replace(microsecond=0)
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
        except Exception as exc:
            self._note_read_failure(exc)
            return False
        return True

    def label_many(self, labels: dict[str, DocAttribution]) -> int:
        """在一次事务中写入归因；失败整批回滚并返回 0。"""
        if not self.ok or not labels:
            return 0
        written = 0
        try:
            with self._connect() as conn:
                for key, item in labels.items():
                    if not key:
                        continue
                    conn.execute(
                        "UPDATE docs SET direction=COALESCE(?,direction), "
                        "event_type=COALESCE(?,event_type), mechanism=COALESCE(?,mechanism) "
                        "WHERE key=?",
                        (item.direction, item.event_type, item.mechanism, key),
                    )
                    conn.executemany(
                        "INSERT OR IGNORE INTO doc_sectors(key, sector) VALUES (?, ?)",
                        [(key, sector) for sector in item.sectors if sector],
                    )
                    written += 1
        except Exception as exc:
            self._note_read_failure(exc)
            return 0
        return written

    # 读

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
        stamp = to_beijing_naive(now or beijing_naive_now())
        floor = (stamp - timedelta(hours=within_hours)).isoformat(timespec="seconds")

        sql = ["SELECT d.* FROM docs d"]
        params: list[Any] = []
        if sector:
            sql.append("JOIN doc_sectors s ON s.key = d.key AND s.sector = ?")
            params.append(sector)
        if symbol:
            sql.append("JOIN doc_symbols y ON y.key = d.key AND y.symbol = ?")
            params.append(symbol)
        sql.append(
            f"WHERE d.{column} >= ? AND d.{column} <= ? ORDER BY d.first_seen_at DESC LIMIT ?"
        )
        params.extend([floor, stamp.isoformat(timespec="seconds"), max(1, int(limit))])

        try:
            with self._connect() as conn:
                rows = conn.execute(" ".join(sql), params).fetchall()
                return self._hydrate(conn, rows)
        except Exception as exc:
            self._note_read_failure(exc)
            return []

    def sector_heat(
        self,
        sector: str,
        within_hours: float = 24.0,
        bucket_hours: int = DEFAULT_BUCKET_HOURS,
        now: datetime | None = None,
    ) -> list[HeatPoint]:
        """截至 now 的新故事数及跨源覆盖数，按首次出现时间分桶。

        重复传播不增加故事数；历史回看排除观察时点之后的记录。
        """
        if not self.ok:
            return []
        stamp = to_beijing_naive(now or beijing_naive_now())
        bucket = max(1, int(bucket_hours))
        floor = stamp - timedelta(hours=within_hours)

        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT d.first_seen_at, d.key FROM docs d "
                    "JOIN doc_sectors s ON s.key = d.key AND s.sector = ? "
                    "WHERE d.first_seen_at >= ? AND d.first_seen_at <= ? "
                    "ORDER BY d.first_seen_at",
                    (
                        sector,
                        floor.isoformat(timespec="seconds"),
                        stamp.isoformat(timespec="seconds"),
                    ),
                ).fetchall()
                if not rows:
                    return []
                sources = self._source_counts(conn, [key for _, key in rows])
        except Exception as exc:
            self._note_read_failure(exc)
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
        """保留单条查询接口以兼容现有调用；未找到或读取失败返回 None，失败见 read_error。"""
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
        except Exception as exc:
            self._note_read_failure(exc)
            return None
        return _parse_iso(row[0]) if row else None

    def annotate_freshness(
        self, docs: list[NewsDoc], now: datetime | None = None
    ) -> tuple[list[NewsDoc], list[str]]:
        """批量读取首次出现时间，报告新增或重复播报，不改文档及其权重。

        读取失败只报告缺失，不能把无法核对的文档当作首次出现。
        """
        if not self.ok or not docs:
            return docs, []
        stamp = to_beijing_naive(now or beijing_naive_now())
        keys = [dedup_key(doc) for doc in docs]
        usable = [key for key in keys if key]
        seen_by_key: dict[str, datetime | None] = {}
        if usable:
            try:
                with self._connect() as conn:
                    seen_by_key = {
                        key: _parse_iso(value)
                        for key, value in _query_keys(
                            conn,
                            "SELECT key, first_seen_at FROM docs WHERE key IN ({})",
                            usable,
                        )
                    }
            except Exception as exc:
                self._note_read_failure(exc)
                return docs, [
                    f"舆情首次出现时间读取失败（{type(exc).__name__}），本次未判断新增或重复"
                ]
        brand_new = 0
        stale = 0
        for key in keys:
            seen = seen_by_key.get(key)
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
                sectors = conn.execute("SELECT COUNT(DISTINCT sector) FROM doc_sectors").fetchone()[
                    0
                ]
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

    # 内部

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

    def _note_read_failure(self, exc: Exception) -> None:
        """读失败记在实例上，让调用方能把它写进 notes。

        **仍然不抛。** 语料库是旁证，它读不到不该中断盘前跑批——但「读不到」必须
        说出口。调用方拿到的空列表，在此之前和「这个板块最近确实没有新故事」长得
        完全一样，而后者会被当成一条读数用进判断。
        """
        self.read_error = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _source_counts(conn: sqlite3.Connection, keys: list[str]) -> dict[str, int]:
        """只聚合本批文档的跨源覆盖数，避免扫描整个历史库。"""
        return dict(
            _query_keys(
                conn,
                "SELECT key, COUNT(*) FROM doc_sources WHERE key IN ({}) GROUP BY key",
                keys,
            )
        )

    @classmethod
    def _hydrate(cls, conn: sqlite3.Connection, rows: list[tuple]) -> list[StoredDoc]:
        keys = [row[0] for row in rows]
        sectors: dict[str, list[str]] = {}
        for key, sector in _query_keys(
            conn,
            "SELECT key, sector FROM doc_sectors WHERE key IN ({}) ORDER BY key, sector",
            keys,
        ):
            sectors.setdefault(key, []).append(sector)
        sources = cls._source_counts(conn, keys)
        return [
            StoredDoc(
                key=row[0],
                title=row[1],
                summary=row[2],
                source=row[3],
                published_at=row[4],
                url=row[5],
                first_seen_at=_parse_iso(row[7]) or datetime.min,
                last_seen_at=_parse_iso(row[8]) or datetime.min,
                seen_count=row[9],
                source_count=max(1, sources.get(row[0], 0)),
                sectors=tuple(sectors.get(row[0], ())),
                direction=row[10],
                event_type=row[11],
            )
            for row in rows
        ]


def _query_keys(conn: sqlite3.Connection, query: str, keys: list[str]) -> Iterator[tuple]:
    """对内部 SQL 模板分批绑定去重后的文档键，兼容 SQLite 的参数个数上限。"""
    unique = list(dict.fromkeys(keys))
    for start in range(0, len(unique), _KEY_QUERY_CHUNK):
        chunk = unique[start : start + _KEY_QUERY_CHUNK]
        yield from conn.execute(query.format(",".join("?" * len(chunk))), chunk)


def _parse_iso(value: Any) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return to_beijing_naive(parsed)
    except (TypeError, ValueError, OverflowError):
        # 库里存的一律是 isoformat，走到这里说明有人手改过或版本迁移出了岔子。
        # 退回通用解析器，它认得各源那些花样格式。
        return parse_published(str(value))
