"""Persistent state for the automation layer.

    positions.json          持仓，人维护，只有你自己会增删
    alerts/<date>.json      当日告警与观测状态，用于跃迁判定和去重
    daemon_state.json       当日任务完成情况，用于幂等与重启恢复
    journal/<month>.jsonl   预测台账（见 journal/）

`watchlist.json` / `candidates.json` 已于 2026-08-11 随自选股与候选池一起删除，
理由见 `state/positions.py` 的模块 docstring。
"""

from ashare_decision_support.state.alerts import AlertState
from ashare_decision_support.state.paths import state_home, state_path
from ashare_decision_support.state.positions import Position, Positions
from ashare_decision_support.state.runstate import STATUS_FAILED, STATUS_OK, DaemonState
from ashare_decision_support.state.store import append_jsonl, read_json, read_jsonl, write_json

__all__ = [
    "STATUS_FAILED",
    "STATUS_OK",
    "AlertState",
    "DaemonState",
    "Position",
    "Positions",
    "append_jsonl",
    "read_json",
    "read_jsonl",
    "state_home",
    "state_path",
    "write_json",
]
