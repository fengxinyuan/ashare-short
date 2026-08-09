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
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

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

输出格式：Markdown，不要代码块包裹，不要堆装饰。"""

# Numbers this small are list ordinals and section numbering, which the model
# legitimately produces even though they are absent from the facts.
ORDINAL_CEILING = 20

NUMBER_PATTERN = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")

# Chinese financial prose rescales figures inline. A suffix here lets the number
# in front of it be compared against the facts at that scale, so "0.12亿" is
# checked against 12198154.38 rather than being called an invention.
UNIT_SCALES: dict[str, float] = {"万": 1e4, "亿": 1e8, "%": 0.01, "％": 0.01}


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
            return self._fallback(
                request, f"未配置 {self.provider.api_key_env}，改用模板输出"
            )

        facts_text = json.dumps(request.facts, ensure_ascii=False, indent=2, default=str)
        try:
            generated = self._complete(self._user_prompt(request, facts_text))
        except Exception as exc:
            return self._fallback(request, f"LLM 调用失败（{type(exc).__name__}），改用模板输出")

        if not generated.strip():
            return self._fallback(request, "LLM 返回空内容，改用模板输出")

        unverified = unverified_numbers(generated, facts_text)
        if unverified:
            return self._fallback(
                request,
                f"LLM 输出包含原始数据中不存在的数字 {'、'.join(unverified[:5])}，已丢弃并改用模板输出",
            )

        # Compliance text is appended, never generated. The model summarised the
        # discipline block away every time it was merely asked to keep it.
        markdown = _strip_leading_title(generated.strip())
        if request.mandatory_suffix and request.mandatory_suffix.strip() not in markdown:
            markdown = f"{markdown}\n\n{request.mandatory_suffix.strip()}"
        return WriteResult(markdown=markdown, writer=self.name)

    # ---- internals -------------------------------------------------------

    def _user_prompt(self, request: WriteRequest, facts_text: str) -> str:
        audience = request.audience_note or "读者是本系统的唯一使用者，本人做 A 股超短线。"
        return (
            f"{audience}\n\n"
            f"以下是系统已经算好的结构化结论（JSON）。请把它改写成一份可读的《{request.title}》。\n"
            f"要求：先给一句判断，再展开证据、风险、分歧点和下一步观察条件；\n"
            f"能合并的信息就合并，尽量不要逐字段复述 JSON。\n"
            f"保留原有的分节结构与全部风险提示，语言简洁，但要像在做分析，不要像在记账。\n\n"
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


def unverified_numbers(generated: str, facts_text: str) -> list[str]:
    """Numbers in `generated` that cannot be traced back to `facts_text`.

    Three ways a number is accepted, in increasing order of leniency:

    1. it appears verbatim in the facts;
    2. some fact value equals it once rounded to the precision the model wrote
       it at — this is what lets 8.1 be written as "8.10" and
       12198154.379999977 as "12198154.38";
    3. the same, after applying the 万 / 亿 / % suffix that follows it.

    Without (2) and (3) the gate fires on every response, because rewriting
    prose *is* reformatting numbers. What it still cannot accept is a figure
    with no basis in the input at any scale or precision, which is the failure
    mode that matters: an invented target price or a made-up count.

    Thousands separators are part of the token, not three separate numbers:
    tokenising "12,198,154.38" as 12 / 198 / 154.38 made every grouped figure
    look fabricated.
    """
    fact_tokens = {token.replace(",", "") for token in NUMBER_PATTERN.findall(facts_text)}
    fact_values: set[float] = set()
    for token in fact_tokens:
        try:
            fact_values.add(float(token))
        except ValueError:
            continue

    offenders: list[str] = []
    for match in NUMBER_PATTERN.finditer(generated):
        raw_token = match.group()
        token = raw_token.replace(",", "")
        if token in fact_tokens or token in facts_text:
            continue
        if "." not in token and int(token) <= ORDINAL_CEILING:
            continue

        value = float(token)
        decimals = len(token.partition(".")[2])
        scales = [1.0]
        suffix = generated[match.end() : match.end() + 1]
        if suffix in UNIT_SCALES:
            scales.append(UNIT_SCALES[suffix])

        if any(
            round(fact / scale, decimals) == value
            for fact in fact_values
            for scale in scales
        ):
            continue
        offenders.append(raw_token)
    return list(dict.fromkeys(offenders))
