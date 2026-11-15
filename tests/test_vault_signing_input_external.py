"""B15: real short-argv CLI, isolated state, legal maximum funding parent.

Helper/parser tests cannot expose ARG_MAX, truncation in normal pending output,
missing persistence or a CLI path that accidentally reaches the owner's daemon.
The only child injection is an explicitly test-only source capability entry and
an opaque public-fixture record. No product service, custody or network is run.
"""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path

from tests.test_vault_bitcoin_ecdsa_context import PUBLIC, _maximal_legacy_case

MAX_OUTPUT = 8 * 1024 * 1024


def _bounded_cli(argv, *, env, cwd):
    """Drain both pipes, with an enforced combined memory budget and deadline."""
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + 30
    with subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ, "stdout")
                selector.register(child.stderr, selectors.EVENT_READ, "stderr")
                total = 0
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    assert remaining > 0, "isolated CLI exceeded 30s"
                    for key, _ in selector.select(min(remaining, 0.2)):
                        chunk = os.read(key.fileobj.fileno(), min(65_536, MAX_OUTPUT - total + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        assert total <= MAX_OUTPUT, "isolated CLI output exceeded bounded capture"
                        output[key.data].extend(chunk)
            status = child.wait(timeout=max(0.01, deadline - time.monotonic()))
        finally:
            if child.poll() is None:
                child.kill()  # Only the child PID started above, never a pattern.
                child.wait(timeout=5)
    assert status == 0, f"isolated CLI failed ({status}): {bytes(output['stderr'][:4096]).decode('utf-8', errors='replace')}"
    return json.loads(output["stdout"])


def test_B15_external_short_argv_maximum_parent_persists_and_reads_back(tmp_path):
    home = tmp_path / "cli-home"
    home.mkdir()
    repo = Path(__file__).resolve().parents[1]
    socket_path = home / "dispatch.sock"
    # Deliberate allowlist, NOT a copy of the Agent/owner process environment.
    env = {
        "PATH": os.defpath, "HOME": str(home), "AVIBE_HOME": str(home / "isolated-instance"),
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CACHE_HOME": str(home / ".cache"), "XDG_STATE_HOME": str(home / ".local/state"),
        "CODEX_HOME": str(home / ".codex"), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "PYTHONPATH": str(repo), "PYTHONDONTWRITEBYTECODE": "1",
        "VIBE_INTERNAL_DISPATCH_SOCKET": str(socket_path),
    }
    runner = tmp_path / "isolated_cli.py"
    runner.write_text('''import os
import socket
from pathlib import Path

# A safety tripwire, not a fake RPC: no TCP/UDP/Internet may be reached.
_original_socket = socket.socket
class LocalSocket(_original_socket):
    def __init__(self, family=socket.AF_INET, *args, **kwargs):
        if family != socket.AF_UNIX:
            raise AssertionError("external network forbidden in isolated CLI")
        super().__init__(family, *args, **kwargs)
socket.socket = LocalSocket

from config import paths
from storage import vault_service
from storage.vault_crypto import Sealed
from vibe import cli, internal_client, vault_sandbox_release as release
home = Path(os.environ["HOME"]).resolve()
for path in (paths.get_vibe_remote_dir(), paths.get_config_dir(), paths.get_state_dir(), paths.get_sqlite_state_path()):
    assert path.resolve().is_relative_to(home)
assert internal_client.default_socket_path() == home / "dispatch.sock"
# Capability is fixture-only; the real packaged pin remains unmodified.
current = release.current_support()
assert current is not None and current.ordinary_input_types == ("p2tr",)
release.CAPABILITIES_BY_PIN_ID = {current.pin_id: {
    "ordinary_input_types": ("p2pkh", "p2wpkh", "p2tr"),
    "hd_input_types": (), "ecdsa_return_types": ("p2pkh", "p2wpkh", "p2tr"),
}}
with cli._open_vault_engine().begin() as conn:
    try:
        vault_service.get_secret_meta(conn, "PUBLIC_TEST_BTC")
    except vault_service.SecretNotFoundError:
        vault_service.create_secret(conn, name="PUBLIC_TEST_BTC", kind="keypair", protection="protected", signer_kind="local",
            sealed=Sealed("public-test-only-opaque", "public-test-only-nonce", "public-test-only-wrap"),
            public_meta={"signing_public_key": {"curve": "secp256k1", "public_key": PUBLIC_POINT}})
# Actual public parser/admission/command/engine/request/release/output path.
cli.main()
'''.replace("PUBLIC_POINT", repr(PUBLIC.hex())), encoding="utf-8")
    context = _maximal_legacy_case()
    assert len(bytes.fromhex(context["prevout"]["parentTransaction"])) == 1_048_576
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps(context, separators=(",", ":")), encoding="utf-8")
    argv = [sys.executable, str(runner), "vault", "sign", "PUBLIC_TEST_BTC",
            "--scheme", "ecdsa-secp256k1-der", "--digest", context["digest"],
            "--signing-context-file", str(context_path), "--no-callback"]
    assert sum(len(item.encode()) + 1 for item in argv) < 4096
    proof_prefix = context["prevout"]["parentTransaction"][:256]
    assert all(proof_prefix not in value for value in [*argv, *env.values()])
    created = _bounded_cli(argv, env=env, cwd=tmp_path)
    assert created["ok"] is True and created["kind"] == "vault_sign_request"
    request_id = created["request_id"]
    assert created["request"]["status"] == "pending"
    # A separate real CLI process must retrieve the exact persisted full request,
    # not the original in-memory context or a summary reconstructed from the file.
    context_path.unlink()
    readback = _bounded_cli([sys.executable, str(runner), "vault", "await", request_id], env=env, cwd=tmp_path)
    assert readback["ok"] is True and readback["kind"] == "vault_request_status"
    request = readback["request"]
    assert request["id"] == request_id and request["status"] == "pending"
    assert request["delivery"] == created["request"]["delivery"]
    assert request["delivery"]["signing_context"] == context
    assert request["delivery"]["operation_context"]["release"]["signingContext"] == context
    assert request["delivery"]["signing_key"]["public_key"] == PUBLIC.hex()
    assert request["requester"]["callback_disabled"] is True
    assert (home / "isolated-instance/state/vibe.sqlite").is_file()
    assert not socket_path.exists()  # No runtime/socket was created or started.
