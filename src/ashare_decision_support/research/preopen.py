"""外部 AI 的每日盘前研究输入包。

组织当前事实、外盘读数、历史基线和研究要求，方向判断留给调用者；历史读数保留样本边界。
与 monthly / leaders 共用研究格式，CLI 只负责参数、适配器构造、调用与落盘。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ashare_decision_support.adapters.global_market import (
    GROUP_COMMODITY,
    GROUP_FX,
    GROUP_INDEX,
    GROUP_US_SECTOR,
)
from ashare_decision_support.adapters.overseas_history import (
    INSTRUMENTS,
    daily_returns,
)
from ashare_decision_support.adapters.overseas_history import (
    load_all_series as load_overseas_series,
)
from ashare_decision_support.adapters.ths_sector_index import load_all_series as load_sector_series
from ashare_decision_support.facts import build_facts_bundle
from ashare_decision_support.research.format import names, percent, score
from ashare_decision_support.research.hypotheses import (
    build_sector_hypotheses,
)
from ashare_decision_support.schedule.calendar import TradingCalendar
from ashare_decision_support.schedule.timezone import BEIJING_TZ, beijing_now, to_beijing_naive
from ashare_decision_support.sectors.baserate import align_sessions, base_rate, build_panel
from ashare_decision_support.sectors.overseas_link import read_overseas_links

PREOPEN_RESEARCH_REQUIREMENTS = """基于 facts、overseas_snapshot 和 hypotheses 写盘前先手观察清单，不能补造数据。
依次判断：海外风险偏好与 A 股映射；政策/地缘/国家队；国内外财报与公告；昨日情绪与新题材。
输出不超过 700 字：总判断、1-3 条先手观察（催化→映射→竞价确认→失效）、只等确认、最多 3 条风险。
先手观察以 hypotheses.items 为底稿：可以删、可以反驳，但保留每条的 invalidate 条件，不另写一版。
每项证据标注已发生、市场预期或推演。没有竞价/开盘数据时，不写正在走强，也不写直接买卖建议。"""

# 海外读数在简报里按组折叠的顺序与标题。
OVERSEAS_GROUPS = (
    (GROUP_INDEX, "指数与风险偏好"),
    (GROUP_US_SECTOR, "美股行业与主题"),
    (GROUP_COMMODITY, "商品与期货"),
    (GROUP_FX, "汇率"),
)

# 每组里最多印几个**动得最厉害**的（两端各取）。

# 品种表铺到 44 个之后，平铺全部等于把「隔夜发生了什么」埋进四十行里——一份没人
# 读完的清单和没有这份清单，对判断的贡献是一样的。两端各取是因为**跌得最惨的那
# 几个和涨得最猛的那几个同样是信息**，只取涨幅榜会让「昨夜费半 -2.1%」消失。
OVERSEAS_HEAD = 5

# 低于这个绝对涨跌幅不单独印：隔夜 ±0.3% 是噪声，写进简报只会稀释真正动了的那几个。
OVERSEAS_QUIET_PCT = 0.5

# 基准率只挑前几条强映射，避免一晚多个资源品/ETF 同时触发时把研报包撑成统计报表。
BASE_RATE_LINK_LIMIT = 8
SCHEMA = "ashare-preopen-research-context-v1"
PREOPEN_FACT_SECTIONS = [
    "持仓",
    "盘前主链路",
    "参数化策略扫描",
    "台账统计",
    "近期验证",
    "数据健康",
]


def build_preopen_research(
    *,
    output_dir: str | Path = "briefs",
    moment: datetime | None = None,
    market_adapter: Any | None = None,
) -> dict[str, Any]:
    """组一份当日盘前研报输入包。

    `market_adapter` 为 None 就是**不带海外快照**（`--no-global`）：这时排版层会
    明说「未请求海外快照，不对隔夜风险偏好作数值判断」，而不是留一段空白让读的人
    以为隔夜没事——这两件事在这套系统里一直是分开的（同 `fanout.py`）。

    适配器由调用方构造并传进来，取数失败不在这里兜：`cli` 是构造适配器的那一层，
    而这一层只组织内容。
    """
    now = to_beijing_naive(moment or beijing_now()).replace(tzinfo=BEIJING_TZ)
    bundle = build_facts_bundle(
        moment=now,
        output_dir=output_dir,
        phase="pre_open",
        is_trading_day=TradingCalendar().is_trading_day(now.date()),
        sections=PREOPEN_FACT_SECTIONS,
        allow_missing_sections=True,
    )
    overseas = _overseas_payload(market_adapter)
    facts = bundle.to_dict(compact_rendered=True)
    morning = facts.get("facts", {}).get("盘前主链路")
    return {
        "schema": SCHEMA,
        "as_of": now.isoformat(timespec="seconds"),
        "task": "A股盘前先手研报",
        "requirements": PREOPEN_RESEARCH_REQUIREMENTS,
        "facts": facts,
        "overseas_snapshot": overseas,
        # 假设链：主题簇情景 × 外盘映射 × 基准率，每条自带证伪条件。外部 AI 拿到的
        # 不再是三段并排的原料，而是已经对齐到同一个簇名上的「先手观察」草稿——
        # 它要做的是审、补、删，不是从头拼。
        "hypotheses": build_sector_hypotheses(
            morning if isinstance(morning, dict) else None, overseas, session_date=now.date()
        ),
    }


def _overseas_payload(market_adapter: Any | None) -> dict[str, Any] | None:
    """海外快照 + **按组折叠好的摘要**。

    分组在这里做而不是在排版层，理由是排版层拿到的是序列化之后的 dict，而
    「谁动得最厉害」是一次排序——放在排版层意味着外部 AI 拿到的 payload 仍然是
    44 条平铺的原始读数，它得自己排一遍。`requirements` 里写着「依次判断：海外
    风险偏好与 A 股映射」，那份判断要的正是这个摘要。

    `readings` 原样保留：摘要是**取前几名**的，被截掉的那些仍然要能查到，
    否则「铜矿 ETF 昨夜怎么样」在包里就没有答案了。
    """
    if market_adapter is None:
        return None
    snapshot = market_adapter.fetch_snapshot()
    payload = snapshot.to_dict()
    payload["groups"] = []
    for group, label in OVERSEAS_GROUPS:
        rows = snapshot.by_group(group)
        payload["groups"].append(
            {
                "group": group,
                "label": label,
                "movers": [item.to_dict() for item in _movers(rows)],
                "total": len(rows),
            }
        )
    # 外盘 → A 股板块的显式映射。**这一段是给外部 AI 的翻译层**：没有它，模型
    # 只能拿到「费半 −2.12%」然后自己去想 A 股哪个板块对得上，而那正是它最容易
    # 编出一个听起来合理、taxonomy 里却不存在的板块名的地方。
    payload["sector_links"] = read_overseas_links(snapshot).to_dict()
    payload["sector_base_rates"] = _sector_base_rates(payload["sector_links"])
    return payload


def _sector_base_rates(sector_links: dict[str, Any]) -> dict[str, Any]:
    """已触发外盘映射的历史基准率。只读本地缓存，不触发网络。

    这层不出现在 `requirements` 里的“预测”位置，而是作为可查证事实：
    过去同样外盘条件出现后，相关 A 股板块的反应日和 1/3/5 日超额表现如何。
    历史缓存缺失时明确返回 notes，而不是悄悄给一段空表。
    """
    links = sector_links.get("links") or []
    if not links:
        return {"ok": False, "items": [], "notes": ["没有触发的外盘映射，不计算历史基准率"]}

    overseas = load_overseas_series()
    sector_series = load_sector_series()
    if not overseas or not sector_series:
        return {
            "ok": False,
            "items": [],
            "notes": [
                (
                    "缺少海外指数历史或同花顺板块历史，无法计算基准率；"
                    "先运行 `ashare-ds history-refresh` 回填"
                )
            ],
        }

    panel = build_panel(sector_series)
    if not panel.ok:
        return {"ok": False, "items": [], "notes": ["同花顺板块历史无法组成有效面板"]}

    items: list[dict[str, Any]] = []
    for link in links[:BASE_RATE_LINK_LIMIT]:
        items.extend(_base_rates_for_link(link, overseas, panel))
    notes = [*panel.notes]
    has_samples = any((item.get("samples") or 0) > 0 for item in items)
    if not has_samples:
        notes.append("触发的外盘品种没有可用历史，或对应板块没有历史序列")
    return {"ok": has_samples, "items": items, "notes": notes}


def _base_rates_for_link(
    link: dict[str, Any],
    overseas: dict[str, list[dict]],
    panel: Any,
) -> list[dict[str, Any]]:
    key = str(link.get("instrument") or "")
    instrument = INSTRUMENTS.get(key)
    bars = overseas.get(key)
    change_pct = link.get("change_pct")
    if instrument is None or not bars or not isinstance(change_pct, int | float):
        return []

    dates = _condition_dates(daily_returns(bars), change_pct / 100.0)
    sessions = align_sessions(
        dates,
        panel.trading_days,
        strictly_after=instrument.after_a_share_close,
    )
    condition = f"{instrument.name} {'上涨' if change_pct > 0 else '下跌'}超阈值"
    rows: list[dict[str, Any]] = []
    for sector in link.get("targets") or ():
        read = base_rate(panel, str(sector), sessions, condition)
        item = read.to_dict()
        item["instrument"] = key
        item["instrument_name"] = instrument.name
        item["alignment"] = "strictly_after" if instrument.after_a_share_close else "same_or_after"
        rows.append(item)
    return rows


def _condition_dates(returns: dict[str, float], observed: float) -> list[str]:
    """历史上与本次外盘方向同侧、且幅度不小于本次的日期。"""
    if observed == 0:
        return []
    threshold = abs(observed)
    if observed > 0:
        return [stamp for stamp, value in returns.items() if value >= threshold]
    return [stamp for stamp, value in returns.items() if value <= -threshold]


def _movers(rows: list[Any]) -> list[Any]:
    """一组里两端各取几个，去掉没动的那些。

    **两端各取，不是只取涨幅榜。** 跌得最惨的那几个和涨得最猛的那几个同样是信息
    ——只取一端会让「昨夜费半 −2.1%」从简报里消失，而那恰好是这一组里最该被看到
    的一条。`rows` 已经由 `by_group` 按涨跌幅降序排好。
    """
    loud = [item for item in rows if abs(item.change_pct or 0.0) >= OVERSEAS_QUIET_PCT]
    if len(loud) <= OVERSEAS_HEAD * 2:
        return loud
    return [*loud[:OVERSEAS_HEAD], *loud[-OVERSEAS_HEAD:]]


def _overseas_lines(overseas: dict[str, Any]) -> list[str]:
    """海外那一节：按组折叠，每组只印动了的那几个，缺失单列一行。

    **「未请求」和「请求了但都没动」必须长得不一样。** 前者是「这份报告不含这个
    维度」，后者是「隔夜确实平静」——对开盘预案是完全相反的两条信息，而一段空白
    对两者给出的观感是同一个。这与本仓一贯的口径一致（`fanout.py`、
    `jobs/sources.py:fetch_market_scan`）。
    """
    if not overseas.get("readings"):
        return ["- 未请求海外快照；不对隔夜风险偏好作数值判断。"]

    lines: list[str] = []
    for block in overseas.get("groups") or []:
        movers = block.get("movers") or []
        total = block.get("total") or 0
        if not movers:
            lines.append(
                f"- **{block.get('label')}**：{total} 个品种，无一超过 "
                f"±{OVERSEAS_QUIET_PCT}%——隔夜平静，不是没取到。"
            )
            continue
        lines.append(f"- **{block.get('label')}**（{len(movers)}/{total} 个动了）：")
        lines.extend(
            f"    - {item['name']} {item.get('change_pct'):+.2f}%"
            f"（{item['last']}，前收 {item.get('previous_close')}，时点 {item.get('as_of') or '未知'}）"
            for item in movers
        )

    missing = [item for item in overseas["readings"] if item.get("last") is None]
    if missing:
        lines.append(
            "- 未覆盖："
            + "、".join(f"{item['name']}（{item.get('note') or '无可用读数'}）" for item in missing)
        )
    lines.extend(_link_lines(overseas.get("sector_links") or {}))
    lines.extend(_base_rate_lines(overseas.get("sector_base_rates") or {}))
    return lines


def _link_lines(links: dict[str, Any]) -> list[str]:
    """外盘 → A 股板块那一段。

    **同一个板块被反向的两条链路同时指向时，必须并排印出来。** 2026-08-20 实测
    就是这种盘面：KOSPI +5.89% 与 KOSDAQ +1.99% 指向半导体利多，同夜费半 −2.12%
    与 SMH −1.55% 指向利空。压成一个净方向会把这件事抹掉，而它恰恰是当晚关于
    半导体最重要的一条信息。
    """
    rows = links.get("links") or []
    if not rows:
        return []

    lines = ["", f"## 外盘 → A股板块（{len(rows)} 条）"]
    lines.extend(
        f"- {item['instrument_name']} {item['change_pct']:+.2f}% → "
        f"{'、'.join(item['targets'])} "
        f"{'强' if item['strength'] == 'strong' else ''}"
        f"{'利多' if item['direction'] == 'bullish' else '利空'}"
        f"（{item['mechanism']}）"
        for item in rows
    )

    conflicted = _conflicts(rows)
    if conflicted:
        lines.append("")
        lines.append("**方向分歧的板块**（多条链路同时指向且方向相反，不要只看其中一条）：")
        lines.extend(
            f"- {sector}：" + "、".join(hits) for sector, hits in sorted(conflicted.items())
        )
    lines.append("")
    lines.append(
        "> 以上是对**已经发生的隔夜行情**的映射，不是对 A 股次日走势的预测；"
        "映射关系是先验设定的传导机制，兑现率尚未验证。"
    )
    return lines


def _conflicts(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    by_sector: dict[str, list[dict[str, Any]]] = {}
    for item in rows:
        for sector in item.get("targets") or []:
            by_sector.setdefault(sector, []).append(item)
    return {
        sector: [f"{hit['instrument_name']}{hit['change_pct']:+.1f}%" for hit in hits]
        for sector, hits in by_sector.items()
        if len({hit["direction"] for hit in hits}) > 1
    }


def _base_rate_lines(payload: dict[str, Any]) -> list[str]:
    rows = payload.get("items") or []
    if not rows:
        notes = payload.get("notes") or []
        return ["", "## 外盘映射历史基准率", f"- {notes[0]}"] if notes else []

    lines = ["", "## 外盘映射历史基准率（只读本地缓存）"]
    for item in rows[:12]:
        lines.append(f"- {item.get('headline')}")
    notes = payload.get("notes") or []
    if notes:
        lines.append(f"- 注：{notes[0]}")
    return lines


def _turnover_items(turnover: dict[str, Any], quadrant: str) -> list[dict[str, Any]]:
    return [
        item
        for item in turnover.get("rising", [])
        if isinstance(item, dict) and item.get("quadrant") == quadrant
    ]


def _risk_lines(payload: dict[str, Any], morning: Any, overseas: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    missing = [
        item.get("name") for item in overseas.get("readings", []) if item.get("last") is None
    ]
    if missing:
        lines.append(
            f"- 海外快照缺少 {'、'.join(str(item) for item in missing)}，风险偏好只做部分判断。"
        )
    facts = payload.get("facts") or {}
    for note in facts.get("notes", []):
        text = str(note)
        if "陈旧" in text or "缺失" in text or "尚无" in text:
            lines.append(f"- 数据边界：{note}")
    if isinstance(morning, dict):
        for note in morning.get("data_notes", []):
            text = str(note)
            if "样本不足" in text or "不同日" in text or "早于本次交易日" in text:
                lines.append(f"- {text}")
    if not lines:
        lines.append("- 暂无额外风险提示；仍需用竞价、开盘承接和成交扩散确认。")
    return lines[:5]


def _hypothesis_lines(payload: Any) -> list[str]:
    """展示已校验日期的主题假设，复用生成时的确认与失效条件。"""
    if not isinstance(payload, dict):
        return ["- 尚无经过日期校验的盘前假设，请重新生成盘前研究资料。"]
    items = payload.get("items") or []
    if not items:
        return [f"- {note}" for note in payload.get("notes") or ["暂无可用的当日盘前假设。"]]
    lines: list[str] = []
    for item in items:
        lines.append(
            f"- 观察 {item.get('name')}（{item.get('scenario') or '待验证'}）："
            f"评分 {score(item.get('score'))}，"
            f"1日 {percent(item.get('ret_1d'))}，5日 {percent(item.get('ret_5d'))}。"
        )
        lines.append(f"  - 关联板块：{names(item.get('members'))}")
        lines.extend(
            f"  - {evidence.get('kind')}：{evidence.get('text')}"
            for evidence in item.get("evidence") or []
        )
        lines.append(
            f"  - 确认（{item.get('confirm_after', '开盘')}后）："
            + "；".join(item.get("confirm") or [])
        )
        lines.append("  - 失效：" + "；".join(item.get("invalidate") or []))
    return lines


def render_preopen_research(payload: dict) -> str:
    """只根据包内事实排版，不把条件预案写成方向预测。"""
    facts = payload["facts"]["facts"]
    morning = facts.get("盘前主链路") if isinstance(facts, dict) else None
    lines = ["# A股盘前情景报告", "", f"数据时点：{payload['as_of']}"]
    lines.extend(["", "## 数据边界"])
    lines.extend(f"- {note}" for note in payload["facts"].get("notes", []))
    overseas = payload.get("overseas_snapshot") or {}
    lines.extend(["", "## 海外隔夜"])
    lines.extend(_overseas_lines(overseas))
    if not isinstance(morning, dict):
        lines.extend(["", "## A股盘前事实"])
        lines.append("- 尚无当日盘前主链路落盘结果，不能生成主线排序。")
    else:
        market = morning.get("market") or {}
        rotation = morning.get("rotation_price") or {}
        turnover = morning.get("turnover_migration") or {}
        limit_pool = morning.get("limit_pool") or {}
        lines.extend(["", "## 总判断"])
        lines.append(
            f"- 市场读数：{market.get('direction', '未覆盖')}，"
            f"综合分 {score(market.get('score'))}，"
            f"5日中位 {percent(market.get('median_ret_5d'))}，"
            f"广度 {percent(market.get('breadth'))}"
        )
        if rotation:
            lines.append(
                f"- 轮动状态：{rotation.get('switch_label') or '未覆盖'}；"
                f"近3样本反复出现：{names(rotation.get('persistent') or rotation.get('recurring_leaders'))}"
            )
        if turnover:
            lines.append(
                f"- 成交额迁移：总量 {percent(turnover.get('total_change'))}，"
                f"最新一日 {percent(turnover.get('total_trend'))}"
            )
        if limit_pool:
            lines.append(
                f"- 情绪旁证：涨停 {limit_pool.get('limit_up', '—')}，"
                f"炸板率 {percent(limit_pool.get('blowup_rate'))}，"
                f"最高连板 {limit_pool.get('max_streak', '—')}"
            )

        if turnover:
            inflow = names(_turnover_items(turnover, "放量上涨") or turnover.get("rising"))
            distribution = names(
                _turnover_items(turnover, "放量下跌") or turnover.get("distribution")
            )
            fading = names(turnover.get("falling"))
            if inflow != "—" or distribution != "—" or fading != "—":
                lines.append(
                    f"- 资金迁移：{inflow}放量上涨，{distribution}放量下跌，"
                    f"{fading}缩量走弱；确认看成交额继续放大，失效看只剩轮动没有增量。"
                )
        for warning in market.get("warnings") or []:
            lines.append(f"- 风险提示：{warning}")
    lines.extend(["", "## 先手观察"])
    lines.extend(_hypothesis_lines(payload.get("hypotheses")))
    lines.extend(
        [
            "",
            "## 只等确认",
            "- 只看已经发生的海外变化和本地晨报；没有竞价/开盘承接，不把推演写成方向。",
            "- 若开盘只有单点拉升、没有板块扩散，按兑现风险处理。",
            "- 若资源、半导体或电力链出现同步承接，再回看外盘映射是否被A股确认。",
            "",
            "## 风险",
        ]
    )
    lines.extend(_risk_lines(payload, morning, overseas))
    return "\n".join(lines)
