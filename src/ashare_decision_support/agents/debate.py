"""多空辩论层：LLM 唯一被允许做的推理工作。

为什么不是一个总结器：单个模型总结一份事实时，天然倾向于把反面证据压进从句。
「量比 3.2 倍」这种读数既能写成放量突破、也能写成情绪透支，一个总结器只会挑
一种说法。强制拆成对立两方各自举证，再让第三方指出分歧点取决于哪个价位，能把
这种双面读数顶到台面上。

三个角色共享一条硬约束：**不产生任何新数字**。它们看到的只有确定性层算好的
fact bundle，输出要过 `writer/llm.py:unverified_numbers` 同一套闸门；越界的
整段丢弃，简报退回模板体。

辩论只在盘前与盘后跑（每天 3 次调用）。盘中告警不走这里——见 docs/ARCHITECTURE.md §五。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ashare_decision_support.writer.llm import (
    DEFAULT_PROVIDER,
    LlmClient,
    unverified_numbers,
)

ROLE_BULL = "bull"
ROLE_BEAR = "bear"
ROLE_JUDGE = "judge"

_SHARED_RULES = """硬约束（违反即整段作废）：
1. 只能引用【事实】里出现过的数字，一个字都不能改，更不能算出新数字。
2. 不得新增标的、板块、指标或理由；【事实】里没有的，就是没有。
3. 不得给出「建议买入/卖出」，不得给出概率、胜率、预期收益率。
4. 不确定就省略，不要用推测填补。
输出 Markdown 无序列表，每条一句话，最多 5 条，不要标题。"""

PROMPTS: dict[str, str] = {
    ROLE_BULL: (
        "你是多头分析师。只从【事实】里找**支持做多**的证据，逐条列出并引用具体读数。\n"
        "找不到就直说「本组事实中没有支持做多的证据」。\n\n" + _SHARED_RULES
    ),
    ROLE_BEAR: (
        "你是空头分析师。只从【事实】里找**反对做多、提示风险**的证据，逐条列出并引用具体读数。\n"
        "找不到就直说「本组事实中没有明显的风险证据」。\n\n" + _SHARED_RULES
    ),
    ROLE_JUDGE: (
        "你是裁判。下面是同一组【事实】上的多头意见与空头意见。\n"
        "你的任务：指出双方**真正的分歧点**，以及这个分歧由哪个价位或哪个读数决定。\n"
        "不要判谁赢，不要给操作建议。最多 4 条。\n\n" + _SHARED_RULES
    ),
}


@dataclass
class Argument:
    role: str
    markdown: str
    used_fallback: bool = False
    note: str | None = None


@dataclass
class DebateResult:
    arguments: list[Argument] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return any(not item.used_fallback for item in self.arguments)

    def markdown(self) -> str:
        titles = {ROLE_BULL: "多头视角", ROLE_BEAR: "空头视角", ROLE_JUDGE: "分歧点"}
        blocks = [
            f"**{titles.get(item.role, item.role)}**\n\n{item.markdown}"
            for item in self.arguments
            if item.markdown.strip()
        ]
        return "\n\n".join(blocks)

    def to_dict(self) -> dict:
        return {
            "arguments": [
                {"role": item.role, "markdown": item.markdown, "used_fallback": item.used_fallback}
                for item in self.arguments
            ],
            "notes": self.notes,
        }


class DebatePanel:
    """多头 → 空头 → 裁判。前两个可以并行，裁判必须等它们。"""

    def __init__(
        self,
        provider: str = DEFAULT_PROVIDER,
        model: str | None = None,
        api_key: str | None = None,
        client: Any = None,
        temperature: float = 0.4,
    ) -> None:
        self.client = client or LlmClient(
            provider=provider, model=model, api_key=api_key, temperature=temperature
        )

    def run(self, facts: Any, include_judge: bool = True) -> DebateResult:
        if not getattr(self.client, "configured", False):
            return DebateResult(
                notes=["未配置 LLM key，跳过多空辩论；确定性结论不受影响"]
            )

        facts_text = json.dumps(facts, ensure_ascii=False, indent=2, default=str)
        result = DebateResult()

        bull = self._ask(ROLE_BULL, PROMPTS[ROLE_BULL], facts_text, result)
        bear = self._ask(ROLE_BEAR, PROMPTS[ROLE_BEAR], facts_text, result)
        result.arguments.extend([bull, bear])

        if include_judge and not (bull.used_fallback and bear.used_fallback):
            # The judge sees both arguments *and* the facts, so its own numbers
            # are still checkable against the same source of truth.
            combined = (
                f"【事实】\n{facts_text}\n\n"
                f"【多头意见】\n{bull.markdown}\n\n【空头意见】\n{bear.markdown}"
            )
            result.arguments.append(
                self._ask(ROLE_JUDGE, PROMPTS[ROLE_JUDGE], combined, result, facts_text=facts_text)
            )
        return result

    def _ask(
        self,
        role: str,
        system: str,
        user_content: str,
        result: DebateResult,
        facts_text: str | None = None,
    ) -> Argument:
        try:
            generated = self.client.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"【事实】\n{user_content}"},
                ]
            )
        except Exception as exc:
            note = f"{role} 角色调用失败（{type(exc).__name__}），本轮缺该视角"
            result.notes.append(note)
            return Argument(role=role, markdown="", used_fallback=True, note=note)

        text = (generated or "").strip()
        if not text:
            note = f"{role} 角色返回空内容"
            result.notes.append(note)
            return Argument(role=role, markdown="", used_fallback=True, note=note)

        offenders = unverified_numbers(text, facts_text or user_content)
        if offenders:
            # Discarded whole, not patched. A partially-trustworthy argument is
            # worse than none: the reader cannot tell which half to believe.
            note = f"{role} 角色输出包含无法追溯的数字 {offenders[:5]}，整段丢弃"
            result.notes.append(note)
            return Argument(role=role, markdown="", used_fallback=True, note=note)

        return Argument(role=role, markdown=text)
