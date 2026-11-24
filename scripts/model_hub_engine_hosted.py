"""Dedicated avault store and immutable Git data helpers for the hosted channel."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import model_hub_engine_channel as producer  # noqa: E402
from core import model_hub_engine_channel as channel  # noqa: E402

LEDGER = "patches/model-hub-engine-channel-ledger"
ROOT = "vibe/model_hub_runtime/engine_channel_roots.json"
KEY_NAME = "MODEL_HUB_ENGINE_CHANNEL"
Error = producer.ProducerError


def avault_run(binary, args, home, *, data=b""):
    env = {"HOME": str(home), "AVAULT_HOME": str(home), "AVAULT_STORE": "file", "PATH": os.defpath}
    proc = subprocess.run([str(binary), *args], input=data, capture_output=True, env=env, timeout=30)
    if proc.returncode:
        raise Error("dedicated avault operation failed")  # never echo secret-bearing diagnostics
    return proc.stdout


def generate_bundle(binary: Path, output: Path) -> str:
    """Owner-only helper: keep the user's actual machine store entirely out of scope."""
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary)
        generated = json.loads(avault_run(binary, ["keypair", "generate", "--name", KEY_NAME, "--curve", "secp256k1"], home))
        phrase = secrets.token_urlsafe(48)
        wrapped = json.loads(avault_run(binary, ["key", "export"], home, data=phrase.encode()))
        bundle = {"import": {"passphrase": phrase, "blob": wrapped}, "envelope": generated["envelope"]}
        # The bundle IS a signing credential. Never print it, even on failure.
        with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as dest:
            json.dump(bundle, dest, separators=(",", ":"))
        return generated["public_key"]


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL)


def file_at(repo, sha, path):
    return git(repo, "show", f"{sha}:{path}")


def check_published(published, allocated, parent, roots, verifier):
    if published is None:
        if parent is not None:
            raise Error("missing published predecessor")
        return
    envelope, _ = channel.authenticate(published, roots=roots, verifier=verifier)
    if envelope.payload not in (allocated, parent):
        raise Error("published object diverges from current allocation or its exact predecessor")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate",))
    parser.add_argument("--avault", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(generate_bundle(args.avault.resolve(strict=True), args.output))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        sys.exit(f"hosted channel action refused ({type(exc).__name__})")  # no credential-bearing diagnostics
