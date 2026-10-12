"""A helper script that runs as the version its launcher imported.

A helper started by its installed path runs whatever that path holds when it starts. After an upgrade
replaces the installed files and before the service restarts, that is the newer version, whose protocol
with the running launcher may differ: when ``job_wrapper.py``'s argv changed, every command failed until
the restart. A ``FrozenScript`` reads the script, and the files it loads from its own directory, when
the launcher's module is imported; the launcher then starts a copy written from those bytes.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets


class FrozenScript:
    """``script``, with ``beside`` (the files it loads from its own directory), as they are now."""

    def __init__(self, script: str, *beside: str) -> None:
        self._name = os.path.basename(script)
        self._files: dict[str, bytes] = {}
        for path in (script, *beside):
            with open(path, "rb") as handle:
                self._files[os.path.basename(path)] = handle.read()
        digest = hashlib.sha256()
        for name, data in sorted(self._files.items()):
            digest.update(b"%s\0%d\0" % (name.encode(), len(data)) + data)
        self._digest = digest.hexdigest()[:16]

    def path(self, root: str) -> str:
        """The script in its copy under ``root``, written first where any of the copy is missing.

        The copy is named by its content (``<root>/<digest>/``): launchers of the same bytes share it, and an
        older and a newer launcher never start each other's. Each file is renamed into place whole, so no
        launcher starts one half-written; on Windows a rename never replaces, so a helper that is reading a
        file is never in the way of a second launcher.
        """
        directory = os.path.join(root, self._digest)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        for name, data in self._files.items():
            target = os.path.join(directory, name)
            if os.path.exists(target):
                continue
            tmp = f"{target}.{secrets.token_hex(4)}.tmp"
            try:
                with open(tmp, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    # Durable before it is named: an empty copy left by a power loss would never be rewritten.
                    os.fsync(handle.fileno())
                os.rename(tmp, target)  # POSIX replaces the same bytes; Windows raises FileExistsError
            except FileExistsError:
                pass
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(tmp)
        return os.path.join(directory, self._name)
