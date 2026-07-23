from __future__ import annotations

import json
from pathlib import Path

from core.memory import everos
from core.memory.sidecar import _processing_healthy_from_child_environment, _request_rejection


def test_sidecar_guard_allows_only_the_fixed_owner_and_memory_scope() -> None:
    payload = (
        b'{"session_id":"src--one--e1","app_id":"avibe","project_id":"personal",'
        b'"messages":[{"sender_id":"owner-1","role":"user","timestamp":1725000001234,'
        b'"content":"text"}]}'
    )

    assert _request_rejection("GET", "/health", b"", "owner-1") is None
    assert _request_rejection("POST", "/api/v1/memory/add", payload, "owner-1") is None
    assert _request_rejection("POST", "/api/v1/memory/add", payload, "other-owner") == "add"
    assert _request_rejection("GET", "/api/v1/memory/search", b"", "owner-1") == "route"
    assert _request_rejection("POST", "/unrelated", b"{}", "owner-1") == "route"


def test_sidecar_guard_allows_workbench_attachment_file_uri_only(tmp_path: Path) -> None:
    attachments_root = tmp_path / "attachments" / "avibe"
    asset = attachments_root / "session-1" / "diagram.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"png")
    payload = {
        "session_id": "src--one--e1",
        "app_id": "avibe",
        "project_id": "personal",
        "messages": [
            {
                "sender_id": "owner-1",
                "role": "user",
                "timestamp": 1_725_000_001_234,
                "content": [
                    {"type": "text", "text": "diagram"},
                    {
                        "type": "image",
                        "name": "diagram.png",
                        "uri": asset.as_uri(),
                        "ext": "png",
                    },
                ],
            }
        ],
    }

    assert (
        _request_rejection(
            "POST",
            "/api/v1/memory/add",
            json.dumps(payload).encode(),
            "owner-1",
            attachments_root=attachments_root,
        )
        is None
    )
    payload["messages"][0]["content"][1]["uri"] = (tmp_path / "outside.png").as_uri()
    assert (
        _request_rejection(
            "POST",
            "/api/v1/memory/add",
            json.dumps(payload).encode(),
            "owner-1",
            attachments_root=attachments_root,
        )
        == "add"
    )

    link = attachments_root / "session-1" / "linked.png"
    link.symlink_to(asset)
    payload["messages"][0]["content"][1]["uri"] = link.as_uri()
    assert (
        _request_rejection(
            "POST",
            "/api/v1/memory/add",
            json.dumps(payload).encode(),
            "owner-1",
            attachments_root=attachments_root,
        )
        == "add"
    )


def test_processing_probe_builds_the_adapter_from_child_environment_only(monkeypatch) -> None:
    received: dict[str, object] = {}

    class _Provider:
        def __init__(self, socket_path, **kwargs) -> None:
            received["socket_path"] = socket_path
            received.update(kwargs)

        async def processing_healthy(self) -> bool:
            return True

    monkeypatch.setenv("EVEROS_LLM__BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("EVEROS_LLM__MODEL", "chat-model")
    monkeypatch.setenv("EVEROS_LLM__API_KEY", "llm-secret")
    monkeypatch.setenv("EVEROS_EMBEDDING__BASE_URL", "https://embed.example.test/v1")
    monkeypatch.setenv("EVEROS_EMBEDDING__MODEL", "embed-model")
    monkeypatch.setenv("EVEROS_EMBEDDING__API_KEY", "embedding-secret")
    monkeypatch.setattr(everos, "EverOSPort", _Provider)

    assert _processing_healthy_from_child_environment() is True
    assert str(received.pop("socket_path")) == "/nonexistent-memory-sidecar.sock"
    assert received == {
        "llm_base_url": "https://llm.example.test/v1",
        "llm_model": "chat-model",
        "llm_api_key": "llm-secret",
        "embedding_base_url": "https://embed.example.test/v1",
        "embedding_model": "embed-model",
        "embedding_api_key": "embedding-secret",
    }
