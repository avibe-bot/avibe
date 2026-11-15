#!/usr/bin/env python3
"""Capture the real Windows Cua Driver metadata from an ordinary-user process.

The caller owns the disposable ordinary-profile fixture and passes this script
an already source-built production executable. This script never creates a
user, changes policy, starts a desktop app, or edits a checked-in snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any


TARGET = "x86_64-pc-windows-msvc"
VERSION = "0.31.0"


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def decode_utf8(data: bytes, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{label} was not valid UTF-8") from exc


def run_finite(binary: Path, args: list[str], *, timeout: int) -> tuple[bytes, bytes]:
    completed = subprocess.run(
        [str(binary), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        stderr = decode_utf8(completed.stderr, "driver stderr")
        raise RuntimeError(
            f"{binary.name} {' '.join(args)} failed with exit {completed.returncode}: {stderr}"
        )
    return completed.stdout, completed.stderr


def run_tools_list(binary: Path, *, timeout: int) -> tuple[dict[str, Any], bytes, bytes]:
    # Windows direct MCP owns the runtime in this process. The request is
    # metadata-only; no tool call, desktop discovery, or input is performed.
    requests = b"\n".join(
        [
            b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
            b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}',
        ]
    ) + b"\n"
    env = os.environ.copy()
    env.update(
        {
            "CUA_DRIVER_MCP_ENVELOPES": "0",
            "CUA_DRIVER_PERMISSION_MODE": "standard",
            "CUA_DRIVER_RS_TELEMETRY_ENABLED": "false",
            "CUA_DRIVER_RS_UPDATE_CHECK": "false",
        }
    )
    with tempfile.TemporaryDirectory(prefix="avibe-cua-driver-state-") as state:
        state_path = Path(state)
        env.update(
            {
                "HOME": str(state_path),
                "USERPROFILE": str(state_path),
                "APPDATA": str(state_path / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(state_path / "AppData" / "Local"),
                "CUA_DRIVER_RS_HOME": str(state_path),
                "CUA_DRIVER_TELEMETRY_HOME": str(state_path / "telemetry"),
                "XDG_CONFIG_HOME": str(state_path / "config"),
                "XDG_STATE_HOME": str(state_path / "state"),
                "XDG_CACHE_HOME": str(state_path / "cache"),
            }
        )
        completed = subprocess.run(
            [str(binary), "mcp", "--direct"],
            input=requests,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            check=False,
            timeout=timeout,
        )
    stdout_text = decode_utf8(completed.stdout, "tools/list stdout")
    stderr_text = decode_utf8(completed.stderr, "tools/list stderr")
    if completed.returncode != 0:
        raise RuntimeError(
            f"direct MCP failed with exit {completed.returncode}: {stderr_text}"
        )
    responses: list[dict[str, Any]] = []
    for line in stdout_text.splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                responses.append(value)
    response = next((value for value in responses if value.get("id") == 2), None)
    if response is None:
        raise RuntimeError("direct MCP did not return the tools/list response")
    result = response.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list) or not tools:
        raise RuntimeError("tools/list did not return a non-empty tools array")
    names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
    if len(names) != len(tools) or any(not isinstance(name, str) or not name for name in names):
        raise RuntimeError("tools/list contains a malformed tool definition")
    if len(set(names)) != len(names):
        raise RuntimeError("tools/list contains duplicate tool names")
    return response, completed.stdout, completed.stderr


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--patch-0001-sha256", required=True)
    parser.add_argument("--patch-0002-sha256", required=True)
    args = parser.parse_args()

    if os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Windows Cua Driver metadata capture requires a hosted Windows job")
    if not args.binary.is_file() or args.binary.is_symlink():
        raise RuntimeError("production Cua Driver executable is missing or not regular")
    if args.binary.suffix.lower() != ".exe":
        raise RuntimeError("production Cua Driver executable must have an .exe suffix")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    version_raw, version_stderr = run_finite(args.binary, ["--version"], timeout=30)
    version = decode_utf8(version_raw, "driver version").strip()
    if version != f"cua-driver {VERSION}":
        raise RuntimeError(f"unexpected production Cua Driver version: {version!r}")
    (args.output_dir / "version.txt").write_bytes(version_raw)
    (args.output_dir / "version.stderr").write_bytes(version_stderr)

    docs_raw, docs_stderr = run_finite(
        args.binary, ["dump-docs", "--type", "mcp"], timeout=60
    )
    docs = json.loads(decode_utf8(docs_raw, "dump-docs output"))
    if not isinstance(docs, dict) or not isinstance(docs.get("tools"), list):
        raise RuntimeError("dump-docs did not return a tools array")
    (args.output_dir / "dump-docs.raw.json").write_bytes(docs_raw)
    (args.output_dir / "dump-docs.stderr").write_bytes(docs_stderr)
    write_json(args.output_dir / "dump-docs.json", docs)

    tools_list, tools_raw, tools_stderr = run_tools_list(args.binary, timeout=120)
    (args.output_dir / "tools-list.raw.jsonl").write_bytes(tools_raw)
    (args.output_dir / "tools-list.stderr").write_bytes(tools_stderr)
    write_json(args.output_dir / "tools-list.json", tools_list)

    evidence = {
        "schema_version": 1,
        "target": TARGET,
        "driver_version": VERSION,
        "binary_sha256": digest(args.binary),
        "source_commit": args.source_commit,
        "patch_0001_sha256": args.patch_0001_sha256,
        "patch_0002_sha256": args.patch_0002_sha256,
        "tools_list_sha256": digest(args.output_dir / "tools-list.raw.jsonl"),
        "dump_docs_sha256": digest(args.output_dir / "dump-docs.raw.json"),
        "tool_count": len(tools_list["result"]["tools"]),
        "tool_names": [tool["name"] for tool in tools_list["result"]["tools"]],
        "capture_commands": {
            "version": [args.binary.name, "--version"],
            "dump_docs": [args.binary.name, "dump-docs", "--type", "mcp"],
            "tools_list": [args.binary.name, "mcp", "--direct"],
        },
        "ordinary_profile": True,
        "desktop_actions": False,
        "snapshot_edited_by_capture": False,
    }
    write_json(args.output_dir / "capture.json", evidence)


if __name__ == "__main__":
    main()
