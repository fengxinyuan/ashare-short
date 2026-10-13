"""检查源码模块、公开符号、dataclass 字段、方法、私有辅助和常量的接线。

从 CLI / MCP 追踪静态 import；其余检查同时扫描源码、脚本与测试。
每次运行只解析一份源码快照。字段与仅测试调用默认只报告，其余问题退出 1。
扫描边界和保留原则见 docs/TESTING.md。

    python3 scripts/inventory_audit.py
    python3 scripts/inventory_audit.py --symbols"""

from __future__ import annotations

import argparse
import ast
import collections
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCAN_ROOTS = [SRC, ROOT / "tests", ROOT / "scripts"]
Trees = dict[pathlib.Path, ast.Module]


def _read_trees() -> Trees:
    """每次审计读取一次源码快照；语法或读取失败由入口报告。"""
    return {
        path: ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for path in _py_files(*SCAN_ROOTS)
    }


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


def _imports_of(path: pathlib.Path, tree: ast.Module) -> set[str]:
    """本包内被这个文件 import 的模块名。相对 import 也解析成绝对名。"""
    module = _module_name(path)
    pkg = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对 import
                base = pkg.split(".")
                if node.level > len(base):
                    continue
                base = base[: len(base) - node.level + 1] if node.level > 1 else base
                mod = ".".join([*base, node.module]) if node.module else ".".join(base)
            else:
                mod = node.module or ""
            if mod:
                found.add(mod)
                found.update(f"{mod}.{a.name}" for a in node.names)
    return {
        name
        for name in found
        if name == "ashare_decision_support" or name.startswith("ashare_decision_support.")
    }


ENTRYPOINTS = (
    "ashare_decision_support.cli",
    "ashare_decision_support.mcp_server",
)


def reachable_from_entrypoints(trees: Trees) -> set[str]:
    by_name = {_module_name(path): path for path in trees if path.is_relative_to(SRC)}
    seen: set[str] = set()
    queue = list(ENTRYPOINTS)
    while queue:
        name = queue.pop()
        if name in seen or name not in by_name:
            continue
        seen.add(name)
        path = by_name[name]
        for dep in {name, *_imports_of(path, trees[path])}:
            parts = dep.split(".")
            queue.extend(".".join(parts[:end]) for end in range(1, len(parts) + 1))
    return seen


def check_modules(trees: Trees) -> list[str]:
    by_name = {_module_name(path): path for path in trees if path.is_relative_to(SRC)}
    reached = reachable_from_entrypoints(trees)
    orphans = sorted(
        name for name, path in by_name.items() if name not in reached and path.name != "__init__.py"
    )
    print(
        f"[模块] {len(by_name)} 个，{len(reached & set(by_name))} 个从"
        f" {len(ENTRYPOINTS)} 个入口可达"
    )
    for name in orphans:
        print(f"  未接线  {name}")
    return orphans


def check_symbols(trees: Trees) -> tuple[list[str], list[str]]:
    """公开函数与类分别报告零引用和仅测试引用；转出口与注释不算消费者。"""
    defs: dict[str, list[tuple[str, int]]] = {}
    refs_src: collections.Counter = collections.Counter()
    refs_test: collections.Counter = collections.Counter()
    for path, tree in trees.items():
        if path.name == "__init__.py":
            continue
        target = (
            refs_src
            if path.is_relative_to(SRC) or path.is_relative_to(ROOT / "scripts")
            else refs_test
        )
        target.update(_names_used_in(tree))
        if not path.is_relative_to(SRC):
            continue
        for node in tree.body:  # 只看模块顶层：嵌套定义不是公开面
            if isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ) and not node.name.startswith("_"):
                defs.setdefault(node.name, []).append((str(path.relative_to(ROOT)), node.lineno))

    dead: list[str] = []
    test_only: list[str] = []
    for name, locs in sorted(defs.items()):
        if refs_src[name]:
            continue
        where = ", ".join(f"{f}:{ln}" for f, ln in locs)
        if refs_test[name]:
            test_only.append(name)
            print(f"  仅测试在调       {name}  ({where})")
        else:
            dead.append(name)
            print(f"  零引用或仅转出口  {name}  ({where})")

    print(
        f"[符号] {len(defs)} 个公开符号，{len(dead)} 个零引用或仅转出口，"
        f"{len(test_only)} 个只有测试在调"
    )
    return dead, test_only


def check_fields(trees: Trees) -> list[str]:
    """检查 dataclass 公开字段的属性或字面量读取点。

    类内通过 asdict 整体序列化的字段由下游消费，不单独报告。
    同名字段可能互相算作引用；默认只报告，删除前仍需人工核对。"""
    read: set[str] = set()
    for tree in trees.values():
        read |= _names_read_in(tree)

    dead: list[str] = []
    total = 0
    for path, tree in sorted(trees.items()):
        if not path.is_relative_to(SRC):
            continue
        for cls in _dataclasses_in(tree):
            for name, lineno in _public_fields(cls):
                total += 1
                if name not in read:
                    dead.append(f"{cls.name}.{name}")
                    print(f"  算了没人读  {cls.name}.{name}  ({path.relative_to(ROOT)}:{lineno})")

    print(f"[字段] {total} 个 dataclass 公开字段，{len(dead)} 个没有任何读取点")
    return dead


def _names_read_in(tree: ast.Module) -> set[str]:
    """一棵树里所有「可能是在读某个字段」的名字。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


def _dataclasses_in(tree: ast.Module):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and _is_dataclass(node)
            and not _serialised_wholesale(node)
        ):
            yield node


def _public_fields(cls: ast.ClassDef):
    for stmt in cls.body:
        if (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and not stmt.target.id.startswith("_")
        ):
            yield stmt.target.id, stmt.lineno


def _is_dataclass(cls: ast.ClassDef) -> bool:
    for decorator in cls.decorator_list:
        func = decorator.func if isinstance(decorator, ast.Call) else decorator
        if (getattr(func, "attr", None) or getattr(func, "id", None)) == "dataclass":
            return True
    return False


def _serialised_wholesale(cls: ast.ClassDef) -> bool:
    """类体内调用了 `asdict` ⇒ 每个字段都进了 JSON 产物，读者在下游。"""
    for node in ast.walk(cls):
        if isinstance(node, ast.Call):
            func = node.func
            if (getattr(func, "attr", None) or getattr(func, "id", None)) == "asdict":
                return True
    return False


def check_methods(trees: Trees) -> tuple[list[str], list[str]]:
    """公开方法按零引用、仅测试引用分别报告。

    属性读取与精确字符串字面量算引用，源码和脚本算生产调用方。
    无接收者类型推导，同名属性可能掩盖未使用方法。"""
    refs_src: set[str] = set()
    refs_test: set[str] = set()
    for path, tree in trees.items():
        target = (
            refs_src
            if path.is_relative_to(SRC) or path.is_relative_to(ROOT / "scripts")
            else refs_test
        )
        target |= _names_read_in(tree)

    unused: list[str] = []
    test_only: list[str] = []
    total = 0
    for path, tree in sorted(trees.items()):
        if not path.is_relative_to(SRC):
            continue
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            for stmt in cls.body:
                if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                if stmt.name.startswith("_"):
                    continue
                total += 1
                where = f"{path.relative_to(ROOT)}:{stmt.lineno}"
                if stmt.name in refs_src:
                    continue
                if stmt.name in refs_test:
                    test_only.append(f"{cls.name}.{stmt.name}")
                    print(f"  仅测试在调  {cls.name}.{stmt.name}  ({where})")
                else:
                    unused.append(f"{cls.name}.{stmt.name}")
                    print(f"  零引用      {cls.name}.{stmt.name}  ({where})")

    print(f"[方法] {total} 个公开方法，{len(unused)} 个零引用，{len(test_only)} 个只有测试在调")
    return unused, test_only


def _names_used_in(node: ast.AST) -> collections.Counter:
    """计数裸名读取、属性读取、精确字符串和导入的原始名称。

    定义名和注释不算引用；导入原名用于识别别名调用。"""
    used: collections.Counter = collections.Counter()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            used[child.id] += 1
        elif isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
            used[child.attr] += 1
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            used[child.value] += 1
        elif isinstance(child, ast.ImportFrom):
            used.update(item.name for item in child.names)
    return used


def _private_defs(tree: ast.Module):
    """模块里的私有定义：模块级 `_func` 与类上 `_method`。`__dunder__` 不算。"""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _is_private(node.name):
            yield node.name, node, node.name
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        for stmt in cls.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and _is_private(stmt.name):
                yield stmt.name, stmt, f"{cls.name}.{stmt.name}"


def _is_private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def check_private(trees: Trees) -> tuple[list[str], list[str]]:
    """逐层剥离失去本模块调用方的私有辅助，自递归不能保活。

    测试调用单独报告，不以删除测试消除告警。方法按名字匹配，
    同模块的同名方法或互相递归仍需人工检查。"""
    external: collections.Counter = collections.Counter()
    script_refs: collections.Counter = collections.Counter()
    for path, tree in trees.items():
        if path.is_relative_to(SRC):
            continue
        target = script_refs if path.is_relative_to(ROOT / "scripts") else external
        target.update(_names_used_in(tree))

    dead: list[str] = []
    test_only: list[str] = []
    total = 0
    for path, tree in sorted(trees.items()):
        if not path.is_relative_to(SRC):
            continue

        bodies: dict[str, collections.Counter] = {}
        where: dict[str, tuple[str, int]] = {}
        for name, node, label in _private_defs(tree):
            bodies[name] = _names_used_in(node)
            where[name] = (label, node.lineno)
        if not bodies:
            continue
        total += len(bodies)

        rest = _names_used_in(tree)
        for used in bodies.values():
            rest.subtract(used)
        rest.update(script_refs)

        alive = set(bodies)
        gone: list[str] = []
        while True:
            newly = {
                name
                for name in alive
                if rest[name] + sum(bodies[o][name] for o in alive if o != name) <= 0
            }
            if not newly:
                break
            alive -= newly
            gone.extend(sorted(newly))

        for name in gone:
            label, lineno = where[name]
            bucket, note = (test_only, "仅测试在调") if external[name] else (dead, "零引用    ")
            bucket.append(label)
            print(f"  {note}  {label}  ({path.relative_to(ROOT)}:{lineno})")

    print(f"[私有] {total} 个私有函数/方法，{len(dead)} 个零引用，{len(test_only)} 个只有测试在调")
    return dead, test_only


def check_constants(trees: Trees) -> list[str]:
    """检查模块级公开常量的 AST 引用；注释不算消费者。

    跨模块同名引用可能掩盖死常量，结果按名称合并。"""
    defs: dict[str, tuple[pathlib.Path, int]] = {}
    used: set[str] = set()
    for path, tree in trees.items():
        used |= set(_names_used_in(tree))
        if not path.is_relative_to(SRC):
            continue
        for node in tree.body:  # 只看模块顶层：函数里的局部量不是公开面
            for name in _assigned_names(node):
                if name.isupper() and not name.startswith("_"):
                    defs.setdefault(name, (path, node.lineno))

    dead: list[str] = []
    for name, (path, lineno) in sorted(defs.items(), key=lambda kv: str(kv[1][0])):
        if name in used:
            continue
        dead.append(name)
        print(f"  零引用      {name}  ({path.relative_to(ROOT)}:{lineno})")

    print(f"[常量] {len(defs)} 个模块级公开常量，{len(dead)} 个零引用")
    return dead


def _assigned_names(node: ast.stmt):
    """一条顶层赋值语句绑定的名字。`A = B = ...` 与 `A: int = ...` 都算。"""
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                yield target.id
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        yield node.target.id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules", action="store_true", help="只跑可达性")
    parser.add_argument("--symbols", action="store_true", help="只跑符号引用")
    parser.add_argument("--fields", action="store_true", help="只跑 dataclass 字段读取")
    parser.add_argument("--methods", action="store_true", help="只跑公开方法调用点")
    parser.add_argument("--private", action="store_true", help="只跑私有函数/方法引用点")
    parser.add_argument("--constants", action="store_true", help="只跑模块级常量引用点")
    parser.add_argument(
        "--strict-fields",
        action="store_true",
        help="把字段检查计入退出码（默认只报告，见 check_fields 的说明）",
    )
    args = parser.parse_args()
    try:
        trees = _read_trees()
    except (OSError, SyntaxError, UnicodeError) as exc:
        print(f"源码读取或解析失败：{exc}", file=sys.stderr)
        return 1
    run_all = not (
        args.modules
        or args.symbols
        or args.fields
        or args.methods
        or args.private
        or args.constants
    )

    failures = 0
    if run_all or args.modules:
        failures += len(check_modules(trees))
    if run_all or args.symbols:
        unused, test_only = check_symbols(trees)
        failures += len(unused)
        if test_only:
            print(f"  ↑ {len(test_only)} 个公开符号仅测试使用，请核对生产用途")
    if run_all or args.fields:
        stale = check_fields(trees)
        if args.strict_fields:
            failures += len(stale)
        elif stale:
            print(f"  ↑ {len(stale)} 个字段待核对：接入消费方、删除，或在定义处记录保留原因")

    if run_all or args.methods:
        unused, test_only = check_methods(trees)
        failures += len(unused)
        if test_only:
            print(f"  ↑ {len(test_only)} 个方法仅测试使用，请核对生产用途")

    if run_all or args.private:
        orphaned, tested_only = check_private(trees)
        failures += len(orphaned)
        if tested_only:
            print(f"  ↑ {len(tested_only)} 个私有符号仅测试使用，请核对生产用途")

    if run_all or args.constants:
        failures += len(check_constants(trees))

    print("干净" if not failures else f"共 {failures} 处未接线，请核对消费方或删除无用实现")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
