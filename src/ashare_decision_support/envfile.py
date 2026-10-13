"""CLI 与 MCP 启动时加载本地配置，仅补充尚未设置的环境变量。

显式环境变量（包括空字符串）优先于文件，同名配置以最先出现的文件为准。
加载只在进程入口执行；适配器读取环境变量，不自行寻找配置文件。
"""

from __future__ import annotations

import os
from pathlib import Path

from ashare_decision_support.state.paths import DEFAULT_HOME, ENV_HOME

ENV_FILE = "ASHARE_DS_ENV_FILE"

# 同名变量以先出现的文件为准。专用文件排在 `.env` 前面，避免通用配置意外
# 抢走某条数据源或推送通道的密钥。
FILENAMES = (".env.tickflow", ".env.feishu", ".env")

_loaded: list[str] | None = None


def _repo_root() -> Path:
    # src/ashare_decision_support/envfile.py → 仓库根。非 editable 安装时指向
    # site-packages 的上层，那里不会有 .env 文件，多找一次而已。
    return Path(__file__).resolve().parents[2]


def _state_home() -> Path:
    raw = os.environ.get(ENV_HOME)
    # 不用 state.paths.state_home()：那个会 mkdir，而「找一下有没有密钥文件」
    # 不该有创建目录的副作用。取的是同一个环境变量名与同一个默认目录——那两个
    # 值原来在这里各写了一份，改了 `state/paths.py` 的默认目录而没改这里的话，
    # 密钥会静默地继续在旧目录里找，而「没找到 .env」和「没有 .env」不报错。
    return Path(raw).expanduser() if raw else DEFAULT_HOME


def candidate_files() -> list[Path]:
    """按优先级从高到低列出会被读取的文件，不管存不存在。"""
    explicit = os.environ.get(ENV_FILE)
    paths = [Path(explicit).expanduser()] if explicit else []
    for root in (Path.cwd(), _repo_root(), _state_home()):
        paths.extend(root / name for name in FILENAMES)

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.expanduser()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def parse(text: str) -> dict[str, str]:
    """`KEY=VALUE` 一行一条。支持 `export` 前缀、`#` 注释、成对引号。

    不处理行内注释：`KEY=value  # 说明` 里的 `#` 之后属于值。密钥里出现 `#` 比
    出现行内注释更常见，猜错了会得到一个被截断的 key —— 401 而不是明确的报错。
    """
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, _, value = line.partition("=")
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values.setdefault(name, value)
    return values


def load_env_files(force: bool = False) -> list[str]:
    """把找到的 `.env` 文件填进 `os.environ`，返回实际读到的文件路径。

    幂等：重复调用不重读，除非 `force=True`（测试用）。
    """
    global _loaded  # noqa: PLW0603 — 模块级一次性缓存就是这个函数的语义
    if _loaded is not None and not force:
        return _loaded

    used: list[str] = []
    for path in candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # 不存在、没权限、不是 UTF-8：都不是要中断启动的理由。密钥缺失会在
            # 用到的地方以「免费档」的形式显式暴露出来。
            continue
        applied = False
        for name, value in parse(text).items():
            if name in os.environ:
                continue
            os.environ[name] = value
            applied = True
        if applied:
            used.append(str(path))

    _loaded = used
    return used


def loaded_files() -> list[str]:
    """`load_env_files()` 这次进程里实际用上的文件，供 `ashare-ds state` 显示。"""
    return list(_loaded) if _loaded is not None else []
