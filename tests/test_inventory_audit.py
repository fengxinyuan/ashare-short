"""接线审计应识别真实消费关系，不能由转出口或注释替死代码保活。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "inventory_audit.py"
_SPEC = importlib.util.spec_from_file_location("inventory_audit", _PATH)
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    monkeypatch.setattr(audit, "SRC", tmp_path / "src")
    monkeypatch.setattr(
        audit, "SCAN_ROOTS", [tmp_path / part for part in ("src", "tests", "scripts")]
    )
    monkeypatch.setattr(audit, "ENTRYPOINTS", ("ashare_decision_support.cli",))
    return tmp_path


def _write(root: Path, name: str, source: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_package_initializers_and_relative_imports_are_reachable(repository) -> None:
    files = {
        "__init__.py": "from . import bootstrap\n",
        "bootstrap.py": "",
        "cli.py": "from .nested import adapter\n",
        "nested/__init__.py": "from . import helpers\n",
        "nested/helpers.py": "",
        "nested/adapter.py": "from .. import shared\n",
        "shared.py": "",
        "unused/__init__.py": "",
        "orphan.py": "",
    }
    for name, source in files.items():
        _write(repository, f"src/ashare_decision_support/{name}", source)

    trees = audit._read_trees()
    reached = audit.reachable_from_entrypoints(trees)

    assert "ashare_decision_support.bootstrap" in reached
    assert "ashare_decision_support.nested.helpers" in reached
    assert "ashare_decision_support.shared" in reached
    assert audit.check_modules(trees) == ["ashare_decision_support.orphan"]


def test_public_symbols_cannot_be_kept_alive_by_exports_or_prose(repository) -> None:
    _write(
        repository,
        "src/ashare_decision_support/helpers.py",
        '''
"""The exported and mentioned helpers used to have callers."""
# mentioned is retained in this historical comment.
def mentioned():
    return 1
def exported():
    return 2
''',
    )
    _write(
        repository,
        "src/ashare_decision_support/__init__.py",
        """
from .helpers import exported
__all__ = ["exported"]
""",
    )

    assert audit.check_symbols(audit._read_trees()) == (["exported", "mentioned"], [])


@pytest.mark.parametrize(
    "caller", ["src/ashare_decision_support/cli.py", "scripts/probe.py", "tests/test_helper.py"]
)
def test_aliased_imports_count_as_public_symbol_consumers(repository, caller) -> None:
    _write(repository, "src/ashare_decision_support/helpers.py", "def build():\n    return 1\n")
    _write(
        repository, caller, "from ashare_decision_support.helpers import build as call\ncall()\n"
    )

    expected = ([], ["build"]) if caller.startswith("tests/") else ([], [])
    assert audit.check_symbols(audit._read_trees()) == expected


def test_public_symbols_distinguish_test_coverage_from_production_use(repository) -> None:
    _write(
        repository,
        "src/ashare_decision_support/helpers.py",
        """
class TestedOnly:
    pass
def unused():
    return 1
def connected():
    return 2
""",
    )
    _write(
        repository,
        "tests/test_helpers.py",
        """
from ashare_decision_support.helpers import TestedOnly
assert TestedOnly() is not None
""",
    )
    _write(
        repository,
        "scripts/probe.py",
        """
from ashare_decision_support.helpers import connected
connected()
""",
    )

    assert audit.check_symbols(audit._read_trees()) == (["unused"], ["TestedOnly"])


def test_private_script_consumers_are_production_callers(repository) -> None:
    _write(
        repository,
        "src/ashare_decision_support/helpers.py",
        """
def _inner():
    return 1
def _outer():
    return _inner()
""",
    )
    _write(
        repository,
        "scripts/probe.py",
        """
from ashare_decision_support.helpers import _outer as probe
probe()
""",
    )

    assert audit.check_private(audit._read_trees()) == ([], [])


def test_test_only_methods_remain_distinct_from_unused_methods(repository) -> None:
    _write(
        repository,
        "src/ashare_decision_support/service.py",
        """
class Service:
    def used_in_tests(self):
        return 1
    def unused(self):
        return 2
""",
    )
    _write(
        repository,
        "tests/test_service.py",
        """
from ashare_decision_support.service import Service
assert Service().used_in_tests() == 1
""",
    )

    assert audit.check_methods(audit._read_trees()) == (
        ["Service.unused"],
        ["Service.used_in_tests"],
    )


def test_later_scan_sees_new_consumers(repository) -> None:
    _write(repository, "src/ashare_decision_support/helpers.py", "def build():\n    return 1\n")
    assert audit.check_symbols(audit._read_trees()) == (["build"], [])

    _write(
        repository,
        "scripts/probe.py",
        "from ashare_decision_support.helpers import build\nbuild()\n",
    )

    assert audit.check_symbols(audit._read_trees()) == ([], [])


def test_invalid_python_fails_audit_instead_of_disappearing(
    repository, monkeypatch, capsys
) -> None:
    _write(repository, "src/ashare_decision_support/broken.py", "def broken(:\n")
    monkeypatch.setattr(audit.sys, "argv", ["inventory_audit.py"])

    assert audit.main() == 1
    assert "broken.py" in capsys.readouterr().err
