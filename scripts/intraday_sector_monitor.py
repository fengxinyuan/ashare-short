"""盘中板块强弱：命令行外壳。

**计算、落盘、渲染全部在 `sectors/intraday.py`，告警在 `monitor/sector_triggers.py`。**
这里只负责解析参数、决定跑一轮还是跑一天，以及把结果打到 stdout——同一份逻辑
盘中由 `ashare-ds monitor` 走告警通道用，人工查看时由这个脚本走终端用，
两条路径读的是同一份代码、同一个 JSONL 序列。

此前这个脚本自带一整套归并与渲染实现，并且读一个**不存在的** `briefs/
sector_taxonomy_sw1.json`——也就是说它跑起来就是 FileNotFoundError。现在分类
走 `adapters/ths_taxonomy.py`（行业 + 概念两套，5550 只全覆盖含北交所）。

```bash
python3 scripts/intraday_sector_monitor.py                  # 跑一轮，打印
python3 scripts/intraday_sector_monitor.py --loop           # 盘中持续跑
python3 scripts/intraday_sector_monitor.py --alerts         # 同时打印跃迁告警
python3 scripts/intraday_sector_monitor.py --level SW1      # 只看行业
```
"""

from __future__ import annotations

import argparse
import time
from datetime import date, datetime
from datetime import time as clock_time

from ashare_decision_support.adapters.freshness import in_session
from ashare_decision_support.monitor.sector_session import SectorSession
from ashare_decision_support.notify.render import render_sector_alerts, sector_alert_title
from ashare_decision_support.sectors import intraday


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="盘中板块强弱快照")
    parser.add_argument("--top", type=int, default=8, help="每层打印前几名")
    parser.add_argument(
        "--level",
        nargs="+",
        choices=[intraday.LEVEL_INDUSTRY, intraday.LEVEL_CONCEPT],
        help="只跑指定层级，省略则行业 + 概念都跑",
    )
    parser.add_argument("--date", help="落盘用的交易日，默认今天")
    parser.add_argument(
        "--no-store", action="store_true", help="只打印，不落盘（与 --alerts 互斥）"
    )
    parser.add_argument(
        "--alerts",
        action="store_true",
        help="同时计算并打印跃迁告警（会读写当日序列与告警状态）",
    )
    parser.add_argument("--loop", action="store_true", help="持续跑，直到 --until")
    parser.add_argument("--interval", type=int, default=600, help="轮询间隔秒，默认 600")
    parser.add_argument("--until", default="15:00", help="收工时刻 HH:MM，默认 15:00")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.alerts and args.no_store:
        # 跃迁是拿这一轮和当日序列比出来的，不落盘就没有序列可比，下一轮会把
        # 同一批变化再判一次。与其给一个安静地退化成「每轮重报」的组合，不如拒绝。
        parser.error("--no-store 与 --alerts 互斥：跃迁判定要读写当日序列")

    session_date = date.fromisoformat(args.date) if args.date else None
    taxonomies = intraday.default_taxonomies()
    if args.level:
        taxonomies = {key: value for key, value in taxonomies.items() if key in set(args.level)}

    if not args.loop:
        _one_pass(args, taxonomies, datetime.now(), session_date)
        return

    hour, _, minute = args.until.partition(":")
    until = clock_time(int(hour), int(minute or 0))

    while True:
        now = datetime.now()
        if now.time() > until:
            print(f"[{now:%H:%M:%S}] 到达 {args.until}，收工", flush=True)
            break
        # 午休与盘前不取数：行情不动，落进去的是一串重复快照，会把「较上轮」
        # 这一列变成噪声。
        if in_session(now):
            _one_pass(args, taxonomies, now, session_date)
        else:
            print(f"[{now:%H:%M:%S}] 非交易时段，跳过本轮", flush=True)
        time.sleep(args.interval)


def _one_pass(args, taxonomies: dict, now: datetime, session_date: date | None) -> None:
    today = session_date or now.date()

    if not args.alerts:
        # 只看截面：不建会话，也就不碰告警状态文件——这一路可以随便跑而不污染
        # 当天的「已经推过了」记录。
        path = intraday.store_path(str(today))
        snapshot = intraday.take_snapshot(taxonomies, now=now)
        print(intraday.render(snapshot, intraday.load_history(path), top=args.top), flush=True)
        print(flush=True)
        if not args.no_store:
            intraday.append(snapshot, path)
        return

    # 要告警就走和盘中监控**完全同一条**代码路径：截面 → 落盘 → 与当日序列比对
    # → 跃迁 → 状态落盘。此前这里自己抄了一遍 evaluate + state.save，
    # 于是「脚本看到的告警」和「推送收到的告警」是两份实现。
    session = SectorSession(session_date=today, taxonomies=taxonomies)
    history = intraday.load_history(session.path)
    outcome = session.poll(now)

    if outcome.snapshot is not None:
        print(intraday.render(outcome.snapshot, history, top=args.top), flush=True)
        print(flush=True)
    if outcome.alerts:
        print(sector_alert_title(outcome.alerts), flush=True)
        print(render_sector_alerts(outcome.alerts, outcome.snapshot), flush=True)
    else:
        print("（本轮无板块跃迁告警）", flush=True)
    for note in outcome.notes:
        print(f"- {note}", flush=True)
    print(flush=True)


if __name__ == "__main__":
    main()
