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


def publish(notifiers: list[Notifier], title: str, content: str) -> list[str]:
    """挨个投递，谁挂了跳过谁。返回**投递成功**的渠道名。

    通知是**副作用**，不是结果。一个渠道的契约违约（webhook 404、磁盘满）不该
    让盘中循环或守护进程停摆——那等于让最不重要的一环决定最重要的一环跑不跑。

    返回投递结果而不是 None，是因为六个 job 都要把它写进 summary（「这份简报发到
    了哪几个通道」）。此前它们各写一遍 `[c.capabilities.channel for c in channels
    if c.send(...)]`——那个写法拿得到结果，但**没有这里的 try**：内置的三个
    notifier 恰好都不抛，所以它一直没出事，可它守的是一个契约而不是一个事实。
    自定义通道一旦抛出来，崩的是已经算完的那份简报。
    """
    delivered: list[str] = []
    for notifier in notifiers:
        try:
            if notifier.send(title, content):
                delivered.append(notifier.capabilities.channel)
        except Exception:  # noqa: S112 — 见上：一个渠道不能决定整轮跑不跑
            continue
    return delivered
