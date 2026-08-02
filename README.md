# A-share Ultra-Short AI Decision Support

A 股超短线 **分析辅助** 系统。**不下单、不撤单、不接管证券账户。**

**从这里开始：[`docs/SOLUTION.md`](docs/SOLUTION.md)**（总览）
· [`docs/AUTOMATION.md`](docs/AUTOMATION.md)（自动化架构）
· [`docs/DATA_SOURCE_AUDIT.md`](docs/DATA_SOURCE_AUDIT.md)（数据源实测）

## 主体：自动运行

```bash
ashare-ds daemon                # 全天按时间表跑
ashare-ds daemon --dry-run      # 只看时间表
ashare-ds daemon --feishu       # 同时推飞书
```

```text
08:00 舆情采集 → 08:20 盘前主链路（舆情→大盘→板块→选股→候选池→台账）
   → 09:15 执行卡 → 09:35–14:55 盘中监控 → 15:10 盘后复盘 → 15:40 兑现核对
```

非交易日整张表跳过；任务失败只重试三次并推一条通知，不影响其余任务；进程中途重启
不会重发已完成的推送。

## 手工插问

```bash
ashare-ds watchlist add 600519 000001.SZ --note "核心仓"   # 自选股是所有命令的默认输入
ashare-ds morning                                          # 盘前主链路
ashare-ds morning --no-sector-index --no-market-scan       # 关掉两路旁支读数（更快）
ashare-ds monitor --once                                   # 跑一轮盘中监控
ashare-ds advise                                           # 自选股买卖点与情景
ashare-ds chat                                             # 多轮追问
ashare-ds review                                           # 兑现核对 + 统计
ashare-ds journal --symbol 600519.SH                       # 某只票的建议与兑现
ashare-ds state                                            # 当前状态一览
```

## Install

```bash
cd /root/a-share-ultra-short-ai-system
python3 -m pip install -e ".[tickflow,akshare]"

export TICKFLOW_API_KEY=...      # 可选，免费档无需
export DASHSCOPE_API_KEY=...     # 可选，仅 --writer llm / chat / 多空辩论
export FEISHU_WEBHOOK_URL=...    # 可选，--feishu 推送
export ASHARE_DS_HOME=...        # 可选，状态目录，默认 ~/.ashare-ds
```

## Diagnostics

```bash
ashare-ds tick-health                              # TickFlow 连通性与能力边界
ashare-ds intraday-check --symbols 600519.SH       # 分钟线盘中时效（须在交易时段跑）
ashare-ds qmt-health                               # 本地 QMT 是否就绪
ashare-ds state                                    # 自选股/候选池/告警/任务/台账

python3 scripts/source_audit.py --repeats 5        # 全部数据源：通 / 时效 / 稳
python3 scripts/source_audit.py --watch 600        # 高频轮询压测（盘中即真实延迟）
```

## 四件值得先知道的事

- **数据时效是自动化的总开关。** 人看简报会自己判断「非当日数据」，自动推送不会。
  监控每轮先验时效，数据不新就推一条「已挂起」然后停止产出信号，循环继续检测恢复。
  每条推送都带 as-of 时间戳。
- **数字只在确定性层产生。** 采集与计算 agent 不含模型；LLM 的三个角色（多头举证 /
  空头举证 / 裁判）只能引用这些数字，写出无法追溯的数字就整段丢弃。合规文本在生成
  之后程序化追加。
- **价格口径。** 日线前复权、分钟线不复权。所有挂单价来自不复权序列；日线位置经复权
  因子换算后才引用，对不上就拒绝给价。台账存相对百分比，因为只有百分比对复权免疫。
- **没有概率。** 没有拟合模型，就不印胜率和预期收益率，只印会改变判断的价位。台账
  攒够 200 条已兑现记录之前，统计只报告、不回写权重。

## Project layout

```text
docs/      SOLUTION.md 总览 · AUTOMATION.md 架构 · DATA_SOURCE_AUDIT.md 实测
scripts/   source_audit.py 数据源审计
src/       源码
tests/     单元测试（303）
```
