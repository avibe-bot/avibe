"""Opt-in test against an extracted, verified released Memory runtime archive.

AVIBE_TEST_NATIVE_PYTHON=/path/to/extracted/bin/python pytest -q -s \
    tests/test_memory_native_write_contract.py

No provider credentials, real user data, or external API calls are used.
"""

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from avibe_memory.everos import AddAck, EverOSPort, MemoryProviderFailure, ProviderCapture
from avibe_memory.types import ProviderSessionRef


@pytest.mark.asyncio
async def test_native_slow_ack_response_loss_and_continued_writes():
    """MEMORY-WAKE-206: slow native success and response loss are distinct."""
    python = os.environ.get("AVIBE_TEST_NATIVE_PYTHON")
    if not python:
        pytest.skip("requires a verified extracted native runtime archive")
    # Short path also satisfies macOS's Unix socket path length limit.
    with tempfile.TemporaryDirectory(prefix="mem2020-", dir="/tmp") as directory:
        root = Path(directory)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "EVEROS_ROOT": str(root / "data"),
               "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
               "XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state"),
               "ENV": "prod", "PYTHONNOUSERSITE": "1"}
        helper = Path(__file__).parent / "helpers" / "memory_native_write_server.py"
        with (root / "stderr").open("w+") as output:
            child = subprocess.Popen([python, "-I", str(helper.resolve()), str(root)],
                                     cwd=root, env=env, stdout=output, stderr=output)
            try:
                for _ in range(200):
                    if child.poll() is not None:
                        output.seek(0)
                        pytest.fail(output.read())
                    if (root / "native.sock").exists():
                        break
                    await asyncio.sleep(.05)
                else:
                    pytest.fail("native fixture failed to start")
                ref = ProviderSessionRef("synthetic-owner", 0, "default", "synthetic-session")
                provider = EverOSPort(root / "native.sock")
                started = asyncio.get_running_loop().time()
                ack = await provider.add(ProviderCapture(ref, "slow", 1))
                assert asyncio.get_running_loop().time() - started > 30
                assert isinstance(ack, AddAck) and ack.request_id
                assert (root / "slow.receipt").read_text() == "synthetic committed"
                lossy = EverOSPort(root / "native.sock", add_timeout_seconds=.1)
                with pytest.raises(MemoryProviderFailure) as failure:
                    await lossy.add(ProviderCapture(ref, "lose", 2))
                assert failure.value.ambiguous
                assert (root / "lose.receipt").is_file()
                ack = await provider.add(ProviderCapture(ref, "continued", 3))
                assert isinstance(ack, AddAck) and ack.request_id
                assert child.poll() is None
            finally:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
