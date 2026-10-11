"""MH-CODEX-LAUNCH-003: the installed Codex launcher's own failure is recognized.

A disposable copy of the installed npm launcher without its platform package is
an incomplete install, the state a failed or interrupted npm update leaves. The
installed CLI is only read. Through the real supervisor and transport, Avibe
must name the repair cause instead of an error that suggests a retry, whichever
launcher version is installed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from modules.agents.codex.transport import CodexTransport
from vibe import backend_model_catalog
from vibe.codex_launch_failure import INSTALL_INCOMPLETE, CodexLaunchError

pytestmark = pytest.mark.e2e_model_hub


@pytest.fixture
def incomplete_codex(tmp_path) -> str:
    binary = shutil.which("codex")
    if binary is None or shutil.which("node") is None:
        pytest.skip("The npm Codex launcher and Node.js are unavailable")
    launcher = Path(binary).resolve()
    package = launcher.parent.parent
    if launcher.name != "codex.js" or not (package / "package.json").is_file():
        pytest.skip("The installed Codex is not the npm launcher")
    copy = tmp_path / "lib" / "node_modules" / "@openai" / "codex"
    (copy / "bin").mkdir(parents=True)
    shutil.copy2(package / "package.json", copy / "package.json")
    shutil.copy2(launcher, copy / "bin" / "codex.js")
    entry = tmp_path / "bin" / "codex"
    entry.parent.mkdir()
    entry.symlink_to(copy / "bin" / "codex.js")
    return str(entry)


def test_incomplete_install_stops_the_catalog_export_with_its_cause(incomplete_codex):
    with pytest.raises(CodexLaunchError) as raised:
        backend_model_catalog._export_codex_bundled_catalog(incomplete_codex)

    assert raised.value.cause == INSTALL_INCOMPLETE


async def test_incomplete_install_stops_the_app_server_with_its_cause(incomplete_codex, tmp_path):
    transport = CodexTransport(binary=incomplete_codex, cwd=str(tmp_path / "work"))

    with pytest.raises(CodexLaunchError) as raised:
        await transport.start()

    assert raised.value.cause == INSTALL_INCOMPLETE
