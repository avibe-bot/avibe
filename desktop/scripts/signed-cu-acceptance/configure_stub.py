"""Provision the isolated installed runtime through supported public HTTP APIs."""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


class RuntimeAPI:
    def __init__(self, origin: str):
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
            raise ValueError("fixture_loopback_origin_required")
        self.origin = origin.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = self.call("GET", "/api/csrf-token")["csrf_token"]

    def call(self, method: str, path: str, payload=None):
        headers = {"Origin": self.origin, "Content-Type": "application/json"}
        if method != "GET":
            headers["X-Vibe-CSRF-Token"] = self.csrf
        request = urllib.request.Request(
            self.origin + path, method=method, headers=headers,
            data=None if payload is None else json.dumps(payload).encode(),
        )
        with self.opener.open(request, timeout=300) as response:
            return json.load(response)


def configure(api: RuntimeAPI, port: int, work: Path):
    api.call("POST", "/api/config", {"setup_completed": True, "runtime": {"default_cwd": str(work)}})
    api.call("POST", "/api/models/runtime/start", {})
    source = api.call("POST", "/api/models/sources", {
        "kind": "api_key", "vendor": "custom", "display_name": "CU acceptance deterministic stub",
        "base_url": f"http://127.0.0.1:{port}/v1", "protocol": "openai_chat",
        # Required nonempty fixture placeholder; the loopback server neither
        # authenticates it nor forwards it. This is not a model credential.
        "key": "acceptance-not-a-secret", "save_unverified": True,
        "models": [{"id": "cu-acceptance", "origin": "manual", "reasoning_efforts": ["low"]}],
    })["source"]["id"]
    api.call("PUT", "/api/models/agents/vibey/sources", {"order": [source]})
    baseline = api.call("GET", "/api/models/agents/vibey/models")["agent"].get("catalog_models", [])
    models = [row for row in baseline if row["id"] != "cu-acceptance"]
    models.append({
        "id": "cu-acceptance", "origin": "manual", "context_window": 200000, "max_output_tokens": 4096,
        "input_modalities": ["text"], "output_modalities": ["text"], "supports_tools": True,
        "supports_reasoning": True, "reasoning_efforts": ["low"],
    })
    api.call("PUT", "/api/models/agents/vibey/models", {"baseline": baseline, "models": models})
    api.call("PUT", "/api/models/agents/vibey/chain?model=cu-acceptance", {
        "hops": [{"source_id": source, "model_id": "cu-acceptance"}],
    })
    listed = api.call("GET", "/api/models/agents/vibey/models")["agent"]["catalog_models"]
    if not any(row["id"] == "cu-acceptance" for row in listed):
        raise ValueError("fixture_model_readback_missing")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--work", required=True, type=Path)
    args = parser.parse_args()
    try:
        configure(RuntimeAPI(args.origin), args.port, args.work)
    except Exception as error:
        # Public API failure code and type only, never a source key/CSRF cookie,
        # request or response body, profile path, or arbitrary exception text.
        report = {"ok": False, "error_type": type(error).__name__}
        if isinstance(error, urllib.error.HTTPError):
            report["http_status"] = error.code
        print(json.dumps(report))
        return 1
    print(json.dumps({"ok": True, "deterministic_model_stub": True, "llm": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
