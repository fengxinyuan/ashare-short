"""输出闸门的第二条与第三条口径：措辞与立场。

第一条（数字追溯）的用例在 `tests/test_writer_and_sector_job.py`。这里钉住的是
**此前只写在提示词里、没有任何执行**的那两条，以及它们最容易误伤的三种正常文字：
系统自己的免责段、空头角色的风险提示、盘前简报里的计划句。
"""

from __future__ import annotations

import json

from ashare_decision_support.agents.debate import DebatePanel
from ashare_decision_support.chat.session import ChatResponder, ChatSession
from ashare_decision_support.factcheck import OutputGuard, prose_violations
from ashare_decision_support.writer import LlmBriefWriter, WriteRequest

# ---- 第二条：措辞 ----------------------------------------------------------


def test_probability_words_are_blocked() -> None:
    assert prose_violations("这只票明天上涨概率 70%") == ["预测性措辞「概率」"]
    assert prose_violations("这套打法胜率不低") == ["预测性措辞「胜率」"]
    assert prose_violations("持有一个月大概率翻倍") == [
        "预测性措辞「概率」",
        "预测性措辞「翻倍」",
    ]


def test_the_systems_own_disclaimer_passes() -> None:
    """纪律段每天都进事实包，模型照抄回来是对的。

    这条如果红了，闸门拦下的第一段文字会是系统自己那句免责声明——比不设闸门更糟。
    """
    assert prose_violations("系统不产出概率、胜率、预期收益率：没有拟合模型能支撑它们。") == []
    assert prose_violations("情景之间只有触发条件，没有概率。") == []
    assert prose_violations("setup_rank 只用于同一策略内排序，不是胜率、概率或预期收益率。") == []


# ---- 第三条：立场 ----------------------------------------------------------


def test_freezing_cycle_blocks_any_entry_wording() -> None:
    guard = OutputGuard.from_facts({"regime": "冰点", "regime_position_cap": 0.0})
    assert guard.stance_conflicts("可以逢低买入一些") == [
        "情绪周期 冰点 的仓位上限 0.00 说今天不出手，文字却在说「买入」"
    ]
    assert guard.stance_conflicts("今天不出手，等封板质量修复") == []


def test_a_stated_cap_blocks_only_heavy_position_wording() -> None:
    """退潮档允许低吸，不允许满仓——闸门要能分开这两件事。

    分不开的话它会在每一份退潮简报上都响一次，然后被当成噪声关掉。
    """
    guard = OutputGuard.from_facts({"regime": "退潮", "regime_position_cap": 0.30})
    assert guard.stance_conflicts("小仓位低吸板块龙头") == []
    assert guard.stance_conflicts("行情不错，可以满仓") == ["仓位系数只有 0.30，文字却在说「满仓」"]


def test_risk_warnings_are_not_stance_violations() -> None:
    """空头角色的正事就是提示风险，而它用的正是主张重仓时的那几个词。"""
    guard = OutputGuard.from_facts({"regime": "退潮", "regime_position_cap": 0.30})
    assert guard.stance_conflicts("此时追高风险极大，重仓需谨慎") == []
    assert guard.stance_conflicts("不建议满仓") == []


def test_immediate_action_needs_the_market_to_be_open() -> None:
    assert OutputGuard(phase="off_hours").stance_conflicts("建议立即买入") == [
        "当前不是盘中（off_hours），文字却给出即时动作「立即买入」"
    ]
    # 盘前简报的正事就是写计划，计划句不能被当成即时指令拦下来。
    assert OutputGuard(phase="pre_open").stance_conflicts("开盘后立即卖出") == []
    assert OutputGuard(phase="intraday").stance_conflicts("建议立即买入") == []


def test_guard_without_readings_judges_nothing() -> None:
    """没算出来就不判——给一个默认上限会让闸门凭空拦下正常文字。"""
    assert OutputGuard.from_facts(None).stance_conflicts("满仓买入，立即加仓") == []
    assert OutputGuard.from_facts({"position_factor": True}).position_cap is None


def test_guard_reads_all_three_fact_shapes() -> None:
    """三条链路各写各的事实，尺子必须都认得。"""
    assert OutputGuard.from_facts({"regime_position_cap": 0.5}).position_cap == 0.5
    assert OutputGuard.from_facts({"regime": {"position_cap": 0.4, "cycle": "高潮"}}).cycle == "高潮"
    assert OutputGuard.from_facts({"position_factor": 0.68}).position_cap == 0.68


def test_guard_reads_the_chinese_keys_the_deep_path_uses() -> None:
    """深挖那条链路把选中的字段改写成中文键给模型看（`ask/plan.py`）。

    漏掉这一条时，分步论证只查得到措辞与时段，立场那一条静默失效——而**失效的
    方向恰好是放行**：模型在冰点档说「可以加仓」照样发得出去。
    """
    step_facts = {"交易日": "2026-08-14", "仓位系数": 0.0, "情绪周期": "冰点"}
    guard = OutputGuard.from_facts(step_facts, phase="pre_open")

    assert guard.position_cap == 0.0
    assert guard.stance_conflicts("可以逢低加仓") == [
        "情绪周期 冰点 的仓位上限 0.00 说今天不出手，文字却在说「加仓」"
    ]


# ---- 四个调用点：越界的整段丢弃 --------------------------------------------


class _FixedClient:
    """固定回复的桩。闸门测的是「拿到这段文字之后怎么处置」。"""

    configured = True
    api_key = "stub"

    def __init__(self, reply: str) -> None:
        self.reply = reply

    def chat(self, messages: list[dict]) -> str:
        """桩不看输入：闸门的判定只取决于这段回复的内容。"""
        return self.reply


def test_brief_writer_falls_back_to_template_on_stance_violation() -> None:
    writer = LlmBriefWriter(api_key="stub")
    writer.client = _FixedClient("今天可以满仓干，龙头随便买。")
    result = writer.write(
        WriteRequest(
            title="盘前简报",
            facts={"regime": "退潮", "regime_position_cap": 0.30},
            fallback_markdown="模板正文",
        )
    )
    assert result.used_fallback
    assert result.markdown == "模板正文"
    assert "越过确定性层" in result.notes[0]


def test_debate_argument_is_discarded_whole() -> None:
    engine = DebatePanel(client=_FixedClient("- 情绪已回暖，建议满仓参与"))
    result = engine.run({"regime": "退潮", "regime_position_cap": 0.30}, include_judge=False)
    assert [argument.markdown for argument in result.arguments] == ["", ""]
    assert all("越过确定性层" in note for note in result.notes)


def test_chat_reply_with_probability_wording_is_discarded() -> None:
    session = ChatSession(symbols=["600519.SH"])
    session.set_facts("advice", {"symbols": [{"symbol": "600519.SH"}]})
    answer, notes = ChatResponder(_FixedClient("这只票反弹概率很高")).answer(session, "怎么看")
    assert "已丢弃" in answer
    assert notes and notes[0].startswith("口径拦截")
    # 丢弃的回合不进历史：留着它等于让下一轮在一段作废的文字上继续推理。
    assert session.history == []


def test_chat_takes_its_stance_ruler_from_the_sectors_bundle() -> None:
    """`/sectors` 装进来的就是盘前那份读数，里面有当天的仓位系数。

    没装过它时只查数字与措辞——那是「判不了就不判」，不是漏了。
    """
    session = ChatSession(symbols=["600519.SH"])
    session.set_facts("advice", {"symbols": [{"symbol": "600519.SH"}]})
    responder = ChatResponder(_FixedClient("情绪不差，可以满仓做一把"))

    answer, _ = responder.answer(session, "怎么看")
    assert "已丢弃" not in answer

    session.set_facts("sectors", {"position_factor": 0.3, "regime": "退潮"})
    answer, notes = responder.answer(session, "怎么看")
    assert "已丢弃" in answer
    assert any("满仓" in note for note in notes)


def test_the_adjudicators_rationale_goes_through_the_same_gate() -> None:
    """价位裁决是第六个出文字的地方，而它的理由会进台账、也会进简报。

    数字那一档它自己更严（只认数值相等），措辞与立场这两条则和别处一样——
    「胜率更高」一个数字都没有，数字闸门看不见它。
    """
    from ashare_decision_support.advice.adjudicate import LevelAdjudicator
    from ashare_decision_support.advice.candidates import build_candidates

    candidates = build_candidates(
        symbol="600519.SH",
        last_price=23.0,
        vwap=22.95,
        session_low=22.9,
        session_high=23.2,
        span=0.3,
        entry_band=22.95,
        rule_stop=22.85,
    )
    reply = json.dumps(
        {
            "action": "select",
            "entry": candidates.default("entry").key(),
            "stop": candidates.default("stop").key(),
            "target": None,
            "reason": "这个位置胜率更高",
        }
    )
    result = LevelAdjudicator(client=_FixedClient(reply)).adjudicate(candidates)

    assert result.used_fallback
    assert any("预测性措辞" in note for note in result.notes)


def test_facts_shaped_like_the_morning_payload_still_parse() -> None:
    """盘前那份 payload 是 JSON 往返之后进来的，键名与类型都不能靠内存对象。"""
    payload = json.loads(json.dumps({"position_factor": 0.0, "regime": "冰点"}))
    assert OutputGuard.from_facts(payload).stance_conflicts("可以加仓") == [
        "情绪周期 冰点 的仓位上限 0.00 说今天不出手，文字却在说「加仓」"
    ]


def test_every_call_site_goes_through_the_single_entry() -> None:
    """上面几条钉的是行为，这条钉的是结构：**没有人可以只查其中一件**。

    措辞与立场那两条能在五个调用点上同时缺席几个月，正是因为每一处都写着
    `unverified_numbers(...)`——看起来都在过闸。所以出散文的地方一律走
    `review_output`，直接调两个子闸门的只允许出现在 `factcheck.py` 自己。

    真要只查其中一件时，正确做法是往 `factcheck.py` 里加一个说得清用途的入口，
    而不是在调用点上挑一件来查。
    """
    import ast
    import pathlib

    from ashare_decision_support import factcheck

    src = pathlib.Path(factcheck.__file__).parent
    offenders = [
        f"{path.relative_to(src).as_posix()}:{node.lineno}"
        for path in sorted(src.rglob("*.py"))
        if path.name != "factcheck.py"
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"unverified_numbers", "prose_violations"}
    ]
    assert offenders == []
