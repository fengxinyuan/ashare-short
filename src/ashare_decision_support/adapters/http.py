"""行情 HTTP 文本读取与 JSONP 解析。

调用方注入 opener，并指定 Referer、编码及解码错误策略。
JSONP 解析供同花顺板块指数和新浪海外日线共用。
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Callable
from typing import Any

# 行情站点认的是「有没有浏览器头」，不认具体型号：新浪的 `hq.sinajs.cn` 不带
# `Referer` 直接返回空串，带上任意 UA 都能取到。所以这里只需要一个占位值。
USER_AGENT = "Mozilla/5.0"

# 东财与同花顺那三路要的是**完整**的浏览器 UA——占位值在它们那里会被挡。三个
# 文件里原来各存了一份逐字相同的这段字符串。
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 两个站点的行情端点与它们各自认的 `Referer`。收在这里是因为**同一个端点被好几个
# 适配器打**：`snapshot`（个股快照）、`broad_index`（宽基指数）与 `global_market`
# （盘前海外快照）打新浪的同一个 `list=` 接口，`snapshot` 与 `tencent_detail` 打
# 腾讯的同一域。四个文件里原来各存了一份地址，改一处不会让另一处报错——它只会让
# 几个适配器从此对着不同的端点取数，而每一个都还能跑。
SINA_QUOTE_URL = "https://hq.sinajs.cn/list="
SINA_REFERER = "https://finance.sina.com.cn"
TENCENT_REFERER = "https://gu.qq.com"


def fetch_text(
    opener: Callable[..., Any],
    url: str,
    *,
    timeout: float,
    referer: str | None = None,
    encoding: str = "utf-8",
    errors: str = "ignore",
    user_agent: str = USER_AGENT,
) -> str:
    """取一段文本。

    `errors` 默认 `ignore` 而不是 `strict`：这些站点会在 gbk 正文里混进个别
    非法字节，`strict` 会让整轮取数因为一个字符失败。丢掉那个字符的代价是一个
    标的名少一个字，比整批取不到小得多——两种取值原来就都在用，这里把默认值
    定成多数派，需要 `replace` 的调用方显式传。
    """
    headers = {"User-Agent": user_agent}
    if referer is not None:
        headers["Referer"] = referer
    request = urllib.request.Request(url, headers=headers)
    with opener(request, timeout=timeout) as response:
        return response.read().decode(encoding, errors)


_JSONP = re.compile(r"\((.*)\)\s*;?\s*$", re.DOTALL)


def jsonp_payload(text: str) -> Any:
    """剥掉 JSONP 外壳，拿到里面的 JSON 值；不是 JSONP 或壳里不是 JSON 时返回 None。

    返回值不限类型——同花顺壳里是对象，新浪美股壳里是数组——形状由各调用方自己判。
    """
    match = _JSONP.search(text.strip())
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
