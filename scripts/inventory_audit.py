"""Recompute `docs/INVENTORY.md`'s wiring checks instead of trusting them.

That document's whole value is that its answer is a **checkable fact**, not an
impression. 五条口径，粒度从粗到细——后四条都是在上一条的盲区里发现死码之后补的：

- **可达性** — 从所有项目入口（CLI 与 MCP）出发，import 图能不能走到这个模块
- **符号引用** — 模块里的公开符号有没有人真的用；只被 `__init__.py` 转出口、
  再没有第二个引用点的，算未接线
- **字段读取** — dataclass 的公开字段有没有人读；算了没人读的，等于没算
- **方法调用** — 类上的公开方法有没有调用点。这一档最容易攒，因为**方法既没有
  `F401` 那样的告警，也不落在前三条的口径里**
- **私有符号** — 模块级 `_func` 与类上 `_method` 有没有引用点。前四条查的全是
  公开面，而私有面**一条自动告警都没有**

补跑的战果：第二条命中 3 个死符号（INVENTORY §0），第三条命中 7 个「算了没人读」
的字段（INVENTORY §4.1），第四条命中 13 处方法（DECISIONS §十二、§十三，其中
`dump_snapshot` × 3 就是 150 行），第五条命中 3 处私有辅助——全是前一条删完之后
**新掉下来的连锁**，所以它迭代到不动点，一次报完整条链。写成脚本是为了让它们下次
还能跑：一条写在文档里、没人执行的口径，和没有这条口径是一回事。

    python3 scripts/inventory_audit.py            # 五条都跑
    python3 scripts/inventory_audit.py --symbols  # 只跑其中一条

退出码 1 表示有未接线的模块、符号或方法——可以直接挂进 CI。字段那条、以及方法与
私有符号里「只有测试调用」那一档默认只报告不计分，理由见各自的 docstring。
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


ENTRYPOINTS = (
    "ashare_decision_support.cli",
    "ashare_decision_support.mcp_server",
)


def reachable_from_entrypoints() -> set[str]:
    by_name = {_module_name(p): p for p in _py_files(SRC)}
    seen: set[str] = set()
    queue = list(ENTRYPOINTS)
    while queue:
        name = queue.pop()
        if name in seen or name not in by_name:
            continue
        seen.add(name)
        for dep in _imports_of(by_name[name]):
            # `from x.y import z` 中 z 是符号而非模块时，落不到 by_name，自然跳过
            queue.extend([dep, dep.rsplit(".", 1)[0]])
    return seen


# 兼容已有的外部脚本调用；新口径包含 MCP 入口。
reachable_from_cli = reachable_from_entrypoints


def check_modules() -> list[str]:
    by_name = {_module_name(p): p for p in _py_files(SRC)}
    reached = reachable_from_entrypoints()
    # `__init__.py` 与包根本身不单独判定：它们是转出口，不是功能模块。
    orphans = sorted(
        name
        for name in by_name
        if name not in reached and not name.endswith("__init__") and name != "ashare_decision_support"
    )
    print(
        f"[模块] {len(by_name)} 个，{len(reached & set(by_name))} 个从"
        f" {len(ENTRYPOINTS)} 个入口可达"
    )
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


def check_fields() -> list[str]:
    """第三条口径：**算了但没人读的 dataclass 字段**。

    前两条都抓不到这一类——字段既不是模块也不是顶层符号。而这一类的代价和死模块
    一样：`SectorIndexReading.volume_ratio`（近 5 日均额 / 近 20 日均额）从上线起
    就在算，2026-08-09 之前**没有任何调用方**，于是「这个板块的反弹有没有量能
    跟上」在简报里根本没有答案，尽管那个数一直躺在内存里。同日的
    `LimitStock.seal_fund` / `first_seal` / `sector` 是同一回事。

    判据：dataclass 的公开字段，在**全仓**范围内既没有 `x.字段名` 的属性读取，
    也没有以字段名为内容的字符串字面量（`getattr(obj, "name")` 与 JSON key 都算）。

    两类排除，都是为了不误报：

    - **`asdict()` 整份序列化的类**——字段跟着进了 JSON 产物，那就是它的读者；
    - **下划线开头的字段**——本来就不是公开面。

    仍然可能漏报（字段名与别处的属性重名时会被当成有人读，例如
    `LimitStock.sector` 撞上了别处的 `item.sector`）。这条检查的口径是宁可漏报
    不可误报，和 `check_symbols` 一致：误删的代价远高于多留一个。
    """
    read: set[str] = set()
    trees: dict[pathlib.Path, ast.Module] = {}
    for path in _py_files(*SCAN_ROOTS):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        trees[path] = tree
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
                    print(
                        f"  算了没人读  {cls.name}.{name}  "
                        f"({path.relative_to(ROOT)}:{lineno})"
                    )

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
        if isinstance(node, ast.ClassDef) and _is_dataclass(node) and not _serialised_wholesale(node):
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


def check_methods() -> tuple[list[str], list[str]]:
    """第四条口径：**被接线的类上、没有生产调用方的公开方法**。

    前三条一条都抓不到这一类，而 2026-08-09 那次人工扫描里它是命中最多的一条
    （13 个）。竞价采集层是最贵的一例：`get_auction_features` / `get_open_features`
    加六份实现、一个归一模块、一个 CLI 开关，`src/` 里零消费者——可是
    **前三条全都判它是活的**：模块级说 `adapters/mapping` 活（`cli.py` 确实
    import 了），符号级说方法名活（抽象方法与六份实现互相引用），字段级只看
    dataclass。代价不是那 950 行，是这一层**看起来是活的**：新写一个适配器的人
    必须实现那两个抽象方法，读文档的人会得出「这套系统能取竞价数据」的结论，
    而 `DECISIONS.md` §一 的结论恰恰是交易所根本不发布这个数据。

    判据分两档，因为这两档要做的事不一样：

    - **零引用**——定义之外全仓一个引用点都没有。这是二值事实，计入退出码。
    - **仅测试引用**——`src/` 里没有任何调用点，只有 `tests/` 在调。这**正是
      `INVENTORY.md` 说的「建成·未接线」**：代码能跑、测试是绿的、没有任何生产
      路径走到它。竞价那一层当年就落在这一档，而按「测试也是调用方」的老口径
      （`check_symbols` 用的那条）它是活的——所以这里必须把测试单独摘出来数，
      不能沿用。

    引用只认 **AST 的属性读取**（`obj.foo`）与**字符串字面量**（`getattr` 与
    分发表），不用 `check_symbols` 那种全文 token 计数：方法名的定义处
    (`def foo`) 在 AST 里不是属性读取，因此不会自己数到自己，不需要再减去定义数。

    口径同前两条——宁可漏报不可误报。重名会让一个死方法蹭到别处的同名属性从而
    被判活（`SecurityMaster.name_of` 撞上任何一个 `x.name_of` 就够了），这是有意
    接受的：误删的代价远高于多留一个。
    """
    refs_src: set[str] = set()
    refs_test: set[str] = set()
    trees: dict[pathlib.Path, ast.Module] = {}
    for path in _py_files(*SCAN_ROOTS):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        trees[path] = tree
        # `scripts/` 归到生产侧：审计脚本与盘中监控外壳是真的调用方。
        target = refs_src if path.is_relative_to(SRC) or path.is_relative_to(ROOT / "scripts") else refs_test
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

    print(
        f"[方法] {total} 个公开方法，{len(unused)} 个零引用，{len(test_only)} 个只有测试在调"
    )
    return unused, test_only


def _names_used_in(node: ast.AST) -> collections.Counter:
    """一棵子树里所有可能是**引用**的名字。

    三种都算，宁可把死的算成活的：裸名读取（`_foo()` 与把函数当值传）、属性读取
    （`self._foo`），以及字符串字面量（`getattr(self, "_foo")` 与分发表）。
    定义处本身是 `FunctionDef`，不是 `Name` 读取，所以不会自己数到自己。
    """
    used: collections.Counter = collections.Counter()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            used[child.id] += 1
        elif isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
            used[child.attr] += 1
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            used[child.value] += 1
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


def check_private() -> tuple[list[str], list[str]]:
    """第五条口径：**私有符号**（模块级 `_func` 与类上 `_method`）没有引用点。

    这一条与前四条的方向相反：前四条查的是公开面，而**私有面根本没有任何自动
    告警**——`F401` 只管未使用的导入，一个没人调的私有函数在 ruff 眼里和被调 100
    次的函数长得一模一样。§十二 删竞价那一层时连锁清掉的 14 个私有辅助
    （`_build_auction_payload` 101 行、`_build_open_payload` 59 行……）**不是扫出来
    的，是删公开方法时顺手撞见的**。没有那次删除，它们会一直躺着。

    私有面反而能查得**准**。`_foo` 按约定只在自己模块里被调，所以引用只在定义它
    的那个模块里数就够了，没有跨模块重名顶账的问题——那正是前四条不得不「宁可
    漏报」的原因。这条是五条里唯一可以要求精确的。

    **迭代到不动点。** 删掉一个私有函数，常常会让它调用的另一个私有函数也失去
    唯一调用方；一轮只能看见第一层。所以这里在内存里反复剥，直到没有新的为止，
    一次就报出整条连锁——`qmt_xtdata` 那三个正好是两层
    （`_extract_source_dt` → `_first_value`）。自引用不算数：递归函数不能靠自己
    把自己撑活。

    分两档，同 `check_methods`：全仓零引用的计入退出码；`src/` 里没人调、只有
    `tests/` 在调的只报告——但这一档在私有面上比在公开面上更刺眼，它的意思是
    **测试正在测一段生产代码碰不到的逻辑**。
    """
    external: collections.Counter = collections.Counter()
    for path in _py_files(*SCAN_ROOTS):
        if path.is_relative_to(SRC):
            continue
        try:
            external.update(_names_used_in(ast.parse(path.read_text(encoding="utf-8"))))
        except (SyntaxError, UnicodeDecodeError):
            continue

    dead: list[str] = []
    test_only: list[str] = []
    total = 0
    for path in sorted(_py_files(SRC)):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue

        bodies: dict[str, collections.Counter] = {}
        where: dict[str, tuple[str, int]] = {}
        for name, node, label in _private_defs(tree):
            bodies[name] = _names_used_in(node)
            where[name] = (label, node.lineno)
        if not bodies:
            continue
        total += len(bodies)

        # 模块里私有定义**之外**的一切：公开函数、类体、模块级语句。
        rest = _names_used_in(tree)
        for used in bodies.values():
            rest.subtract(used)

        alive = set(bodies)
        gone: list[str] = []
        while True:
            newly = {
                name
                for name in alive
                # 自己的函数体不算数，否则递归函数永远活着。
                if rest[name] + sum(bodies[o][name] for o in alive if o != name) <= 0
            }
            if not newly:
                break
            alive -= newly
            gone.extend(sorted(newly))

        for name in gone:
            label, lineno = where[name]
            bucket, note = (
                (test_only, "仅测试在调") if external[name] else (dead, "零引用    ")
            )
            bucket.append(label)
            print(f"  {note}  {label}  ({path.relative_to(ROOT)}:{lineno})")

    print(
        f"[私有] {total} 个私有函数/方法，{len(dead)} 个零引用，"
        f"{len(test_only)} 个只有测试在调"
    )
    return dead, test_only


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modules", action="store_true", help="只跑可达性")
    parser.add_argument("--symbols", action="store_true", help="只跑符号引用")
    parser.add_argument("--fields", action="store_true", help="只跑 dataclass 字段读取")
    parser.add_argument("--methods", action="store_true", help="只跑公开方法调用点")
    parser.add_argument("--private", action="store_true", help="只跑私有函数/方法引用点")
    parser.add_argument(
        "--strict-fields",
        action="store_true",
        help="把字段检查计入退出码（默认只报告，见 check_fields 的说明）",
    )
    args = parser.parse_args()
    run_all = not (
        args.modules or args.symbols or args.fields or args.methods or args.private
    )

    failures = 0
    if run_all or args.modules:
        failures += len(check_modules())
    if run_all or args.symbols:
        failures += len(check_symbols())
    if run_all or args.fields:
        stale = check_fields()
        # **默认不计入退出码。** 前两条的判据是二值的——一个模块要么有调用方要么
        # 没有；而一个字段完全可以是**有意留着**的（留给台账 payload、留给下一轮
        # 要接的那一层）。让它直接把 CI 变红，只会逼人加豁免清单，而豁免清单会在
        # 三个月内长成第二份没人读的文档——正是这份脚本存在的理由所要避免的。
        #
        # 所以这条默认是**看板**：它把候选列出来，接不接、删不删是一次人工判断。
        # 想让它当闸门用 `--strict-fields`。
        if args.strict_fields:
            failures += len(stale)
        elif stale:
            print(
                f"  ↑ {len(stale)} 个字段待分诊：接上它（像 volume_ratio / seal_fund 那样）"
                "、删掉它，或在 docs/INVENTORY.md 里写明为什么留着"
            )

    if run_all or args.methods:
        unused, test_only = check_methods()
        # 「零引用」计入退出码：和模块、符号一样，这是二值事实。
        failures += len(unused)
        # 「仅测试在调」不计入。它是 `INVENTORY.md` §1 那张表的自动版——接不接是
        # 产品决定（`NewsStore.sector_heat` 与 `DailyKChartRenderer.render_svg`
        # 就摆在这一档），不是清理。让它把 CI 变红只会逼人删掉测试来消警，那正好
        # 把「建成·未接线」变成「建成·未接线·还没测试」。
        if test_only:
            print(
                f"  ↑ {len(test_only)} 个「建成·未接线」：接上它、删掉它，"
                "或在 docs/INVENTORY.md §1 里写明为什么留着"
            )

    if run_all or args.private:
        orphaned, tested_only = check_private()
        # 同 `check_methods`：零引用是二值事实，计入退出码。
        failures += len(orphaned)
        # 而私有面上的「仅测试在调」比公开面上更值得看一眼：私有辅助不是对外
        # 契约，没有「留给下一个消费者」这种理由。它通常意味着**测试在测一段
        # 生产代码已经碰不到的逻辑**，而那种测试是绿的，只是没在测系统。
        if tested_only:
            print(
                f"  ↑ {len(tested_only)} 个私有符号只有测试在调："
                "多半是某次删除留下的连锁，测试和它一起删"
            )

    print("干净" if not failures else f"共 {failures} 处未接线——更新 docs/INVENTORY.md 或删掉它们")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
