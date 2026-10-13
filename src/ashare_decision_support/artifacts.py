"""交接产物的统一文件名，供写入端与读取端共同使用。

产物目录由调用方的 output_dir 决定，文件名在此维护，避免读写路径分叉。"""

from __future__ import annotations

# 盘前主链路的事实包。`ask` / `chat` / `facts` 都从这份读盘前结论。
MORNING_LATEST_NAME = "morning_latest.json"

# 单标的买卖点。`advise` 写，`ask` 在问到具体标的时读。
ADVICE_LATEST_NAME = "advice_latest.json"

# 盘后复盘与次日执行卡。`post_close` 写，`pre_open` 与 `ask` 读。
POST_CLOSE_LATEST_NAME = "post_close_latest.json"

# 每日归因与次日复查项；同时保留 daily-reviews/YYYY-MM-DD.json 历史版本。
DAILY_REVIEW_LATEST_NAME = "daily_review_latest.json"

# 盘后快照，和上面那份是一对：`pre_open` 按同一个目录去找它的兄弟文件。
SNAPSHOT_LATEST_NAME = "snapshots_latest.json"

# 全市场策略筛选结果。`strategy-screen` 写，`ask` / `facts` 自动带入最近一份。
STRATEGY_SCREEN_LATEST_NAME = "strategy_screen_latest.json"

# 单次盘中监控的最新健康状态。每日告警 Markdown 是历史记录，不能代表当前状态。
MONITOR_STATUS_LATEST_NAME = "monitor_status_latest.json"

# 全板块量价龙头候选。`sector-leaders` 写，月度研究包读。
LEADERS_LATEST_NAME = "sector_leaders_latest.json"
