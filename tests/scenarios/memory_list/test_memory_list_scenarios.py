"""Closed-loop scenario evidence for processed Memory episode listing."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timezone

from core.caller_context import AVIBE_SESSION_ID_ENV
from core.memory.everos import AddAck, FakeMemoryProvider
from core.memory.module import MIN_FREE_DISK_BYTES, MemoryModule
from core.memory.runtime import MemoryRuntime
from core.memory.store import MemoryStore
from core.memory.types import CaptureAccepted, CaptureRequest, MemoryListItem, MemoryListPage
from vibe import cli, internal_client


PRINCIPAL = "u-11111111111111111111111111111111"


class _CapturedEpisodeProvider(FakeMemoryProvider):
    async def list_episodes(
        self,
        principal_id: str,
        project_id: str,
        page: int,
        page_size: int,
    ) -> MemoryListPage:
        self.list_requests.append((principal_id, project_id, page, page_size))
        matching = sorted(
            (
                capture
                for capture in self.captures
                if capture.session_ref.principal_id == principal_id
                and capture.session_ref.project_ref == project_id
            ),
            key=lambda capture: capture.provider_timestamp_ms,
            reverse=True,
        )
        start = (page - 1) * page_size
        selected = matching[start : start + page_size]
        items = tuple(
            MemoryListItem(
                id=f"episode-{capture.provider_timestamp_ms}",
                subject=capture.text,
                summary="",
                body=capture.text,
                timestamp=datetime.fromtimestamp(
                    capture.provider_timestamp_ms / 1000,
                    tz=timezone.utc,
                )
                .isoformat()
                .replace("+00:00", "Z"),
                project=project_id,
            )
            for capture in selected
        )
        return MemoryListPage(
            items=items,
            page=page,
            page_size=page_size,
            count=len(items),
            total_count=len(matching),
        )


def test_captured_processed_episodes_list_through_cli_with_exact_page_boundary(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    """Scenarios: MEMORY-LIST-001, MEMORY-LIST-002, MEMORY-LIST-004."""

    monkeypatch.setenv("AVIBE_HOME", str(tmp_path / "avibe-home"))
    provider = _CapturedEpisodeProvider(
        add_results=deque(
            AddAck(request_id=f"add-{index}", status="extracted")
            for index in range(4)
        )
    )
    effective_home = tmp_path / "avibe-home"
    effective_home.mkdir(mode=0o700)
    module = MemoryModule(
        MemoryStore(effective_home / "state" / "memory.sqlite"),
        provider,
        enabled=True,
        disk_free_bytes=lambda: MIN_FREE_DISK_BYTES,
        effective_home=effective_home,
    )

    async def _capture_and_process() -> None:
        requests = (
            CaptureRequest("notes-1", "session", PRINCIPAL, "notes", "user_input", "old", 1_000),
            CaptureRequest("notes-2", "session", PRINCIPAL, "notes", "user_input", "middle", 2_000),
            CaptureRequest("notes-3", "session", PRINCIPAL, "notes", "user_input", "new", 3_000),
            CaptureRequest("default-1", "session", PRINCIPAL, "default", "user_input", "foreign", 4_000),
        )
        for request in requests:
            assert await module.capture(request) == CaptureAccepted()
        await module.wait_writer_idle_for_tests()

    asyncio.run(_capture_and_process())
    runtime = object.__new__(MemoryRuntime)
    runtime._module = module
    runtime._retired = False
    monkeypatch.setenv(AVIBE_SESSION_ID_ENV, "ses-memory-list-scenario")

    def list_sync(*, project, page, limit, caller_session_id):
        assert caller_session_id == "ses-memory-list-scenario"
        return {
            "status_code": 200,
            "body": asyncio.run(
                runtime.list_episodes_payload(
                    PRINCIPAL,
                    project,
                    page=page,
                    page_size=limit,
                )
            ),
        }

    monkeypatch.setattr(internal_client, "memory_list_sync", list_sync)

    first_args = cli.build_parser().parse_args(
        ["memory", "list", "--project", "notes", "--limit", "2", "--page", "1", "--json"]
    )
    assert cli.cmd_memory(first_args) == 0
    first = json.loads(capsys.readouterr().out)["result"]

    second_args = cli.build_parser().parse_args(
        ["memory", "list", "--project", "notes", "--limit", "2", "--page", "2", "--json"]
    )
    assert cli.cmd_memory(second_args) == 0
    second = json.loads(capsys.readouterr().out)["result"]

    assert [item["subject"] for item in first["items"]] == ["new", "middle"]
    assert [item["subject"] for item in second["items"]] == ["old"]
    assert first["total_count"] == second["total_count"] == 3
    assert first["count"] == 2
    assert second["count"] == 1
    assert all(item["project"] == "notes" for item in first["items"] + second["items"])
    assert all(item["subject"] != "foreign" for item in first["items"] + second["items"])
