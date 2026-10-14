"""将已发生的外盘价格变化映射到 A 股板块，作为研究旁证。

本模块从实际价格和品种映射取得方向；news/macro_links.py 从新闻文本补充政策、地缘等线索。
两类输入保持各自来源，不把文本方向当成已验证的价格变化。

输出不预测 A 股未来方向，也不直接进入评分。quiet 与 missing 分别表示平静与缺失。
异动阈值按品种的波动量级设定，目前属于先验假设，需要独立样本验证。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BULLISH = "bullish"
BEARISH = "bearish"

STRENGTH_NOTABLE = "notable"
STRENGTH_STRONG = "strong"

# 品种的异动档位：(值得一提, 强信号)，单位是绝对涨跌幅百分比。

# 分档依据是**该品种自身的日常波动量级**，不是它有多重要。宽基指数 1% 就算大，
# 主题 ETF 2% 才刚够看——把它们放在同一条线上，结果是主题 ETF 天天触发、宽基
# 永不触发，也就是这张表只对其中一半品种有效。
DEFAULT_THRESHOLD = (1.5, 3.0)
THRESHOLDS: dict[str, tuple[float, float]] = {
    # 宽基：波动小，1% 已是大事
    "sp500": (1.0, 2.0),
    "dow": (1.0, 2.0),
    "nasdaq": (1.0, 2.0),
    # 高波动指数与行业 ETF
    "sox": (1.5, 3.0),
    "kospi": (1.5, 3.0),
    "kosdaq": (1.5, 3.0),
    "taiex": (1.5, 3.0),
    "nikkei": (1.5, 3.0),
    "smh": (1.5, 3.0),
    "xlk": (1.5, 3.0),
    "xlv": (1.5, 3.0),
    "xle": (1.5, 3.0),
    "xop": (2.0, 4.0),
    "xlb": (1.5, 3.0),
    "jets": (2.0, 4.0),
    "moo": (1.5, 3.0),
    # 主题 ETF：这一组本来就是高 beta，2% 才刚够看
    "tan": (2.0, 4.0),
    "lit": (2.0, 4.0),
    "botz": (2.0, 4.0),
    "ura": (2.0, 4.0),
    "copx": (2.0, 4.0),
    "gdx": (2.0, 4.0),
    "xme": (2.0, 4.0),
    "ibb": (2.0, 4.0),
    # 商品
    "wti": (2.0, 4.0),
    "brent": (2.0, 4.0),
    "natgas": (3.0, 6.0),
    "gold": (1.0, 2.0),
    "silver": (2.0, 4.0),
    "copper": (1.5, 3.0),
    "lme_copper": (1.5, 3.0),
    "lme_alu": (1.5, 3.0),
    "lme_zinc": (1.5, 3.0),
    "lme_nickel": (2.0, 4.0),
    "lme_tin": (2.0, 4.0),
    "lme_lead": (1.5, 3.0),
    "corn": (2.0, 4.0),
    "wheat": (2.0, 4.0),
    "cotton": (2.0, 4.0),
    # 宏观期指与流动性指标
    "a50": (1.0, 2.0),
    "nq_future": (1.0, 2.0),
    "es_future": (1.0, 2.0),
    "hsi": (1.0, 2.0),
    "hscei": (1.0, 2.0),
    "usdcnh": (0.3, 0.6),
    "dxy": (0.3, 0.6),
}


@dataclass(frozen=True)
class Mapping:
    """一条「外盘品种 → A 股板块」的映射。

    `sign` 为 −1 表示**反向**：油价涨对石油石化是利好、对航空是成本，同一个读数
    在两个板块上的方向相反。用一个符号位表达，比为反向关系另建一张表好——两张表
    会各自演化，而它们说的是同一件事。

    `targets` 里的每一个名字都必须在同花顺行业或概念中真实存在。
    写错一个字不会报错，只会让这条映射静默地永远匹配不上任何板块——
    `tests/test_overseas_link.py` 有一条断言把整张表对着实际分类校一遍。
    """

    instrument: str
    targets: tuple[str, ...]
    sign: int
    mechanism: str


# 映射表。**每一条都要能回答「它动了，A 股哪个板块该被多看一眼，凭什么」。**

# 收进来的标准是「说得出机制」，不是「相关系数高」——后者这套系统还没有数据算，
# 而前者是可以被人读、被人反驳的。**但反驳目前只能靠人读**：按 `instrument` 分组
# 统计兑现率的那本账（原注释写的 `journal/catalyst.py`）没有建过，说不通的那几条
# 不会自己浮出来，得有人来删。
MAPPINGS: tuple[Mapping, ...] = (
    # 半导体链：外盘映射里最硬的一段
    Mapping(
        "sox",
        ("半导体", "消费电子", "芯片概念", "存储芯片", "第三代半导体"),
        1,
        "费城半导体指数是全球半导体景气与估值的定价锚，A股半导体的隔夜参照系",
    ),
    Mapping("smh", ("半导体", "芯片概念", "存储芯片"), 1, "美股半导体ETF，与费半同源但含个股权重"),
    Mapping(
        "kospi",
        ("半导体", "存储芯片", "消费电子"),
        1,
        "韩股权重高度集中在三星与SK海力士，是存储周期最直接的亚洲时段读数",
    ),
    Mapping("kosdaq", ("半导体", "元件", "光学光电子"), 1, "韩国中小盘科技，偏电子零部件与面板链"),
    Mapping(
        "taiex",
        ("半导体", "消费电子", "芯片概念"),
        1,
        "台积电在台湾加权里权重极高；台指收在北京13:30，是A股当日午后就能看到的半导体读数",
    ),
    Mapping(
        "xlk",
        ("软件开发", "计算机设备", "半导体", "东数西算(算力)", "算力租赁"),
        1,
        "美股科技板块整体风险偏好，映射A股算力与计算机估值",
    ),
    # 主题 ETF：对得上 A 股概念板块的那一组
    Mapping(
        "tan",
        ("光伏设备", "光伏概念", "HJT电池", "TOPCON电池"),
        1,
        "全球光伏需求与组件价格预期，A股光伏链的同步读数",
    ),
    Mapping(
        "lit",
        ("电池", "锂电池概念", "能源金属", "盐湖提锂"),
        1,
        "锂价与锂电产业链指数，映射A股锂矿与电池环节",
    ),
    Mapping(
        "botz",
        ("机器人概念", "人形机器人", "减速器", "自动化设备"),
        1,
        "全球机器人与自动化主题资金，A股机器人概念的情绪参照",
    ),
    Mapping("ura", ("核电", "可控核聚变", "电力"), 1, "铀价与核电扩张预期"),
    Mapping(
        "ibb",
        ("生物制品", "创新药", "化学制药"),
        1,
        "美股生物科技风险偏好，创新药出海与License-out估值的外部锚",
    ),
    Mapping("xlv", ("化学制药", "生物制品", "医疗服务", "医疗器械"), 1, "美股医疗保健板块整体"),
    # 有色与资源：LME 那一组比 COMEX 更贴 A 股定价
    Mapping("copx", ("工业金属", "金属铜"), 1, "全球铜矿股，反映铜供给与矿端资本开支"),
    Mapping("lme_copper", ("工业金属", "金属铜"), 1, "伦铜是国内铜定价基准，直接映射A股铜企"),
    Mapping("lme_alu", ("工业金属",), 1, "伦铝映射电解铝盈利"),
    Mapping("lme_zinc", ("工业金属", "小金属概念"), 1, "伦锌"),
    Mapping("lme_lead", ("工业金属", "小金属概念"), 1, "伦铅"),
    Mapping(
        "lme_nickel",
        ("能源金属", "小金属概念", "钢铁"),
        1,
        "伦镍同时是三元电池正极原料与不锈钢原料，两条链都受影响",
    ),
    Mapping("lme_tin", ("小金属概念", "元件"), 1, "伦锡，电子焊料的主要原料"),
    Mapping("xme", ("小金属", "工业金属", "钢铁"), 1, "美股金属与采矿，资源品整体风险偏好"),
    Mapping(
        "xlb",
        ("化学原料", "化学制品"),
        1,
        "美股材料板块以化工与工业气体为主，反映全球工业原料需求",
    ),
    Mapping("gdx", ("贵金属", "黄金概念"), 1, "全球金矿股，对金价的高beta表达"),
    Mapping("gold", ("贵金属", "黄金概念"), 1, "金价直接映射A股黄金股盈利"),
    Mapping("silver", ("贵金属", "黄金概念"), 1, "银价，贵金属板块的次要驱动"),
    # 能源：同一个读数在上下游反向
    Mapping("wti", ("油气开采及服务", "石油加工贸易"), 1, "油价上行抬升上游开采与炼化盈利"),
    Mapping(
        "wti",
        ("机场航运", "化学原料", "化学制品"),
        -1,
        "油价是航空燃油与化工原料的成本端，方向相反",
    ),
    Mapping("brent", ("油气开采及服务", "石油加工贸易"), 1, "布伦特，欧亚定价基准"),
    Mapping("brent", ("机场航运",), -1, "航空燃油成本"),
    Mapping("xle", ("油气开采及服务", "石油加工贸易"), 1, "美股能源板块"),
    Mapping("xop", ("油气开采及服务", "石油加工贸易"), 1, "美股油气开采，比xle更偏上游"),
    Mapping("natgas", ("燃气",), 1, "天然气价格映射燃气运营商采购成本与售价联动"),
    Mapping("jets", ("机场航运",), 1, "美股航空，全球航空客运景气"),
    # 农产品
    Mapping("moo", ("种植业与林业", "养殖业", "农产品加工"), 1, "全球农业股"),
    Mapping("corn", ("种植业与林业", "农产品加工"), 1, "玉米是饲料主要成本，同时利好种植端"),
    Mapping("wheat", ("种植业与林业", "农产品加工"), 1, "小麦"),
    Mapping("cotton", ("纺织制造", "服装家纺"), -1, "棉价是纺织服装的原料成本端"),
)

# 有读数但**刻意不映射任何板块**的品种，以及不映射的理由。

# 写出来而不是留空：一个在品种表里、却不在映射表里的 key，读代码的人无从判断
# 那是「决定不映射」还是「漏了」。`tests/test_overseas_link.py` 断言两张表的并集
# 覆盖全部品种，所以将来往品种表加东西时，这里会强制你表态。
UNMAPPED: dict[str, str] = {
    "sp500": "宽基，映射的是整体风险偏好而不是某个板块",
    "dow": "宽基，同上",
    "nasdaq": "宽基；科技映射由 xlk 与 sox 承担，重复挂会让同一件事计三次",
    "fxi": "外资对中国资产的整体定价，不对应任何单一A股板块",
    "kweb": "中概互联，同上；映射到A股传媒/计算机是一条讲不清机制的联想",
    "copper": "COMEX铜与伦铜说同一件事，保留读数但映射走 lme_copper 一条，避免重复计数",
    "nikkei": "日经是宽基，权重分散在零售/金融/半导体设备等多条线；它的半导体含量已由台指与费半覆盖，再挂一条等于把同一件事计三次",
    "xlf": "A股银行与非银由净息差、地产风险与政策定价，与美股金融板块（美债利率与信用周期）几乎没有共同驱动——这条联想讲不出机制",
    "xli": "美股工业以航空航天与运输为主，A股机械设备由国内资本开支与专项债驱动，两者不是同一个周期",
    "usdcny": "汇率同时作用于出口链（利好）与成长股估值（利空），净方向取决于哪条占优——这套系统给不出这个判断，所以只报读数不给方向",
    "usdcnh": "离岸人民币汇率，映射外资流动性偏好与汇率预期，同 usdcny 只作宏观读数不给单向题材",
    "dxy": "美元指数，全球流动性与美元强弱标尺，作为宏观基准不挂单一A股板块",
    "a50": "富时中国A50期指，映射大盘蓝筹与外资次日集合竞价情绪，整体大势视角不挂单一板块",
    "nq_future": "纳指期货，映射海外科技成长风险偏好先行指标，宏观联动不挂单一板块",
    "es_future": "标普500期指，映射美股大盘隔夜风险偏好，宏观联动不挂单一板块",
    "hsi": "恒生指数，港股宽基，映射整体离岸中国资产风险偏好与互联互通情绪，不挂单一板块",
    "hscei": "恒生国企指数，中资核心资产离岸定价，整体视角不挂单一板块",
}


@dataclass(frozen=True)
class OverseasLink:
    """一条**已经触发**的映射。"""

    instrument: str
    instrument_name: str
    change_pct: float
    targets: tuple[str, ...]
    direction: str
    strength: str
    mechanism: str
    as_of: str | None = None

    @property
    def headline(self) -> str:
        arrow = "利多" if self.direction == BULLISH else "利空"
        tag = "强" if self.strength == STRENGTH_STRONG else ""
        return (
            f"{self.instrument_name} {self.change_pct:+.2f}% → "
            f"{'、'.join(self.targets)} {tag}{arrow}（{self.mechanism}）"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "instrument": self.instrument,
            "instrument_name": self.instrument_name,
            "change_pct": self.change_pct,
            "targets": list(self.targets),
            "direction": self.direction,
            "strength": self.strength,
            "mechanism": self.mechanism,
            "as_of": self.as_of,
        }


@dataclass
class OverseasRead:
    """一次外盘映射读数。"""

    links: list[OverseasLink] = field(default_factory=list)
    # 取到了读数但没到异动门槛的品种名。**和 missing 是两件事。**
    quiet: list[str] = field(default_factory=list)
    # 压根没取到读数的品种名。
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """有没有拿到过任何读数——**不是「有没有触发映射」**。

        隔夜全球平静是一个合法且常见的结论，那时 `links` 为空而 `ok` 为真。
        用 `links` 判 ok 会让「今晚没事」和「取数挂了」变成同一个状态。
        """
        return bool(self.links or self.quiet)

    def by_sector(self) -> dict[str, list[OverseasLink]]:
        """板块 → 指向它的全部链路。

        一个板块可以被多条链路同时指向（半导体同时有费半、SMH、KOSPI 三条），
        **而它们可以方向相反**——伦镍涨对钢铁是成本、对能源金属是利好。所以这里
        返回的是链路清单不是一个净方向：净方向要由读的人看着证据定，压成一个
        符号就把分歧本身丢掉了。
        """
        result: dict[str, list[OverseasLink]] = {}
        for link in self.links:
            for sector in link.targets:
                result.setdefault(sector, []).append(link)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "links": [item.to_dict() for item in self.links],
            "quiet": list(self.quiet),
            "missing": list(self.missing),
            "notes": list(self.notes),
        }


def read_overseas_links(
    snapshot: Any,
    mappings: tuple[Mapping, ...] = MAPPINGS,
    thresholds: dict[str, tuple[float, float]] | None = None,
) -> OverseasRead:
    """海外快照 → 触发的映射清单。纯计算，不碰网络。

    `snapshot` 是 `adapters/global_market.GlobalMarketSnapshot`，或任何带
    `readings`（每条有 `key` / `name` / `change_pct` / `as_of`）的对象。
    """
    read = OverseasRead()
    readings = {item.key: item for item in getattr(snapshot, "readings", [])}
    if not readings:
        read.notes.append("没有海外读数，本次无外盘映射")
        return read

    table = THRESHOLDS if thresholds is None else thresholds
    seen_quiet: set[str] = set()
    seen_missing: set[str] = set()
    # 「不在这次快照里」和「取不到读数」是两件事，第三次分开。

    # `_fetch_sina` 对**每一个配置的品种**都必然产出一条读数（失败时带 note），
    # 所以一个 key 压根不在 `readings` 里，只可能是调用方用了一张更小的品种表——
    # 那是配置，不是故障。把它算进 `missing` 会让「我只关心费半」这种合法用法
    # 报出三十条「缺失」，而真正的取数故障就淹没在里面了。
    absent: set[str] = set()
    for mapping in mappings:
        reading = readings.get(mapping.instrument)
        if reading is None:
            absent.add(mapping.instrument)
            continue
        if reading.change_pct is None:
            if reading.name not in seen_missing:
                seen_missing.add(reading.name)
                read.missing.append(reading.name)
            continue

        notable, strong = table.get(mapping.instrument, DEFAULT_THRESHOLD)
        move = abs(reading.change_pct)
        if move < notable:
            if reading.name not in seen_quiet:
                seen_quiet.add(reading.name)
                read.quiet.append(reading.name)
            continue

        read.links.append(
            OverseasLink(
                instrument=mapping.instrument,
                instrument_name=reading.name,
                change_pct=reading.change_pct,
                targets=mapping.targets,
                direction=BULLISH if reading.change_pct * mapping.sign > 0 else BEARISH,
                strength=STRENGTH_STRONG if move >= strong else STRENGTH_NOTABLE,
                mechanism=mapping.mechanism,
                as_of=reading.as_of,
            )
        )

    # 强信号排前面，同档按幅度。排序只影响呈现——「先看哪条」是这一层能给的最强
    # 表态，再往前一步（「所以买哪个」）就越界了。
    read.links.sort(
        key=lambda item: (item.strength == STRENGTH_STRONG, abs(item.change_pct)), reverse=True
    )
    read.notes.extend(_notes(read, absent))
    return read


def _notes(read: OverseasRead, absent: set[str]) -> list[str]:
    notes: list[str] = []
    if absent:
        notes.append(
            f"{len(absent)} 个品种不在本次快照的品种表里，对应映射未评估："
            + "、".join(sorted(absent)[:6])
            + ("等" if len(absent) > 6 else "")
        )
    if read.missing:
        notes.append(
            f"外盘映射缺 {len(read.missing)} 个品种的读数："
            + "、".join(read.missing[:6])
            + ("等" if len(read.missing) > 6 else "")
        )
    if not read.links:
        notes.append(
            f"{len(read.quiet)} 个品种取到读数但都没到异动门槛——**隔夜平静，不是没取到**"
            if read.quiet
            else "没有任何品种触发映射"
        )
        return notes

    strong = [item for item in read.links if item.strength == STRENGTH_STRONG]
    notes.append(
        f"外盘映射 {len(read.links)} 条（强信号 {len(strong)} 条），"
        f"另有 {len(read.quiet)} 个品种隔夜平静"
    )
    notes.append(
        "以上是对**已经发生的隔夜行情**的映射，不是对A股次日走势的预测——"
        "各条链路在A股的历史兑现率目前没有账本可查，读数只作提示，不作权重"
    )
    return notes
