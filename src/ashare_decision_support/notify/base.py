"""Notifier contract.

A notifier takes an already-rendered markdown document and delivers it. It never
raises: delivery is a side channel, and a broken webhook must not take down the
analysis run. Failures are reported via the boolean return value.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class NotifierCapabilities:
    channel: str
    markdown: bool
    max_bytes: int


class Notifier(ABC):
    """Delivers a rendered brief to one channel."""

    capabilities: NotifierCapabilities

    @abstractmethod
    def send(self, title: str, content: str) -> bool:
        """Deliver `content`. Returns True on success, False on failure."""


def split_utf8_text(text: str, max_bytes: int) -> list[str]:
    """Split text on line boundaries while staying inside a UTF-8 byte limit.

    Chat webhooks limit payloads in bytes, not characters, and Chinese text is
    3 bytes per character — so a naive character count silently truncates.
    """
    raw = str(text or "")
    limit = max(int(max_bytes), 1)
    if len(raw.encode("utf-8")) <= limit:
        return [raw]

    chunks: list[str] = []
    current = ""
    for piece in raw.splitlines(keepends=True) or [raw]:
        if current and len((current + piece).encode("utf-8")) > limit:
            chunks.append(current.rstrip())
            current = ""
        if len(piece.encode("utf-8")) <= limit:
            current += piece
            continue
        if current:
            chunks.append(current.rstrip())
            current = ""
        chunks.extend(_split_long_piece(piece, limit))
    if current:
        chunks.append(current.rstrip())
    return [chunk for chunk in chunks if chunk]


def _split_long_piece(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for char in text:
        if current and len((current + char).encode("utf-8")) > limit:
            chunks.append(current)
            current = char
        else:
            current += char
    if current:
        chunks.append(current)
    return chunks
