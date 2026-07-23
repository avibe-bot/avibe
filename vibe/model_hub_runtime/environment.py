from __future__ import annotations

import os
from typing import Mapping


_ENGINE_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "USERPROFILE",
    }
)


def engine_subprocess_environment(
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if base_env is None else base_env
    return {name: source[name] for name in _ENGINE_ENV_ALLOWLIST if name in source}
