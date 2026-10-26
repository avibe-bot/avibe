import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import vibe.screenshot as screenshot
from vibe import cli
from vibe.screenshot import ScreenshotError, ScreenshotResult, capture_screenshot, default_screenshot_path


def test_screenshot_parser_accepts_output_and_json() -> None:
    parser = cli.build_parser()

    args = parser.parse_args(["screenshot", "--output", "screen.png", "--json"])

    assert args.command == "screenshot"
    assert args.output == "screen.png"
    assert args.json is True


def test_cmd_screenshot_prints_path(capsys, monkeypatch) -> None:
    captured: list[str | None] = []

    def fake_capture(output=None):
        captured.append(output)
        return ScreenshotResult(path=Path("/tmp/vibe-screen.png"), backend="fake")

    monkeypatch.setattr(cli, "capture_screenshot", fake_capture)
    parser = cli.build_parser()
    args = parser.parse_args(["screenshot", "--output", "custom.png"])

    assert cli.cmd_screenshot(args) == 0
    assert captured == ["custom.png"]
    assert capsys.readouterr().out.strip() == "/tmp/vibe-screen.png"


def test_cmd_screenshot_prints_json(capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "capture_screenshot",
        lambda output=None: ScreenshotResult(path=Path("/tmp/vibe-screen.png"), backend="fake"),
    )
    parser = cli.build_parser()
    args = parser.parse_args(["screenshot", "--json"])

    assert cli.cmd_screenshot(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "schema_version": 1,
        "ok": True,
        "kind": "screenshot",
        "path": "/tmp/vibe-screen.png",
        "backend": "fake",
    }


def test_cmd_screenshot_reports_errors(capsys, monkeypatch) -> None:
    def fake_capture(output=None):
        raise ScreenshotError("display is unavailable")

    monkeypatch.setattr(cli, "capture_screenshot", fake_capture)
    parser = cli.build_parser()
    args = parser.parse_args(["screenshot", "--json"])

    assert cli.cmd_screenshot(args) == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["ok"] is False
    assert payload["code"] == "screenshot_failed"
    assert payload["error"] == "display is unavailable"


def test_capture_screenshot_wraps_output_preparation_errors(tmp_path: Path) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked", encoding="utf-8")

    try:
        capture_screenshot(blocked_parent / "screen.png")
    except ScreenshotError as exc:
        assert "failed to prepare screenshot output path" in str(exc)
    else:
        raise AssertionError("expected ScreenshotError")


def test_default_screenshot_paths_are_unique(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(screenshot.paths, "get_vibe_remote_dir", lambda: tmp_path / ".vibe_remote")

    first = default_screenshot_path()
    second = default_screenshot_path()

    assert first != second
    assert first.parent == second.parent == tmp_path / ".vibe_remote" / "screenshots"
    assert first.name.startswith("screenshot_")
    assert first.suffix == ".png"


def test_capture_screenshot_preserves_existing_output_when_linux_backend_fails(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "screen.png"
    output.write_bytes(b"existing")

    monkeypatch.setattr(screenshot.sys, "platform", "linux")
    monkeypatch.setattr(screenshot, "which", lambda name: f"/usr/bin/{name}" if name == "grim" else None)

    def fail_capture(command, label):
        Path(command[-1]).write_bytes(b"partial")
        raise ScreenshotError("backend failed")

    monkeypatch.setattr(screenshot, "_run_capture_command", fail_capture)

    try:
        capture_screenshot(output)
    except ScreenshotError:
        pass
    else:
        raise AssertionError("expected ScreenshotError")

    assert output.read_bytes() == b"existing"


def test_linux_fallback_clears_partial_capture_before_next_backend(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "screen.png"
    output.write_bytes(b"existing")
    available = {"grim", "gnome-screenshot"}

    monkeypatch.setattr(screenshot.sys, "platform", "linux")
    monkeypatch.setattr(screenshot, "which", lambda name: f"/usr/bin/{name}" if name in available else None)

    def capture_with_partial_then_empty_success(command, label):
        capture_path = Path(command[-1])
        if label == "grim":
            capture_path.write_bytes(b"partial")
            raise ScreenshotError("backend failed")
        if label == "gnome-screenshot":
            return
        raise AssertionError(f"unexpected backend: {label}")

    monkeypatch.setattr(screenshot, "_run_capture_command", capture_with_partial_then_empty_success)

    try:
        capture_screenshot(output)
    except ScreenshotError as exc:
        assert "screenshot file was not created" in str(exc)
    else:
        raise AssertionError("expected ScreenshotError")

    assert output.read_bytes() == b"existing"


def test_capture_screenshot_replaces_existing_output_only_after_success(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "screen.png"
    output.write_bytes(b"existing")
    capture_paths: list[Path] = []

    monkeypatch.setattr(screenshot.sys, "platform", "linux")
    monkeypatch.setattr(screenshot, "which", lambda name: f"/usr/bin/{name}" if name == "grim" else None)

    def successful_capture(command, label):
        capture_path = Path(command[-1])
        capture_paths.append(capture_path)
        capture_path.write_bytes(b"new screenshot")

    monkeypatch.setattr(screenshot, "_run_capture_command", successful_capture)

    result = capture_screenshot(output)

    assert result.path == output
    assert output.read_bytes() == b"new screenshot"
    assert capture_paths
    assert output not in capture_paths
    assert all(not path.exists() for path in capture_paths)
