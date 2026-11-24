"""Vendor-neutral scripted stdio fake ACP peer for hermetic tests.

Run directly as a subprocess via :func:`fake_acp_server_command` so unit and
integration tests can exercise the real NDJSON stdio transport without
external binaries or credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

FAKE_ACP_SERVER_PATH = Path(__file__).resolve()

DEFAULT_AGENT_INFO: dict[str, Any] = {
    "name": "fake-acp-server",
    "title": "Fake ACP Server",
    "version": "1.0.0",
}

DEFAULT_AGENT_CAPABILITIES: dict[str, Any] = {
    "loadSession": True,
    "promptCapabilities": {
        "image": True,
        "embeddedContext": True,
    },
    "sessionCapabilities": {
        "resume": {},
        "list": {},
        "fork": {},
        "close": {},
        "additionalDirectories": {},
    },
    "auth": {
        "logout": {},
    },
}

DEFAULT_AUTH_METHODS: list[dict[str, Any]] = [
    {
        "id": "token",
        "name": "Token Auth",
        "description": "Authenticate with a test token",
    }
]

DEFAULT_CONFIG_OPTIONS: list[dict[str, Any]] = [
    {
        "id": "model",
        "name": "Model",
        "category": "model",
        "type": "select",
        "currentValue": "model-alpha",
        "options": [
            {"value": "model-alpha", "name": "Model Alpha"},
            {"value": "model-beta", "name": "Model Beta"},
        ],
    },
    {
        "id": "thought_level",
        "name": "Thought Level",
        "category": "thought_level",
        "type": "select",
        "currentValue": "medium",
        "options": [
            {"value": "low", "name": "Low"},
            {"value": "medium", "name": "Medium"},
            {"value": "high", "name": "High"},
        ],
    },
]

DEFAULT_MODES: list[str] = ["code", "plan"]
DEFAULT_MODELS: list[str] = ["model-alpha", "model-beta"]
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2dIAAAAASUVORK5CYII="
)


def fake_acp_server_command(
    *,
    state_file: Path | None = None,
    config: Mapping[str, Any] | None = None,
    require_auth_method: str | None = None,
) -> list[str]:
    """Build an argv list that spawns this scripted ACP server over stdio."""
    cmd = [sys.executable, "-u", str(FAKE_ACP_SERVER_PATH)]
    if state_file is not None:
        cmd.extend(["--state-file", str(state_file)])
    if config is not None:
        cmd.extend(["--config-json", json.dumps(dict(config), separators=(",", ":"))])
    if require_auth_method is not None:
        cmd.extend(["--require-auth-method", require_auth_method])
    return cmd


def _deep_copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _prompt_text(prompt_blocks: Any) -> str:
    if not isinstance(prompt_blocks, list):
        return ""
    parts: list[str] = []
    for block in prompt_blocks:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


class FakeAcpServer:
    """Async JSON-RPC 2.0 ACP server speaking NDJSON over stdin/stdout."""

    def __init__(
        self,
        *,
        state_file: Path | None,
        config: dict[str, Any],
        require_auth_method: str | None = None,
    ) -> None:
        self._state_file = state_file
        self._config = config
        self._agent_info = _deep_copy_json(config.get("agentInfo", DEFAULT_AGENT_INFO))
        self._agent_capabilities = _deep_copy_json(
            config.get("agentCapabilities", DEFAULT_AGENT_CAPABILITIES)
        )
        raw_required_method = (
            require_auth_method
            if require_auth_method is not None
            else config.get("requireAuthMethod")
        )
        self._require_auth_method = (
            str(raw_required_method).strip()
            if isinstance(raw_required_method, str) and raw_required_method.strip()
            else None
        )
        if "authMethods" in config:
            self._auth_methods = _deep_copy_json(config["authMethods"])
        elif self._require_auth_method is not None:
            self._auth_methods = [
                {
                    "id": self._require_auth_method,
                    "name": self._require_auth_method,
                    "description": f"Authenticate with {self._require_auth_method}",
                }
            ]
        else:
            self._auth_methods = _deep_copy_json(DEFAULT_AUTH_METHODS)
        if self._require_auth_method is not None and not any(
            isinstance(m, dict) and m.get("id") == self._require_auth_method
            for m in self._auth_methods
        ):
            self._auth_methods.append(
                {
                    "id": self._require_auth_method,
                    "name": self._require_auth_method,
                    "description": f"Authenticate with {self._require_auth_method}",
                }
            )
        self._require_auth = bool(config.get("requireAuth", False)) or self._require_auth_method is not None
        self._method_authenticated = self._require_auth_method is None
        self._page_size = config.get("listPageSize")
        self._server_req_id = 0
        self._pending_server_requests: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._active_cancels: dict[str, asyncio.Event] = {}
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if self._state_file is not None and self._state_file.exists():
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        return {
            "authenticated": not self._require_auth,
            "next_session_num": 1,
            "sessions": {},
        }

    def _save_state(self) -> None:
        if self._state_file is None:
            return
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(self._state, indent=2), encoding="utf-8")

    def _write_json(self, msg: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def _send_result(self, req_id: int | str, result: dict[str, Any]) -> None:
        self._write_json({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _send_error(self, req_id: int | str, code: int, message: str, data: Any = None) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._write_json({"jsonrpc": "2.0", "id": req_id, "error": error})

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        self._write_json({"jsonrpc": "2.0", "method": method, "params": params})

    def _send_update(self, session_id: str, update: dict[str, Any]) -> None:
        self._send_notification("session/update", {"sessionId": session_id, "update": update})

    async def _send_server_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._server_req_id += 1
        req_id = f"srv-{self._server_req_id}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_server_requests[req_id] = fut
        self._write_json({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return await fut

    def _require_authenticated(self, req_id: int | str) -> bool:
        if self._require_auth_method is not None and not self._method_authenticated:
            self._send_error(
                req_id,
                -32000,
                "Authentication required",
                {"authMethods": self._auth_methods},
            )
            return False
        if self._state.get("authenticated", True):
            return True
        self._send_error(
            req_id,
            -32000,
            "Authentication required",
            {"authMethods": self._auth_methods},
        )
        return False

    def _get_session(self, req_id: int | str, session_id: Any) -> dict[str, Any] | None:
        if not isinstance(session_id, str) or session_id not in self._state["sessions"]:
            self._send_error(req_id, -32002, f"Session not found: {session_id}")
            return None
        return self._state["sessions"][session_id]

    async def run(self) -> None:
        while True:
            raw = await asyncio.to_thread(sys.stdin.readline)
            if not raw:
                break
            line = raw.strip()
            if not line:
                continue
            msg = json.loads(line)
            if not isinstance(msg, dict):
                continue
            self._dispatch(msg)

        for task in list(self._active_tasks):
            task.cancel()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        # Response from client to a server-initiated request
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending_server_requests.pop(msg["id"], None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return

        method = msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

        # Notification from client
        if "id" not in msg:
            if method == "session/cancel":
                session_id = params.get("sessionId")
                if isinstance(session_id, str) and session_id in self._active_cancels:
                    self._active_cancels[session_id].set()
            return

        req_id = msg["id"]
        if method == "session/prompt":
            task = asyncio.create_task(self._handle_prompt(req_id, params))
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)
            return

        self._handle_sync_request(req_id, str(method), params)

    def _handle_sync_request(self, req_id: int | str, method: str, params: dict[str, Any]) -> None:
        if method == "initialize":
            self._send_result(
                req_id,
                {
                    "protocolVersion": params.get("protocolVersion", 1),
                    "agentInfo": self._agent_info,
                    "agentCapabilities": self._agent_capabilities,
                    "authMethods": self._auth_methods,
                },
            )
            return

        if method == "authenticate":
            method_id = params.get("methodId")
            valid_ids = {m["id"] for m in self._auth_methods if isinstance(m, dict) and "id" in m}
            if method_id not in valid_ids:
                self._send_error(req_id, -32602, f"Unknown auth method: {method_id}")
                return
            if self._require_auth_method is not None and method_id != self._require_auth_method:
                self._send_error(
                    req_id,
                    -32000,
                    f"Authentication required via {self._require_auth_method}",
                    {"authMethods": self._auth_methods},
                )
                return
            self._method_authenticated = True
            self._state["authenticated"] = True
            self._save_state()
            self._send_result(req_id, {})
            return

        if method == "logout":
            auth_caps = self._agent_capabilities.get("auth")
            if not isinstance(auth_caps, dict) or "logout" not in auth_caps:
                self._send_error(req_id, -32601, "Method not found: logout")
                return
            self._method_authenticated = False
            self._state["authenticated"] = False
            self._save_state()
            self._send_result(req_id, {})
            return

        if not self._require_authenticated(req_id):
            return

        if method == "session/new":
            session_num = self._state["next_session_num"]
            self._state["next_session_num"] = session_num + 1
            session_id = f"sess-{session_num}"
            config_options = _deep_copy_json(
                self._config.get("configOptions", DEFAULT_CONFIG_OPTIONS)
            )
            session = {
                "sessionId": session_id,
                "cwd": params.get("cwd", ""),
                "mcpServers": params.get("mcpServers", []),
                "additionalDirectories": params.get("additionalDirectories", []),
                "configOptions": config_options,
                "mode": DEFAULT_MODES[0],
                "model": DEFAULT_MODELS[0],
                "title": None,
                "history": [],
            }
            self._state["sessions"][session_id] = session
            self._save_state()
            self._send_result(
                req_id,
                {
                    "sessionId": session_id,
                    "configOptions": _deep_copy_json(config_options),
                    "modes": {
                        "currentModeId": session["mode"],
                        "availableModes": [{"id": m, "name": m.title()} for m in DEFAULT_MODES],
                    },
                    "models": {
                        "currentModelId": session["model"],
                        "availableModels": [{"modelId": m, "name": m} for m in DEFAULT_MODELS],
                    },
                },
            )
            return

        if method == "session/resume":
            session = self._get_session(req_id, params.get("sessionId"))
            if session is None:
                return
            if "cwd" in params:
                session["cwd"] = params["cwd"]
            if "additionalDirectories" in params:
                session["additionalDirectories"] = params["additionalDirectories"]
            self._save_state()
            self._send_result(
                req_id,
                {
                    "sessionId": session["sessionId"],
                    "configOptions": _deep_copy_json(session["configOptions"]),
                },
            )
            return

        if method == "session/load":
            session = self._get_session(req_id, params.get("sessionId"))
            if session is None:
                return
            for turn in session["history"]:
                self._send_update(
                    session["sessionId"],
                    {
                        "sessionUpdate": "user_message_chunk",
                        "content": {"type": "text", "text": turn["prompt"]},
                    },
                )
                self._send_update(
                    session["sessionId"],
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": turn["reply"]},
                    },
                )
            self._send_result(
                req_id,
                {"configOptions": _deep_copy_json(session["configOptions"])},
            )
            return

        if method == "session/list":
            cwd_filter = params.get("cwd")
            items = [
                {
                    "sessionId": s["sessionId"],
                    "cwd": s["cwd"],
                    "title": s["title"],
                }
                for s in self._state["sessions"].values()
                if cwd_filter is None or s["cwd"] == cwd_filter
            ]
            start = int(params["cursor"]) if isinstance(params.get("cursor"), str) else 0
            next_cursor = None
            if isinstance(self._page_size, int) and self._page_size > 0:
                end = start + self._page_size
                if end < len(items):
                    next_cursor = str(end)
                items = items[start:end]
            result: dict[str, Any] = {"sessions": items}
            if next_cursor is not None:
                result["nextCursor"] = next_cursor
            self._send_result(req_id, result)
            return

        if method == "session/close":
            session_id = params.get("sessionId")
            session = self._get_session(req_id, session_id)
            if session is None:
                return
            del self._state["sessions"][session["sessionId"]]
            self._save_state()
            self._send_result(req_id, {})
            return

        if method == "session/set_config_option":
            session = self._get_session(req_id, params.get("sessionId"))
            if session is None:
                return
            config_id = params.get("configId")
            value = params.get("value")
            target = next(
                (opt for opt in session["configOptions"] if opt["id"] == config_id),
                None,
            )
            if target is None:
                self._send_error(req_id, -32602, f"Unknown configId: {config_id}")
                return
            allowed = {opt["value"] for opt in target.get("options", [])}
            if value not in allowed:
                self._send_error(req_id, -32602, f"Invalid value {value!r} for {config_id}")
                return
            target["currentValue"] = value
            if config_id == "model":
                session["model"] = value
            self._save_state()
            self._send_update(
                session["sessionId"],
                {
                    "sessionUpdate": "config_option_update",
                    "configOptions": _deep_copy_json(session["configOptions"]),
                },
            )
            self._send_result(
                req_id,
                {"configOptions": _deep_copy_json(session["configOptions"])},
            )
            return

        if method == "session/set_model":
            session = self._get_session(req_id, params.get("sessionId"))
            if session is None:
                return
            model_id = params.get("modelId")
            if model_id not in DEFAULT_MODELS:
                self._send_error(req_id, -32602, f"Unsupported model: {model_id}")
                return
            session["model"] = model_id
            for opt in session["configOptions"]:
                if opt["id"] == "model":
                    opt["currentValue"] = model_id
            self._save_state()
            self._send_result(req_id, {})
            return

        if method == "session/set_mode":
            session = self._get_session(req_id, params.get("sessionId"))
            if session is None:
                return
            mode_id = params.get("modeId")
            if mode_id not in DEFAULT_MODES:
                self._send_error(req_id, -32602, f"Unsupported mode: {mode_id}")
                return
            session["mode"] = mode_id
            self._save_state()
            self._send_result(req_id, {})
            return

        self._send_error(req_id, -32601, f"Method not found: {method}")

    async def _handle_prompt(self, req_id: int | str, params: dict[str, Any]) -> None:
        if not self._require_authenticated(req_id):
            return
        session = self._get_session(req_id, params.get("sessionId"))
        if session is None:
            return
        session_id = session["sessionId"]
        text = _prompt_text(params.get("prompt"))

        cancel_event = asyncio.Event()
        self._active_cancels[session_id] = cancel_event
        try:
            if text == "crash_mid_prompt":
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "starting before crash"},
                    },
                )
                sys.stderr.write("fatal: simulated peer crash\n")
                sys.stderr.flush()
                os._exit(2)

            if text == "wait_cancel":
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "working"},
                    },
                )
                await cancel_event.wait()
                self._send_result(req_id, {"stopReason": "cancelled"})
                return

            if text == "request_permission":
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-perm-1",
                        "title": "Edit config file",
                        "kind": "edit",
                        "status": "pending",
                        "rawInput": {"path": "app.py"},
                    },
                )
                client_resp = await self._send_server_request(
                    "session/request_permission",
                    {
                        "sessionId": session_id,
                        "toolCall": {"toolCallId": "tc-perm-1", "title": "Edit config file"},
                        "options": [
                            {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                            {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                        ],
                    },
                )
                outcome = client_resp.get("result", {}).get("outcome", {})
                reply = f"permission:{outcome.get('optionId', outcome.get('outcome', 'error'))}"
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": reply},
                    },
                )
                session["history"].append({"prompt": text, "reply": reply})
                self._save_state()
                self._send_result(req_id, {"stopReason": "end_turn", "permissionResponse": client_resp})
                return

            if text == "request_unknown":
                client_resp = await self._send_server_request(
                    "vendor/unhandled_op",
                    {"sessionId": session_id, "detail": "probe"},
                )
                error_code = client_resp.get("error", {}).get("code")
                reply = f"unknown_req_error:{error_code}"
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": reply},
                    },
                )
                self._send_result(req_id, {"stopReason": "end_turn", "clientResponse": client_resp})
                return

            if text == "stream_all":
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "agent_thought_chunk",
                        "content": {"type": "text", "text": "planning edit"},
                    },
                )
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-1",
                        "title": "Update greeting",
                        "kind": "edit",
                        "status": "in_progress",
                        "rawInput": {"path": "hello.py"},
                        "locations": [{"path": "hello.py", "line": 1}],
                    },
                )
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc-1",
                        "status": "completed",
                        "rawOutput": {"ok": True},
                        "content": [
                            {
                                "type": "diff",
                                "path": "hello.py",
                                "oldText": "print('old')\n",
                                "newText": "print('new')\n",
                            }
                        ],
                    },
                )
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "Updated "},
                    },
                )
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello.py."},
                    },
                )
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "usage_update",
                        "used": 1200,
                        "size": 128000,
                        "cost": {"amount": 0.01, "currency": "USD"},
                    },
                )
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "available_commands_update",
                        "availableCommands": [
                            {
                                "name": "review",
                                "description": "Review workspace changes",
                                "input": {"hint": "optional path"},
                            }
                        ],
                    },
                )
                session["title"] = "Update greeting"
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "session_info_update",
                        "title": "Update greeting",
                        "updatedAt": "2026-11-24T12:00:00Z",
                    },
                )
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "plan",
                        "entries": [{"content": "edit file", "status": "completed"}],
                    },
                )
                session["history"].append({"prompt": text, "reply": "Updated hello.py."})
                self._save_state()
                self._send_result(req_id, {"stopReason": "end_turn"})
                return

            if text.startswith("stop_reason:"):
                reason = text.split(":", 1)[1]
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": f"partial:{reason}"},
                    },
                )
                self._send_result(req_id, {"stopReason": reason})
                return

            if text == "stop_reason_missing":
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "partial:missing"},
                    },
                )
                self._send_result(req_id, {})
                return

            if text == "emit_image_only":
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {
                            "type": "image",
                            "data": _TINY_PNG_B64,
                            "mimeType": "image/png",
                        },
                    },
                )
                self._send_result(req_id, {"stopReason": "end_turn"})
                return

            if text == "emit_image_uri":
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {
                            "type": "image",
                            "uri": "https://example.test/chart.png",
                            "mimeType": "image/png",
                        },
                    },
                )
                self._send_result(req_id, {"stopReason": "end_turn"})
                return

            if text == "recall":
                prior = [turn["prompt"] for turn in session["history"]]
                reply = (
                    f"prior={prior};model={session['model']};"
                    f"mode={session['mode']};extra_dirs={session['additionalDirectories']}"
                )
                self._send_update(
                    session_id,
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": reply},
                    },
                )
                session["history"].append({"prompt": text, "reply": reply})
                self._save_state()
                self._send_result(req_id, {"stopReason": "end_turn"})
                return

            reply = f"echo:{text}|model={session['model']}|mode={session['mode']}"
            self._send_update(
                session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": reply},
                },
            )
            session["history"].append({"prompt": text, "reply": reply})
            self._save_state()
            self._send_result(req_id, {"stopReason": "end_turn"})
        finally:
            self._active_cancels.pop(session_id, None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scripted stdio fake ACP server")
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--config-json", type=str, default="{}")
    parser.add_argument("--require-auth-method", type=str, default=None)
    args = parser.parse_args()
    config = json.loads(args.config_json)
    server = FakeAcpServer(
        state_file=args.state_file,
        config=config,
        require_auth_method=args.require_auth_method,
    )
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
