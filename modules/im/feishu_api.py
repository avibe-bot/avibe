"""The Feishu/Lark OpenAPI message calls the adapter sends, over plain HTTPS.

Five IM v1 endpoints: create, reply, patch and delete a message, and add a
reaction (and one more, read-only, for the history check). Each request is the one ``lark-oapi`` produced for the same call
(method, path, query and JSON body), and a reply is read the way that SDK read
it: the body is parsed as JSON whatever the HTTP status or content type, and
``code == 0`` means success. A redirect is not followed. ``tests/test_feishu_api.py``
holds the two against each other on the wire.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional
from urllib.parse import quote, urlencode

import aiohttp
from yarl import URL

# The SDK's client default; one request, token already in hand.
REQUEST_TIMEOUT_SECONDS = 30.0


class FeishuApiError(Exception):
    """A reply that is JSON but not the envelope Feishu answers with."""


@dataclass
class FeishuApiResponse:
    """The ``code``/``msg``/``data`` envelope every OpenAPI reply carries.

    ``data`` is ``None`` when the reply has none, as it is on most refusals.
    It is the object Feishu sent, unchecked beyond being an object: the
    adapter reads ``message_id`` and ``chat_id`` from it and nothing else.
    """

    code: Optional[int] = None
    msg: Optional[str] = None
    data: Optional[Dict[str, Any]] = None

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
            data_is_read=True,
        )

    async def reply_message(
        self, message_id: str, msg_type: str, content: str, *, reply_in_thread: bool
    ) -> FeishuApiResponse:
        return await self._request(
            "POST",
            f"/open-apis/im/v1/messages/{_segment(message_id)}/reply",
            body={"content": content, "msg_type": msg_type, "reply_in_thread": reply_in_thread},
            data_is_read=True,
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
            data_is_read=True,
        )

    async def list_messages(
        self, chat_id: str, start_time: int, end_time: int, page_token: Optional[str] = None
    ) -> FeishuApiResponse:
        """One page of a chat's history between two times (seconds), oldest first."""
        params = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "start_time": str(start_time),
            "end_time": str(end_time),
            "sort_type": "ByCreateTimeAsc",
            "page_size": "50",
        }
        if page_token:
            params["page_token"] = page_token
        return await self._request("GET", "/open-apis/im/v1/messages", params=params, data_is_read=True)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, str]] = None,
        body: Optional[Dict[str, Any]] = None,
        data_is_read: bool = False,
    ) -> FeishuApiResponse:
        token = await self._tenant_token()
        headers = {"Authorization": f"Bearer {token}"}
        data: Optional[bytes] = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(_without_nulls(body), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        target = self._base_url + path
        if params:
            target += "?" + urlencode(params)
        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # ``encoded=True``: the path goes out as written, so an encoded
            # character in an id is not decoded back on the way. A redirect is
            # answered as it stands, never by sending the message a second time.
            async with session.request(
                method, URL(target, encoded=True), data=data, headers=headers, allow_redirects=False
            ) as reply:
                raw = await reply.read()
        payload = json.loads(raw.decode("utf-8"))
        if payload is None:
            return FeishuApiResponse()
        if not isinstance(payload, dict):
            raise FeishuApiError(f"Feishu answered {method} {path} with JSON that is not an object")
        reply_data = payload.get("data")
        if not data_is_read:
            reply_data = None
        elif reply_data is not None and not isinstance(reply_data, dict):
            raise FeishuApiError(f"Feishu answered {method} {path} with data that is not an object")
        return FeishuApiResponse(code=payload.get("code"), msg=payload.get("msg"), data=reply_data)


def _without_nulls(value: Any) -> Any:
    """Leave out fields that have no value, rather than sending them as ``null``."""
    if isinstance(value, dict):
        return {key: _without_nulls(item) for key, item in value.items() if item is not None}
    return value


def _segment(value: str) -> str:
    """One path segment, encoded so that an id cannot add a segment or a query."""
    return quote(str(value), safe="")
