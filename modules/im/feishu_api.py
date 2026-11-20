"""The Feishu/Lark OpenAPI message calls the adapter sends, over plain HTTPS.

Five IM v1 endpoints: create, reply, patch and delete a message, and add a
reaction. Each request is the one ``lark-oapi`` produced for the same call
(method, path, query and JSON body), and a reply is read the way that SDK read
it: the body is parsed as JSON whatever the HTTP status, and ``code == 0``
means success. ``tests/test_feishu_api.py`` holds the two against each other on
the wire.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import quote, urlencode

import aiohttp
from yarl import URL

# The SDK's client default; one request, token already in hand.
REQUEST_TIMEOUT_SECONDS = 30.0


@dataclass
class FeishuApiResponse:
    """The ``code``/``msg``/``data`` envelope every OpenAPI reply carries."""

    code: Optional[int] = None
    msg: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)

    def success(self) -> bool:
        return self.code is not None and self.code == 0


class FeishuApi:
    """Message calls for one app, authorised by its tenant access token.

    ``tenant_token`` returns a valid token or raises; the adapter owns its
    fetch and cache, shared with the calls it already sends itself.
    """

    def __init__(
        self,
        base_url: str,
        tenant_token: Callable[[], Awaitable[str]],
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._tenant_token = tenant_token
        self._timeout = timeout

    async def create_message(
        self, receive_id_type: str, receive_id: str, msg_type: str, content: str
    ) -> FeishuApiResponse:
        return await self._request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            body={"receive_id": receive_id, "msg_type": msg_type, "content": content},
        )

    async def reply_message(
        self, message_id: str, msg_type: str, content: str, *, reply_in_thread: bool
    ) -> FeishuApiResponse:
        return await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{_segment(message_id)}/reply",
            body={"content": content, "msg_type": msg_type, "reply_in_thread": reply_in_thread},
        )

    async def patch_message(self, message_id: str, content: str) -> FeishuApiResponse:
        return await self._request(
            "PATCH",
            f"/open-apis/im/v1/messages/{_segment(message_id)}",
            body={"content": content},
        )

    async def delete_message(self, message_id: str) -> FeishuApiResponse:
        return await self._request("DELETE", f"/open-apis/im/v1/messages/{_segment(message_id)}")

    async def add_reaction(self, message_id: str, emoji_type: str) -> FeishuApiResponse:
        return await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{_segment(message_id)}/reactions",
            body={"reaction_type": {"emoji_type": emoji_type}},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, str]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> FeishuApiResponse:
        token = await self._tenant_token()
        headers = {"Authorization": f"Bearer {token}"}
        data: Optional[bytes] = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        target = self._base_url + path
        if params:
            target += "?" + urlencode(params)
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # ``encoded=True``: the path goes out as written, so an encoded
            # character in an id is not decoded back on the way.
            async with session.request(method, URL(target, encoded=True), data=data, headers=headers) as reply:
                raw = await reply.read()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            return FeishuApiResponse()
        data_field = payload.get("data")
        return FeishuApiResponse(
            code=payload.get("code"),
            msg=payload.get("msg"),
            data=data_field if isinstance(data_field, dict) else {},
        )


def _segment(value: str) -> str:
    """One path segment, encoded so that an id cannot add a segment or a query."""
    return quote(str(value), safe="")
