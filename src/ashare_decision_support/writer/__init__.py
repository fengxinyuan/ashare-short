"""Brief writing: deterministic template plus an LLM rewrite layer."""

from ashare_decision_support.writer.base import BriefWriter, WriteRequest, WriteResult
from ashare_decision_support.writer.llm import DEFAULT_PROVIDER, PROVIDERS, LlmBriefWriter
from ashare_decision_support.writer.template import TemplateWriter

__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDERS",
    "BriefWriter",
    "LlmBriefWriter",
    "TemplateWriter",
    "WriteRequest",
    "WriteResult",
]
