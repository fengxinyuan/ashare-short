"""测试共用的假 HTTP 响应。

适配器层只对 `urllib` 的响应对象要求三件事：`read()`、能当上下文管理器用、
读出来是 bytes。此前十三份测试各写一份满足这三件事的小类（`_Resp` /
`Response` / `JsonResponse`，还有三份嵌在 `StubHttp.__call__` 里），逐字相同
或只差一个编码参数。集中到这里不是为了省行数——是让「响应对象长什么样」
只有一处定义：哪天适配器开始读 `.status` 或 `.headers`，改这里一次即可，
不用在十三份桩上各补一次、漏掉的那份还不会变红。
"""

from __future__ import annotations

import io
import json
from typing import Any


class FakeResponse(io.BytesIO):
    """`urlopen` 返回值的最小替身：字节体、可 `read()`、可 `with`。

    `body` 给 str 时按 `encoding` 编码——新浪、腾讯、同花顺的行情接口是 GBK，
    舆情源是 UTF-8，所以编码由调用方声明，不在这里猜。
    """

    def __init__(self, body: bytes | str, *, encoding: str = "utf-8") -> None:
        if isinstance(body, str):
            body = body.encode(encoding, errors="replace")
        super().__init__(body)

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def json_response(payload: Any) -> FakeResponse:
    """把一个 Python 对象序列化成 UTF-8 JSON 响应。"""
    return FakeResponse(json.dumps(payload, ensure_ascii=False))


class PageOpener:
    """按 URL 查表的 `urlopen` 替身：`pages` 里有的返回正文，`dead` 里的抛 OSError。

    `calls` 按顺序记下每个被请求的 URL——缓存类测试靠它证明「第二次没有出网」。
    舆情源（官方公告、RSS）的测试原来各自写一份，这里是那三份的并集。
    """

    def __init__(self, pages: dict[str, str], dead: set[str] | None = None) -> None:
        self.pages = pages
        self.dead = dead or set()
        self.calls: list[str] = []

    def __call__(self, request: Any, timeout: float | None = None) -> FakeResponse:
        url = request.full_url
        self.calls.append(url)
        if url in self.dead:
            raise OSError("down")
        return FakeResponse(self.pages[url])
