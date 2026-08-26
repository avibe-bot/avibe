from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import requests


SESSION_ID = "ses-issue-1617"
CALLER_SECRET = "AVIBE_CALLER_SECRET_NEVER_FORWARD"
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "show_markdown_integration"


def _vibe(*args: str) -> dict:
    result = subprocess.run(
        ["vibe", *args, "--json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _install_fixture() -> Path:
    workspace = Path(_vibe("show", "path", "--session-id", SESSION_ID)["path"])
    for relative in (Path("src/pages"), Path("api")):
        destination = workspace / relative
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(FIXTURE_ROOT / relative, destination)
    return workspace


def _assert_markdown(response: requests.Response, *, cache: str | None = None) -> str:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].lower() == "text/markdown; charset=utf-8"
    assert "accept" in {part.strip().lower() for part in response.headers["vary"].split(",")}
    assert "no-store" in response.headers["cache-control"].lower()
    if cache is not None:
        assert response.headers["x-avibe-render-cache"] == cache
    return response.text


def _assert_html(response: requests.Response) -> None:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].lower().startswith("text/html")
    assert "<!doctype html>" in response.text.lower()


@pytest.mark.integration
def test_published_show_runtime_renders_private_and_public_markdown() -> None:
    base_url = os.environ.get("AVIBE_SHOW_MARKDOWN_BASE_URL")
    expected_runtime = os.environ.get("AVIBE_SHOW_MARKDOWN_EXPECT_RUNTIME_VERSION")
    expected_manifest = os.environ.get("AVIBE_SHOW_MARKDOWN_EXPECT_MANIFEST_SHA256")
    expected_archive = os.environ.get("AVIBE_SHOW_MARKDOWN_EXPECT_ARCHIVE_SHA256")
    if not base_url or not expected_runtime or not expected_manifest or not expected_archive:
        pytest.skip(
            "set the Show Markdown base URL, runtime version, manifest digest, and archive digest "
            "inside an isolated Incus regression environment"
        )

    runtime_status = _vibe("runtime", "status")
    assert runtime_status["manifest"]["runtime_version"] == expected_runtime
    assert runtime_status["manifest"]["sha256"] == expected_manifest
    assert runtime_status["archive"]["sha256"] == expected_archive
    assert runtime_status["install"]["runtime_version"] == expected_runtime
    assert runtime_status["install"]["matches_manifest"] is True

    expected_browserless = os.environ.get("AVIBE_SHOW_MARKDOWN_EXPECT_BROWSERLESS") == "1"
    browser_cache = Path.home() / ".cache" / "ms-playwright"
    browser_targets = (
        "google-chrome-stable",
        "google-chrome",
        "microsoft-edge",
        "chromium",
        "chromium-browser",
    )
    if expected_browserless:
        assert not any(shutil.which(target) for target in browser_targets)
        assert not list(browser_cache.glob("chromium_headless_shell-*"))

    _install_fixture()
    _vibe("show", "update", "--session-id", SESSION_ID, "--visibility", "private")
    private_url = f"{base_url.rstrip('/')}/show/{SESSION_ID}/"
    markdown_headers = {"Accept": "text/markdown"}
    caller_headers = {
        **markdown_headers,
        "Authorization": f"Bearer {CALLER_SECRET}",
        "Cookie": f"caller={CALLER_SECRET}",
        "X-Vibe-CSRF-Token": CALLER_SECRET,
    }

    try:
        _assert_html(requests.get(private_url, timeout=30))

        first = requests.get(private_url, headers=markdown_headers, timeout=420)
        first_body = _assert_markdown(first, cache="miss")
        assert "AVIBE_MARKDOWN_VISIBLE" in first_body
        assert "AVIBE_MARKDOWN_HIDDEN" not in first_body
        assert "Preserve the verified release source" in first_body

        if expected_browserless:
            provisioned = list(browser_cache.glob("chromium_headless_shell-*"))
            assert len(provisioned) == 1

        second = requests.get(private_url, headers=markdown_headers, timeout=60)
        assert _assert_markdown(second, cache="hit") == first_body

        query_path = "reports/daily?view=week&timezone=Asia%2FShanghai"
        report = requests.get(f"{private_url}{query_path}", headers=caller_headers, timeout=120)
        report_body = _assert_markdown(report)
        assert "Daily report" in report_body
        assert "week" in report_body
        assert "Asia/Shanghai" in report_body
        assert CALLER_SECRET not in report_body
        assert report_body.count("none") >= 3

        public_state = _vibe(
            "show",
            "update",
            "--session-id",
            SESSION_ID,
            "--visibility",
            "public",
        )
        share_id = public_state["share_id"]
        public_url = f"{base_url.rstrip('/')}/p/{share_id}/"

        public_markdown = requests.get(public_url, headers=markdown_headers, timeout=120)
        public_body = _assert_markdown(public_markdown)
        assert "/show/" not in public_body
        assert "NEVER_EXPOSE" not in public_body
        assert f"/p/{share_id}/reports/daily?view=week" in public_body

        _assert_html(requests.get(public_url, timeout=30))
        public_report = requests.get(f"{public_url}{query_path}", headers=markdown_headers, timeout=120)
        public_report_body = _assert_markdown(public_report)
        assert "Daily report" in public_report_body
        assert "week" in public_report_body
        assert "Asia/Shanghai" in public_report_body
        assert CALLER_SECRET not in public_report_body

        if expected_browserless:
            assert len(list(browser_cache.glob("chromium_headless_shell-*"))) == 1
    finally:
        _vibe("show", "update", "--session-id", SESSION_ID, "--visibility", "offline")
