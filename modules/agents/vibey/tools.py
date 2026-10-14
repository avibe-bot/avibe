"""Vibey's tools: Pi's coding tools over a Watch-backed job host (C-7).

``ToolSuite`` is the adapter's whole dependency on ``core.agent_core.tools``:
the job host, the tools built over the loop's tracking wrapper (``Agent.jobs``,
so an abort kills foreground commands), the bash renderer recovery uses for open
calls (``recovery.md`` T2), the lookup from a call instance to its job, and
job pruning.

The job host lives in Watch's ``agent_jobs_dir()``, where ``vibe stop`` and the
job-Watch sweep look, and hands a job over through Watch's own
``hand_over_job``: adopt-or-create the once Watch with target kind ``job``,
keyed by ``job_id`` (J6). Raising means no Watch owns the job; ``bash`` then
keeps the command in the foreground.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

import httpx

from core.agent_core.agent.models import ModelSelection
from core.agent_core.agent.recovery import RecoveryRenderer
from core.agent_core.ai._common import CONNECT_TIMEOUT_S, auth_headers, join_endpoint_url
from core.agent_core.ai.hosted import bounded_error, image_dimensions, record_text
from core.agent_core.ai.provider import HostedExecutor, MediaSink, ModelEndpoint
from core.agent_core.messages import (
    HostedImage,
    HostedRecord,
    HostedResult,
    TextBlock,
    ToolResultMessage,
    message_from_dict,
)
from core.agent_core.tools.base import CallInstance, JobHost, Tool, ToolContext, ToolResult, ToolSpec
from core.agent_core.tools.hosted import hosted_tool_spec
from core.agent_core.tools.shell import default_shell

ImageSink = Callable[[bytes, str, str], Awaitable[str]]
#: Whether a job's tool call has a durable ``tool_result`` (the adapter's half of J5).
CallSettled = Callable[[Mapping[str, Any]], bool]


@dataclass(frozen=True)
class ToolSuite:
    jobs: JobHost
    create_tools: Callable[[JobHost, Optional[ImageSink]], Sequence[Tool]]
    render_recovered: RecoveryRenderer
    #: The job a call instance started, if any.
    find_job: Callable[[CallInstance], Optional[str]]
    prune: Optional[Callable[[CallSettled], list[str]]] = None
    #: The shell commands run in, as the environment block names it (C-7 tools.md section 8).
    shell: str = field(default_factory=lambda: default_shell().describe())


#: The hand-over route for a job (``hand_over_job``'s keywords), from its owning Turn.
HandOverRoute = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def local_tool_suite(jobs_dir: Optional[str] = None, *, route: Optional[HandOverRoute] = None) -> ToolSuite:
    """Pi's ``read``, ``write``, ``edit``, and ``bash`` over the ``pipe`` job host.

    Every path that creates a job Watch (``watch=true``, the foreground-window hand-over,
    and T2 recovery) goes through the host's ``on_hand_over``, which takes the Watch's
    Agent and authorization from ``route``: the job's owning Turn.
    """
    from core.agent_core.tools.bash import recovered_result
    from core.agent_core.tools.coding import create_coding_tools
    from core.agent_core.tools.jobs import LocalJobHost
    from core.watches import ManagedWatchStore, agent_jobs_dir

    async def hand_over(meta: Mapping[str, Any]) -> str:
        import core.watches as watches
        from core.vibe_agents import VibeAgentAccessError
        from storage.resource_access_service import ResourceAccessError

        owner = dict(await asyncio.to_thread(route, meta)) if route is not None else {}
        try:
            return await watches.hand_over_job(meta, **owner)
        except (ResourceAccessError, VibeAgentAccessError, ValueError):
            # The Turn's principal may not create this Watch (no Harness edit right, or
            # no access to the Agent): the Watch still owns the job, and its follow-up
            # stays denied.
            fallback = {key: value for key, value in owner.items() if key != "user_context"}
            return await watches.hand_over_job(meta, **fallback, unverifiable_remote=True)

    host = LocalJobHost(str(jobs_dir or agent_jobs_dir()), on_hand_over=hand_over)

    def create_tools(jobs: JobHost, image_sink: Optional[ImageSink]) -> Sequence[Tool]:
        return create_coding_tools(jobs, image_sink=image_sink, shell=host.shell)

    async def render(call: Any, job_id: str, status: Any, watch_id: Optional[str]) -> Any:
        return await recovered_result(host, job_id, status, watch_id)

    def prune(call_settled: CallSettled) -> list[str]:
        # J5: a job's files stay until its call has a durable result and no Watch still manages it.
        watches = ManagedWatchStore()
        return host.prune(lambda meta: call_settled(meta) and watches.job_watch_settled(str(meta.get("job_id"))))

    return ToolSuite(
        jobs=host,
        create_tools=create_tools,
        render_recovered=render,
        find_job=host.find_job,
        prune=prune,
        shell=host.shell.describe(),
    )


#: The hosted tools a Turn registers, each serving both modes. A generated image, native or proxied, is saved through
#: the media sink as it arrives, so history holds only its path (§7.3).
PARSED_HOSTED_TOOLS = ("web_search", "x_search", "image_generation")


@dataclass(frozen=True)
class HostedTool:
    """One hosted tool of the Turn (``vibey-hosted-tools.md`` §3.4, §4.1), with one name and schema in both modes.

    On a hop whose route for it is native, the adapter sends the provider's declaration in its spec's place and the
    provider runs the call. A call that reaches ``execute`` is proxied: the gateway runs it on the executor Model Hub
    bound for the Turn, and nothing else. The gateway writes the record; this tool writes the text the model reads,
    with the same function that renders a replayed call.
    """

    spec: ToolSpec
    executor: Optional[HostedExecutor]
    #: The Turn's gateway: where the executor endpoint lives, and the token and headers that authorize the call.
    endpoint: ModelEndpoint = field(repr=False)
    client: Optional[httpx.AsyncClient] = field(default=None, repr=False)
    #: Where a proxied image is saved: the same sink as a native one.
    media: Optional[MediaSink] = field(default=None, repr=False)

    async def execute(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        del ctx
        hosted = await self._run(dict(arguments))
        query = arguments.get("query") or arguments.get("prompt")
        text = record_text(self.spec.name, hosted.record, query=query if isinstance(query, str) else None)
        failed = hosted.record.status in ("failed", "incomplete")
        return ToolResult(content=(TextBlock(text),), is_error=failed, hosted=hosted)

    async def _run(self, arguments: dict[str, Any]) -> HostedResult:
        url = join_endpoint_url(self.endpoint.base_url, f"/hosted-tools/{self.spec.name}")
        executor = self.executor
        body = {
            "arguments": arguments,
            "executor": {"source_id": executor.source_id, "model_id": executor.model_id} if executor else None,
        }
        headers = auth_headers(self.endpoint, provider="openai", gateway=True)
        # The gateway bounds the call (180 s for a search); only connecting is bounded here.
        timeout = httpx.Timeout(None, connect=CONNECT_TIMEOUT_S)
        try:
            if self.client is not None:
                response = await self.client.post(url, json=body, headers=headers, timeout=timeout)
            else:
                # The Model Hub gateway is loopback: an environment or system proxy must never carry it.
                async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                    response = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as error:
            return _failed(self.spec.name, f"the gateway could not run the call ({type(error).__name__})")
        if response.status_code != 200:
            return _failed(
                self.spec.name, f"the gateway refused the call (HTTP {response.status_code} {_error_code(response)})"
            )
        try:
            # A proxied image's answer carries megabytes of base64: parsed off the controller loop.
            answer = await asyncio.to_thread(response.json)
            hosted = _hosted_result(self.spec.name, answer)
        except (TypeError, ValueError):
            return _failed(self.spec.name, "the gateway answered with no hosted result")
        if self.spec.name == "image_generation" and hosted.record.status == "completed":
            hosted = await self._saved(hosted, answer)
        return hosted

    async def _saved(self, hosted: HostedResult, answer: Mapping[str, Any]) -> HostedResult:
        """A proxied image, saved through the media sink as a native one is (§7.3, item 27): the executor returns its
        base64, and the record lists the file. A completed call that saved no image failed."""

        generation = answer.get("generation")
        width, height = image_dimensions(generation.get("size") if isinstance(generation, Mapping) else None)
        saved: list[HostedImage] = []
        try:
            for item in answer.get("images") or ():
                encoded = item.get("data") if isinstance(item, Mapping) else None
                if not isinstance(encoded, str) or self.media is None:
                    raise ValueError("nothing here can keep a generated image")
                saved.append(replace(await self.media.save_image(encoded), width=width, height=height))
        except (ValueError, OSError) as error:
            return replace(hosted, record=_failed_record(self.spec.name, f"the image could not be saved: {error}"))
        if not saved:
            return replace(hosted, record=_failed_record(self.spec.name, "the executor returned no image"))
        return replace(hosted, record=replace(hosted.record, images=tuple(saved)))


def _failed_record(tool: str, reason: str) -> HostedRecord:
    return HostedRecord(
        status="failed", error=bounded_error(reason), images=() if tool == "image_generation" else None
    )


def _failed(tool: str, reason: str) -> HostedResult:
    """A call that never reached an executor: a failed record, and no execution (§4.2)."""

    return HostedResult(record=_failed_record(tool, reason))


def _error_code(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
    except (TypeError, ValueError, AttributeError):
        return ""
    code = error.get("code") or error.get("type") if isinstance(error, Mapping) else None
    return code if isinstance(code, str) else ""


def _hosted_result(tool: str, body: Any) -> HostedResult:
    """The executor's answer, read with the transcript's own reader for ``ToolResultMessage.hosted``."""

    if not isinstance(body, Mapping) or not isinstance(body.get("record"), Mapping):
        raise ValueError("no hosted record")
    hosted = {key: body[key] for key in ("record", "execution") if key in body}
    if tool == "image_generation":
        # The executor answers an image's bytes beside the record (``images``); the record lists what is saved.
        hosted["record"] = {**hosted["record"], "images": []}
    message = message_from_dict(
        {
            "role": "tool_result",
            "tool_call_id": "proxied",
            "tool_name": tool,
            "content": [],
            "is_error": body["record"].get("status") in ("failed", "incomplete"),
            "hosted": hosted,
        }
    )
    if not isinstance(message, ToolResultMessage) or message.hosted is None:
        raise ValueError("no hosted result")
    return message.hosted


def hosted_tools(
    selection: Optional[ModelSelection],
    client: Optional[httpx.AsyncClient] = None,
    media: Optional[MediaSink] = None,
) -> tuple[Tool, ...]:
    """The Turn's hosted tools: every tool the user enabled, in Model Hub's order, whatever preflight's hop runs it as.
    They lead the Turn's tools, so its last tool stays a local one whatever a hop renders them as, and the list stays
    the same for the whole Turn."""

    if selection is None:
        return ()
    endpoint = selection.endpoint
    return tuple(
        HostedTool(hosted_tool_spec(name), route.executor, endpoint, client, media)
        for name, route in endpoint.hosted_tools.items()
        if name in PARSED_HOSTED_TOOLS
    )
