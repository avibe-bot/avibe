"""Process-private proof for Memory reads initiated by the local Settings UI."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys

MEMORY_UI_PROOF_HEADER = "X-Avibe-Memory-UI-Proof"
MEMORY_UI_SECRET_STDIN_ENV = "AVIBE_MEMORY_UI_SECRET_STDIN"

_process_secret: str | None = None


def generate_ui_read_secret() -> str:
    return secrets.token_urlsafe(32)


def initialize_process_ui_read_secret() -> str | None:
    """Read the launcher-provided secret once, then remove its inheritance marker."""

    global _process_secret
    if _process_secret is not None:
        return _process_secret
    if os.environ.pop(MEMORY_UI_SECRET_STDIN_ENV, None) != "1":
        return None
    value = sys.stdin.readline().strip()
    if value:
        _process_secret = value
    return _process_secret


def process_ui_read_secret() -> str | None:
    return _process_secret


def build_ui_read_proof(secret: str, *, method: str, path: str, user_key: str) -> str:
    message = _proof_message(method, path, user_key)
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_ui_read_proof(
    secret: str,
    proof: str,
    *,
    method: str,
    path: str,
    user_key: str,
) -> bool:
    if not secret or not proof:
        return False
    expected = build_ui_read_proof(secret, method=method, path=path, user_key=user_key)
    return hmac.compare_digest(expected, proof)


def _proof_message(method: str, path: str, user_key: str) -> bytes:
    return f"memory-ui-read-v1\n{method.upper()}\n{path}\n{user_key}".encode("utf-8")
