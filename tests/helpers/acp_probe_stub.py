"""A scripted peer at the auth probe boundary; adapter tests use the stdio peer."""

from copy import deepcopy

from modules.agents import acp


def make_probe_client(*, options, selection=None, result=None, chunks=None, legacy_model=None, authenticated=lambda: True):
    observed = {"prompt_called": False, "closed": False, "spawns": 0, "selections": [], "commands": []}

    class ProbeClient:
        supports_close = True

        def __init__(self, *_args, **_kwargs):
            observed["spawns"] += 1
            observed["commands"].append((list(_args[0]), dict(_kwargs.get("env") or {})))
            self.notify = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        def on_notification(self, callback):
            self.notify = callback

        async def initialize(self, **_kwargs):
            return {"protocolVersion": 1}

        async def authenticate(self, _method):
            return {}

        async def session_new(self, _cwd, **_kwargs):
            if not authenticated():
                raise acp.AcpRpcError(acp.ACP_AUTH_REQUIRED, "Authentication required")
            return {"sessionId": "sess-probe-1", "configOptions": deepcopy(options)}

        async def session_set_config_option(self, _sid, cid, value):
            observed["selections"].append((cid, value))
            if selection == "method_not_found":
                raise acp.AcpRpcError(acp.JSONRPC_METHOD_NOT_FOUND, "Method not found")
            return deepcopy(selection if selection is not None else {"configOptions": options})

        async def session_set_model(self, _sid, _model):
            # Only the no-fallback regression supplies this legacy receipt.
            assert legacy_model is not None
            return deepcopy(legacy_model)

        async def session_prompt(self, _sid, _blocks, **_kwargs):
            observed["prompt_called"] = True
            for sid, text in chunks if chunks is not None else [("sess-probe-1", "partial reply")]:
                await self.notify("session/update", {"sessionId": sid, "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}})
            return deepcopy(result if result is not None else {"stopReason": "end_turn"})

        async def session_close(self, _sid):
            observed["closed"] = True
            return {}

    return ProbeClient, observed
