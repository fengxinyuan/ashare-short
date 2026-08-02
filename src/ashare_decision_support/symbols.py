"""Symbol normalization, shared by every layer that stores or compares codes.

Codes arrive in three shapes depending on the source — `600519.SH` from
TickFlow, `600519` from a dragon-tiger row, `sh600519` from Sina — and any
layer that persists or compares them has to agree on one. Two lists that
disagree about whether a watchlist entry is `600519` or `600519.SH` will
silently fail to match, and the failure looks like "the alert never fired".
"""

from __future__ import annotations

# Prefix → exchange suffix. Ordered longest-first is unnecessary here because
# every A-share code prefix compared below is exactly three digits.
SH_PREFIXES = ("600", "601", "603", "605", "688", "689", "730", "731", "732", "733")
SZ_PREFIXES = ("000", "001", "002", "003", "300", "301")
BJ_PREFIXES = (
    "430", "440", "830", "831", "832", "833", "834", "835", "836", "837",
    "838", "839", "870", "871", "872", "873", "920",
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
