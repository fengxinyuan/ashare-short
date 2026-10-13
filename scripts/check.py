"""统一运行 Ruff 静态与格式检查、接线审计、文档审计和 pytest。

检查由快到慢执行，任一失败后仍继续其他检查；最终退出码为失败的检查数量。
使用 --fast 跳过 pytest。检查范围、离线回归与手动验收见 docs/TESTING.md。

    python3 scripts/check.py
    python3 scripts/check.py --fast
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

GATES: list[tuple[str, list[str], bool]] = [
    # (检查名, 命令, 是否在 --fast 时跳过)
    ("Ruff 静态检查", [sys.executable, "-m", "ruff", "check", "."], False),
    ("Ruff 格式检查", [sys.executable, "-m", "ruff", "format", "--check", "."], False),
    ("接线审计", [sys.executable, "scripts/inventory_audit.py"], False),
    ("文档审计", [sys.executable, "scripts/docs_audit.py"], False),
    ("pytest", [sys.executable, "-m", "pytest", "-q"], True),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true", help="跳过 pytest，只运行静态检查")
    args = parser.parse_args()

    failed: list[str] = []
    completed = 0
    for name, command, slow in GATES:
        if slow and args.fast:
            print(f"—— {name}：--fast 跳过", flush=True)
            continue
        started = time.monotonic()
        print(f"—— {name}：开始", flush=True)
        # 命令来自固定检查列表。
        result = subprocess.run(command, cwd=ROOT, check=False)  # noqa: S603
        elapsed = time.monotonic() - started
        completed += 1
        verdict = "通过" if result.returncode == 0 else f"失败（退出码 {result.returncode}）"
        print(f"—— {name}：{verdict}（{elapsed:.1f}s）\n", flush=True)
        if result.returncode != 0:
            failed.append(name)

    if failed:
        print(f"{len(failed)}/{completed} 项检查失败：{'、'.join(failed)}")
    else:
        suffix = "（pytest 未运行）" if args.fast else ""
        print(f"{completed} 项检查全部通过{suffix}")
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
