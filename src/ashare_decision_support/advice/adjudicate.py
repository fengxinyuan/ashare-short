"""L3 价位裁决：模型选择已有候选键或弃权。

候选键必须存在，说明中的数字须对应候选价或 L1 事实。
未配置模型、调用失败或输出无效时回退 default_rank == 0 的规则候选。
台账同时保留规则默认与模型裁决，供后续比较。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.advice.candidates import (
    SIDE_ENTRY,
    SIDE_STOP,
    SIDE_TARGET,
    CandidateSet,
    PriceCandidate,
)
from ashare_decision_support.factcheck import (
    NUMBER_PATTERN,
    ORDINAL_CEILING,
    OutputGuard,
    fact_numbers,
    review_output,
)
from ashare_decision_support.llmreply import parse_object
from ashare_decision_support.writer.llm import DEFAULT_PROVIDER, LlmClient

SYSTEM_PROMPT = """你是一名 A 股超短线交易员的价位裁决助手。

规则层已经算好了**若干个候选价位**，每个都带明确出处。你的任务只有一件：
**从候选里挑**，并说明为什么挑它而不是别的。

硬约束（违反即整条作废，系统回退规则默认值）：
1. 你只能通过 `key` 指认候选，**不得写出任何候选之外的价格**。
2. 说明文字里出现的数字，必须原样来自候选价或【事实】里的读数，一个字都不能改。
3. 不得计算：不得相加、取中值、取整、留缓冲。要一个中间价你也没有这个权限。
4. 不得给概率、胜率、预期收益率——系统没有拟合模型。
5. 判断今天不该买，就弃权，这是允许且被鼓励的答案。

只输出一个 JSON 对象，不要代码块，不要任何额外文字：

{
  "action": "select" 或 "abstain",
  "entry": "候选的 key 字符串（action=select 时必填）",
  "stop": "候选的 key 字符串（action=select 时必填）",
  "target": "候选的 key 字符串，没有合适的就填 null",
  "reason": "一句话，不超过 60 字，说明取舍依据"
}

弃权时 entry/stop/target 一律填 null，reason 写明为什么今天不该动。"""


@dataclass
class Adjudication:
    """裁决结果。`used_fallback=True` 时下面的选择全部来自规则默认。"""

    symbol: str
    entry: PriceCandidate | None = None
    stop: PriceCandidate | None = None
    target: PriceCandidate | None = None
    abstained: bool = False
    reason: str = ""
    used_fallback: bool = True
    notes: list[str] = field(default_factory=list)

    # 规则默认值一并留着，好让台账能同时记两套结论。
    rule_entry: PriceCandidate | None = None
    rule_stop: PriceCandidate | None = None
    rule_target: PriceCandidate | None = None

    @property
    def overrode_rule(self) -> bool:
        """模型是不是真的改了规则的排序。相同的选择不算「模型做了决定」。"""
        if self.used_fallback or self.abstained:
            return False
        return any(
            chosen is not None and chosen is not default
            for chosen, default in (
                (self.entry, self.rule_entry),
                (self.stop, self.rule_stop),
                (self.target, self.rule_target),
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "abstained": self.abstained,
            "used_fallback": self.used_fallback,
            "overrode_rule": self.overrode_rule,
            "reason": self.reason,
            "entry": self.entry.to_dict() if self.entry else None,
            "stop": self.stop.to_dict() if self.stop else None,
            "target": self.target.to_dict() if self.target else None,
            "rule_entry": self.rule_entry.to_dict() if self.rule_entry else None,
            "rule_stop": self.rule_stop.to_dict() if self.rule_stop else None,
            "rule_target": self.rule_target.to_dict() if self.rule_target else None,
            "notes": self.notes,
        }


def rule_default(candidates: CandidateSet) -> Adjudication:
    """规则自己的答案。这条路径不碰网络、不需要 key，永远可用。"""
    return Adjudication(
        symbol=candidates.symbol,
        entry=candidates.default(SIDE_ENTRY),
        stop=candidates.default(SIDE_STOP),
        target=candidates.default(SIDE_TARGET),
        used_fallback=True,
        rule_entry=candidates.default(SIDE_ENTRY),
        rule_stop=candidates.default(SIDE_STOP),
        rule_target=candidates.default(SIDE_TARGET),
    )


class LevelAdjudicator:
    """从候选集里选价。任何异常都退回规则默认，不抛。"""

    def __init__(
        self,
        provider: str = DEFAULT_PROVIDER,
        model: str | None = None,
        api_key: str | None = None,
        client: Any = None,
        temperature: float = 0.2,
    ) -> None:
        # 温度压得比简报低：这里要的是稳定的取舍，不是措辞多样性。
        self.client = client or LlmClient(
            provider=provider, model=model, api_key=api_key, temperature=temperature
        )

    @property
    def configured(self) -> bool:
        return bool(getattr(self.client, "configured", False))

    def adjudicate(self, candidates: CandidateSet, facts: Any = None) -> Adjudication:
        fallback = rule_default(candidates)
        if candidates.is_empty:
            fallback.notes.append("候选集为空，无可裁决内容")
            return fallback
        if not self.configured:
            fallback.notes.append("未配置 LLM key，买卖点走规则默认排序")
            return fallback

        facts_text = json.dumps(
            {"候选": candidates.to_dict(), "事实": facts or {}},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        try:
            raw = self.client.chat(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": facts_text},
                ]
            )
        except Exception as exc:
            fallback.notes.append(f"裁决调用失败（{type(exc).__name__}），退回规则默认")
            return fallback

        payload = parse_object(raw or "")
        if payload is None:
            fallback.notes.append("裁决输出不是可解析的 JSON，退回规则默认")
            return fallback

        reason = str(payload.get("reason") or "").strip()[:120]
        offenders = offending_numbers(reason, candidates, facts_text)
        if offenders:
            # 整条丢弃而不是抹掉那个数：一段只有一半可信的裁决理由，比没有理由
            # 更危险——读的人分辨不出该信哪一半。
            fallback.notes.append(
                f"裁决理由含无法追溯的数字 {offenders[:3]}，整条丢弃，退回规则默认"
            )
            return fallback

        # 提示词第 4 条（不得给概率）与「不得改变结论」在这一层同样成立，而它们
        # 数字闸门看不见——「胜率更高」一个数字都没有。数字那一档不交给共用入口：
        # 这一层只认数值相等，比散文那边严（见 `factcheck.py` 开头的表）。
        crossed = review_output(reason, guard=OutputGuard.from_facts(facts))
        if not crossed.ok:
            fallback.notes.append(f"裁决理由{crossed.reason()}，整条丢弃，退回规则默认")
            return fallback

        if str(payload.get("action") or "").lower() == "abstain":
            return Adjudication(
                symbol=candidates.symbol,
                abstained=True,
                reason=reason or "模型判断今天不该买，未给出理由",
                used_fallback=False,
                notes=["模型弃权：本轮不给买入价位"],
                rule_entry=fallback.rule_entry,
                rule_stop=fallback.rule_stop,
                rule_target=fallback.rule_target,
            )

        entry = _resolve(candidates, payload.get("entry"), SIDE_ENTRY)
        stop = _resolve(candidates, payload.get("stop"), SIDE_STOP)
        target = _resolve(candidates, payload.get("target"), SIDE_TARGET)

        if entry is None or stop is None:
            # 缺 entry 或 stop 意味着模型指了一个不存在的 key，或者指错了 side。
            # 两者都说明它没在候选集里作业，不能只补全缺的那一半。
            fallback.notes.append("裁决未能指认有效的买入价或止损价，退回规则默认")
            return fallback
        if stop.price >= entry.price:
            fallback.notes.append(
                f"裁决给出的止损 {stop.price} 不低于买入 {entry.price}，逻辑不成立，退回规则默认"
            )
            return fallback

        notes: list[str] = []
        result = Adjudication(
            symbol=candidates.symbol,
            entry=entry,
            stop=stop,
            target=target,
            reason=reason,
            used_fallback=False,
            notes=notes,
            rule_entry=fallback.rule_entry,
            rule_stop=fallback.rule_stop,
            rule_target=fallback.rule_target,
        )
        if result.overrode_rule:
            notes.append(
                "模型推翻了规则的默认排序："
                + "；".join(
                    f"{label} 规则 {default.basis} {default.price} → 模型 {chosen.basis} {chosen.price}"
                    for label, chosen, default in (
                        ("买入", entry, fallback.rule_entry),
                        ("止损", stop, fallback.rule_stop),
                    )
                    if chosen is not None and default is not None and chosen is not default
                )
            )
        return result


def offending_numbers(text: str, candidates: CandidateSet, facts_text: str) -> list[str]:
    """裁决文字里**不能恰好对上**任何候选价或事实读数的数字。

    和 `factcheck.py:unverified_numbers` 的区别只有一条，但很关键：那边接受
    「按你写的精度四舍五入之后相等」，这边只接受**数值相等**。散文改写会重排
    数字，价位不会——把 22.9512 写成 22.95 在简报里是正常的表述，在挂单价上是
    另一个价。

    小整数（≤20 且无小数点）放行：它们是「第 3 档」「5 档买盘」这类序数。
    """
    allowed = candidates.prices()
    fact_tokens, fact_values = fact_numbers(facts_text)
    offenders: list[str] = []
    for match in NUMBER_PATTERN.finditer(text):
        raw = match.group()
        token = raw.replace(",", "")
        if token in fact_tokens:
            continue
        try:
            value = float(token)
        except ValueError:
            continue
        if "." not in token and value <= ORDINAL_CEILING:
            continue
        if value in fact_values or round(value, 3) in allowed:
            continue
        offenders.append(raw)
    return list(dict.fromkeys(offenders))


def _resolve(candidates: CandidateSet, key: Any, side: str) -> PriceCandidate | None:
    if not isinstance(key, str) or not key.strip():
        return None
    found = candidates.by_key(key.strip())
    # side 也要对：模型把止损位填进 entry 字段时，价格本身是合法候选，但那组
    # 结论是错的。按 key 查到还不够，还得查到的是这一侧的候选。
    return found if found is not None and found.side == side else None
