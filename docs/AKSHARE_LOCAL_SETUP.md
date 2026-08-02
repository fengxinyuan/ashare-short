# AkShare Local Setup

AkShare supplies everything TickFlow does not: **minute bars**, **news**,
**announcements**, and the **dragon-tiger board**. All free, no key.

## Install

```bash
cd /root/a-share-ultra-short-ai-system
python -m pip install -e ".[akshare]"
```

## What is actually reachable

Measured on this host, not copied from the AkShare docs. The distinction that
matters is which upstream host an interface talks to.

| 数据 | 接口 | 上游 | 状态 |
| --- | --- | --- | --- |
| 分钟线 1/5/15/30/60 | `stock_zh_a_minute` | 新浪 | ✅ 各 1970 根，`adjust=""` 为不复权 |
| 个股新闻 | `stock_news_em` | 东财 search | ✅ 10 条/股，含正文 |
| 财联社电报 | `stock_info_global_cls` | 财联社 | ✅ 20 条 |
| 东财全球快讯 | `stock_info_global_em` | 东财 | ✅ 200 条 |
| 新闻联播文字稿 | `news_cctv` | 央视 | ✅ 15 条 |
| 沪深全量公告 | `stock_notice_report` | 东财 data | ✅ 1570 条/日，**带公告类型** |
| 龙虎榜 | `stock_lhb_detail_em` | 东财 data | ✅ 468 条/周，带净买额 |
| 东财人气榜 | `stock_hot_rank_em` | 东财 | ✅ 100 条（暂未接入） |
| 批量现货行情 | `stock_zh_a_spot_em` | **push2** | ❌ RemoteDisconnected |
| 板块列表/行情 | `stock_board_*_em` | **push2** | ❌ RemoteDisconnected |
| 分钟线 | `stock_zh_a_hist_min_em` | **push2his** | ❌ RemoteDisconnected |
| 五档盘口 | `stock_bid_ask_em` | **push2** | ❌ RemoteDisconnected |
| 逐笔成交 | `stock_intraday_em` | **push2ex** | ❌ 5 次全失败 |

**Rule of thumb: anything on `push2*` is unavailable here.** Nothing in the
system depends on those endpoints — sector performance is aggregated from
constituent daily bars instead of read off a board index, and minute bars come
from Sina.

## The two roles AkShare plays

### 1. Minute bars (`adapters/sina_intraday.py`)

`adjust=""` returns **raw traded prices**, which is exactly what order levels
need. Each period returns a fixed 1970-bar window, so the span differs:
1m ≈ 8 sessions, 5m ≈ 41, 15m ≈ 123, 30m ≈ 1 year, 60m ≈ 2 years.

Freshness during a live session is **not yet verified** — run
`ashare-ds intraday-check` during trading hours.

### 2. Sentiment and money (`news/akshare_source.py`)

Every feed is guarded independently: a dead source costs its documents and adds
a note to the brief, never the run. Results are cached per trading day under
`<output-dir>/news_cache/`, so re-running the morning is free and reproducible.

The announcement feed's **公告类型** column is the most valuable field here — it
is an exchange-assigned label, so `screens/announcement.py` screens on it
without parsing free text.

## Notes

- AkShare interfaces change between releases; pin the version if a feed starts
  returning different columns. Tested against `akshare 1.18.81`.
- The adapters never raise on a single failed symbol or feed — check the
  `data_notes` in any brief to see what was missing.
