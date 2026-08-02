"""Feishu (Lark) custom-bot notifier.

Three things about this API that a naive implementation gets wrong, all of
which are handled below:

1. **Feishu answers HTTP 200 on application errors.** A wrong webhook token,
   an IP not on the allow-list, a malformed body — all come back 200 with a
   non-zero `code` in the JSON. Checking the status line alone reports success
   for messages nobody received.
2. **The size limit is in bytes, not characters.** Chinese is 3 bytes per
   character, so a brief that looks half the limit long is over it.
3. **Signed bots sign the timestamp, not the payload.** The HMAC key is
   `f"{timestamp}\\n{secret}"` and the message is empty — which reads like a
   bug and is not.

Never raises. Delivery is a side channel; a dead webhook must not take down
the analysis run that produced the content.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

from ashare_decision_support.notify.base import Notifier, NotifierCapabilities, split_utf8_text

ENV_WEBHOOK = "FEISHU_WEBHOOK_URL"
ENV_SECRET = "FEISHU_SECRET"

# Feishu's documented ceiling is 30 KB; staying well under it leaves room for
# the JSON envelope and avoids edge-case rejections on long briefs.
MAX_BYTES = 11_000
DEFAULT_TIMEOUT = 10.0


class FeishuNotifier(Notifier):
    capabilities = NotifierCapabilities(channel="feishu", markdown=True, max_bytes=MAX_BYTES)

    def __init__(
        self,
        webhook_url: str | None = None,
        secret: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        opener: object | None = None,
    ) -> None:
        self.webhook_url = webhook_url or os.environ.get(ENV_WEBHOOK)
        self.secret = secret or os.environ.get(ENV_SECRET)
        self.timeout = timeout
        # Injectable for tests; defaults to urllib so the package keeps no
        # HTTP dependency.
        self._opener = opener or urllib.request.urlopen
        self.last_error: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    def send(self, title: str, content: str) -> bool:
        if not self.configured:
            self.last_error = f"未配置 {ENV_WEBHOOK}"
            return False

        chunks = split_utf8_text(content, self.capabilities.max_bytes)
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            heading = title if total == 1 else f"{title}（{index}/{total}）"
            if not self._post(self._card(heading, chunk)):
                return False
        self.last_error = None
        return True

    # ---- internals -------------------------------------------------------

    def _card(self, title: str, body: str) -> dict:
        payload: dict = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": "blue",
                },
                "elements": [{"tag": "markdown", "content": body}],
            },
        }
        if self.secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = _sign(timestamp, self.secret)
        return payload

    def _post(self, payload: dict) -> bool:
        request = urllib.request.Request(
            self.webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

        try:
            body = json.loads(raw)
        except ValueError:
            self.last_error = f"响应不是 JSON：{raw[:200]}"
            return False

        # HTTP 200 with a non-zero code is the normal failure shape here.
        code = body.get("code", body.get("StatusCode", 0))
        if code:
            self.last_error = f"飞书返回 code={code} msg={body.get('msg') or body.get('StatusMessage')}"
            return False
        return True


def _sign(timestamp: str, secret: str) -> str:
    """The signature is an HMAC over an *empty* message, keyed on ts+secret."""
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")
