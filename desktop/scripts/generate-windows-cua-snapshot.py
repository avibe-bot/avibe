#!/usr/bin/env python3
"""Generate the pinned Windows Cua Driver snapshot from captured MCP output.

The input is a real production binary capture from the hosted Windows
ordinary-profile job. This tool selects the reviewed policy roster in policy
order and copies each complete tools/list definition without inventing fields.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "cua-driver" / "sources.json"
POLICY = ROOT / "cua-driver" / "policy.yaml"
TARGET = "x86_64-pc-windows-msvc"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def generate(tools_list_path: Path, dump_docs_path: Path, output: Path) -> dict[str, Any]:
    manifest = read_json(SOURCES)
    driver = manifest["driver"]
    windows = driver["windows"]
    policy = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    policy_names = policy["allow"]["tools"]
    if not isinstance(policy_names, list) or not policy_names:
        raise ValueError("managed Windows policy has no tool roster")

    tools_envelope = read_json(tools_list_path)
    result = tools_envelope.get("result") if isinstance(tools_envelope, dict) else None
    actual_tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(actual_tools, list):
        raise ValueError("captured MCP tools/list has no result.tools array")
    actual_by_name = {
        tool.get("name"): tool
        for tool in actual_tools
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    }
    if len(actual_by_name) != len(actual_tools):
        raise ValueError("captured MCP tools/list has malformed or duplicate names")
    missing = [name for name in policy_names if name not in actual_by_name]
    if missing:
        raise ValueError(f"captured MCP tools/list is missing managed tools: {missing}")

    docs = read_json(dump_docs_path)
    docs_tools = docs.get("tools") if isinstance(docs, dict) else None
    docs_by_name = {
        tool.get("name"): tool
        for tool in docs_tools or []
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    }
    if len(docs_by_name) != len(docs_tools or []):
        raise ValueError("captured dump-docs has malformed or duplicate names")
    for name in policy_names:
        tool = actual_by_name[name]
        doc = docs_by_name.get(name)
        if not isinstance(doc, dict) or doc.get("input_schema") != tool.get("inputSchema"):
            raise ValueError(f"dump-docs input_schema disagrees with tools/list for {name}")

    patch = windows["patch"]
    snapshot = {
        "driver_version": driver["version"],
        "patch_sha256": patch["sha256"],
        "schema_version": 1,
        "source_commit": driver["source_commit"],
        "source_tag": driver["tag"],
        "source_variant": patch["variant"],
        "target": windows["target"],
        "tools": [actual_by_name[name] for name in policy_names],
    }
    if snapshot["target"] != TARGET:
        raise ValueError("Windows manifest target mismatch")
    output.write_bytes(
        (json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    )
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tools-list", type=Path, required=True)
    parser.add_argument("--dump-docs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = generate(args.tools_list, args.dump_docs, args.output)
    print(json.dumps({
        "output": str(args.output),
        "sha256": digest(args.output),
        "target": snapshot["target"],
        "tool_count": len(snapshot["tools"]),
        "tool_names": [tool["name"] for tool in snapshot["tools"]],
    }, separators=(",", ":")))


if __name__ == "__main__":
    main()
