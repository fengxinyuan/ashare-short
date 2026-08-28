"""行情站点的取文本这一步：装浏览器头、按站点的编码解码。

八个适配器（新浪快照、腾讯明细、宽基指数、全市场扫描、涨停池、同花顺板块指数与
分类、盘前海外快照）各自写过一份**逐字相同**的四行：建一个带 `User-Agent` /
`Referer` 的 `Request`、用注入的 `opener` 打开、`read()`、按站点编码 `decode`。连
`USER_AGENT = "Mozilla/5.0"` 这个值都各自写了一遍——七处是同名常量，海外快照那处
是直接内联的字面量，所以按常量名去搜只搜得到七份。

四行本身不难写，收在这里是因为**它们之间的差别只有两个参数**——编码（新浪与
腾讯是 gbk、海外快照那一路是 gb18030，东财与同花顺是 utf-8）和解码失败时是丢
还是替换。差别摊在八个文件里的时候，看不出这是同一件事的八种取值，只看得出
八段长得差不多的代码；真要改取数行为（比如加一个超时重试、换 UA），得先把
八处都找齐。

`opener` 仍由调用方注入并原样传入——测试桩替换的是它，这一层不碰。
"""

from __future__ import annotations

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
