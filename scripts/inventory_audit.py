"""Recompute `docs/INVENTORY.md`'s two wiring checks instead of trusting them.

That document's whole value is that its answer is a **checkable fact**, not an
impression. It states two criteria — and until 2026-08-09 only the first had
ever actually been run:

- **可达性** — 从 `cli.py` 出发，import 图能不能走到这个模块
- **符号引用** — 模块里的公开符号有没有人真的用；只被 `__init__.py` 转出口、
  再没有第二个引用点的，算未接线

第二条那次补跑命中 3 个死符号（见 INVENTORY §0）。写成脚本是为了让它下次还能跑：
一条写在文档里、没人执行的口径，和没有这条口径是一回事。

    python3 scripts/inventory_audit.py            # 两条都跑
    python3 scripts/inventory_audit.py --symbols  # 只跑第二条

退出码 1 表示有未接线的模块或符号——可以直接挂进 CI。
"""

from __future__ import annotations

import argparse
import ast
import collections
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PKG = SRC / "ashare_decision_support"
# 引用要在**全仓**范围内数：只在 src/ 里数会把「仅测试用到」误判成死码，
# 而那类符号是活的（测试是调用方）。
SCAN_ROOTS = [SRC, ROOT / "tests", ROOT / "scripts"]


def _py_files(*roots: pathlib.Path) -> list[pathlib.Path]:
    return [
        p
        for root in roots
        if root.exists()
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


def _module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


# --- 第一条：从 cli.py 出发的 import 可达性 ---------------------------------


def _imports_of(path: pathlib.Path) -> set[str]:
    """本包内被这个文件 import 的模块名。相对 import 也解析成绝对名。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()

    pkg = _module_name(path).rsplit(".", 1)[0] if "." in _module_name(path) else ""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对 import
                base = pkg.split(".")
                base = base[: len(base) - node.level + 1] if node.level > 1 else base
                mod = ".".join([*base, node.module]) if node.module else ".".join(base)
            else:
                mod = node.module or ""
            if mod:
                found.add(mod)
                # `from pkg.mod import X` 里 X 也可能是子模块
                found.update(f"{mod}.{a.name}" for a in node.names)
    return {m for m in found if m.startswith("ashare_decision_support")}


def reachable_from_cli() -> set[str]:
    by_name = {_module_name(p): p for p in _py_files(SRC)}
    seen: set[str] = set()
    queue = ["ashare_decision_support.cli"]
    while queue:
        name = queue.pop()
        if name in seen or name not in by_name:
            continue
        seen.add(name)
        for dep in _imports_of(by_name[name]):
            # `from x.y import z` 中 z 是符号而非模块时，落不到 by_name，自然跳过
            queue.extend([dep, dep.rsplit(".", 1)[0]])
    return seen


def check_modules() -> list[str]:
    by_name = {_module_name(p): p for p in _py_files(SRC)}
    reached = reachable_from_cli()
    # `__init__.py` 与包根本身不单独判定：它们是转出口，不是功能模块。
    orphans = sorted(
        name
        for name in by_name
        if name not in reached and not name.endswith("__init__") and name != "ashare_decision_support"
    )
    print(f"[模块] {len(by_name)} 个，{len(reached & set(by_name))} 个从 cli.py 可达")
    for name in orphans:
        print(f"  未接线  {name}")
    return orphans


# --- 第二条：公开符号有没有被真正引用 ---------------------------------------


def check_symbols() -> list[str]:
    inits = set(PKG.rglob("__init__.py"))
    defs: dict[str, list[tuple[str, int]]] = {}
    for path in _py_files(SRC):
        if path in inits:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in tree.body:  # 只看模块顶层：嵌套定义不是公开面
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and not node.name.startswith("_"):
                defs.setdefault(node.name, []).append((str(path.relative_to(ROOT)), node.lineno))

    # 按文件数 token 出现次数。正则而非 AST：装饰器、字符串里的名字、`__all__`
    # 条目都要算进来，宁可**高**估引用数——这条检查只在「确实一个引用都没有」
    # 时报警，误放过好过误删。
    per_file: dict[pathlib.Path, collections.Counter] = {}
    for path in _py_files(*SCAN_ROOTS):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        per_file[path] = collections.Counter(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))

    dead: list[str] = []
    for name, locs in sorted(defs.items()):
        total = sum(c[name] for c in per_file.values())
        in_init = sum(per_file[p][name] for p in inits if p in per_file)
        # 定义处自身占 len(locs) 次；其余全在 __init__ 里 ⇒ 只有转出口，没有调用方
        if total - len(locs) - in_init == 0 and in_init > 0:
            where = ", ".join(f"{f}:{ln}" for f, ln in locs)
            dead.append(name)
            print(f"  仅转出口  {name}  ({where})")

    print(f"[符号] {len(defs)} 个公开符号，{len(dead)} 个只被 __init__.py 转出口")
    return dead


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules", action="store_true", help="只跑可达性")
    parser.add_argument("--symbols", action="store_true", help="只跑符号引用")
    args = parser.parse_args()
    run_all = not (args.modules or args.symbols)

    failures = 0
    if run_all or args.modules:
        failures += len(check_modules())
    if run_all or args.symbols:
        failures += len(check_symbols())

    print("干净" if not failures else f"共 {failures} 处未接线——更新 docs/INVENTORY.md 或删掉它们")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
