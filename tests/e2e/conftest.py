"""E2E test fixtures: manage Docker container lifecycle."""

import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Mapping

import pytest
import urllib.request
import urllib.error

from tests.e2e.drivers.model_hub_app import ModelHubTestApp
from tests.e2e.drivers.mock_llm_upstream import MockLLMUpstream

# E2E tests connect to the Vibe container on this port
E2E_PORT = int(os.environ.get("VIBE_E2E_PORT", "15123"))
E2E_BASE_URL = f"http://127.0.0.1:{E2E_PORT}"

# Compose file path (relative to repo root)
COMPOSE_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "docker-compose.e2e.yml")

# When true, skip container teardown (set by run_e2e.sh --keep)
KEEP_CONTAINER = os.environ.get("VIBE_E2E_KEEP", "false").lower() == "true"


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Keep Model Hub process suites opt-in during broad E2E runs."""

    if "e2e_model_hub" in (config.option.markexpr or ""):
        return
    skip = pytest.mark.skip(
        reason="Model Hub E2E is opt-in; run with -m e2e_model_hub"
    )
    for item in items:
        if item.get_closest_marker("e2e_model_hub") is not None:
            item.add_marker(skip)


@pytest.fixture
def mock_llm_upstream(monkeypatch):
    """Run the frozen Model Hub upstream contract on loopback."""

    with MockLLMUpstream() as upstream:
        monkeypatch.setenv("VIBE_E2E_MOCK_UPSTREAM_URL", upstream.url)
        yield upstream


@pytest.fixture
def model_hub_app_factory():
    """Build isolated runtimes, optionally seeding their native HOME."""

    repo_root = Path(__file__).resolve().parents[2]

    @contextmanager
    def launch(
        *,
        enabled: bool = True,
        extra_env: Mapping[str, str] | None = None,
        before_start: Callable[[ModelHubTestApp], None] | None = None,
    ) -> Iterator[ModelHubTestApp]:
        prefix = (
            "avibe-model-hub-e2e-"
            if enabled
            else "avibe-model-hub-disabled-e2e-"
        )
        with tempfile.TemporaryDirectory(
            prefix=prefix, dir="/tmp"
        ) as runtime_root:
            runtime = ModelHubTestApp(
                repo_root,
                Path(runtime_root),
                enabled=enabled,
                extra_env=extra_env,
            )
            if before_start is not None:
                before_start(runtime)
            with runtime:
                yield runtime

    return launch


@pytest.fixture
def model_hub_app(model_hub_app_factory):
    """Run feature-enabled Avibe over real HTTP and controller IPC."""

    with model_hub_app_factory() as runtime:
        yield runtime


@pytest.fixture
def disabled_model_hub_app(model_hub_app_factory):
    """Run feature-disabled Avibe against isolated state."""

    with model_hub_app_factory(enabled=False) as runtime:
        yield runtime


def _docker_available() -> bool:
    """Check if Docker CLI is installed and the daemon is reachable."""
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _url(path: str) -> str:
    return f"{E2E_BASE_URL}{path}"


def _compose_env() -> dict:
    env = os.environ.copy()
    env["VIBE_E2E_PORT"] = str(E2E_PORT)
    return env


def _compose_down(compose_file: str, env: dict) -> None:
    subprocess.run(
        ["docker", "compose", "-f", compose_file, "down", "-v"],
        env=env,
        capture_output=True,
    )


def _wait_for_healthy(timeout: int = 60) -> bool:
    """Poll /health until it responds or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(_url("/health"), timeout=3)
            if resp.status == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    return False


@pytest.fixture(scope="session")
def vibe_container():
    """Start Vibe container for the entire test session, tear down after."""
    if not _docker_available():
        pytest.skip("Docker is not available — skipping E2E tests")

    compose_file = os.path.abspath(COMPOSE_FILE)
    env = _compose_env()

    try:
        subprocess.run(
            ["docker", "compose", "-f", compose_file, "build"],
            check=True,
            env=env,
        )
        subprocess.run(
            ["docker", "compose", "-f", compose_file, "up", "-d"],
            check=True,
            env=env,
        )

        # Wait for healthy
        if not _wait_for_healthy(timeout=60):
            logs = subprocess.run(
                ["docker", "compose", "-f", compose_file, "logs"],
                capture_output=True,
                text=True,
                env=env,
            )
            pytest.fail(
                f"Vibe container did not become healthy within 60s.\nSTDOUT:\n{logs.stdout}\nSTDERR:\n{logs.stderr}"
            )
    except Exception:
        # Always clean up on startup failure
        _compose_down(compose_file, env)
        raise

    yield E2E_BASE_URL

    # Teardown (skip if --keep)
    if not KEEP_CONTAINER:
        _compose_down(compose_file, env)


@pytest.fixture(scope="session")
def api_url(vibe_container):
    """Convenience: returns the base URL string."""
    return vibe_container


# =============================================================================
# Platform Integration Driver Fixtures
# Auto-skip when platform tokens are not configured.
# =============================================================================


def _env(key: str) -> str:
    """Get env var or empty string."""
    return os.environ.get(key, "")


def _has_slack_config() -> bool:
    return bool(_env("E2E_SLACK_BOT_B_TOKEN") and _env("E2E_SLACK_CHANNEL") and _env("E2E_SLACK_BOT_A_ID"))


def _has_discord_config() -> bool:
    return bool(_env("E2E_DISCORD_BOT_B_TOKEN") and _env("E2E_DISCORD_CHANNEL") and _env("E2E_DISCORD_BOT_A_ID"))


def _has_feishu_config() -> bool:
    return bool(
        _env("E2E_FEISHU_BOT_B_APP_ID")
        and _env("E2E_FEISHU_BOT_B_APP_SECRET")
        and _env("E2E_FEISHU_CHAT_ID")
        and _env("E2E_FEISHU_BOT_A_ID")
    )


def _platform_enabled(platform: str) -> bool:
    """Check if a specific platform is selected for integration testing.

    If E2E_PLATFORM is not set, all configured platforms are enabled.
    If set (e.g. "slack"), only that platform runs.
    """
    selected = _env("E2E_PLATFORM")
    return not selected or selected.lower() == platform.lower()


@pytest.fixture(scope="session")
def slack_driver():
    """Session-scoped Slack driver. Skips if not configured."""
    if not _has_slack_config():
        pytest.skip("Slack E2E not configured (need E2E_SLACK_BOT_B_TOKEN, E2E_SLACK_CHANNEL, E2E_SLACK_BOT_A_ID)")
    if not _platform_enabled("slack"):
        pytest.skip(f"Slack skipped: E2E_PLATFORM={_env('E2E_PLATFORM')}")

    import asyncio
    from tests.e2e.drivers.slack_driver import SlackDriver

    driver = SlackDriver()
    asyncio.get_event_loop().run_until_complete(driver.setup())
    yield driver
    asyncio.get_event_loop().run_until_complete(driver.teardown())


@pytest.fixture(scope="session")
def discord_driver():
    """Session-scoped Discord driver. Skips if not configured."""
    if not _has_discord_config():
        pytest.skip(
            "Discord E2E not configured (need E2E_DISCORD_BOT_B_TOKEN, E2E_DISCORD_CHANNEL, E2E_DISCORD_BOT_A_ID)"
        )
    if not _platform_enabled("discord"):
        pytest.skip(f"Discord skipped: E2E_PLATFORM={_env('E2E_PLATFORM')}")

    import asyncio
    from tests.e2e.drivers.discord_driver import DiscordDriver

    driver = DiscordDriver()
    asyncio.get_event_loop().run_until_complete(driver.setup())
    yield driver
    asyncio.get_event_loop().run_until_complete(driver.teardown())


@pytest.fixture(scope="session")
def feishu_driver():
    """Session-scoped Feishu driver. Skips if not configured."""
    if not _has_feishu_config():
        pytest.skip("Feishu E2E not configured (need E2E_FEISHU_BOT_B_*, E2E_FEISHU_CHAT_ID, E2E_FEISHU_BOT_A_ID)")
    if not _platform_enabled("feishu"):
        pytest.skip(f"Feishu skipped: E2E_PLATFORM={_env('E2E_PLATFORM')}")

    import asyncio
    from tests.e2e.drivers.feishu_driver import FeishuDriver

    driver = FeishuDriver()
    asyncio.get_event_loop().run_until_complete(driver.setup())
    yield driver
    asyncio.get_event_loop().run_until_complete(driver.teardown())
