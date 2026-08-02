"""The default writer: hand back the deterministic markdown unchanged.

Zero cost, zero latency, zero hallucination risk. Every other writer is
measured against this one, and every other writer falls back to it.
"""

from __future__ import annotations

from ashare_decision_support.writer.base import BriefWriter, WriteRequest, WriteResult


class TemplateWriter(BriefWriter):
    name = "template"

    def write(self, request: WriteRequest) -> WriteResult:
        return WriteResult(markdown=request.fallback_markdown, writer=self.name)
