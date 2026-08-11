"""四道闸门跑一遍，任意一道红就非零退出。

这个仓库有四条独立的口径，各自都有存在的理由：

    ruff check .                     静态规则（规则族与每条忽略的理由在 pyproject）
    pytest -q                        行为
    scripts/inventory_audit.py       六条接线口径：模块/符号/字段/方法/私有/常量
    scripts/docs_audit.py            文档里的命令与链接还成不成立

**没有 CI，没有 git 钩子，也没有 remote**——所以这四条只在有人记得的时候才跑。
而它们恰恰是那种「不跑就完全看不出来」的检查：接线审计红了不影响任何测试，
文档审计红了不影响任何代码，两者都能绿着烂上两个月（`INVENTORY.md` §0 的三个
死符号、§5 那条照文档敲会直接报错的 `advise` 命令，都是这么攒出来的）。

四条变一条，是为了让「全都跑一遍」不再是一次需要回忆的动作：

    python3 scripts/check.py           # 四道全跑
    python3 scripts/check.py --fast    # 跳过 pytest，只跑三道静态的

顺序是**由快到慢**：ruff 秒级、两个审计各几秒、pytest 最久。先红的先报，
但**不短路**——四条互相独立，一次把四个结论都给出来，比修一条重跑一次快。
退出码是「有几道红」，不是第一道红的退出码。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

GATES: list[tuple[str, list[str], bool]] = [
    # (名字, 命令, 是不是慢的那档)
    ("ruff", [sys.executable, "-m", "ruff", "check", "."], False),
    ("接线审计", [sys.executable, "scripts/inventory_audit.py"], False),
    ("文档审计", [sys.executable, "scripts/docs_audit.py"], False),
    ("pytest", [sys.executable, "-m", "pytest", "-q"], True),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true", help="跳过 pytest，只跑三道静态的")
    args = parser.parse_args()

    failed: list[str] = []
    for name, command, slow in GATES:
        if slow and args.fast:
            print(f"—— {name}：--fast 跳过")
            continue
        started = time.monotonic()
        # S603：命令全部来自上面那张字面量表，没有任何一段拼进了外部输入。
        result = subprocess.run(command, check=False)  # noqa: S603
        elapsed = time.monotonic() - started
        verdict = "通过" if result.returncode == 0 else "**红**"
        print(f"—— {name}：{verdict}（{elapsed:.1f}s）\n")
        if result.returncode != 0:
            failed.append(name)

    if failed:
        print(f"{len(failed)} 道红：{'、'.join(failed)}")
    else:
        print("四道全绿" if not args.fast else "三道静态口径全绿（pytest 未跑）")
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
