"""`.env` 文件加载：优先级、幂等、以及「不覆盖已有变量」这条不能破的规矩。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ashare_decision_support import envfile


@pytest.fixture(autouse=True)
def reset_loader(monkeypatch, tmp_path):
    monkeypatch.setattr(envfile, "_loaded", None)
    monkeypatch.delenv(envfile.ENV_FILE, raising=False)
    monkeypatch.setenv(envfile.ENV_HOME, str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    # 仓库根上真的放着一份 .env.tickflow（运维用），测试不能读到它
    monkeypatch.setattr(envfile, "_repo_root", lambda: tmp_path / "norepo")


def test_parses_export_prefix_comments_and_quotes():
    values = envfile.parse(
        "# comment\n\nPLAIN=one\nexport EXPORTED=two\nQUOTED=\"three\"\nSINGLE='four'\nSPACED  =  five  \nno_equals_sign"
    )
    assert values == {
        "PLAIN": "one",
        "EXPORTED": "two",
        "QUOTED": "three",
        "SINGLE": "four",
        "SPACED": "five",
    }


def test_hash_inside_value_is_not_a_comment():
    """密钥里带 `#` 比行内注释常见，猜错会得到一个被截断的 key。"""
    assert envfile.parse("TICKFLOW_API_KEY=tk_a#b")["TICKFLOW_API_KEY"] == "tk_a#b"


def test_loads_file_into_environment(tmp_path, monkeypatch):
    (tmp_path / ".env.tickflow").write_text("TICKFLOW_API_KEY=tk_from_file\n", encoding="utf-8")
    monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)

    used = envfile.load_env_files()

    assert os.environ["TICKFLOW_API_KEY"] == "tk_from_file"
    assert used == [str(tmp_path / ".env.tickflow")]


def test_existing_environment_wins(tmp_path, monkeypatch):
    """显式 export / systemd Environment= 必须压过磁盘文件。"""
    (tmp_path / ".env.tickflow").write_text("TICKFLOW_API_KEY=tk_from_file\n", encoding="utf-8")
    monkeypatch.setenv("TICKFLOW_API_KEY", "tk_from_shell")

    envfile.load_env_files()

    assert os.environ["TICKFLOW_API_KEY"] == "tk_from_shell"


def test_explicit_empty_environment_disables_a_file_credential(tmp_path, monkeypatch):
    (tmp_path / ".env.tickflow").write_text("TICKFLOW_API_KEY=tk_from_file\n", encoding="utf-8")
    monkeypatch.setenv("TICKFLOW_API_KEY", "")

    assert envfile.load_env_files() == []
    assert os.environ["TICKFLOW_API_KEY"] == ""


def test_env_tickflow_beats_dot_env(tmp_path, monkeypatch):
    (tmp_path / ".env.tickflow").write_text("TICKFLOW_API_KEY=specific\n", encoding="utf-8")
    (tmp_path / ".env").write_text("TICKFLOW_API_KEY=generic\n", encoding="utf-8")
    monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)

    envfile.load_env_files()

    assert os.environ["TICKFLOW_API_KEY"] == "specific"


def test_loads_feishu_env_file(tmp_path, monkeypatch):
    (tmp_path / ".env.feishu").write_text(
        "FEISHU_WEBHOOK_URL=https://open.feishu.cn/hook/test\nFEISHU_SECRET=s\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("FEISHU_SECRET", raising=False)

    used = envfile.load_env_files()

    assert os.environ["FEISHU_WEBHOOK_URL"].endswith("/test")
    assert os.environ["FEISHU_SECRET"] == "s"
    assert used == [str(tmp_path / ".env.feishu")]


def test_explicit_env_file_takes_priority(tmp_path, monkeypatch):
    explicit = tmp_path / "custom.env"
    explicit.write_text("TICKFLOW_API_KEY=explicit\n", encoding="utf-8")
    (tmp_path / ".env.tickflow").write_text("TICKFLOW_API_KEY=cwd\n", encoding="utf-8")
    monkeypatch.setenv(envfile.ENV_FILE, str(explicit))
    monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)

    envfile.load_env_files()

    assert os.environ["TICKFLOW_API_KEY"] == "explicit"


def test_missing_and_unreadable_files_are_not_errors(tmp_path, monkeypatch):
    monkeypatch.setenv(envfile.ENV_FILE, str(tmp_path / "nope.env"))
    assert envfile.load_env_files() == []
    assert envfile.loaded_files() == []


def test_load_is_idempotent(tmp_path, monkeypatch):
    target = tmp_path / ".env.tickflow"
    target.write_text("TICKFLOW_API_KEY=first\n", encoding="utf-8")
    monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)
    envfile.load_env_files()

    target.write_text("TICKFLOW_API_KEY=second\n", encoding="utf-8")
    monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)
    envfile.load_env_files()
    assert "TICKFLOW_API_KEY" not in os.environ

    envfile.load_env_files(force=True)
    assert os.environ["TICKFLOW_API_KEY"] == "second"


def test_state_home_lookup_does_not_create_directories(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv(envfile.ENV_HOME, str(home))
    envfile.candidate_files()
    assert not home.exists()


def test_candidate_order_covers_cwd_repo_and_state_home(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    monkeypatch.setattr(envfile, "_repo_root", lambda: repo)
    monkeypatch.setenv(envfile.ENV_HOME, str(home))

    names = [str(p) for p in envfile.candidate_files()]

    assert names == [
        str(Path(tmp_path) / ".env.tickflow"),
        str(Path(tmp_path) / ".env.feishu"),
        str(Path(tmp_path) / ".env"),
        str(repo / ".env.tickflow"),
        str(repo / ".env.feishu"),
        str(repo / ".env"),
        str(home / ".env.tickflow"),
        str(home / ".env.feishu"),
        str(home / ".env"),
    ]
