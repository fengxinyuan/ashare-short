"""Persistent state for the automation layer.

    watchlist.json          自选股，人维护，永不自动删除
    candidates.json         候选池，每早生成，隔日失效
    alerts/<date>.json      当日告警与观测状态，用于跃迁判定和去重
    daemon_state.json       当日任务完成情况，用于幂等与重启恢复
    journal/<month>.jsonl   预测台账（见 journal/）
"""

from ashare_decision_support.state.alerts import AlertState
from ashare_decision_support.state.paths import state_home, state_path
from ashare_decision_support.state.runstate import STATUS_FAILED, STATUS_OK, DaemonState
from ashare_decision_support.state.store import append_jsonl, read_json, read_jsonl, write_json
from ashare_decision_support.state.watchlist import (
    SOURCE_MANUAL,
    SOURCE_SCAN,
    CandidatePool,
    WatchEntry,
    Watchlist,
)

__all__ = [
    "SOURCE_MANUAL",
    "SOURCE_SCAN",
    "STATUS_FAILED",
    "STATUS_OK",
    "AlertState",
    "CandidatePool",
    "DaemonState",
    "WatchEntry",
    "Watchlist",
    "append_jsonl",
    "read_json",
    "read_jsonl",
    "state_home",
    "state_path",
    "write_json",
]
