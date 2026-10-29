from datetime import datetime, timedelta, timezone
import json

import pytest

from vibe.desktop_update_request import (
    DesktopUpdateRequestError,
    create_or_reuse_desktop_update_request,
    expire_desktop_update_request,
    parse_desktop_update_request,
    read_desktop_update_request,
)


NOW = datetime(2026, 10, 29, 2, 0, tzinfo=timezone.utc)


def _request(**changes):
    payload = {
        "schema_version": 1,
        "request_id": "87dd4212-fb78-4c57-968c-2f699cc04462",
        "target": "3.2.4-rc.1",
        "state": "pending",
        "created_at": "2026-10-29T02:00:00Z",
        "expires_at": "2026-10-29T02:30:00Z",
    }
    payload.update(changes)
    return payload


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        _request(schema_version=True),
        _request(request_id="forged"),
        _request(target="../3.2.4"),
        _request(state=[]),
        _request(state={}),
        _request(state=7),
        _request(state=None),
        _request(state="unknown"),
        _request(state="acknowledged"),
        _request(created_at="not-a-time"),
        _request(expires_at="2026-10-29T01:59:59Z"),
        {**_request(), "forged": True},
    ],
)
def test_request_parser_rejects_bad_or_forged_input(payload) -> None:
    assert parse_desktop_update_request(payload) is None


def test_request_creation_is_idempotent_and_serializes_other_targets(tmp_path) -> None:
    request_path = tmp_path / "desktop-update-request.json"
    status_path = tmp_path / "update-status.json"

    first = create_or_reuse_desktop_update_request(
        "3.2.4-rc.1",
        request_path=request_path,
        status_path=status_path,
        now=NOW,
    )
    second = create_or_reuse_desktop_update_request(
        "3.2.4-rc.1",
        request_path=request_path,
        status_path=status_path,
        now=NOW + timedelta(seconds=1),
    )
    assert first.reused is False
    assert second.reused is True
    assert second.request["request_id"] == first.request["request_id"]
    assert read_desktop_update_request(request_path) == first.request

    with pytest.raises(DesktopUpdateRequestError, match="update_in_progress"):
        create_or_reuse_desktop_update_request(
            "3.2.5",
            request_path=request_path,
            status_path=status_path,
            now=NOW + timedelta(seconds=2),
        )


def test_terminal_status_is_not_reused_and_expired_request_cannot_revive(tmp_path) -> None:
    request_path = tmp_path / "desktop-update-request.json"
    status_path = tmp_path / "update-status.json"
    first = create_or_reuse_desktop_update_request(
        "3.2.4-rc.1",
        request_path=request_path,
        status_path=status_path,
        now=NOW,
    )

    status_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "latest": "3.2.4-rc.1",
                "has_update": True,
                "remote_request": {
                    "request_id": first.request["request_id"],
                    "target": "3.2.4-rc.1",
                    "phase": "expired",
                    "progress": None,
                    "error_code": None,
                    "updated_at": "2026-10-29T02:00:10Z",
                },
            }
        ),
        encoding="utf-8",
    )
    next_request = create_or_reuse_desktop_update_request(
        "3.2.4-rc.1",
        request_path=request_path,
        status_path=status_path,
        now=NOW + timedelta(seconds=11),
    )
    assert next_request.request["request_id"] != first.request["request_id"]
    assert expire_desktop_update_request(next_request.request["request_id"], path=request_path)
    assert not expire_desktop_update_request(next_request.request["request_id"], path=request_path)

    after_expiry = create_or_reuse_desktop_update_request(
        "3.2.4-rc.1",
        request_path=request_path,
        status_path=status_path,
        now=NOW + timedelta(seconds=12),
    )
    assert after_expiry.request["request_id"] != next_request.request["request_id"]


def test_unexpired_shell_status_is_the_idempotency_record(tmp_path) -> None:
    request_path = tmp_path / "desktop-update-request.json"
    status_path = tmp_path / "update-status.json"
    status_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "latest": "3.2.4-rc.1",
                "has_update": True,
                "remote_install_allowed": False,
                "remote_request": {
                    "request_id": "87dd4212-fb78-4c57-968c-2f699cc04462",
                    "target": "3.2.4-rc.1",
                    "phase": "awaiting_host",
                    "progress": None,
                    "error_code": None,
                    "updated_at": "2026-10-29T02:00:05Z",
                },
            }
        ),
        encoding="utf-8",
    )

    decision = create_or_reuse_desktop_update_request(
        "3.2.4-rc.1",
        request_path=request_path,
        status_path=status_path,
        now=NOW + timedelta(seconds=10),
    )
    assert decision.reused is True
    assert decision.needs_nudge is False
    assert decision.request["request_id"] == "87dd4212-fb78-4c57-968c-2f699cc04462"


@pytest.mark.parametrize(
    "phase",
    [
        "requested",
        "acknowledged",
        "awaiting_host",
        "checking",
        "downloading",
        "installing",
        "restarting",
    ],
)
@pytest.mark.parametrize("expiry", ["file_state", "ttl"])
def test_nonterminal_status_cannot_keep_an_expired_request_alive(
    tmp_path,
    phase,
    expiry,
) -> None:
    request_path = tmp_path / "desktop-update-request.json"
    status_path = tmp_path / "update-status.json"
    first = create_or_reuse_desktop_update_request(
        "3.2.4-rc.1",
        request_path=request_path,
        status_path=status_path,
        now=NOW,
    )
    status_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "latest": "3.2.4-rc.1",
                "has_update": True,
                "remote_request": {
                    "request_id": first.request["request_id"],
                    "target": "3.2.4-rc.1",
                    "phase": phase,
                    "progress": None,
                    "error_code": None,
                    "updated_at": "2026-10-29T02:00:05Z",
                },
            }
        ),
        encoding="utf-8",
    )
    if expiry == "file_state":
        assert expire_desktop_update_request(
            first.request["request_id"],
            path=request_path,
        )
        retry_at = NOW + timedelta(seconds=10)
    else:
        retry_at = NOW + timedelta(minutes=31)

    replacement = create_or_reuse_desktop_update_request(
        "3.2.4-rc.1",
        request_path=request_path,
        status_path=status_path,
        now=retry_at,
    )

    assert replacement.reused is False
    assert replacement.request["request_id"] != first.request["request_id"]
