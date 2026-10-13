"""LLM writer with a numeric fidelity gate.

The rule you set for the AI layer is "只起组织语言的作用". That is only a real
constraint if it is checked, so it is: every number in the generated text must
appear verbatim in the facts that were handed to the model. A response that
invents a price, a percentage, or a score is discarded and the template output
ships instead.

Providers are OpenAI-compatible, so switching vendors is a config change. Only
the standard library is used — the package deliberately has no HTTP dependency.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from ashare_decision_support.factcheck import OutputGuard, review_output
from ashare_decision_support.writer.base import BriefWriter, WriteRequest, WriteResult


@dataclass(frozen=True)
class LlmProvider:
    key: str
    base_url: str
    model: str
    api_key_env: str


PROVIDERS: dict[str, LlmProvider] = {
    "qwen": LlmProvider(
        key="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-plus",
        api_key_env="DASHSCOPE_API_KEY",
    ),
    "deepseek": LlmProvider(
        key="deepseek",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        api_key_env="DEEPSEEK_API_KEY",
    ),
    "glm": LlmProvider(
        key="glm",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-4-flash",
        api_key_env="GLM_API_KEY",
    ),
    "kimi": LlmProvider(
        key="kimi",
        base_url="https://api.moonshot.cn/v1",
        model="moonshot-v1-8k",
        api_key_env="KIMI_API_KEY",
    ),
}

DEFAULT_PROVIDER = "qwen"

SYSTEM_PROMPT = """你是一名 A 股短线分析助手的文字改写器。

你的工作不是复述表格，而是把规则引擎已经算好的事实写成更像交易者会直接阅读的分析。

严格约束：
1. 只负责改写已算好的结论，不得改变任何结论、数字、标的或风险判断。
2. 不得出现输入数据中没有的数字。价格、涨跌幅、评分、数量一律照抄。
3. 不得新增标的、不得新增板块、不得新增理由；可以合并同类项，但不能补新事实。
4. 不得写「概率」「胜率」「预期收益率」这类数字，也不要把结果包装成预测。
5. 不得直接写成投资建议；你可以保留系统的观察、情景、风险与纪律要求。
6. 输出要像分析，不像报表：先给判断，再讲证据，再讲风险/分歧点/下一步观察条件。
7. 每个数字只能和它在输入里所属的板块或标的一起出现：不得把一个板块的涨跌幅、
   业绩或资金读数安到另一个板块头上，也不得把「5 日涨幅」写成「当日跌幅」或
   「业绩」。记不清一个数属于谁、是什么口径，就不写那个数。

输出格式：Markdown，不要代码块包裹，不要堆装饰。"""


class LlmClient:
    """Minimal OpenAI-compatible chat client.

    Shared by the brief writer and the conversation layer so provider handling,
    credentials, and timeouts live in exactly one place. Standard library only:
    the package has no HTTP dependency and should not acquire one for this.
    """

    def __init__(
        self,
        provider: str = DEFAULT_PROVIDER,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> None:
        config = PROVIDERS.get(provider)
        if config is None:
            raise ValueError(f"unknown provider {provider!r}; expected one of {sorted(PROVIDERS)}")
        self.provider = config
        self.model = model or config.model
        self.base_url = (base_url or config.base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv(config.api_key_env)
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list[dict]) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"]


class LlmBriefWriter(BriefWriter):
    name = "llm"

    def __init__(
        self,
        provider: str = DEFAULT_PROVIDER,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> None:
        self.client = LlmClient(
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        self.provider = self.client.provider
        self.model = self.client.model
        self.base_url = self.client.base_url
        self.name = f"llm:{self.client.provider.key}"

    @property
    def api_key(self) -> str | None:
        return self.client.api_key

    @api_key.setter
    def api_key(self, value: str | None) -> None:
        self.client.api_key = value

    def write(self, request: WriteRequest) -> WriteResult:
        if not self.api_key:
            return self._fallback(request, f"未配置 {self.provider.api_key_env}，改用模板输出")

        facts_text = json.dumps(request.facts, ensure_ascii=False, indent=2, default=str)
        try:
            generated = self._complete(self._user_prompt(request, facts_text))
        except Exception as exc:
            return self._fallback(request, f"LLM 调用失败（{type(exc).__name__}），改用模板输出")

        if not generated.strip():
            return self._fallback(request, "LLM 返回空内容，改用模板输出")

        # 三件一起查：数字能不能追溯、有没有预测性措辞、有没有改掉规则层的结论。
        # 处置只有一种——整段丢弃回落模板，因为一份「结论被悄悄改过」的简报比没有
        # 简报更危险：它读起来和规则层算出来的那份一模一样。
        review = review_output(generated, facts_text, OutputGuard.from_facts(request.facts))
        if not review.ok:
            return self._fallback(request, f"LLM 输出{review.reason()}，已丢弃并改用模板输出")

        # Compliance text is appended, never generated. The model summarised the
        # discipline block away every time it was merely asked to keep it.
        markdown = _strip_leading_title(generated.strip())
        if request.mandatory_suffix and request.mandatory_suffix.strip() not in markdown:
            markdown = f"{markdown}\n\n{request.mandatory_suffix.strip()}"
        return WriteResult(markdown=markdown, writer=self.name)

    def _user_prompt(self, request: WriteRequest, facts_text: str) -> str:
        audience = request.audience_note or "读者是本系统的唯一使用者，本人做 A 股超短线。"
        verbatim = ""
        if request.verbatim_sections:
            names = "、".join(section.lstrip("#").strip() for section in request.verbatim_sections)
            verbatim = (
                f"「{names}」这几节的表格会由系统在稿后按原样附上，不必复述表格；"
                "正文里可以点名先看哪几只，但价位以附表为准。\n"
            )
        return (
            f"{audience}\n\n"
            f"以下是系统已经算好的结构化结论（JSON）。请把它改写成一份可读的《{request.title}》。\n"
            f"要求：先给一句判断，再展开证据、风险、分歧点和下一步观察条件；\n"
            f"能合并的信息就合并，尽量不要逐字段复述 JSON。\n"
            f"保留原有的分节结构与全部风险提示，语言简洁，但要像在做分析，不要像在记账。\n"
            f"{verbatim}\n"
            f"```json\n{facts_text}\n```"
        )

    def _complete(self, prompt: str) -> str:
        return self.client.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )

    def _fallback(self, request: WriteRequest, note: str) -> WriteResult:
        return WriteResult(
            markdown=request.fallback_markdown,
            writer=self.name,
            used_fallback=True,
            notes=[note],
        )


def _strip_leading_title(markdown: str) -> str:
    """Drop a leading H1.

    Writers return a body; the notifier owns the title and prepends it. The
    model titles its output anyway, which showed up as the heading printed
    twice in the delivered file.
    """
    if not markdown.startswith("# "):
        return markdown
    _, _, rest = markdown.partition("\n")
    return rest.lstrip("\n")
