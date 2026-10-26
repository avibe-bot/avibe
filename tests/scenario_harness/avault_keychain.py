"""A fake avault for the one-time macOS Keychain authorization contract.

Contract (avault 0.1.8+, avault/docs/DESIGN.md "Keychain authorization"): a call
that needs the user's approval exits 77 (one-shot) or sends
``code: keychain_authorization_required`` (resident agent); only
``avault key authorize`` may show the dialog. Older avault exits 70 for every
failure and has no ``key authorize``. The fake is a POSIX-sh script whose
behavior follows files in its state directory, so no real avault, Keychain, or
``~/.local/bin`` is ever touched. Suites that forbid real processes bind it in
process instead, with the same state and answers.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

_UNAUTHORIZED = "avault: failed to read master key from Keychain: run `avault key authorize` in a logged-in session"


@dataclass(frozen=True)
class FakeAvault:
    script: Path
    state: Path
    version: str
    unauthorized_status: int

    def calls(self) -> list[str]:
        """Every invocation's arguments, without the ``--store`` prefix."""
        path = self.state / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def flag(self, name: str, present: bool = True) -> None:
        """``authorized`` lets it read the master key; ``deny`` makes ``key authorize``
        fail as a refused dialog does; ``hold`` keeps it waiting for ``release``."""
        path = self.state / name
        if present:
            path.touch()
        else:
            path.unlink(missing_ok=True)

    def bind_in_process(self, monkeypatch) -> None:
        """Answer this avault's ``version``, ``pubkey`` and ``key authorize`` without a process.

        Same state and answers as the script, minus ``hold``. Any other command
        still reaches the suite's own ``subprocess.run``, so its guard stands.
        """
        outer = subprocess.run

        def run(argv, *args, **kwargs):
            if not argv or str(argv[0]) != str(self.script):
                return outer(argv, *args, **kwargs)
            command = list(argv[1:])
            if command[:1] == ["--store"]:
                command = command[2:]
            with (self.state / "calls").open("a") as calls:
                calls.write(" ".join(command) + "\n")
            status, stdout, stderr = self._answer(command)
            if not kwargs.get("text"):
                stdout, stderr = stdout.encode(), stderr.encode()
            return subprocess.CompletedProcess(argv, status, stdout, stderr)

        monkeypatch.setattr(subprocess, "run", run)

    def _answer(self, command: list[str]) -> tuple[int, str, str]:
        authorized = (self.state / "authorized").exists()
        if command == ["version"]:
            return 0, f"avault {self.version}\n", ""
        if command == ["pubkey"]:
            if not authorized:
                return self.unauthorized_status, "", _UNAUTHORIZED
            return 0, '{"public_key":"pk","fingerprint":"fp"}', ""
        if command == ["key", "authorize"]:
            if (self.state / "deny").exists():
                return 70, "", "avault: Keychain read failed: User canceled the operation."
            self.flag("authorized")
            return 0, '{"ok":true}', ""
        return 70, "", f"avault: fake has no in-process answer for {' '.join(command)}"


def make_fake_avault(root: Path, *, version: str = "0.1.8", unauthorized_status: int = 77) -> FakeAvault:
    state = root / "avault-state"
    state.mkdir(parents=True)
    script = root / "avault-bin" / "avault"
    script.parent.mkdir(parents=True)
    unauthorized_message = _UNAUTHORIZED.replace("`", "\\`")
    script.write_text(
        f"""#!/bin/sh
STATE="{state}"
[ "$1" = "--store" ] && shift 2
echo "$*" >> "$STATE/calls"
unauthorized() {{
  echo "{unauthorized_message}" >&2
  exit {unauthorized_status}
}}
case "$1" in
  version) echo "avault {version}"; exit 0 ;;
  pubkey)
    [ -f "$STATE/authorized" ] || unauthorized
    echo '{{"public_key":"pk","fingerprint":"fp"}}'; exit 0 ;;
  key)
    if [ "$2" = "authorize" ]; then
      if [ -f "$STATE/hold" ]; then while [ ! -f "$STATE/release" ]; do sleep 0.02; done; fi
      if [ -f "$STATE/deny" ]; then echo "avault: Keychain read failed: User canceled the operation." >&2; exit 70; fi
      touch "$STATE/authorized"; echo '{{"ok":true}}'; exit 0
    fi ;;
  deliver)
    shift 2
    [ "$1" = "--" ] && shift
    cat > /dev/null
    [ -f "$STATE/authorized" ] || unauthorized
    "$@"; exit $? ;;
esac
exit 70
"""
    )
    script.chmod(0o755)
    return FakeAvault(script=script, state=state, version=version, unauthorized_status=unauthorized_status)


def use_fake_avault_on_mac(monkeypatch, root: Path, **options) -> FakeAvault:
    """Point Avibe at a fake avault on a Mac with a desktop session and no attempt in flight."""
    from vibe import api, avault_authorization

    avault = make_fake_avault(root, **options)
    monkeypatch.setattr(api.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(api, "_process_cannot_show_macos_dialogs", lambda: False)
    monkeypatch.setattr(api, "_resolve_avault_cli_path", lambda: str(avault.script))
    monkeypatch.setattr(avault_authorization, "_ATTEMPT", None)
    return avault
