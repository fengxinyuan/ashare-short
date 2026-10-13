# A-share Ultra-Short AI Decision Support

A 股超短线事实采集与分析辅助系统，提供盘前分析、盘中提醒、盘后复盘和外部 AI 事实包。
系统不下单、不撤单、不接管账户。事实由确定性规则产生，默认模板出稿；
模型调用取决于命令与密钥配置，见 [模型开关](docs/USAGE.md#模型开关)。

个股分析优先使用命令中给出的代码，省略时读取用户登记的持仓。盘前筛选候选只作为分析结果，
不会自动成为后续监控名单。

[使用指南](docs/USAGE.md) · [文档导航](docs/README.md)

如果想问“下午有什么股票可关注”，先运行 `ashare-ds strategy-screen --profile all`
查看筛选截止日，再对候选逐只运行 `ashare-ds advise <代码>` 核对当日价位、止损与风险。
也可运行 `ashare-ds ask --deep "下午有什么能够入手的股票"` 查看一页式说明；
旧筛选结果只作历史观察，筛选排名不等于买入信号。

## 安装

需要 Python 3.11 或更高版本。建议使用独立虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[tickflow,akshare]"
cp .env.tickflow.example .env.tickflow
cp .env.feishu.example .env.feishu
```

Windows PowerShell 使用 `.venv\Scripts\Activate.ps1` 激活环境。
在 `.env.tickflow` 填写数据源配置；飞书为可选项，配置后通过 `--feishu` 开启推送。
数据源权限、量纲和历史实测见 [数据源说明](docs/DATA_SOURCES.md)。

## 日常工作流

```bash
# 登记持仓
ashare-ds position open 600519.SH --cost 1350.6 --stop 1330 --target 1430
ashare-ds position list

# 盘前分析与板块情景
ashare-ds morning
ashare-ds sector-forecast

# 盘中检查与单票分析
ashare-ds monitor --once
ashare-ds sector-confirmation
ashare-ds advise 600519.SH

# 盘后复盘与到期建议核对
ashare-ds post-close-brief
ashare-ds review

# 导出事实给外部 AI
ashare-ds facts --output briefs/facts.json
```

`daemon` 按交易日历和时间表运行全天任务，`daemon --dry-run` 查看安排。
`review --daily` 可离线补跑当日过程复盘。状态默认保存在 `~/.ashare-ds/`，可通过
`ASHARE_DS_HOME` 指定；报告默认写入 `briefs/`。

## 其他入口

| 场景 | 命令 |
|---|---|
| 查询与多轮查看事实 | `ask`、`chat`；显式 `--llm` 开启模型回答 |
| 板块复看与历史基线 | `sector-analysis`、`sector-backtest` |
| 全市场策略筛选 | `strategy-screen --profile all` |
| 龙头与研究资料 | `sector-leaders`、`monthly-context`、`preopen-research` |
| 外部工具服务 | `ashare-ds-mcp`，配置见 [工具接入](docs/EXTERNAL_AI_TOOLS.md) |
| 数据维护与诊断 | `history-refresh`、`calendar-refresh`、`tick-health`、`intraday-check`、`state` |

完整参数使用 `ashare-ds --help` 和子命令的 `--help` 查看，场景说明集中在
[使用指南](docs/USAGE.md)。排序与历史统计用于研究，不代表未来胜率或收益承诺。

## 开发

```bash
python3 -m pip install -e ".[dev]"
python3 scripts/check.py
python3 scripts/check.py --fast
```

完整检查包含 Ruff 静态检查、格式检查、接线审计、文档审计和离线 pytest；
`--fast` 跳过 pytest。构建使用 `python3 -m build`，专项回归、持续集成和检查边界见
[测试指南](docs/TESTING.md)。

源码位于 `src/ashare_decision_support/`，测试位于 `tests/`，维护脚本位于 `scripts/`。
