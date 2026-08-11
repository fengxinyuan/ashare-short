"""标的元数据与估算筹码分布的测试。

筹码那半边最重要的一条是**守恒律**：分布之和恒等于 1。真实筹码源给不了这个保证，
自算的可以，所以它必须被钉死。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from ashare_decision_support.adapters.base import float_shares, security_metadata
from ashare_decision_support.adapters.security_master import (
    SecurityMasterAdapter,
)
from ashare_decision_support.signals.chips import (
    GRID_SIZE,
    MIN_BARS,
    estimate_chips,
)

# ---- 元数据 ---------------------------------------------------------------

INDUSTRIES = [
    {"symbol": "920130.BJ", "name": "立方控股", "industries": ["计算机", "IT服务", "IT服务Ⅲ"]},
    {"symbol": "600519.SH", "name": "贵州茅台", "industries": ["食品饮料", "白酒", "白酒Ⅲ"]},
]
CONCEPTS = [
    {"symbol": "600519.SH", "name": "贵州茅台", "concepts": ["白酒", "MSCI中国"]},
    {"symbol": "000001.SZ", "name": "平安银行", "concepts": ["银行", "深股通"]},
]


def fake_opener(payloads: dict[str, object], fail: set[str] | None = None):
    broken = fail or set()

    def opener(url: str, timeout: float) -> str:
        name = url.rsplit("/", 1)[-1]
        if name in broken:
            raise OSError(f"boom: {name}")
        return json.dumps(payloads[name], ensure_ascii=False)

    return opener


def adapter_for(tmp_path, payloads=None, fail=None) -> SecurityMasterAdapter:
    payloads = payloads or {"industries.json": INDUSTRIES, "concepts.json": CONCEPTS}
    return SecurityMasterAdapter(
        base_url="https://example.invalid/ths/",
        cache_path=tmp_path / "security_master.json",
        opener=fake_opener(payloads, fail),
    )


def test_two_files_are_merged_on_symbol(tmp_path) -> None:
    master = adapter_for(tmp_path).load()

    assert len(master) == 3
    maotai = master.get("600519.SH")
    assert maotai.name == "贵州茅台"
    assert maotai.industries == ("食品饮料", "白酒", "白酒Ⅲ")
    assert maotai.concepts == ("白酒", "MSCI中国")


def test_english_name_is_none_because_the_source_has_none(tmp_path) -> None:
    """需求里提到中英文名，但这个源只有中文。**不拿拼音或代码凑一个。**"""
    master = adapter_for(tmp_path).load()
    assert all(info.name_en is None for info in master.entries.values())
    assert master.stats()["provides_english_name"] is False


def test_symbols_are_normalised_so_lookups_match(tmp_path) -> None:
    master = adapter_for(tmp_path).load()
    assert master.get("600519").name == "贵州茅台"
    assert master.get("sh600519").name == "贵州茅台"


def test_cache_is_reused_without_refetching(tmp_path) -> None:
    calls = {"n": 0}

    def counting(url: str, timeout: float) -> str:
        calls["n"] += 1
        name = url.rsplit("/", 1)[-1]
        return json.dumps({"industries.json": INDUSTRIES, "concepts.json": CONCEPTS}[name],
                          ensure_ascii=False)

    source = SecurityMasterAdapter(
        base_url="https://example.invalid/ths/",
        cache_path=tmp_path / "m.json",
        opener=counting,
    )
    source.load()
    first = calls["n"]
    source.load()

    assert first == 2  # 两个文件各一次
    assert calls["n"] == first, "缓存新鲜时不该再拉"


def test_stale_cache_is_still_used_when_the_fetch_fails(tmp_path) -> None:
    """一份三天前的行业分类仍然基本正确；没有分类才是彻底不能用。"""
    adapter_for(tmp_path, None).load()  # 先写一份缓存
    cache_written = tmp_path / "security_master.json"
    assert cache_written.exists()

    broken = SecurityMasterAdapter(
        base_url="https://example.invalid/ths/",
        cache_path=cache_written,
        opener=fake_opener({}, fail={"industries.json", "concepts.json"}),
    )
    master = broken.load(max_age_seconds=0)  # 强制判定为过期

    assert master.ok and len(master) == 3
    assert any("继续使用" in note for note in master.notes)


def test_total_failure_with_no_cache_is_survivable(tmp_path) -> None:
    broken = SecurityMasterAdapter(
        base_url="https://example.invalid/ths/",
        cache_path=tmp_path / "absent.json",
        opener=fake_opener({}, fail={"industries.json", "concepts.json"}),
    )
    master = broken.load()

    assert not master.ok
    assert master.get("600519.SH") is None
    assert any("不可用" in note for note in master.notes)


def test_broad_attributes_are_excluded_from_themes(tmp_path) -> None:
    """`融资融券` 覆盖 69% 的市场——它是可交易属性，不是题材。

    留在题材榜里，「今天哪个题材在动」的答案会永远是它。
    """
    payloads = {
        "industries.json": [],
        "concepts.json": [
            {"symbol": f"{600000 + i}.SH", "name": f"N{i}",
             "concepts": ["融资融券"] + (["第三代半导体"] if i < 2 else [])}
            for i in range(10)
        ],
    }
    master = adapter_for(tmp_path, payloads).load()

    themes = master.themes()
    assert "融资融券" not in themes, "覆盖 100% 的标签不是题材"
    assert themes["第三代半导体"] == 2
    # 但它仍然是一个真实的事实，逐票查询照常返回。
    assert "融资融券" in master.get("600000.SH").concepts


def test_a_non_list_payload_is_refused_not_guessed(tmp_path) -> None:
    payloads = {"industries.json": {"oops": 1}, "concepts.json": CONCEPTS}
    master = adapter_for(tmp_path, payloads).load()

    assert any("顶层不是数组" in note for note in master.notes)
    # 概念那一份仍然可用——一个文件坏掉不该带走另一个。
    assert master.get("000001.SZ") is not None


# ---- 筹码 -----------------------------------------------------------------


def chip_bars(
    count: int = 120, start: float = 20.0, drift: float = 0.05, volume: float = 1_000_000.0
) -> list[dict]:
    rows: list[dict] = []
    price = start
    day = date(2026, 1, 5)
    for index in range(count):
        price = max(price + drift, 1.0)
        low, high = price - 0.4, price + 0.4
        rows.append(
            {
                "timestamp": datetime.combine(day + timedelta(days=index), datetime.min.time()),
                "open": price,
                "high": high,
                "low": low,
                "close": price,
                "volume": volume,
                "amount": volume * price,
            }
        )
    return rows


FLOAT_SHARES = 200_000_000.0


def test_distribution_conserves_mass() -> None:
    """守恒律：权重之和恒为 1，所以获利盘比例必然落在 [0,1]。

    真实筹码源给不了这个保证，自算的可以——这正是这条路值得先做的理由之一。
    """
    result = estimate_chips(chip_bars(), FLOAT_SHARES)
    assert result.chip_winner_ratio_est is not None
    assert 0.0 <= result.chip_winner_ratio_est <= 1.0


def test_uptrend_leaves_most_chips_in_profit() -> None:
    """一路上涨：绝大多数筹码的成本低于现价。"""
    result = estimate_chips(chip_bars(drift=0.15), FLOAT_SHARES)
    assert result.chip_winner_ratio_est > 0.8
    assert result.dist_to_chip_avg_est > 0


def test_downtrend_leaves_most_chips_trapped() -> None:
    result = estimate_chips(chip_bars(start=60.0, drift=-0.2), FLOAT_SHARES)
    assert result.chip_winner_ratio_est < 0.2
    assert result.dist_to_chip_avg_est < 0


def test_average_cost_lies_inside_the_traded_range() -> None:
    bars = chip_bars()
    result = estimate_chips(bars, FLOAT_SHARES)
    low = min(row["low"] for row in bars)
    high = max(row["high"] for row in bars)
    assert low <= result.chip_avg_cost_est <= high


def test_high_turnover_forgets_old_chips_faster() -> None:
    """换手率就是这套算法的记忆长度——换手越高，平均成本越贴近近期价格。"""
    bars = chip_bars(drift=0.15)
    slow = estimate_chips(bars, FLOAT_SHARES * 20)   # 低换手，记得久
    fast = estimate_chips(bars, FLOAT_SHARES)        # 高换手，忘得快
    assert fast.chip_avg_cost_est > slow.chip_avg_cost_est


def test_missing_float_shares_declines_entirely() -> None:
    """没有流通股本就没有换手率，硬算等于给一个任意的记忆长度。"""
    result = estimate_chips(chip_bars(), None)
    assert result.chip_avg_cost_est is None
    assert result.chip_winner_ratio_est is None


def test_short_history_declines_but_reports_what_it_saw() -> None:
    result = estimate_chips(chip_bars(count=MIN_BARS - 1), FLOAT_SHARES)
    assert result.chip_avg_cost_est is None
    assert result.bars_used == MIN_BARS - 1


def test_unrecoverable_volume_unit_is_dropped_not_guessed() -> None:
    """量纲推不出来的那一根整根丢弃——volume 记手还是股直接决定换手率差 100 倍。"""
    bars = chip_bars()
    for row in bars:
        row["amount"] = 0
    result = estimate_chips(bars, FLOAT_SHARES)
    assert result.chip_avg_cost_est is None


def test_inverted_high_low_is_repaired() -> None:
    """复权后 high < low 的倒挂实测占 9.33%，不修会让三角分布整个翻掉。"""
    bars = chip_bars()
    for row in bars[:10]:
        row["high"], row["low"] = row["low"], row["high"]
    result = estimate_chips(bars, FLOAT_SHARES)
    assert result.chip_avg_cost_est is not None


def test_naming_never_collides_with_the_turnover_cost_proxy() -> None:
    """三个数三个名字。混用一个名字，事后查不出某个数当时是从哪来的。"""
    keys = set(estimate_chips(chip_bars(), FLOAT_SHARES).to_dict())
    assert "chip_avg_cost_est" in keys
    # 真实筹码源的字段名与成交成本代理的字段名都不许出现在这里。
    assert "chip_avg_cost" not in keys
    assert "turnover_cost_30" not in keys


def test_grid_is_fine_enough_to_avoid_quantisation() -> None:
    """网格太粗会让平均成本量化到几个离散值上。"""
    a = estimate_chips(chip_bars(drift=0.05), FLOAT_SHARES).chip_avg_cost_est
    b = estimate_chips(chip_bars(drift=0.051), FLOAT_SHARES).chip_avg_cost_est
    assert a != b
    assert GRID_SIZE >= 100


# ---- 流通股本的取数与接线 --------------------------------------------------


class MasterAdapter:
    """TickFlow 把 float_shares 放在 `ext` 里；别的源可能放顶层。两种都要认。"""

    def __init__(self, rows):
        self.rows = rows

    def get_security_master(self, symbols):
        return self.rows


class ReferenceAdapter:
    def __init__(self, tmp_path, payloads=None, fail=None):
        self.adapter = adapter_for(tmp_path, payloads=payloads, fail=fail)

    def load(self):
        return self.adapter.load()


def test_float_shares_is_read_from_either_nesting() -> None:
    adapter = MasterAdapter(
        {
            "600519.SH": {"name": "A", "ext": {"float_shares": 1_250_081_601.0}},
            "000001.SZ": {"name": "B", "float_shares": 19_405_600_653.0},
        }
    )
    result = float_shares(adapter, ["600519.SH", "000001.SZ"])
    assert result == {"600519.SH": 1_250_081_601.0, "000001.SZ": 19_405_600_653.0}


def test_total_shares_never_substitutes_for_float_shares() -> None:
    """两者对茅台恰好相等，对有限售股的票差很多——「恰好相等」正是这类错误
    最容易被漏掉的原因。缺流通股本就是缺，不拿总股本顶。"""
    adapter = MasterAdapter({"X.SH": {"ext": {"total_shares": 5_000_000_000.0}}})
    assert float_shares(adapter, ["X.SH"]) == {}


def test_float_shares_survives_a_broken_adapter() -> None:
    class Broken:
        def get_security_master(self, symbols):
            raise RuntimeError("boom")

    assert float_shares(Broken(), ["X.SH"]) == {}
    assert float_shares(object(), ["X.SH"]) == {}


def test_security_metadata_merges_adapter_rows_with_reference_tags(tmp_path) -> None:
    adapter = MasterAdapter(
        {
            "600519.SH": {"name": "茅台", "ext": {"float_shares": 1_250_081_601.0}},
            "000001.SZ": {"name": "平安银行"},
        }
    )

    rows = security_metadata(
        adapter,
        ["600519.SH", "000001.SZ"],
        ReferenceAdapter(tmp_path),
    )

    assert rows["600519.SH"]["name"] == "茅台"
    assert rows["600519.SH"]["industry_path"] == "食品饮料·白酒·白酒Ⅲ"
    assert rows["600519.SH"]["concepts"] == ["白酒", "MSCI中国"]
    assert rows["000001.SZ"]["industry_path"] is None
    assert rows["000001.SZ"]["concepts"] == ["银行", "深股通"]


def test_security_metadata_stays_local_when_no_reference_is_given() -> None:
    adapter = MasterAdapter({"600519.SH": {"name": "茅台"}})
    rows = security_metadata(adapter, ["600519.SH"])

    assert rows == {"600519.SH": {"symbol": "600519.SH", "name": "茅台"}}


def test_pipeline_actually_receives_float_shares() -> None:
    """接线测试。

    **这一条钉住的是一个真实发生过的缺口**：`analyze_universe` 接受 `float_shares`，
    但两个调用方一开始都没传，于是换手率和筹码在生产里恒为 None——而所有单元测试
    照样全绿，因为它们都是直接调 `estimate_chips` 的。缺的不是算法，是那一根线。
    """
    from ashare_decision_support.strategy.select import evaluate_universe

    bars = {"AAA.SH": chip_bars(count=120)}

    without = evaluate_universe(bars, require_playbook=False)
    with_shares = evaluate_universe(
        bars, float_shares={"AAA.SH": FLOAT_SHARES}, require_playbook=False
    )

    blind = (without.selected + without.rejected)[0].to_row()
    wired = (with_shares.selected + with_shares.rejected)[0].to_row()

    assert blind["chips"]["chip_avg_cost_est"] is None
    assert blind["volume_price"]["turnover_rate"] is None

    assert wired["chips"]["chip_avg_cost_est"] is not None
    assert wired["volume_price"]["turnover_rate"] is not None


# ---- 筹码：按下标切片的等价性 ----------------------------------------------


def _deposit_over_full_grid(chips, grid, step, row, turnover) -> None:
    """`_deposit` 收窄扫描范围**之前**的写法，逐字保留，只作为对照实现。

    它扫整条 200 档网格，其中落在 `[low, high]` 之外的档恒为 0。现在的实现按下标
    只扫覆盖到的那一段——两者必须逐位相同，否则「快 3.4 倍」买到的是一份不一样的
    筹码分布。
    """
    low, high, peak = row["low"], row["high"], row["vwap"]
    if high <= low:
        index = min(max(int((low - grid[0] + step / 2) / step), 0), len(chips) - 1)
        chips[index] += turnover
        return
    shares_of = []
    for price in grid:
        if price < low or price > high:
            shares_of.append(0.0)
            continue
        if price <= peak:
            height = (price - low) / (peak - low) if peak > low else 1.0
        else:
            height = (high - price) / (high - peak) if high > peak else 1.0
        shares_of.append(max(height, 0.0))
    weight_total = sum(shares_of)
    if weight_total <= 0:
        return
    for index, weight in enumerate(shares_of):
        if weight:
            chips[index] += turnover * weight / weight_total


def test_deposit_matches_a_full_grid_scan_including_the_edges() -> None:
    """边界档是这个优化唯一可能出错的地方，所以专门把三种边界摆进来。"""
    from ashare_decision_support.signals.chips import _deposit

    floor, ceiling = 10.0, 40.0
    step = (ceiling - floor) / GRID_SIZE
    grid = [floor + step * (index + 0.5) for index in range(GRID_SIZE)]

    rows = [
        # 贴着网格下沿 / 上沿 / 跨满全幅 / 落在正中 / 高低相等（一字板）
        {"low": floor, "high": floor + step * 3, "vwap": floor + step},
        {"low": ceiling - step * 3, "high": ceiling, "vwap": ceiling - step},
        {"low": floor, "high": ceiling, "vwap": (floor + ceiling) / 2},
        {"low": 24.0, "high": 26.0, "vwap": 25.0},
        {"low": 20.0, "high": 20.0, "vwap": 20.0},
        # 均价压在区间端点上：三角形退化成直角三角形
        {"low": 21.0, "high": 23.0, "vwap": 21.0},
        {"low": 21.0, "high": 23.0, "vwap": 23.0},
    ]

    for row in rows:
        fast = [0.0] * GRID_SIZE
        reference = [0.0] * GRID_SIZE
        _deposit(fast, grid, step, row, 0.05)
        _deposit_over_full_grid(reference, grid, step, row, 0.05)
        assert fast == reference, row


def test_deposit_puts_nothing_outside_the_bar_range() -> None:
    """三角分布铺的是当日 low..high，区间外一分都不该落。"""
    from ashare_decision_support.signals.chips import _deposit

    floor, ceiling = 10.0, 40.0
    step = (ceiling - floor) / GRID_SIZE
    grid = [floor + step * (index + 0.5) for index in range(GRID_SIZE)]
    chips = [0.0] * GRID_SIZE
    _deposit(chips, grid, step, {"low": 24.0, "high": 26.0, "vwap": 25.0}, 0.05)

    assert sum(chips) == pytest.approx(0.05)
    for price, weight in zip(grid, chips, strict=True):
        if price < 24.0 or price > 26.0:
            assert weight == 0.0


def test_every_described_line_is_marked_as_an_estimate() -> None:
    """**这条钉的是模块 docstring 的承诺**：简报里出现的每一处都必须说明是估算。

    真实筹码源与换手衰减法估算是两个数，混起来事后没有任何办法查出某个数当时是
    从哪来的。免责那一行不是礼貌，是审计线索。
    """
    lines = estimate_chips(chip_bars(), FLOAT_SHARES).describe()
    assert lines
    assert lines[-1] == "以上为换手衰减法估算，不是真实筹码数据"
    assert any("估算平均成本" in line for line in lines)


def test_describe_says_nothing_rather_than_something_wrong_when_it_declined() -> None:
    """算不出来时一行都不出——空读数比一行「估算平均成本 None」有用得多。"""
    assert estimate_chips(chip_bars(), None).describe() == []
    assert estimate_chips(chip_bars(count=MIN_BARS - 1), FLOAT_SHARES).describe() == []


def test_a_flat_bar_still_deposits_and_widens_the_touched_range() -> None:
    """一字板（high == low）走的是 `_deposit` 的另一条分支。

    它必须照样把筹码铺进去、并把「铺过的下标区间」撑开——否则后面几根的衰减会
    漏掉这一档，那一档的筹码就再也不会变少。
    """
    from ashare_decision_support.signals.chips import _deposit

    floor, ceiling = 10.0, 40.0
    step = (ceiling - floor) / GRID_SIZE
    grid = [floor + step * (index + 0.5) for index in range(GRID_SIZE)]
    chips = [0.0] * GRID_SIZE

    low_index, high_index = _deposit(chips, grid, step, {"low": 25.0, "high": 25.0, "vwap": 25.0}, 0.05)
    assert low_index == high_index
    assert chips[low_index] == pytest.approx(0.05)
    assert sum(chips) == pytest.approx(0.05)


def test_deposit_reports_an_empty_range_when_it_writes_nothing() -> None:
    """写不进任何一档时要交回一个空区间，调用方据此不去撑开衰减范围。"""
    from ashare_decision_support.signals.chips import _deposit

    floor, ceiling = 10.0, 40.0
    step = (ceiling - floor) / GRID_SIZE
    grid = [floor + step * (index + 0.5) for index in range(GRID_SIZE)]
    chips = [0.0] * GRID_SIZE

    # 整根落在网格之外：没有一档的中心价落进 [low, high]。
    low_index, high_index = _deposit(chips, grid, step, {"low": 100.0, "high": 100.2, "vwap": 100.1}, 0.05)
    assert low_index > high_index
    assert sum(chips) == 0.0


def test_limit_up_days_mixed_into_the_history_still_conserve_mass() -> None:
    """连续一字板是 A 股真实形态，不该让整份分布失衡。"""
    bars = chip_bars()
    for row in bars[30:34]:
        row["high"] = row["low"] = row["open"] = row["close"]
        row["amount"] = row["volume"] * row["close"]
    result = estimate_chips(bars, FLOAT_SHARES)
    assert result.chip_avg_cost_est is not None
    assert 0.0 <= result.chip_winner_ratio_est <= 1.0
    low = min(row["low"] for row in bars)
    high = max(row["high"] for row in bars)
    assert low <= result.chip_avg_cost_est <= high


def test_a_completely_flat_history_declines_instead_of_dividing_by_zero() -> None:
    """所有 K 线同一个价 → 网格宽度为 0，此时弃权而不是造一个宽度 0 的分布。"""
    bars = chip_bars()
    for row in bars:
        row["open"] = row["high"] = row["low"] = row["close"] = 10.0
        row["amount"] = row["volume"] * 10.0
    result = estimate_chips(bars, FLOAT_SHARES)
    assert result.chip_avg_cost_est is None
    assert result.bars_used == len(bars)
