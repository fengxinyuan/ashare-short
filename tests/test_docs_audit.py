"""文档里的命令和链接要按 Markdown / shell 语义校验。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "docs_audit.py"
_SPEC = importlib.util.spec_from_file_location("docs_audit", _PATH)
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


def _doc(tmp_path, *lines: str) -> Path:
    path = tmp_path / "GUIDE.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "command",
    [
        "ashare-ds --help",
        "ashare-ds review --help",
        "ashare-ds position open --help",
        'ashare-ds ask "#机器人 涨幅 > 3% 怎么看" # 注释不属于问题',
        'ashare-ds ask "ashare-ds-mcp 如何取事实"',
    ],
)
def test_valid_documented_commands_parse_without_execution(tmp_path, command, monkeypatch) -> None:
    from ashare_decision_support import cli

    def unexpected(*args, **kwargs):
        pytest.fail("文档审计不应执行命令或取数")

    monkeypatch.setattr(cli, "_tickflow", unexpected)
    monkeypatch.setattr(cli, "_cmd_ask", unexpected)
    path = _doc(tmp_path, f"```bash\n{command}\n```\n")
    assert audit.check_docs([path]) == []


@pytest.mark.parametrize(
    "command",
    [
        'ashare-ds ask "涨幅 > 3% 怎么看" --missing-option',
        'ashare-ds ask "ashare-ds-mcp 如何取事实" --missing-option',
        "ashare-ds missing-command",
        'ashare-ds ask "引号未闭合',
    ],
)
def test_invalid_commands_are_reported_with_the_document_line(tmp_path, command) -> None:
    path = _doc(tmp_path, f"# 指南\n\n```bash\n{command}\n```\n")

    errors = audit.check_docs([path])

    assert len(errors) == 1
    assert "GUIDE.md:4" in errors[0]


def test_multiline_and_inline_commands_keep_the_starting_line(tmp_path) -> None:
    path = _doc(
        tmp_path,
        "```bash",
        "ashare-ds facts \\",
        "  --no-live \\",
        "  --phase off_hours",
        "```",
        "也可运行 `ashare-ds position list`。",
    )

    assert audit._command_lines(path.read_text(encoding="utf-8")) == [
        (2, "ashare-ds facts --no-live --phase off_hours"),
        (6, "ashare-ds position list"),
    ]
    assert audit.check_docs([path]) == []


def test_shell_fences_are_checked_but_example_data_is_not(tmp_path) -> None:
    path = _doc(
        tmp_path,
        "```text",
        "ashare-ds illustrative-output",
        "```",
        "~~~sh",
        "ashare-ds missing-command",
        "~~~",
    )
    errors = audit.check_docs([path])
    assert len(errors) == 1
    assert "GUIDE.md:5" in errors[0]


def test_placeholders_are_skipped_without_hiding_quoted_comparisons(tmp_path) -> None:
    path = _doc(
        tmp_path,
        "```bash",
        "ashare-ds position open <代码> --cost <成本价>",
        "ashare-ds facts --output=<路径>",
        'ashare-ds ask "1 < 2" --typo',
        "```",
    )
    errors = audit.check_docs([path])
    assert len(errors) == 1
    assert "GUIDE.md:4" in errors[0]


def test_links_support_spaces_titles_and_url_encoded_paths(tmp_path) -> None:
    target = tmp_path / "参考 资料.md"
    target.write_text("# 数据", encoding="utf-8")
    path = _doc(
        tmp_path,
        '[参考](<参考 资料.md> "标题")',
        "[参考](参考%20资料.md#数据)",
        "[本站](#数据)",
        "[外链](https://example.com)",
        "[应用](app://connector)",
        "`[链接示例](missing.md)`",
        "```markdown",
        "[链接示例](missing.md)",
        "```",
    )
    assert audit.check_links([path]) == []


def test_recursive_discovery_checks_broken_links_in_nested_history(tmp_path) -> None:
    archive = tmp_path / "docs" / "history"
    archive.mkdir(parents=True)
    (tmp_path / "README.md").write_text("# 项目", encoding="utf-8")
    history = archive / "HISTORY.md"
    history.write_text("[失效链接](../../missing.md)", encoding="utf-8")

    docs = audit.discover_docs(tmp_path)

    assert history in docs
    errors = audit.check_links(docs)
    assert len(errors) == 1
    assert "HISTORY.md:1" in errors[0]


def test_main_checks_commands_in_design_documents(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "HISTORY.md"
    path.write_text("```bash\nashare-ds removed-command\n```\n", encoding="utf-8")
    monkeypatch.setattr(audit, "discover_docs", lambda: [path])

    assert audit.main() == 1
    assert "HISTORY.md:2" in capsys.readouterr().out


def test_local_links_resolve_relative_to_each_document(tmp_path) -> None:
    guide = tmp_path / "guides"
    guide.mkdir()
    (tmp_path / "README.md").write_text("# 项目", encoding="utf-8")
    path = _doc(guide, "[入口](../README.md)\n[误写](README.md)\n")

    errors = audit.check_links([path])

    assert len(errors) == 1
    assert "GUIDE.md:2" in errors[0]


@pytest.mark.parametrize(
    "command",
    [
        "python3 scripts/removed.py",
        "python -u -B scripts/removed.py --help",
        "python3.11 -W ignore -X utf8 scripts/removed.py",
        "python3 -- scripts/removed.py",
        "python3 scripts/removed.py --output <路径>",
    ],
)
def test_missing_documented_python_scripts_are_reported(tmp_path, command) -> None:
    path = _doc(tmp_path, "```bash", command, "```")

    errors = audit.check_docs([path])

    assert len(errors) == 1
    assert "GUIDE.md:2" in errors[0]
    assert "scripts/removed.py" in errors[0]


def test_existing_script_with_spaces_is_checked_without_running_it(tmp_path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "检查 缓存.py").write_text('raise RuntimeError("must not run")', encoding="utf-8")
    path = _doc(tmp_path, '运行 `python3 "scripts/检查 缓存.py" --unknown-argument`。')

    assert audit.check_docs([path], root=tmp_path) == []


def test_python_options_do_not_turn_module_or_code_arguments_into_script_paths(tmp_path) -> None:
    path = _doc(
        tmp_path,
        "```bash",
        "python3 -m pytest tests/fixture_not_in_this_root.py",
        "python3 -mpytest tests/fixture_not_in_this_root.py",
        "python3 -c 'print(\"scripts/missing.py\")'",
        "python3 --version",
        "```",
        "```bash",
        "python3 -cpass scripts/argument_only.py",
        "python3 - scripts/stdin_argument_only.py",
        "```",
        "```python",
        "python3 scripts/example_not_a_shell_command.py",
        "```",
    )

    assert audit.check_docs([path], root=tmp_path) == []


def test_script_paths_use_the_project_root_not_the_document_directory(tmp_path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "check.py").write_text("", encoding="utf-8")
    guides = tmp_path / "docs" / "guides"
    guides.mkdir(parents=True)
    path = _doc(guides, "```bash", "python3 scripts/check.py", "```")

    assert audit.check_docs([path], root=tmp_path) == []


def test_multiline_python_script_keeps_the_starting_line(tmp_path) -> None:
    path = _doc(tmp_path, "```sh", "python3 -u \\", "  scripts/removed.py", "```")

    errors = audit.check_docs([path], root=tmp_path)

    assert len(errors) == 1
    assert "GUIDE.md:2" in errors[0]
