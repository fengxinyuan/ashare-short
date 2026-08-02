"""Contracts for the brief-writing layer.

The split exists so the LLM is never load-bearing. A writer receives facts that
have *already been decided* by the rule engine and returns prose. If it fails,
times out, or invents a number, the caller drops back to the template writer
and the brief still goes out — just less fluent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WriteRequest:
    """Everything the writer may use. There is no other input channel.

    `facts` is the serialized decision output. The LLM prompt is built only from
    this, which is what makes the numeric check below meaningful: any number in
    the response that is not in `facts` was invented.

    `mandatory_suffix` holds text that must appear verbatim in the delivered
    brief — auction discipline, caveats, the disclaimer. It is appended after
    generation rather than described in the prompt, because a model asked to
    "keep the risk warnings" quietly drops them when it summarises.
    """

    title: str
    facts: dict[str, Any]
    fallback_markdown: str
    audience_note: str = ""
    mandatory_suffix: str = ""


@dataclass
class WriteResult:
    markdown: str
    writer: str
    used_fallback: bool = False
    notes: list[str] = field(default_factory=list)


class BriefWriter(ABC):
    name: str = "base"

    @abstractmethod
    def write(self, request: WriteRequest) -> WriteResult:
        """Return the brief body."""
