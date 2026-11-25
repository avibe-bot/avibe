"""Vendor-neutral scripted stdio fake ACP peer for hermetic tests."""

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
DEFAULT_AGENT_INFO: dict[str, Any] = {"name": "fake-acp-server", "title": "Fake ACP Server", "version": "1.0.0"}
DEFAULT_AGENT_CAPABILITIES: dict[str, Any] = {
    "loadSession": True,
    "promptCapabilities": {"image": True, "embeddedContext": True},
    "sessionCapabilities": {"resume": {}, "list": {}, "fork": {}, "close": {}, "additionalDirectories": {}},
    "auth": {"logout": {}},
}
DEFAULT_AUTH_METHODS: list[dict[str, Any]] = [
    {"id": "token", "name": "Token Auth", "description": "Authenticate with a test token"}
]
DEFAULT_CONFIG_OPTIONS: list[dict[str, Any]] = [
    {
        "id": "model",
        "name": "Model",
        "category": "model",
        "type": "select",
        "currentValue": "model-alpha",
        "options": [{"value": "model-alpha", "name": "Model Alpha"}, {"value": "model-beta", "name": "Model Beta"}],
    },
    {
        "id": "thought_level",
        "name": "Thought Level",
        "category": "thought_level",
        "type": "select",
        "currentValue": "medium",
        "options": [{"value": "low", "name": "Low"}, {"value": "medium", "name": "Medium"}, {"value": "high", "name": "High"}],
    },
]
DEFAULT_MODES: list[str] = ["code", "plan"]
DEFAULT_MODELS: list[str] = ["model-alpha", "model-beta"]
_TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+X2dIAAAAASUVORK5CYII="


def fake_acp_server_command(
    *, state_file: Path | None = None, config: Mapping[str, Any] | None = None, require_auth_method: str | None = None
) -> list[str]:
    cmd = [sys.executable, "-u", str(FAKE_ACP_SERVER_PATH)]
    if state_file is not None:
        cmd.extend(["--state-file", str(state_file)])
    if config is not None:
        cmd.extend(["--config-json", json.dumps(dict(config), separators=(",", ":"))])
    if require_auth_method is not None:
        cmd.extend(["--require-auth-method", require_auth_method])
    return cmd


def _copy(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _prompt_text(blocks: Any) -> str:
    if not isinstance(blocks, list):
        return ""
    return "\n".join(b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))


class FakeAcpServer:
    """Async JSON-RPC 2.0 ACP server speaking NDJSON over stdin/stdout."""

    def __init__(self, *, state_file: Path | None, config: dict[str, Any], require_auth_method: str | None = None) -> None:
        self._state_file = state_file
        self._config = config
        self._agent_info = _copy(config.get("agentInfo", DEFAULT_AGENT_INFO))
        self._agent_capabilities = _copy(config.get("agentCapabilities", DEFAULT_AGENT_CAPABILITIES))
        raw_req = require_auth_method if require_auth_method is not None else config.get("requireAuthMethod")
        self._require_auth_method = str(raw_req).strip() if isinstance(raw_req, str) and raw_req.strip() else None

        if "authMethods" in config:
            self._auth_methods = _copy(config["authMethods"])
        elif self._require_auth_method:
            self._auth_methods = [{"id": self._require_auth_method, "name": self._require_auth_method, "description": f"Authenticate with {self._require_auth_method}"}]
        else:
            self._auth_methods = _copy(DEFAULT_AUTH_METHODS)
        if self._require_auth_method and not any(isinstance(m, dict) and m.get("id") == self._require_auth_method for m in self._auth_methods):
            self._auth_methods.append({"id": self._require_auth_method, "name": self._require_auth_method, "description": f"Authenticate with {self._require_auth_method}"})

        self._require_auth = bool(config.get("requireAuth", False)) or self._require_auth_method is not None
        self._method_authenticated = self._require_auth_method is None
        self._page_size = config.get("listPageSize")
        self._server_req_id = 0
        self._pending_server_requests: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._active_cancels: dict[str, asyncio.Event] = {}
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._steer_chains: dict[str, list[int | str]] = {}
        self._steer_resolved: set[int | str] = set()
        self._state = (
            json.loads(state_file.read_text(encoding="utf-8"))
            if state_file is not None and state_file.exists()
            else {"authenticated": not self._require_auth, "next_session_num": 1, "sessions": {}}
        )

    def _save_state(self) -> None:
        if self._state_file is not None:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(self._state, indent=2), encoding="utf-8")

    def _write_json(self, msg: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def _send_result(self, req_id: int | str, result: dict[str, Any]) -> None:
        self._write_json({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _send_error(self, req_id: int | str, code: int, message: str, data: Any = None) -> None:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._write_json({"jsonrpc": "2.0", "id": req_id, "error": err})

    def _send_update(self, sid: str, update: dict[str, Any]) -> None:
        self._write_json({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": update}})

    def _send_text(self, sid: str, text: str, kind: str = "agent_message_chunk") -> None:
        self._send_update(sid, {"sessionUpdate": kind, "content": {"type": "text", "text": text}})

    async def _send_server_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._server_req_id += 1
        req_id = f"srv-{self._server_req_id}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_server_requests[req_id] = fut
        self._write_json({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return await fut

    def _require_authenticated(self, req_id: int | str) -> bool:
        if (self._require_auth_method is not None and not self._method_authenticated) or not self._state.get("authenticated", True):
            self._send_error(req_id, -32000, "Authentication required", {"authMethods": self._auth_methods})
            return False
        return True

    def _get_session(self, req_id: int | str, session_id: Any) -> dict[str, Any] | None:
        if not isinstance(session_id, str) or session_id not in self._state["sessions"]:
            self._send_error(req_id, -32002, f"Session not found: {session_id}")
            return None
        return self._state["sessions"][session_id]

    @staticmethod
    def _session_payload(session: dict[str, Any]) -> dict[str, Any]:
        return {
            "sessionId": session["sessionId"],
            "configOptions": _copy(session["configOptions"]),
            "modes": {"currentModeId": session["mode"], "availableModes": [{"id": m, "name": m.title()} for m in DEFAULT_MODES]},
            "models": {"currentModelId": session["model"], "availableModels": [{"modelId": m, "name": m} for m in DEFAULT_MODELS]},
        }

    async def run(self) -> None:
        while raw := await asyncio.to_thread(sys.stdin.readline):
            if line := raw.strip():
                msg = json.loads(line)
                if isinstance(msg, dict):
                    self._dispatch(msg)
        for task in list(self._active_tasks):
            task.cancel()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending_server_requests.pop(msg["id"], None)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        method = msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if "id" not in msg:
            if method == "session/cancel" and (chain := self._steer_chains.get(str(params.get("sessionId")))):
                self._steer_resolved.add(chain[-1])
                self._send_result(chain[-1], {"stopReason": "cancelled"})
            if method == "session/cancel" and (ev := self._active_cancels.get(str(params.get("sessionId") or ""))):
                ev.set()
            return
        if method == "session/prompt":
            task = asyncio.create_task(self._handle_prompt(msg["id"], params))
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)
            return
        self._handle_sync_request(msg["id"], str(method), params)

    def _handle_sync_request(self, req_id: int | str, method: str, params: dict[str, Any]) -> None:
        if method == "test/steer":
            sid, action = str(params["sessionId"]), params["action"]
            chain = self._steer_chains[sid]
            if action in {"receipt", "late_error"}:
                for previous in chain[:-1]:
                    if previous not in self._steer_resolved:
                        self._steer_resolved.add(previous)
                        if action == "late_error":
                            self._send_error(previous, -32603, "late superseded failure")
                        else:
                            self._send_result(previous, {"stopReason": "end_turn"})
            elif action == "finish":
                self._send_update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "chain-tool", "status": "completed"})
                self._send_update(sid, {"sessionUpdate": "usage_update", "used": 150, "size": 128000})
                self._send_text(sid, str(params.get("text", "FIRST BANANA")))
                self._steer_resolved.add(chain[-1])
                self._send_result(chain[-1], {"stopReason": "end_turn"})
            return self._send_result(req_id, {})
        if method == "initialize":
            return self._send_result(
                req_id,
                {"protocolVersion": params.get("protocolVersion", 1), "agentInfo": self._agent_info, "agentCapabilities": self._agent_capabilities, "authMethods": self._auth_methods},
            )
        if method == "authenticate":
            method_id = params.get("methodId")
            if method_id not in {m["id"] for m in self._auth_methods if isinstance(m, dict) and "id" in m}:
                return self._send_error(req_id, -32602, f"Unknown auth method: {method_id}")
            if self._require_auth_method is not None and method_id != self._require_auth_method:
                return self._send_error(req_id, -32000, f"Authentication required via {self._require_auth_method}", {"authMethods": self._auth_methods})
            self._method_authenticated = True
            self._state["authenticated"] = True
            self._save_state()
            return self._send_result(req_id, {})
        if method == "logout":
            auth_caps = self._agent_capabilities.get("auth")
            if not isinstance(auth_caps, dict) or "logout" not in auth_caps:
                return self._send_error(req_id, -32601, "Method not found: logout")
            self._method_authenticated = False
            self._state["authenticated"] = False
            self._save_state()
            return self._send_result(req_id, {})
        if not self._require_authenticated(req_id):
            return

        if method == "session/new":
            num = self._state["next_session_num"]
            self._state["next_session_num"] = num + 1
            session = {
                "sessionId": f"sess-{num}",
                "cwd": params.get("cwd", ""),
                "mcpServers": params.get("mcpServers", []),
                "additionalDirectories": params.get("additionalDirectories", []),
                "configOptions": _copy(self._config.get("configOptions", DEFAULT_CONFIG_OPTIONS)),
                "mode": DEFAULT_MODES[0],
                "model": DEFAULT_MODELS[0],
                "title": None,
                "history": [],
            }
            self._state["sessions"][session["sessionId"]] = session
            self._save_state()
            return self._send_result(req_id, self._session_payload(session))

        if method == "session/list":
            cwd_filter = params.get("cwd")
            items = [{"sessionId": s["sessionId"], "cwd": s["cwd"], "title": s["title"]} for s in self._state["sessions"].values() if cwd_filter is None or s["cwd"] == cwd_filter]
            start = int(params["cursor"]) if isinstance(params.get("cursor"), str) else 0
            result: dict[str, Any] = {"sessions": items}
            if isinstance(self._page_size, int) and self._page_size > 0:
                end = start + self._page_size
                result["sessions"] = items[start:end]
                if end < len(items):
                    result["nextCursor"] = str(end)
            return self._send_result(req_id, result)

        if method == "session/fork":
            caps = self._agent_capabilities.get("sessionCapabilities")
            if not isinstance(caps, dict) or "fork" not in caps:
                return self._send_error(req_id, -32601, "Method not found: session/fork")
            if (source := self._get_session(req_id, params.get("sessionId"))) is None:
                return
            num = self._state["next_session_num"]
            self._state["next_session_num"] = num + 1
            forked = {
                **source,
                "sessionId": f"sess-{num}",
                "cwd": params.get("cwd", source["cwd"]),
                "mcpServers": params.get("mcpServers", []),
                "additionalDirectories": params.get("additionalDirectories", _copy(source.get("additionalDirectories", []))),
                "configOptions": _copy(source["configOptions"]),
                "history": _copy(source["history"]),
            }
            self._state["sessions"][forked["sessionId"]] = forked
            self._save_state()
            return self._send_result(req_id, self._session_payload(forked))

        if method in {"session/resume", "session/load", "session/close", "session/set_config_option", "session/set_model", "session/set_mode"}:
            if (session := self._get_session(req_id, params.get("sessionId"))) is None:
                return
            sid = session["sessionId"]
            if method == "session/resume":
                for key in ("cwd", "additionalDirectories"):
                    if key in params:
                        session[key] = params[key]
                self._save_state()
                return self._send_result(req_id, {"sessionId": sid, "configOptions": _copy(session["configOptions"])})
            if method == "session/load":
                for turn in session["history"]:
                    self._send_text(sid, turn["prompt"], "user_message_chunk")
                    self._send_text(sid, turn["reply"], "agent_message_chunk")
                return self._send_result(req_id, {"configOptions": _copy(session["configOptions"])})
            if method == "session/close":
                del self._state["sessions"][sid]
                self._save_state()
                return self._send_result(req_id, {})
            if method == "session/set_config_option":
                cid, val = params.get("configId"), params.get("value")
                target = next((o for o in session["configOptions"] if o["id"] == cid), None)
                if target is None:
                    return self._send_error(req_id, -32602, f"Unknown configId: {cid}")
                if val not in {o["value"] for o in target.get("options", [])}:
                    return self._send_error(req_id, -32602, f"Invalid value {val!r} for {cid}")
                target["currentValue"] = val
                if cid == "model":
                    session["model"] = val
                self._save_state()
                opts = _copy(session["configOptions"])
                self._send_update(sid, {"sessionUpdate": "config_option_update", "configOptions": opts})
                return self._send_result(req_id, {"configOptions": opts})
            if method == "session/set_model":
                model_id = params.get("modelId")
                if model_id not in DEFAULT_MODELS:
                    return self._send_error(req_id, -32602, f"Unsupported model: {model_id}")
                session["model"] = model_id
                for opt in session["configOptions"]:
                    if opt["id"] == "model":
                        opt["currentValue"] = model_id
                self._save_state()
                return self._send_result(req_id, {})
            mode_id = params.get("modeId")
            if mode_id not in DEFAULT_MODES:
                return self._send_error(req_id, -32602, f"Unsupported mode: {mode_id}")
            session["mode"] = mode_id
            self._save_state()
            return self._send_result(req_id, {})

        self._send_error(req_id, -32601, f"Method not found: {method}")

    def _finish_reply(self, req_id: int | str, session: dict[str, Any], prompt: str, reply: str, extra: dict[str, Any] | None = None) -> None:
        self._send_text(session["sessionId"], reply)
        session["history"].append({"prompt": prompt, "reply": reply})
        self._save_state()
        self._send_result(req_id, {"stopReason": "end_turn", **(extra or {})})

    async def _handle_prompt(self, req_id: int | str, params: dict[str, Any]) -> None:
        if not self._require_authenticated(req_id) or (session := self._get_session(req_id, params.get("sessionId"))) is None:
            return
        sid, text = session["sessionId"], _prompt_text(params.get("prompt"))
        cancel_event = asyncio.Event()
        self._active_cancels[sid] = cancel_event
        try:
            if text == "STEER_CHAIN" or sid in self._steer_chains:
                chain = self._steer_chains.setdefault(sid, [])
                previous = chain[-1] if chain else None
                chain.append(req_id)
                if previous is not None and not self._config.get("deferSteerReceipt"):
                    self._steer_resolved.add(previous)
                    self._send_result(previous, {"stopReason": "end_turn"})
                if previous is None:
                    self._send_update(sid, {"sessionUpdate": "tool_call", "toolCallId": "chain-tool", "title": "sleep 20", "status": "in_progress"})
                    self._send_update(sid, {"sessionUpdate": "usage_update", "used": 100, "size": 128000})
                return
            if text == "crash_mid_prompt":
                self._send_text(sid, "starting before crash")
                sys.stderr.write("fatal: simulated peer crash\n")
                sys.stderr.flush()
                os._exit(2)
            if text == "wait_cancel":
                self._send_text(sid, "working")
                await cancel_event.wait()
                return self._send_result(req_id, {"stopReason": "cancelled"})
            if text == "request_permission":
                self._send_update(sid, {"sessionUpdate": "tool_call", "toolCallId": "tc-perm-1", "title": "Edit config file", "kind": "edit", "status": "pending", "rawInput": {"path": "app.py"}})
                resp = await self._send_server_request(
                    "session/request_permission",
                    {
                        "sessionId": sid,
                        "toolCall": {"toolCallId": "tc-perm-1", "title": "Edit config file"},
                        "options": [{"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"}, {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}],
                    },
                )
                outcome = resp.get("result", {}).get("outcome", {})
                return self._finish_reply(req_id, session, text, f"permission:{outcome.get('optionId', outcome.get('outcome', 'error'))}", {"permissionResponse": resp})
            if text == "request_unknown":
                resp = await self._send_server_request("vendor/unhandled_op", {"sessionId": sid, "detail": "probe"})
                self._send_text(sid, f"unknown_req_error:{resp.get('error', {}).get('code')}")
                return self._send_result(req_id, {"stopReason": "end_turn", "clientResponse": resp})
            if text == "tool_details":
                self._send_update(sid, {"sessionUpdate": "tool_call", "toolCallId": "exec", "kind": "execute", "title": "Run command", "status": "in_progress", "rawInput": {"CommandLine": "printf hello"}})
                self._send_update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "exec", "status": "completed", "rawOutput": {"stdout": "hello\n" + "x" * 5000, "exitCode": 0}})
                self._send_update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "exec", "status": "completed"})
                self._send_update(sid, {"sessionUpdate": "tool_call", "toolCallId": "file", "title": "File ../outside.md", "kind": "read", "status": "completed"})
                return self._finish_reply(req_id, session, text, "done")
            if text == "stream_all":
                self._send_text(sid, "planning edit", "agent_thought_chunk")
                self._send_update(sid, {"sessionUpdate": "tool_call", "toolCallId": "tc-1", "title": "Update greeting", "kind": "edit", "status": "in_progress", "rawInput": {"path": "hello.py"}, "locations": [{"path": "hello.py", "line": 1}]})
                self._send_update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "tc-1", "status": "completed", "rawOutput": {"ok": True}, "content": [{"type": "diff", "path": "hello.py", "oldText": "print('old')\n", "newText": "print('new')\n"}]})
                self._send_text(sid, "Updated ")
                self._send_text(sid, "hello.py.")
                self._send_update(sid, {"sessionUpdate": "usage_update", "used": 1200, "size": 128000, "cost": {"amount": 0.01, "currency": "USD"}})
                self._send_update(sid, {"sessionUpdate": "available_commands_update", "availableCommands": [{"name": "review", "description": "Review workspace changes", "input": {"hint": "optional path"}}]})
                session["title"] = "Update greeting"
                self._send_update(sid, {"sessionUpdate": "session_info_update", "title": "Update greeting", "updatedAt": "2026-11-24T12:00:00Z"})
                self._send_update(sid, {"sessionUpdate": "plan", "entries": [{"content": "edit file", "status": "completed"}]})
                session["history"].append({"prompt": text, "reply": "Updated hello.py."})
                self._save_state()
                return self._send_result(req_id, {"stopReason": "end_turn"})
            if text.startswith("stop_reason:"):
                reason = text.split(":", 1)[1]
                self._send_text(sid, f"partial:{reason}")
                return self._send_result(req_id, {"stopReason": reason})
            if text == "stop_reason_missing":
                self._send_text(sid, "partial:missing")
                return self._send_result(req_id, {})
            if text == "emit_image_only":
                self._send_update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "image", "data": _TINY_PNG_B64, "mimeType": "image/png"}})
                return self._send_result(req_id, {"stopReason": "end_turn"})
            if text == "emit_image_uri":
                self._send_update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "image", "uri": "https://example.test/chart.png", "mimeType": "image/png"}})
                return self._send_result(req_id, {"stopReason": "end_turn"})
            if text == "recall":
                prior = [t["prompt"] for t in session["history"]]
                return self._finish_reply(req_id, session, text, f"prior={prior};model={session['model']};mode={session['mode']};extra_dirs={session['additionalDirectories']}")
            self._finish_reply(req_id, session, text, f"echo:{text}|model={session['model']}|mode={session['mode']}")
        finally:
            self._active_cancels.pop(sid, None)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scripted stdio fake ACP server")
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--config-json", type=str, default="{}")
    parser.add_argument("--require-auth-method", type=str, default=None)
    args = parser.parse_args()
    asyncio.run(FakeAcpServer(state_file=args.state_file, config=json.loads(args.config_json), require_auth_method=args.require_auth_method).run())


if __name__ == "__main__":
    main()
