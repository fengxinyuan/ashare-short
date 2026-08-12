"""把用法文档里的每一条 `ashare-ds` 命令真的交给解析器跑一遍。

这个闸门是被一处两个月没人发现的错逼出来的：README 和 `docs/USAGE.md` 从第一版
起写的都是 `ashare-ds advise 600519.SH`，而 `advise` 的解析器只认 `--symbols`。
照文档敲的那条命令直接以 `unrecognized arguments` 退出——**文档、代码、测试三边
各自都是绿的**：文档写得对（那是想要的用法）、代码跑得对（另一种用法）、测试测的
是代码。没有任何一条现有口径会看这两者对不对得上。

判据有两条，都是「文档说的东西今天还在不在」：

1. **用法类文档里出现的命令行，必须能被 `build_parser()` 接受**；
2. **文档里指向仓库内文件的相对链接，目标必须存在**——合并两份文档的那次会话
   里，README 的索引就指向了一个刚被删掉的文件。

- 只解析、不执行。这条闸门管的是「这条命令存不存在、参数拼没拼错」，
  不打网络、不碰状态目录。
- 三份文档不在范围内，因为它们**按用途就不是用法文档**：`ROADMAP.md` /
  `REWORK.md` 写的是还没做的设计，`DECISIONS.md` 记的是已经删掉的东西——两者
  里面的命令解析失败都是正确结果。范围按文档的用途划，不是按一份「这几行豁免」
  的清单：豁免清单三个月内就会长成第二份没人读的文档。
- 带 `<占位符>` 的模板行跳过。`ashare-ds position open <代码> --cost <成本价>`
  按定义就不是一条能执行的命令，它是在告诉你要填什么。

    python3 scripts/docs_audit.py

退出码 1 表示有文档里的命令跑不通、或者有链接指向了已经不存在的文件。
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import re
import shlex
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# 不是用法文档：ROADMAP / REWORK 写将来要做的，DECISIONS 记已经删掉的。
NOT_USAGE_DOCS = {"ROADMAP.md", "REWORK.md", "DECISIONS.md"}

# 占位符模板：`<代码>` / `<成本价>` 这类，按定义不是一条能执行的命令。
_PLACEHOLDER = re.compile(r"[<>]")

_PROMPT = re.compile(r"^[\s>$]*")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
# Markdown 链接的目标那一段：`[标题](docs/USAGE.md)`、图片、带 title 的写法都收。
_MD_LINK = re.compile(r"!?\[[^\]\n]+\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_FENCE = re.compile(r"^\s*```")


def _command_lines(text: str) -> list[tuple[int, str]]:
    """文档里所有 `ashare-ds` 命令，连行号一起返回。

    两种出处都要查，因为两种都会被照着敲：**代码块里的整行**，以及**正文里用
    反引号包起来的那一小段**（「用 `ashare-ds facts --no-live` 导一份」）。
    后者不能按整行取——它左右都是散文。
    """
    found: list[tuple[int, str]] = []
    in_fence = False
    pending: list[str] = []
    pending_at = 0

    for number, raw in enumerate(text.splitlines(), 1):
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue

        if in_fence:
            line = _PROMPT.sub("", raw).strip()
            line = line.split("#", 1)[0].strip()
            if pending or line.startswith("ashare-ds "):
                if not pending:
                    pending_at = number
                # 续行：`\` 结尾的命令要拼回一条再解析。
                if line.endswith("\\"):
                    pending.append(line[:-1].strip())
                    continue
                pending.append(line)
                found.append((pending_at, " ".join(part for part in pending if part)))
                pending = []
            continue

        for span in _INLINE_CODE.findall(raw):
            command = span.strip()
            if command.startswith("ashare-ds "):
                found.append((number, command))

    if pending:
        found.append((pending_at, " ".join(pending)))
    return [
        (number, line)
        for number, line in found
        if "ashare-ds-mcp" not in line and not _PLACEHOLDER.search(line)
    ]


def check_docs(paths: list[pathlib.Path]) -> list[str]:
    from ashare_decision_support.cli import build_parser

    failures: list[str] = []
    for path in paths:
        for number, line in _command_lines(path.read_text(encoding="utf-8")):
            try:
                argv = shlex.split(line)[1:]
            except ValueError as exc:
                failures.append(f"{path.name}:{number}  {line}\n    -> 引号不配对：{exc}")
                continue
            buffer = io.StringIO()
            try:
                with contextlib.redirect_stderr(buffer), contextlib.redirect_stdout(buffer):
                    build_parser().parse_args(argv)
            except SystemExit:
                reason = (buffer.getvalue().strip().splitlines() or ["解析失败"])[-1]
                failures.append(f"{path.name}:{number}  {line}\n    -> {reason}")
    return failures


def check_links(paths: list[pathlib.Path]) -> list[str]:
    """文档里指向仓库内文件的相对链接，目标还在不在。

    同一类错的另一半：命令会失效，链接也会——2026-08-11 合并两份提示词文档时，
    README 的索引就在一次会话里指向了一个已经删掉的文件。外链不查（那要打网络，
    而这条闸门的判据是「本地事实」），锚点 `#section` 只查文件那一段。
    """
    failures: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for number, raw in enumerate(text.splitlines(), 1):
            for target in _MD_LINK.findall(raw):
                link = target.split("#", 1)[0].strip()
                if not link or link.startswith(("http://", "https://", "mailto:")):
                    continue
                resolved = (ROOT if path.parent.name == "" else path.parent) / link
                if not (path.parent / link).exists() and not resolved.exists():
                    failures.append(f"{path.name}:{number}  链接指向不存在的文件：{link}")
    return failures


def main() -> int:
    docs = [
        path
        for path in [*sorted((ROOT / "docs").glob("*.md")), ROOT / "README.md"]
        if path.name not in NOT_USAGE_DOCS
    ]
    failures = check_docs(docs)
    total = sum(len(_command_lines(path.read_text(encoding="utf-8"))) for path in docs)
    print(f"[文档命令] {len(docs)} 份用法文档，{total} 条 `ashare-ds` 命令行")

    # 链接这条查全部文档，包括设计文档：一条断链在哪份文档里都是断的。
    all_docs = [*sorted((ROOT / "docs").glob("*.md")), ROOT / "README.md"]
    link_failures = check_links(all_docs)
    print(f"[文档链接] {len(all_docs)} 份文档的仓库内相对链接")

    for item in [*failures, *link_failures]:
        print(f"  {item}")
    if failures or link_failures:
        print(f"{len(failures) + len(link_failures)} 处文档与今天的代码对不上")
        return 1
    print("干净")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
