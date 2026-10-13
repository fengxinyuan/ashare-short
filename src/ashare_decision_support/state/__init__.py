"""Persistent state for the automation layer.

    positions.json            持仓，人维护，只有你自己会增删
    alerts/<date>.json        当日个股告警与观测状态，用于跃迁判定和去重
    alerts/sector-<date>.json 当日板块告警状态，与个股分开存
    events/<date>.jsonl       盘中轮询、告警与采样价位，供每日复盘按时点回放
    runtime/                  monitor / daemon 的实例互斥锁
    daemon_state.json         当日任务完成情况，用于幂等与重启恢复
    journal/<month>.jsonl     判断版本与兑现台账（见 journal/）

合并型状态（持仓、日线缓存、历史序列、计划与事件追加）在 `store.file_lock` 内读改写；
告警与调度状态由单个运行实例维护，实例互斥见 `runtime.exclusive_run`。
完整目录清单见 docs/ARCHITECTURE.md「状态与归档」。
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
