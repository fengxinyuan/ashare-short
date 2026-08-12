"""A-share ultra-short analysis and decision support package."""

__all__ = ["__version__"]

# 0.3.0：自选股与候选池删掉，`watchlist` 命令换成 `position`——对着旧命令写的
# 脚本会失败，所以这是 minor 而不是 patch。见 docs/DECISIONS.md §十八。
__version__ = "0.3.0"
