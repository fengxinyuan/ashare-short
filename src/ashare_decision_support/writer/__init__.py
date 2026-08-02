"""Brief writing: deterministic template by default, LLM as an opt-in overlay."""

from ashare_decision_support.writer.base import BriefWriter, WriteRequest, WriteResult
from ashare_decision_support.writer.llm import (
    DEFAULT_PROVIDER,
    PROVIDERS,
    LlmBriefWriter,
    unverified_numbers,
)
from ashare_decision_support.writer.template import TemplateWriter

__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDERS",
    "BriefWriter",
    "LlmBriefWriter",
    "TemplateWriter",
    "WriteRequest",
    "WriteResult",
    "unverified_numbers",
]
