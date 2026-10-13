"""检查 Markdown 中的 CLI 参数、项目 Python 脚本路径和本地文件链接。

只解析 shell 代码块及正文内联命令，不执行、不取数、不写状态。CLI 占位符模板跳过。
Python 脚本路径以项目根目录为基准，只查文件存在，不执行脚本或验证脚本参数。
链接只校验文件存在性，不验证锚点或外网可达性。

运行：python3 scripts/docs_audit.py；发现问题时退出码为 1。
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import re
import shlex
import sys
from urllib.parse import unquote

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# 占位符模板：`<代码>` / `<成本价>` 这类，按定义不是一条能执行的命令。
_PLACEHOLDER = re.compile(r"(?:[^=]+=)?<[^<>\s]+>")

_PROMPT = re.compile(r"^[\s>$]*")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
# Markdown 链接的目标那一段：`[标题](docs/USAGE.md)`、图片、带 title 的写法都收。
_MD_LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w-]*)\s*$")
_SHELL_LANGUAGES = {"", "bash", "sh", "shell", "console", "zsh", "shellsession"}
_COMMAND_START = re.compile(r"^(?:ashare-ds|python(?:3(?:\.\d+)?)?)(?:\s|$)")


def _markdown_lines(text: str):
    """逐行返回行号、正文与围栏语言；None 表示正文。"""
    fence = ""
    language = None
    for number, raw in enumerate(text.splitlines(), 1):
        match = _FENCE.match(raw)
        if match:
            marker, tag = match.groups()
            if not fence:
                fence, language = marker, tag.lower()
            elif not tag and marker[0] == fence[0] and len(marker) >= len(fence):
                fence, language = "", None
            continue
        yield number, raw, language


def _is_template(command: str) -> bool:
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return False  # 引号错误仍交给 check_docs 报告。
    return any(_PLACEHOLDER.fullmatch(token) for token in tokens)


def _python_script(argv: list[str]) -> str | None:
    """从 Python 参数中取字面量 .py 路径；模块、内联代码和动态路径不在此检查。"""
    arguments = iter(argv)
    for value in arguments:
        if value in {"-", "-h", "--help", "-V", "--version"} or value.startswith(("-c", "-m")):
            return None
        if value in {"-W", "-X", "--check-hash-based-pycs"}:
            next(arguments, None)
            continue
        if value == "--":
            value = next(arguments, "")
        elif value.startswith("-"):
            continue
        if value.endswith(".py") and not any(char in value for char in "$~*?{}<>"):
            return value
        return None
    return None


def _checkable_command(command: str) -> bool:
    if command.startswith("ashare-ds"):
        return not _is_template(command)
    try:
        argv = shlex.split(command, comments=True)
    except ValueError:
        return True  # 引号错误交给 check_docs 定位。
    return _python_script(argv[1:]) is not None


def _command_lines(text: str) -> list[tuple[int, str]]:
    """抽取 shell 代码块与正文内联的 CLI / Python 脚本命令，保留起始行号。

    两种出处都要查，因为两种都会被照着敲：**代码块里的整行**，以及**正文里用
    反引号包起来的那一小段**（「用 `ashare-ds facts --no-live` 导一份」）。
    后者不能按整行取——它左右都是散文。
    """
    found: list[tuple[int, str]] = []
    pending: list[str] = []
    pending_at = 0

    for number, raw, language in _markdown_lines(text):
        if language in _SHELL_LANGUAGES:
            line = _PROMPT.sub("", raw).strip()
            if pending or _COMMAND_START.match(line):
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

        if language is not None:
            continue
        for span in _INLINE_CODE.findall(raw):
            command = span.strip()
            if _COMMAND_START.match(command):
                found.append((number, command))

    if pending:
        found.append((pending_at, " ".join(pending)))
    return [(number, line) for number, line in found if _checkable_command(line)]


def check_docs(paths: list[pathlib.Path], *, root: pathlib.Path = ROOT) -> list[str]:
    from ashare_decision_support.cli import build_parser

    parser = build_parser()
    failures: list[str] = []
    for path in paths:
        for number, line in _command_lines(path.read_text(encoding="utf-8")):
            try:
                argv = shlex.split(line, comments=True)
            except ValueError as exc:
                failures.append(f"{path.name}:{number}  {line}\n    -> 引号不配对：{exc}")
                continue
            if argv[0] != "ashare-ds":
                script = _python_script(argv[1:])
                target = (root / script).resolve() if script is not None else None
                if (
                    target is not None
                    and target.is_relative_to(root.resolve())
                    and not target.is_file()
                ):
                    failures.append(
                        f"{path.name}:{number}  {line}\n    -> 项目 Python 脚本不存在：{script}"
                    )
                continue
            buffer = io.StringIO()
            try:
                with contextlib.redirect_stderr(buffer), contextlib.redirect_stdout(buffer):
                    parser.parse_args(argv[1:])
            except SystemExit as exc:
                if exc.code == 0:  # --help 是有效用法。
                    continue
                reason = (buffer.getvalue().strip().splitlines() or ["解析失败"])[-1]
                failures.append(f"{path.name}:{number}  {line}\n    -> {reason}")
    return failures


def check_links(paths: list[pathlib.Path]) -> list[str]:
    """检查正文中的本地文件链接；相对路径以当前文档目录为准。"""
    failures: list[str] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for number, raw, language in _markdown_lines(text):
            if language is not None:
                continue
            for target in _MD_LINK.findall(_INLINE_CODE.sub("", raw)):
                link = target.removeprefix("<").removesuffix(">")
                if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", link) or link.startswith("//"):
                    continue
                link = unquote(link.split("#", 1)[0].split("?", 1)[0])
                if link and not (path.parent / link).exists():
                    failures.append(f"{path.name}:{number}  链接指向不存在的文件：{link}")
    return failures


def discover_docs(root: pathlib.Path = ROOT) -> list[pathlib.Path]:
    return [*sorted((root / "docs").rglob("*.md")), root / "README.md"]


def main() -> int:
    docs = discover_docs()
    failures = check_docs(docs)
    total = sum(len(_command_lines(path.read_text(encoding="utf-8"))) for path in docs)
    print(f"[文档命令] {len(docs)} 份文档，{total} 条 CLI / Python 脚本命令")

    link_failures = check_links(docs)
    print(f"[文档链接] {len(docs)} 份文档的仓库内相对链接")

    for item in [*failures, *link_failures]:
        print(f"  {item}")
    if failures or link_failures:
        print(f"{len(failures) + len(link_failures)} 处文档与今天的代码对不上")
        return 1
    print("干净")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
