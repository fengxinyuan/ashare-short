# A-share Ultra-Short AI Decision Support

A 股超短线 **分析辅助** 系统。**不下单、不撤单、不接管证券账户。**

**从这里开始：[`docs/SOLUTION.md`](docs/SOLUTION.md)**（总览）

| 文档 | 内容 |
|---|---|
| [`SOLUTION.md`](docs/SOLUTION.md) | 总览 · 四条需求 · 全天链路 · 三层自主度 · 设计约束 |
| [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 模块地图 · 层次边界 · 调度 · 状态 · 失败模式 |
| [`DATA_SOURCES.md`](docs/DATA_SOURCES.md) | 实测矩阵 · 档位差异 · 量纲陷阱 · 校验 · 安装 |
| [`PIPELINE.md`](docs/PIPELINE.md) | 舆情 → 板块 → 选股 → 买卖点，每段怎么算 |
| [`DECISIONS.md`](docs/DECISIONS.md) | 为什么这么定 · 被否决的方案 · 已删除的东西 |

## 主体：自动运行

```bash
ashare-ds daemon                # 全天按时间表跑
ashare-ds daemon --dry-run      # 只看时间表
ashare-ds daemon --feishu       # 同时推飞书
```

```text
08:00 舆情采集入库 → 08:20 盘前主链路（舆情→大盘→板块→选股→候选池→台账）
   → 09:15 执行卡 → 09:35–14:55 盘中监控 → 15:10 盘后复盘 → 15:40 兑现核对
```

非交易日整张表跳过；任务失败只重试三次并推一条通知，不影响其余任务；进程中途重启
不会重发已完成的推送。

## 手工插问

```bash
ashare-ds watchlist add 600519 000001.SZ --note "核心仓"   # 自选股是所有命令的默认输入
ashare-ds watchlist hold 600519 --cost 1350.6              # 有成本价才能盯卖点

ashare-ds morning                                          # 盘前主链路
ashare-ds monitor --once                                   # 跑一轮盘中监控
ashare-ds advise                                           # 自选股买卖点与情景
ashare-ds chat                                             # 多轮追问
ashare-ds review                                           # 兑现核对 + 统计
ashare-ds journal --symbol 600519.SH                       # 某只票的建议与兑现
ashare-ds state                                            # 当前状态一览
```

## 实现状态

文档同时描述**已建成**与**已定但未建**的部分，后者一律标注。
避免把设计当现状，是这套文档上一次需要重写的直接原因。

| | 状态 |
|---|---|
| 舆情采集 → 持久语料库（记 first_seen） | ✅ `news/store.py`，已接盘前链路 |
| 盘前主链路：舆情→大盘→板块→选股→候选池→台账 | ✅ |
| 盘中监控：时效闸门 · 跃迁告警 · 持仓卖点 · 止跌确认 | ✅ |
| 数据校验：跨源比对 · 量纲反推 · 浮点卫生 | ✅ `adapters/validate.py` |
| 台账 → 兑现核对 → 统计（含对照组与筛选价值） | ✅ |
| 多轮对话 | ✅ `chat` |
| **买卖点三层（L2 候选集 + L3 模型裁决）** | ⬜ **未建**，现为单一规则路径，见 `SOLUTION.md` §三 |
| **五档盘口进买卖点** | ⬜ **未建**，适配器已实测可用但无调用方 |
| **热门股池（第三个标的列表）** | ⬜ **未建**，现只有自选股与候选池 |
| **`ask` 单入口问答** | ⬜ **未建**，用 `chat` 的 `/` 命令 |


## Install

```bash
cd /root/a-share-ultra-short-ai-system
python3 -m pip install -e ".[tickflow,akshare]"

export TICKFLOW_API_KEY=...      # 可选。不设走免费档；设了自动切付费档，代码无需改动
export DASHSCOPE_API_KEY=...     # 可选，仅 LLM 写稿 / 买卖点裁决 / 对话 / 多空辩论
export FEISHU_WEBHOOK_URL=...    # 可选，--feishu 推送
export ASHARE_DS_HOME=...        # 可选，状态目录，默认 ~/.ashare-ds
```

完整环境变量与诊断命令见 [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md) §七。

## 五件值得先知道的事

- **数据时效是自动化的总开关。** 人看简报会自己判断「非当日数据」，自动推送不会。
  监控每轮先验时效，数据不新就推一条「已挂起」然后停止产出信号，循环继续检测恢复。
  每条推送都带 as-of 时间戳。

- **数字只在确定性层产生，取舍才交给模型。** 规则算出所有数并给一组**带出处的候选
  价位**；模型在候选集内选择、权衡、可以说「今天不该买」，但说出一个候选集外的价
  就整段丢弃。台账同时记规则默认与模型裁决两套结论，让兑现核对回答哪个更好。

- **价格口径。** 日线比例前复权、分钟线不复权——两者本来就共用今天的价格尺度
  （实测 400 只，最新一根无一例外完全相同）。所以那个换算比值**恒等于 1.0，
  这正是它的价值**：一旦不等于 1.0 就说明两份数据对不上，此时宁可不给价。
  台账存相对百分比，因为只有百分比对复权免疫。

- **没有概率。** 没有拟合模型，就不印胜率和预期收益率，只印会改变判断的价位。
  台账攒够 200 条已兑现记录之前，统计只报告、不回写权重。

- **五档是挂单快照，不是成交意图。** 可撤、可假挂、只有五档，所以设计上它只能是
  **有界旁证**而不是主判据。真正的委托流（逐笔委托、撤单记录）任何档位都拿不到。
  （适配器已实测可用，**尚未接进买卖点**。）

## Project layout

```text
docs/      SOLUTION · ARCHITECTURE · DATA_SOURCES · PIPELINE · DECISIONS
scripts/   source_audit.py 数据源审计
src/       源码
tests/     单元测试
```
