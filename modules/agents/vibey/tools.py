"""Vibey's tools: Pi's coding tools over a Watch-backed job host (C-7).

``ToolSuite`` is the adapter's whole dependency on ``core.agent_core.tools``:
the job host, the tools built over the loop's tracking wrapper (``Agent.jobs``,
so an abort kills foreground commands), the bash renderer recovery uses for open
calls (``recovery.md`` T2), the foreground wait a Turn continued after a restart
gives a surviving job, the lookup from a call instance to its job, and job
pruning.

The job host lives in Watch's ``agent_jobs_dir()``, where ``vibe stop`` and the
job-Watch sweep look, and hands a job over through Watch's own
``hand_over_job``: adopt-or-create the once Watch with target kind ``job``,
keyed by ``job_id`` (J6). Raising means no Watch owns the job; ``bash`` then
keeps the command in the foreground.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

import httpx

from config import v2_config
from core.agent_core.agent.recovery import RecoveryRenderer
from core.agent_core.ai._common import CONNECT_TIMEOUT_S, auth_headers, join_endpoint_url
from core.agent_core.ai.hosted import failure, generate_action, image_dimensions, record_text
from core.agent_core.ai.provider import ImageModel, ImageModels, MediaSink, ModelEndpoint
from core.agent_core.messages import (
    HostedAction,
    HostedFallback,
    HostedImage,
    HostedRecord,
    HostedResult,
    TextBlock,
    ToolResultMessage,
    WebFetchRecord,
    message_from_dict,
)
from core.agent_core.tools.base import CallInstance, JobHost, JobStatus, Tool, ToolContext, ToolResult, ToolSpec
from core.agent_core.tools.hosted import MAX_IMAGES, hosted_tool_spec, image_generate_spec, size_form
from core.agent_core.tools.read import MAX_INLINE_IMAGE_BYTES, ImageUnreadable, read_image
from core.agent_core.tools.truncate import format_size
from core.agent_core.tools.shell import default_shell
from modules.agents.vibey.models import HubModelRouter
from vibe.i18n import t as i18n_t

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
    #: ``(call, job_id, stop)``: a surviving job settled as its live call would, from its launch, or until
    #: ``stop``: its status and Watch (``settle_in_foreground``); ``None`` hands a running job over at once.
    wait_recovered: Optional[Callable[[Any, str, asyncio.Event], Awaitable[tuple[JobStatus, Optional[str]]]]] = None
    #: The shell commands run in, as the environment block names it (C-7 tools.md section 8).
    shell: str = field(default_factory=lambda: default_shell().describe())


#: The hand-over route for a job (``hand_over_job``'s keywords), from its owning Turn.
HandOverRoute = Callable[[Mapping[str, Any]], Mapping[str, Any]]


def local_tool_suite(jobs_dir: Optional[str] = None, *, route: Optional[HandOverRoute] = None) -> ToolSuite:
    """Pi's ``read``, ``write``, ``edit``, and ``bash`` over the ``pipe`` job host.

    Every path that creates a job Watch (``watch=true``, the foreground-window hand-over,
    and T2 recovery) goes through the host's ``on_hand_over``, which takes the Watch's
    Agent and authorization from ``route``: the job's owning Turn. ``bash`` and the
    recovered wait use the same default foreground window.
    """
    from core.agent_core.tools.bash import recovered_result, settle_in_foreground
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

    async def wait_recovered(call: Any, job_id: str, stop: asyncio.Event) -> tuple[JobStatus, Optional[str]]:
        return await settle_in_foreground(host, call, job_id, stop=stop)

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
        wait_recovered=wait_recovered,
    )


@dataclass(frozen=True)
class HostedTool:
    """One search tool of the Turn (``vibey-hosted-tools.md`` §3.4, §4.1), with one name and schema in both modes.

    On a hop whose route for it is native, the adapter sends the provider's declaration in its spec's place and the
    provider runs the call. A call that reaches ``execute`` is proxied: the gateway runs it on its executor chain,
    which it resolves from the live Turn the request names (§6). The gateway writes the record; this tool writes the
    text the model reads, with the same function that renders a replayed call.
    """

    # Proxied search and fetch calls are read-only and have per-call records, so
    # neighbouring calls from one response may share the loop's concurrent group.
    concurrent = True

    spec: ToolSpec
    #: The hop the Turn runs on now: where the executor endpoint lives, and the token and headers that authorize the
    #: call. A re-resolve can move the Turn to another Source, and the gateway answers only the hop it now holds.
    route: Callable[[], ModelEndpoint] = field(repr=False)
    client: Optional[httpx.AsyncClient] = field(default=None, repr=False)

    async def execute(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        del ctx
        endpoint = self.route()
        hosted, _ = await _gateway_call(endpoint, self.client, self.spec.name, dict(arguments))
        route = endpoint.hosted_tools.get(self.spec.name)
        if route is not None and route.refused and endpoint.source_id:
            # The hop's own Source would run the tool but refused it, so this call is a fallback from it (C-6
            # ``refused``): the card and the IM line say so.
            hosted = replace(hosted, fallback_from=HostedFallback(endpoint.source_id))
        query = arguments.get("query")
        return _result(self.spec.name, hosted, query=query if isinstance(query, str) else None)


@dataclass(frozen=True)
class ImageGenerateTool:
    """``image_generate`` (``vibey-hosted-tools.md`` §7): one of the Turn's image models, called directly on the
    gateway's image endpoint, never through a chat model.

    The Turn's first resolution freezes ``models``, the enum the spec offers (§3.4). A call that names a model outside
    them, or an argument its model does not take, fails before anything runs; reference images are read as ``read``
    reads them. The images the endpoint returns are saved through the media sink, and the model reads where they are.
    """

    spec: ToolSpec
    models: ImageModels
    #: The hop the Turn runs on now: where the executor endpoint lives, and the token and headers that authorize the
    #: call. A re-resolve can move the Turn to another Source, and the gateway answers only the hop it now holds.
    route: Callable[[], ModelEndpoint] = field(repr=False)
    client: Optional[httpx.AsyncClient] = field(default=None, repr=False)
    media: Optional[MediaSink] = field(default=None, repr=False)
    #: The user's language, in which the tool words the reasons it writes (the gateway words its own the same way).
    language: Callable[[], str] = field(default=lambda: "en", repr=False)

    async def execute(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolResult:
        model, prompt = arguments.get("model"), arguments.get("prompt")
        hosted = await self._run(dict(arguments), ctx.cwd)
        if isinstance(model, str) and model:
            # The model the call ran on, or was refused for: the call never moves to another (§7.4).
            ran = hosted.action or HostedAction("generate")
            hosted = replace(hosted, action=replace(ran, model=model))
        notes = (_image_facts(hosted),) if hosted.record.status == "completed" else ()
        return _result(self.spec.name, hosted, query=prompt if isinstance(prompt, str) else None, notes=notes)

    def _say(self, key: str, **params: str) -> str:
        return i18n_t(key, self.language(), **params)

    async def _run(self, arguments: dict[str, Any], cwd: str) -> HostedResult:
        refusal = _image_refusal(self.models, arguments, self._say)
        if refusal is not None:
            return _failed(self.spec.name, *refusal)
        try:
            references = await asyncio.to_thread(_reference_images, arguments.get("images") or (), cwd)
        except _ReferenceUnreadable as refused:
            return _failed(self.spec.name, _reference_refusal(refused, self._say), "image_reference_unreadable")
        if references:
            arguments["images"] = references
        hosted, answer = await _gateway_call(self.route(), self.client, self.spec.name, arguments)
        if answer is None or hosted.record.status != "completed":
            return hosted
        return await self._saved(hosted, answer)

    async def _saved(self, hosted: HostedResult, answer: Mapping[str, Any]) -> HostedResult:
        """The endpoint's images, saved through the media sink (§7.3): the record lists the files. A completed call
        that saved no image failed."""

        generation = answer.get("generation")
        generation = generation if isinstance(generation, Mapping) else {}
        width, height = image_dimensions(generation.get("size"))
        images = answer.get("images")
        if not isinstance(images, list) or not images:
            failed = _failed_record(self.spec.name, "the endpoint returned no image", "executor_no_image")
            return replace(hosted, record=failed)
        if self.media is None:
            failed = _failed_record(self.spec.name, "nothing here can keep a generated image", "image_no_store")
            return replace(hosted, record=failed)
        saved: list[HostedImage] = []
        try:
            for item in images:
                encoded = item.get("data") if isinstance(item, Mapping) else None
                if not isinstance(encoded, str):
                    raise ValueError("the endpoint's image is not base64 text")
                saved.append(replace(await self.media.save_image(encoded), width=width, height=height))
        except (ValueError, OSError) as error:
            failed = _failed_record(self.spec.name, f"the image could not be saved: {error}", "image_not_saved")
            return replace(hosted, record=failed)
        # What the endpoint reports it did: its revision of the prompt and the delivered size and quality.
        return replace(hosted, record=replace(hosted.record, images=tuple(saved)), action=generate_action(generation))


_IMAGE_ARGUMENTS = ("model", "prompt", "size", "images", "n")
#: Words a reason the image tool writes, in the user's language: ``say(key, **params)``.
Say = Callable[..., str]


def _image_refusal(models: ImageModels, arguments: dict[str, Any], say: Say) -> Optional[tuple[str, str]]:
    """Why the Turn's frozen image models refuse ``arguments`` before anything runs (§7.2), worded by ``say``, with the
    error code every reason Avibe writes carries, which a surface shows in its reader's language (#2589); None when
    the call may go out. It refuses what the gateway, the execution boundary,
    would, so a bad call costs nothing, and words the reasons the gateway also writes as the gateway does; a refusal
    names the models that do take the call. An empty ``images`` is no reference image, and is left out of the call."""

    if arguments.get("images") == []:
        del arguments["images"]
    unknown = sorted(set(arguments) - set(_IMAGE_ARGUMENTS))
    if unknown:
        return say("hostedTools.image.unknownArguments", names=", ".join(unknown)), "image_argument_invalid"
    chosen = next((model for model in models.models if model.model_id == arguments.get("model")), None)
    if chosen is None:
        available = ", ".join(model.model_id for model in models.models)
        reason = say("hostedTools.image.notOffered", model=str(arguments.get("model")))
        return f"{reason} {say('modelHub.hostedTools.imageAlternatives', models=available)}", "image_model_unavailable"
    prompt = arguments.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return say("hostedTools.image.noPrompt"), "image_argument_invalid"
    if "size" in arguments and size_form(arguments["size"]) is None:
        forms = {form: say(f"hostedTools.image.form.{form}") for form in ("pixels", "ratio")}
        return say("hostedTools.image.sizeForm", **forms), "image_argument_invalid"
    images = arguments.get("images")
    if "images" in arguments and (
        not isinstance(images, list) or not all(isinstance(path, str) and path for path in images) or len(images) > MAX_IMAGES
    ):
        return say("hostedTools.image.badImages", max=str(MAX_IMAGES)), "image_argument_invalid"
    n = arguments.get("n", 1)
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_IMAGES:
        return say("hostedTools.image.badCount", max=str(MAX_IMAGES)), "image_argument_invalid"
    unsupported = _unsupported(chosen, arguments)
    if unsupported is None:
        return None
    if unsupported == "size" and "size" in chosen.params:
        # Two forms exist, so a model that refuses one takes the other.
        (form,) = chosen.size_forms
        reason = say(
            "hostedTools.image.sizeFormUnsupported",
            model=chosen.model_id,
            size=str(arguments["size"]),
            form=say(f"hostedTools.image.form.{form}"),
        )
    else:
        reason = say("modelHub.hostedTools.imageParamUnsupported", model=chosen.model_id, param=unsupported)
    takers = [model.model_id for model in models.models if _unsupported(model, arguments) is None]
    if takers:
        reason += " " + say("modelHub.hostedTools.imageParamTakers", models=", ".join(takers))
    return reason, "image_param_unsupported"


def _unsupported(model: ImageModel, arguments: Mapping[str, Any]) -> Optional[str]:
    """The first optional argument ``model`` does not take, or ``size`` in a form it does not accept."""

    for param in ("images", "n", "size"):
        if param in arguments and param not in model.params:
            return param
    if "size" in arguments and size_form(arguments["size"]) not in model.size_forms:
        return "size"
    return None


class _ReferenceUnreadable(Exception):
    def __init__(self, path: str, error: ImageUnreadable) -> None:
        super().__init__(path)
        self.path, self.error = path, error


def _reference_images(paths: Sequence[str], cwd: str) -> list[str]:
    """The reference images at ``paths``, base64, each read as ``read`` reads it (§7.4). Blocking."""

    encoded = []
    for path in paths:
        try:
            _, data = read_image(path, cwd)
        except ImageUnreadable as error:
            raise _ReferenceUnreadable(path, error) from None
        encoded.append(base64.b64encode(data).decode("ascii"))
    return encoded


def _reference_refusal(refused: _ReferenceUnreadable, say: Say) -> str:
    """Why a reference image cannot be sent, worded by ``say``; the system's own words for a file it could not open."""

    error, path = refused.error, refused.path
    if error.kind == "not_image":
        return say("hostedTools.image.reference.notImage", path=path)
    if error.kind == "too_large" and error.size is not None:
        limit = format_size(MAX_INLINE_IMAGE_BYTES)
        return say("hostedTools.image.reference.tooLarge", path=path, size=format_size(error.size), limit=limit)
    if error.kind == "unsupported":
        return say("hostedTools.image.reference.unsupported", path=path, mime=error.mime_type or "")
    return say("hostedTools.image.reference.unreadable", path=path, reason=error.reason)


def _image_facts(hosted: HostedResult) -> str:
    """The line of a generated image's record that says what made it and what it cost (§7.3)."""

    model = hosted.action.model if hosted.action is not None else None
    execution = hosted.execution
    facts = [f"Model: {model}" if model else "Model: unknown"]
    if execution is not None:
        facts[0] += f" via {execution.executor_origin.provider}"
        cost = execution.cost
        if cost is not None and cost.usd is None:
            facts.append("cost: included in the subscription")
        elif cost is not None:
            facts.append(f"cost: {'' if cost.basis == 'reported' else '≈ '}${_usd(cost.usd)}")
    return " · ".join(facts)


def _usd(value: float) -> str:
    # Small per-call figures keep their first significant digits: $0.006, not $0.01.
    return f"{value:.2f}" if value >= 0.1 else f"{value:.4f}".rstrip("0").rstrip(".")


def _result(tool: str, hosted: HostedResult, *, query: Optional[str], notes: Sequence[str] = ()) -> ToolResult:
    """A hosted call's result: its record, and the text the model reads, rendered as a replayed call's is."""

    text = record_text(tool, hosted.record, query=query, notes=notes)
    failed = hosted.record.status in ("failed", "incomplete")
    return ToolResult(content=(TextBlock(text),), is_error=failed, hosted=hosted)


async def _gateway_call(
    endpoint: ModelEndpoint, client: Optional[httpx.AsyncClient], tool: str, arguments: Mapping[str, Any]
) -> tuple[HostedResult, Optional[Mapping[str, Any]]]:
    """One call on the gateway's executor endpoint (§6, §7.4): its result, and the answer it was read from (None
    when the gateway gave none)."""

    def failed(reason: str, code: str) -> tuple[HostedResult, None]:
        if tool == "web_fetch":
            # A transport/admission failure still has a portable fetch record;
            # it claims no executor, network attempt or cost.
            requested = arguments.get("url")
            record = WebFetchRecord(
                status="failed", requested_url=requested if isinstance(requested, str) and len(requested) <= 8192 else "",
                content="", size_bytes=0, retrieved_at=datetime.now(timezone.utc).isoformat(),
                error=reason, error_code=code,
            )
            return HostedResult(record=record), None
        return _failed(tool, reason, code), None

    url = join_endpoint_url(endpoint.base_url, f"/hosted-tools/{tool}")
    # Reference images make the body megabytes of base64: serialized off the controller loop.
    content = await asyncio.to_thread(json.dumps, {"arguments": dict(arguments)})
    headers = {**auth_headers(endpoint, provider="openai", gateway=True), "content-type": "application/json"}
    # The gateway bounds the call; only connecting is bounded here.
    timeout = httpx.Timeout(None, connect=CONNECT_TIMEOUT_S)
    try:
        if client is not None:
            response = await client.post(url, content=content, headers=headers, timeout=timeout)
        else:
            # The Model Hub gateway is loopback: an environment or system proxy must never carry it.
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as own:
                response = await own.post(url, content=content, headers=headers)
    except httpx.HTTPError as error:
        return failed(f"the gateway could not run the call ({type(error).__name__})", "gateway_unreachable")
    if response.status_code != 200:
        reason = f"the gateway refused the call (HTTP {response.status_code} {_error_code(response)})"
        return failed(reason, "gateway_refused")
    try:
        # An image's answer carries megabytes of base64: parsed off the controller loop.
        answer = await asyncio.to_thread(response.json)
        return _hosted_result(tool, answer), answer
    except (TypeError, ValueError):
        return failed("the gateway answered with no hosted result", "gateway_no_result")


def _failed_record(tool: str, reason: str, code: Optional[str]) -> HostedRecord:
    error, error_code = failure(reason, code)
    return HostedRecord(
        status="failed", error=error, error_code=error_code, images=() if tool == "image_generate" else None
    )


def _failed(tool: str, reason: str, code: Optional[str]) -> HostedResult:
    """A call that never reached an executor: a failed record, and no execution (§4.2)."""

    return HostedResult(record=_failed_record(tool, reason, code))


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
    if tool == "image_generate":
        # The executor answers an image's bytes beside the record (``images``); the record lists what is saved.
        hosted["record"] = {**hosted["record"], "images": []}
    content = []
    if tool == "web_fetch":
        from core.agent_core.web_fetch import fetch_record_from_dict, frame_fetch_record

        # The gateway owns normalization/cutting. Build the ordinary content
        # from that exact bounded record before C-1 validates both together.
        content = [{"type": "text", "text": frame_fetch_record(fetch_record_from_dict(hosted["record"]))}]
    message = message_from_dict(
        {
            "role": "tool_result",
            "tool_call_id": "proxied",
            "tool_name": tool,
            "content": content,
            "is_error": body["record"].get("status") in ("failed", "incomplete"),
            "hosted": hosted,
        }
    )
    if not isinstance(message, ToolResultMessage) or message.hosted is None:
        raise ValueError("no hosted result")
    return message.hosted


def hosted_tools(
    router: HubModelRouter,
    client: Optional[httpx.AsyncClient] = None,
    media: Optional[MediaSink] = None,
    *,
    language: Callable[[], str] = lambda: "en",
) -> tuple[Tool, ...]:
    """The Turn's hosted tools from its first resolution (§3.4): every search tool the user enabled, in Model Hub's
    order, whatever a hop runs it as, then ``image_generate`` when an image model is available. They lead the Turn's
    tools, so its last tool stays a local one whatever a hop renders them as, and the list and the image enum stay the
    same for the whole Turn. Each call goes out on the hop the Turn runs on then (``HubModelRouter.current``)."""

    if router.preflight is None:
        return ()
    frozen = router.preflight.endpoint

    def route() -> ModelEndpoint:
        return router.current.endpoint

    tools: list[Tool] = [
        HostedTool(hosted_tool_spec(name), route, client)
        for name in frozen.hosted_tools if name != "web_fetch" or v2_config.WEB_FETCH_ACTIVE
    ]
    if frozen.image_models is not None:
        spec = image_generate_spec(frozen.image_models)
        tools.append(ImageGenerateTool(spec, frozen.image_models, route, client, media, language))
    return tuple(tools)
