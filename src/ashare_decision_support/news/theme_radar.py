"""Evidence-first radar for themes that may precede sector rotation.

The radar is intentionally a discovery layer.  It never changes sector scores
or candidate selection: social mentions can nominate a theme, while a topic
needs independent news evidence and observable sector breadth before it is
labelled ``价格确认观察``.  This prevents a viral post from being presented as
an investable forecast.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ashare_decision_support.news.base import NewsDoc
from ashare_decision_support.news.events import detect_events
from ashare_decision_support.sectors.base import SectorMetrics
from ashare_decision_support.state.store import read_json


@dataclass(frozen=True)
class ThemeDefinition:
    name: str
    terms: tuple[str, ...]
    sectors: tuple[str, ...]
    transmission: str
    invalidation: str


THEMES: tuple[ThemeDefinition, ...] = (
    ThemeDefinition("AI高速互连", ("高速PCB", "高多层板", "800G", "1.6T", "交换机", "CPO"), ("电子", "通信", "计算机"), "交换机与AI集群扩建传导至高端PCB和光互连", "PCB、通信与计算机无法形成至少两条价格确认"),
    ThemeDefinition("PCB关键材料", ("电子布", "电子级玻纤", "覆铜板", "低损耗树脂", "铜箔", "mSAP"), ("电子", "基础化工"), "高速板升级传导至玻纤、树脂、覆铜板和铜箔", "只有概念股异动，材料报价与板块广度没有跟随"),
    ThemeDefinition("光芯片材料", ("磷化铟", "薄膜铌酸锂", "光芯片", "硅光", "光模块"), ("电子", "通信"), "高速光模块升级传导至InP、调制器和光器件", "光通信下游走弱或材料消息无法被产业新闻交叉验证"),
    ThemeDefinition("先进封装与设备", ("先进封装", "2.5D", "3D封装", "CoWoS", "ABF", "类载板", "半导体设备"), ("电子", "机械设备"), "算力芯片迭代带动封装、载板和关键设备投入", "只有单一公告，电子和机械设备均无趋势确认"),
    ThemeDefinition("战略金属", ("稀土", "钨", "钨精矿", "锗", "镓", "锑", "出口管制"), ("有色金属", "国防军工"), "供给政策、库存与战略需求共同影响资源定价", "没有现货报价或政策原文验证，仅有情绪转述"),
    ThemeDefinition("铜供给扰动", ("铜精矿", "精炼铜", "COMEX", "铜库存", "现货升水", "铜关税"), ("有色金属", "电力设备"), "库存迁移、供给扰动与电网/算力需求影响铜价和资源股", "近远月结构、库存和关税结果未能验证，或价格冲高回落"),
    ThemeDefinition("创新药与CRO", ("创新药", "BD授权", "临床数据", "CRO", "CXO", "药明康德"), ("医药生物",), "研发投入、授权交易与订单改善传导至医药服务", "订单/盈利兑现缺失，或政策与海外客户风险恶化"),
    ThemeDefinition("算力芯片与存储", ("GPU", "算力芯片", "HBM", "存储芯片", "服务器", "液冷"), ("电子", "计算机", "机械设备"), "AI资本开支传导至芯片、存储、服务器和温控", "下游资本开支或芯片订单不及预期"),
    ThemeDefinition("电网与储能", ("特高压", "配电网", "电网投资", "储能", "电力现货", "数据中心电力"), ("电力设备", "公用事业"), "算力和新能源负荷提升电网、储能及电力运营需求", "电网订单、招标或板块广度转弱"),
    ThemeDefinition("机器人", ("人形机器人", "减速器", "滚珠丝杠", "机器人", "伺服"), ("机械设备", "电子", "汽车"), "产业化和零部件国产化带来订单预期", "只有概念传播，没有订单或量价确认"),
    ThemeDefinition("低空与商业航天", ("低空经济", "eVTOL", "飞行汽车", "无人机", "商业航天", "卫星互联网"), ("国防军工", "汽车", "机械设备"), "政策试点、订单和基础设施建设驱动新应用", "政策落地慢或订单兑现不足"),
    ThemeDefinition("核电与核聚变", ("核电", "核聚变", "铀", "核燃料", "核岛"), ("公用事业", "机械设备", "有色金属"), "能源安全和设备国产化带动长期订单", "项目核准与订单节奏低于预期"),
    ThemeDefinition("锂电材料", ("固态电池", "六氟磷酸锂", "电解液", "锂电池", "锂矿"), ("电力设备", "基础化工", "有色金属"), "产品价格、技术迭代和新能源需求共同作用", "产品报价下行或产能过剩重新定价"),
    ThemeDefinition("消费与家电更新", ("以旧换新", "消费券", "家电", "智能家居", "服务消费"), ("家用电器", "商贸零售", "食品饮料"), "政策补贴和内需改善传导至可选消费", "补贴退坡或终端销售不及预期"),
    ThemeDefinition("航运与运价", ("集运", "运价", "航运", "海峡封锁", "港口拥堵"), ("交通运输", "石油石化"), "航线扰动和运力约束改变运价与油运利润", "扰动解除或运价快速回落"),
)

ENV_THEME_CATALOG_PATH = "ASHARE_DS_THEME_CATALOG_PATH"


def load_themes(path: str | Path | None = None) -> tuple[ThemeDefinition, ...]:
    """Load optional user themes while retaining the built-in audited catalog.

    The file is an array of objects with ``name``, ``terms``, ``sectors``,
    ``transmission`` and ``invalidation``. Invalid rows are ignored and never
    make the morning run fail.
    """
    raw_path = path or os.environ.get(ENV_THEME_CATALOG_PATH, "").strip()
    if not raw_path:
        return THEMES
    payload = read_json(Path(raw_path), default=[])
    rows = payload.get("themes", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return THEMES
    custom: list[ThemeDefinition] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        terms = tuple(str(item).strip() for item in row.get("terms", []) if str(item).strip())
        sectors = tuple(str(item).strip() for item in row.get("sectors", []) if str(item).strip())
        if not name or not terms or not sectors:
            continue
        custom.append(ThemeDefinition(
            name=name,
            terms=terms,
            sectors=sectors,
            transmission=str(row.get("transmission") or "待补充传导链").strip(),
            invalidation=str(row.get("invalidation") or "待补充失效条件").strip(),
        ))
    known = {item.name for item in THEMES}
    return THEMES + tuple(item for item in custom if item.name not in known)


@dataclass(frozen=True)
class ThemeSignal:
    name: str
    stage: str
    verified_docs: int
    social_docs: int
    price_confirmed: bool
    sectors: tuple[str, ...]
    event_types: tuple[str, ...]
    transmission: str
    invalidation: str
    headlines: tuple[str, ...]
    evidence: tuple[dict[str, str | float | int | None], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "stage": self.stage,
            "verified_docs": self.verified_docs,
            "social_docs": self.social_docs,
            "price_confirmed": self.price_confirmed,
            "sectors": list(self.sectors),
            "event_types": list(self.event_types),
            "transmission": self.transmission,
            "invalidation": self.invalidation,
            "headlines": list(self.headlines),
            "evidence": [dict(item) for item in self.evidence],
        }


def scan_themes(
    verified_docs: list[NewsDoc], social_docs: list[NewsDoc], metrics: dict[str, SectorMetrics],
    themes: tuple[ThemeDefinition, ...] | None = None,
) -> list[ThemeSignal]:
    """Return only evidenced themes, ordered by confirmation then evidence."""
    signals: list[ThemeSignal] = []
    for theme in themes or load_themes():
        verified = _matching(verified_docs, theme.terms)
        social = _matching(social_docs, theme.terms)
        if not verified and not social:
            continue
        price_confirmed = _price_confirmed(theme.sectors, metrics)
        if verified and price_confirmed:
            stage = "价格确认观察"
        elif verified:
            stage = "事件预警，等待价格确认"
        else:
            stage = "社媒线索，等待事实验证"
        evidence = detect_events(doc.text for doc in verified)
        evidence_docs = [*verified, *social][:5]
        signals.append(ThemeSignal(
            name=theme.name,
            stage=stage,
            verified_docs=len(verified),
            social_docs=len(social),
            price_confirmed=price_confirmed,
            sectors=theme.sectors,
            event_types=evidence.event_types,
            transmission=theme.transmission,
            invalidation=theme.invalidation,
            headlines=tuple(doc.title for doc in evidence_docs[:3]),
            evidence=tuple(
                {
                    "title": doc.title,
                    "source": doc.source,
                    "url": doc.url or None,
                    "published_at": doc.published_at or None,
                    "engagement": doc.engagement,
                    "heat_rank": doc.heat_rank,
                }
                for doc in evidence_docs
            ),
        ))
    order = {"价格确认观察": 0, "事件预警，等待价格确认": 1, "社媒线索，等待事实验证": 2}
    return sorted(signals, key=lambda item: (order[item.stage], -item.verified_docs, -item.social_docs))


def _matching(docs: list[NewsDoc], terms: tuple[str, ...]) -> list[NewsDoc]:
    return [doc for doc in docs if any(term in doc.text for term in terms)]


def _price_confirmed(sectors: tuple[str, ...], metrics: dict[str, SectorMetrics]) -> bool:
    confirmations = 0
    for name in sectors:
        metric = metrics.get(name)
        if metric is None:
            continue
        strong = (metric.ret_5d is not None and metric.ret_5d >= 0.015) or (
            metric.breadth is not None and metric.breadth >= 0.62
        )
        confirmations += int(strong)
    return confirmations >= (2 if len(sectors) >= 2 else 1)
