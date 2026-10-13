"""Feishu (Lark) notifier.

Supports two delivery modes:

1. **Custom bot webhook** (`FEISHU_WEBHOOK_URL`, optional `FEISHU_SECRET`)
2. **Self-built app credentials** (`FEISHU_APP_ID`, `FEISHU_APP_SECRET`,
   `FEISHU_RECEIVE_ID_TYPE`, `FEISHU_RECEIVE_ID`)

Three things about these APIs that a naive implementation gets wrong, all of
which are handled below:

1. **Feishu answers HTTP 200 on application errors.** A wrong webhook token,
   an IP not on the allow-list, a malformed body — all can come back 200 with a
   non-zero `code` in the JSON. Checking the status line alone reports success
   for messages nobody received.
2. **The size limit is in bytes, not characters.** Chinese is 3 bytes per
   character, so a brief that looks half the limit long is over it.
3. **Signed bots sign the timestamp, not the payload.** The HMAC key is
   `f"{timestamp}\\n{secret}"` and the message is empty — which reads like a
   bug and is not.

Never raises. Delivery is a side channel; a dead Feishu channel must not take
down the analysis run that produced the content.
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
from urllib.parse import urlencode

from ashare_decision_support.notify.base import Notifier, NotifierCapabilities, split_utf8_text

ENV_WEBHOOK = "FEISHU_WEBHOOK_URL"
ENV_SECRET = "FEISHU_SECRET"
ENV_APP_ID = "FEISHU_APP_ID"
ENV_APP_SECRET = "FEISHU_APP_SECRET"
ENV_RECEIVE_ID_TYPE = "FEISHU_RECEIVE_ID_TYPE"
ENV_RECEIVE_ID = "FEISHU_RECEIVE_ID"

# Feishu's documented ceiling is 30 KB; staying well under it leaves room for
# the JSON envelope and avoids edge-case rejections on long briefs.
MAX_BYTES = 11_000
DEFAULT_TIMEOUT = 10.0
TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"


class FeishuNotifier(Notifier):
    capabilities = NotifierCapabilities(channel="feishu", markdown=True, max_bytes=MAX_BYTES)

    def __init__(
        self,
        webhook_url: str | None = None,
        secret: str | None = None,
        app_id: str | None = None,
        app_secret: str | None = None,
        receive_id_type: str | None = None,
        receive_id: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        opener: object | None = None,
    ) -> None:
        self.webhook_url = webhook_url or os.environ.get(ENV_WEBHOOK)
        self.secret = secret or os.environ.get(ENV_SECRET)
        self.app_id = app_id or os.environ.get(ENV_APP_ID)
        self.app_secret = app_secret or os.environ.get(ENV_APP_SECRET)
        self.receive_id_type = receive_id_type or os.environ.get(ENV_RECEIVE_ID_TYPE)
        self.receive_id = receive_id or os.environ.get(ENV_RECEIVE_ID)
        self.timeout = timeout
        # Injectable for tests; defaults to urllib so the package keeps no
        # HTTP dependency.
        self._opener = opener or urllib.request.urlopen
        self.last_error: str | None = None
        self._tenant_access_token: str | None = None
        self._tenant_access_token_expires_at = 0.0

    @property
    def configured(self) -> bool:
        if self.webhook_url:
            return True
        return bool(self.app_id and self.app_secret and self.receive_id_type and self.receive_id)

    def send(self, title: str, content: str) -> bool:
        mode = self._mode()
        if mode is None:
            self.last_error = self._configuration_error()
            return False

        chunks = split_utf8_text(content, self.capabilities.max_bytes)
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            heading = title if total == 1 else f"{title}（{index}/{total}）"
            if mode == "webhook":
                ok = self._post_webhook(self._webhook_payload(heading, chunk))
            else:
                ok = self._post_app(heading, chunk)
            if not ok:
                return False
        self.last_error = None
        return True

    def _mode(self) -> str | None:
        if self.webhook_url:
            return "webhook"
        if self.app_id and self.app_secret and self.receive_id_type and self.receive_id:
            return "app"
        return None

    def _configuration_error(self) -> str:
        if self.webhook_url:
            return f"未配置完整飞书 webhook 参数：{ENV_WEBHOOK}"
        missing = [
            name
            for name, value in (
                (ENV_APP_ID, self.app_id),
                (ENV_APP_SECRET, self.app_secret),
                (ENV_RECEIVE_ID_TYPE, self.receive_id_type),
                (ENV_RECEIVE_ID, self.receive_id),
            )
            if not value
        ]
        if missing:
            return f"未配置飞书推送；需设置 {ENV_WEBHOOK}，或应用凭证模式下的 " + " / ".join(
                missing
            )
        return f"未配置 {ENV_WEBHOOK}"

    def _card_content(self, title: str, body: str) -> dict:
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": [{"tag": "markdown", "content": body}],
        }

    def _webhook_payload(self, title: str, body: str) -> dict:
        payload: dict = {"msg_type": "interactive", "card": self._card_content(title, body)}
        if self.secret:
            timestamp = str(int(time.time()))
            payload["timestamp"] = timestamp
            payload["sign"] = _sign(timestamp, self.secret)
        return payload

    def _post_webhook(self, payload: dict) -> bool:
        request = urllib.request.Request(
            self.webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        body = self._request_json(request)
        if body is None:
            return False
        code = body.get("code", body.get("StatusCode", 0))
        if code:
            self.last_error = (
                f"飞书返回 code={code} msg={body.get('msg') or body.get('StatusMessage')}"
            )
            return False
        return True

    def _post_app(self, title: str, body: str) -> bool:
        token = self._tenant_token()
        if not token:
            return False

        payload = {
            "receive_id": self.receive_id,
            "msg_type": "interactive",
            "content": json.dumps(self._card_content(title, body), ensure_ascii=False),
        }
        url = f"{MESSAGE_URL}?{urlencode({'receive_id_type': self.receive_id_type})}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        response = self._request_json(request)
        if response is None:
            return False
        code = response.get("code", response.get("StatusCode", 0))
        if code:
            self.last_error = f"飞书消息发送失败 code={code} msg={response.get('msg') or response.get('StatusMessage')}"
            return False
        return True

    def _tenant_token(self) -> str | None:
        now = time.time()
        if self._tenant_access_token and now < self._tenant_access_token_expires_at:
            return self._tenant_access_token

        request = urllib.request.Request(
            TOKEN_URL,
            data=json.dumps(
                {"app_id": self.app_id, "app_secret": self.app_secret},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        body = self._request_json(request)
        if body is None:
            return None
        code = body.get("code", body.get("StatusCode", 0))
        if code:
            self.last_error = f"飞书 token 获取失败 code={code} msg={body.get('msg') or body.get('StatusMessage')}"
            return None

        token = body.get("tenant_access_token")
        if not token:
            self.last_error = "飞书 token 响应缺少 tenant_access_token"
            return None
        expires_in = int(body.get("expire") or 7200)
        self._tenant_access_token = str(token)
        self._tenant_access_token_expires_at = time.time() + max(expires_in - 60, 60)
        return self._tenant_access_token

    def _request_json(self, request: urllib.request.Request) -> dict | None:
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

        try:
            return json.loads(raw)
        except ValueError:
            self.last_error = f"响应不是 JSON：{raw[:200]}"
            return None


def _sign(timestamp: str, secret: str) -> str:
    """The signature is an HMAC over an *empty* message, keyed on ts+secret."""
    key = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(key, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")
