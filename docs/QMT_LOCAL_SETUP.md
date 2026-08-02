# QMT Local Setup

QMT is **not activated yet**. This document records the intended integration so
that switching it on later is configuration, not a rewrite.

## The division of labour

QMT is the execution-side channel; the third-party APIs are the research side.
That split is already how the code is organised:

```text
盘前研究 / 盘后复盘   TickFlow(日线+行业) + AkShare(舆情/公告/龙虎榜) + 新浪(分钟线)
盘中执行             QMT / MiniQMT        ← adapters/qmt_xtdata.py，待开通
```

Even with QMT, this system stays **analysis-only**: it uses `xtdata`, never
`xttrader`. It does not submit, cancel, or manage orders.

## Setup when you do activate it

1. Install and log in to `MiniQMT` or `QMT` on the machine that will run this.
2. Confirm the local Python environment can `import xtquant.xtdata`.
3. Run `ashare-ds qmt-health` — it verifies the required `xtdata` functions are
   visible and reports what is missing.
4. If your broker's field names differ, edit
   `examples/custom_mapping_profile.json`; the normalization layer
   (`adapters/mapping.py`) resolves aliases before the strategy layer sees data.
   Reference: [QMT_FIELD_MAPPING.md](QMT_FIELD_MAPPING.md).

## What QMT would add

| 能力 | 现状（免费第三方） | QMT 之后 |
| --- | --- | --- |
| 分钟线 | 新浪，盘中时效待验证 | 券商柜台内存流，低延迟 |
| 五档盘口 | 拿不到 | 有 |
| 逐笔委托/成交 | 拿不到 | 有 |
| 实时快照 | 拿不到 | 有 |

## What QMT would *not* fix

Auction interception. Two independent reasons, both structural:

1. **09:20–09:25 交易所不接受撤单**，所以该窗口的「撤单率」恒等于 0，无论数据多好。
2. 09:25 撮合完成、开盘价已定；此后再下单只能是开盘单，参与不了竞价。

See `docs/PLAN_REVIEW.md`. The auction engine was removed from the codebase for
this reason — it was 221 lines implementing a decision that cannot be made in
time with data that does not exist.
