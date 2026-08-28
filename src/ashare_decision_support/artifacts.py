"""落盘产物的文件名：写的那一端和读的那一端认的是同一个字符串。

这一层没有逻辑，只有六个字符串。单独成模块的理由是**它们此前不止一处定义**：

- `morning_latest.json` 在 `jobs/`（写）与 `ask/context.py`（读）各一份；
- `advice_latest.json`、`post_close_latest.json` 同样各一份；
- `strategy_screen_latest.json` 更糟——读的那端有常量，写的那端（`cli.py`）是
  **两处裸字面量**，一处真写文件、一处写在 `--output` 的帮助文本里。

这类重复的失效方式和 `numeric.py` 收 `_float`、`envfile` 收 `ENV_HOME` 是同一种：
**改了一端不会报错**。改写入端的文件名，读取端不会抛异常，它只会走「文件不存在」
那条分支——而那条分支是正常路径（今天还没跑过盘前，本来就没有这份产物）。于是
`ask` 从此静默地少一块上下文，或者读到一份**上一次**的产物，两者都不会有任何
一行提示。

产物本身的目录由调用方给（`--output-dir`，默认 `briefs/`）：同一份产物写到哪个
目录是**调用时的选择**，叫什么名字不是。
"""

from __future__ import annotations

# 盘前主链路的事实包。`ask` / `chat` / `facts` 都从这份读盘前结论。
MORNING_LATEST_NAME = "morning_latest.json"

# 单标的买卖点。`advise` 写，`ask` 在问到具体标的时读。
ADVICE_LATEST_NAME = "advice_latest.json"

# 盘后复盘与次日执行卡。`post_close` 写，`pre_open` 与 `ask` 读。
POST_CLOSE_LATEST_NAME = "post_close_latest.json"

# 盘后快照，和上面那份是一对：`pre_open` 按同一个目录去找它的兄弟文件。
SNAPSHOT_LATEST_NAME = "snapshots_latest.json"

# 全市场策略筛选结果。`strategy-screen` 写，`ask` / `facts` 自动带入最近一份。
STRATEGY_SCREEN_LATEST_NAME = "strategy_screen_latest.json"

# 全板块量价龙头候选。`sector-leaders` 写，月度研究包读。
LEADERS_LATEST_NAME = "sector_leaders_latest.json"
