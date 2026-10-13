"""Symbol normalization, shared by every layer that stores or compares codes.

Codes arrive in three shapes depending on the source — `600519.SH` from
TickFlow, `600519` from a dragon-tiger row, `sh600519` from Sina — and any
layer that persists or compares them has to agree on one. Two lists that
disagree about whether a position entry is `600519` or `600519.SH` will
silently fail to match, and the failure looks like "the alert never fired".
"""

from __future__ import annotations

# Prefix → exchange suffix. Ordered longest-first is unnecessary here because
# every A-share code prefix compared below is exactly three digits.
SH_PREFIXES = ("600", "601", "603", "605", "688", "689", "730", "731", "732", "733")
SZ_PREFIXES = ("000", "001", "002", "003", "300", "301")
BJ_PREFIXES = (
    "430",
    "440",
    "830",
    "831",
    "832",
    "833",
    "834",
    "835",
    "836",
    "837",
    "838",
    "839",
    "870",
    "871",
    "872",
    "873",
    "920",
)

VALID_SUFFIXES = ("SH", "SZ", "BJ")


def normalize_symbol(symbol: str) -> str:
    """Return the canonical `NNNNNN.XX` form.

    Anything unrecognisable is returned upper-cased and unchanged rather than
    guessed at — a wrong exchange suffix fetches a different company's bars.
    """
    text = str(symbol or "").strip().upper()
    if not text:
        return ""

    # `SH600519` / `sh600519`
    if len(text) == 8 and text[:2] in VALID_SUFFIXES and text[2:].isdigit():
        return f"{text[2:]}.{text[:2]}"

    code, _, suffix = text.partition(".")
    if suffix:
        return f"{code}.{suffix}" if suffix in VALID_SUFFIXES else text
    if not (code.isdigit() and len(code) == 6):
        return text
    return f"{code}.{_exchange_of(code)}"


def bare_code(symbol: str) -> str:
    """`600519.SH` -> `600519`. Used when matching against sources that drop the suffix."""
    return normalize_symbol(symbol).partition(".")[0]


def prefixed_code(symbol: str) -> str:
    """`600519.SH` / `600519` / `sh600519` -> `sh600519`.

    新浪与腾讯的行情端点认的都是这一种形状，所以快照、逐笔明细与分钟线三路要的
    是同一个转换。三处原来各写了一份，其中 `sina_intraday` 那份**自己抄了一遍
    交易所前缀表**——抄的是 2026-08 之前的版本，少了 `730/731/732/733`，于是裸
    码 `730001` 在那一路被判成 `bj730001`，另外两路是 `sh730001`。这正是重复常量
    的失效形状：往 `SH_PREFIXES` 里加前缀的人不会知道还有第二张表，两边都不报错。

    收在这里而不是 `adapters/http.py`：这是**代码形状**的事，和用什么协议取数
    无关。判交易所只有 `_exchange_of` 一处，其余全部经由 `normalize_symbol`。
    """
    code, _, suffix = normalize_symbol(symbol).partition(".")
    return f"{suffix.lower()}{code}" if suffix else code


def _exchange_of(code: str) -> str:
    if code.startswith(SH_PREFIXES):
        return "SH"
    if code.startswith(SZ_PREFIXES):
        return "SZ"
    if code.startswith(BJ_PREFIXES):
        return "BJ"
    # Unallocated prefix. SH is the historical default and is what the
    # TickFlow adapter has always assumed; keeping it consistent avoids a
    # silent behaviour change for existing callers.
    return "SH"
