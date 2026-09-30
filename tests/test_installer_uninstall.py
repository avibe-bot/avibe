"""The installers own uninstall (#2239).

`install.sh --uninstall` and `install.ps1 -Uninstall` cannot rely on the
installed Python, which may come from any earlier release or be broken, so each
carries its own copy of the launcher rule in
``vibe.upgrade.managed_stable_launchers``. The shared cases pin both copies to
the Python rule; the rest exercise the POSIX owner end to end. The Windows owner
runs end to end in the windows-install-smoke CI job.
"""

from __future__ import annotations

import os
import select
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.test_install_generation_retirement import candidate
from vibe import upgrade


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "install.sh"
INSTALL_POWERSHELL = REPO_ROOT / "install.ps1"
WINDOWS = os.name == "nt"
LAUNCHER = "vibe.exe" if WINDOWS else "vibe"
MARKER = f".{LAUNCHER}.avibe-generation"
SYSTEM_PATH = (
    [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")] if WINDOWS else ["/usr/bin", "/bin"]
)


@dataclass
class Layout:
    tmp: Path
    home: Path
    avibe_home: Path
    root: Path
    current: Path
    older: Path
    path_dirs: list[Path]
    uv_tool_bin: Path
    installer_dir: Path
    elsewhere: Path

    @property
    def search_path(self) -> str:
        return os.pathsep.join([*map(str, self.path_dirs), *SYSTEM_PATH])

    def env(self, **extra: str) -> dict[str, str]:
        # HOME alone selects the default home, as for most users.
        return {
            "HOME": str(self.home),
            "PATH": self.search_path,
            "UV_TOOL_BIN_DIR": str(self.uv_tool_bin),
            "LC_ALL": "C",
            **extra,
        }


@pytest.fixture
def layout(tmp_path):
    home = tmp_path / "home"
    avibe_home = home / ".avibe"
    root = avibe_home / "runtime" / "install-generations"
    dirs = [tmp_path / name for name in ("path-a", "path-b", "path-c", "uv-bin", "installer-bin", "elsewhere")]
    for directory in dirs:
        directory.mkdir(parents=True)
    (avibe_home / "config").mkdir(parents=True)
    (avibe_home / "config" / "config.json").write_text("{}\n", encoding="utf-8")
    (avibe_home / "state").mkdir()
    (avibe_home / "state" / "vibe.sqlite").write_bytes(b"user data")
    return Layout(
        tmp=tmp_path,
        home=home,
        avibe_home=avibe_home,
        root=root,
        current=candidate(root, "current", export_name=LAUNCHER, copy_export=WINDOWS),
        older=candidate(root, "older", export_name=LAUNCHER, copy_export=WINDOWS),
        path_dirs=dirs[:3],
        uv_tool_bin=dirs[3],
        installer_dir=dirs[4],
        elsewhere=dirs[5],
    )


def _foreign(path: Path, text: str = "#!/bin/sh\necho foreign\n") -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _copy(path: Path, exported: Path, *, marker: bool) -> Path:
    shutil.copy2(exported.resolve(), path)
    if marker:
        (path.parent / MARKER).write_text(str(exported.parent.parent.resolve()), encoding="utf-8")
    return path


def _symlink(path: Path, target: Path | str) -> Path:
    try:
        path.symlink_to(target)
    except OSError as error:
        if not WINDOWS:
            raise
        pytest.skip(f"this Windows session cannot create symbolic links: {error}")
    return path


def _hardlink(path: Path, exported: Path) -> Path:
    os.link(exported.resolve(), path)
    return path


def _replaced_copy(path: Path, exported: Path) -> Path:
    """A launcher someone replaced after Avibe copied it and wrote its marker."""
    _copy(path, exported, marker=True)
    path.unlink()
    return _foreign(path)


def _searched(directory: Path, layout: Layout) -> Path:
    directory.mkdir(parents=True)
    layout.path_dirs.append(directory)
    return directory


def _other_home(layout: Layout) -> Path:
    root = layout.tmp / "other" / ".avibe" / "runtime" / "install-generations"
    return candidate(root, "g", export_name=LAUNCHER, copy_export=WINDOWS)


def _unrecognized_generation(layout: Layout) -> Path:
    target = layout.root / "partial" / "bin" / LAUNCHER
    target.parent.mkdir(parents=True)
    return _foreign(target, "#!/bin/sh\n# partial\n")


SHARED_CASES = {
    # Shapes the Python owner manages; the uninstall removes each of them.
    "symlink": (lambda l: _symlink(l.path_dirs[0] / LAUNCHER, l.current), True, True),
    "relative-symlink": (
        lambda l: _symlink(l.path_dirs[0] / LAUNCHER, os.path.relpath(l.current, l.path_dirs[0])), True, True,
    ),
    "chained-symlink": (
        lambda l: _symlink(l.path_dirs[0] / LAUNCHER, _symlink(l.elsewhere / LAUNCHER, l.older)), True, True,
    ),
    "hardlink": (lambda l: _hardlink(l.path_dirs[0] / LAUNCHER, l.current), True, True),
    "marked-copy": (lambda l: _copy(l.path_dirs[0] / LAUNCHER, l.current, marker=True), True, True),
    "found-through-uv-tool-bin": (lambda l: _symlink(l.uv_tool_bin / LAUNCHER, l.current), True, True),
    "found-through-installer-dir": (lambda l: _symlink(l.installer_dir / LAUNCHER, l.older), True, True),
    # The Python owner will not move these, but deleting the root breaks them.
    "unmarked-copy": (lambda l: _copy(l.path_dirs[0] / LAUNCHER, l.current, marker=False), False, True),
    "dangling-into-root": (
        lambda l: _symlink(l.path_dirs[0] / LAUNCHER, l.root / "deleted" / "bin" / LAUNCHER), False, True,
    ),
    "unrecognized-generation": (
        lambda l: _symlink(l.path_dirs[0] / LAUNCHER, _unrecognized_generation(l)), False, True,
    ),
    # Nobody's to remove.
    "foreign-file": (lambda l: _foreign(l.path_dirs[0] / LAUNCHER), False, False),
    "foreign-symlink": (
        lambda l: _symlink(l.path_dirs[0] / LAUNCHER, _foreign(l.elsewhere / "other-vibe")), False, False,
    ),
    "copy-whose-marker-is-stale": (lambda l: _replaced_copy(l.path_dirs[0] / LAUNCHER, l.current), False, False),
    "another-homes-generation": (lambda l: _symlink(l.path_dirs[0] / LAUNCHER, _other_home(l)), False, False),
    "inside-a-uv-tool-environment": (
        lambda l: _symlink(_searched(l.tmp / "uv" / "tools" / "x" / "bin", l) / LAUNCHER, l.current), False, False,
    ),
    "not-searched": (lambda l: _symlink(l.elsewhere / LAUNCHER, l.current), False, False),
}


def _python_rule(layout: Layout, monkeypatch) -> set[str]:
    monkeypatch.setenv("AVIBE_HOME", str(layout.avibe_home))
    monkeypatch.setenv("PATH", layout.search_path)
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(layout.uv_tool_bin))
    monkeypatch.delenv(upgrade.CURRENT_VIBE_EXECUTABLE_ENV, raising=False)
    monkeypatch.setattr(upgrade, "INSTALLER_LAUNCHER_DIRS", (str(layout.installer_dir),))
    return {os.path.normcase(str(launcher)) for launcher, _ in upgrade.managed_stable_launchers()}


def _installer_shell(layout: Layout, command: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Run install.sh functions against the layout alone, never the host's directories."""
    source = INSTALL_SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0]
    script = f"{source}\nINSTALLER_LAUNCHER_DIRS=({shlex.quote(str(layout.installer_dir))})\n{command}\n"
    return subprocess.run(
        ["bash", "-c", script],
        cwd=layout.tmp,
        env=layout.env(**env),
        capture_output=True,
        text=True,
        timeout=60,
        # No controlling terminal, as under CI or an unattended pipe.
        start_new_session=True,
    )


def _powershell(layout: Layout, shell: str, command: str, **env: str) -> list[str]:
    """Run install.ps1 functions against the layout; the owner's own flow runs in CI."""
    source = INSTALL_POWERSHELL.read_text(encoding="utf-8").split("\n# Each option means one thing", 1)[0]
    script = layout.tmp / "installer-functions.ps1"
    installer_dir = str(layout.installer_dir).replace("'", "''")
    script.write_text(
        f"{source}\n$INSTALLER_LAUNCHER_DIRS = @('{installer_dir}')\n{command}\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        # A shell started from Python keeps PowerShell 7's module path, which
        # Windows PowerShell cannot load; a user's own session has its default.
        env={
            **{name: value for name, value in os.environ.items() if name.upper() != "PSMODULEPATH"},
            **layout.env(USERPROFILE=str(layout.home), AVIBE_HOME=str(layout.avibe_home), APPDATA=str(layout.tmp)),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _powershell_rule(layout: Layout, shell: str) -> set[str]:
    launchers = _powershell(layout, shell, "Get-ManagedLaunchers (Join-Path (Get-RuntimeHome) 'runtime\\install-generations')")
    return {os.path.normcase(launcher) for launcher in launchers}


def _rule_implementations() -> list:
    if not WINDOWS:
        return ["install.sh"]
    # Resolved now: each case replaces PATH with its layout's before the shell runs.
    return [pytest.param(shutil.which(shell), id=shell) for shell in ("pwsh", "powershell") if shutil.which(shell)]


@pytest.mark.parametrize("implementation", _rule_implementations())
@pytest.mark.parametrize("case", SHARED_CASES)
def test_installer_launcher_rule_matches_the_python_owner(layout, monkeypatch, implementation, case):
    build, python_manages, uninstall_removes = SHARED_CASES[case]
    launcher = os.path.normcase(str(build(layout)))
    # A launcher the Python owner manages is always one the uninstall removes.
    assert not python_manages or uninstall_removes

    python_managed = _python_rule(layout, monkeypatch)
    if implementation == "install.sh":
        result = _installer_shell(layout, "managed_launchers")
        assert result.returncode == 0, result.stdout + result.stderr
        removed = set(result.stdout.splitlines())
    else:
        removed = _powershell_rule(layout, implementation)

    assert python_managed == ({launcher} if python_manages else set())
    assert removed == ({launcher} if uninstall_removes else set())


def test_installer_launcher_dirs_match_the_python_owner():
    """The shared cases replace these lists, so pin the shipped ones directly."""
    listed = ", ".join(f'"{directory}"' for directory in upgrade.INSTALLER_LAUNCHER_DIRS)
    assert f"$INSTALLER_LAUNCHER_DIRS = @({listed})" in INSTALL_POWERSHELL.read_text(encoding="utf-8")
    if WINDOWS:
        return
    source = INSTALL_SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0]
    result = subprocess.run(
        ["bash", "-c", f'{source}\nprintf "%s\\n" "${{INSTALLER_LAUNCHER_DIRS[@]}}"'],
        env={"HOME": "/home/u", "PATH": os.pathsep.join(SYSTEM_PATH)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [
        directory.replace("~", "/home/u", 1) for directory in upgrade.INSTALLER_LAUNCHER_DIRS
    ]


@pytest.mark.skipif(not WINDOWS, reason="Windows path roots")
@pytest.mark.parametrize("shell", _rule_implementations())
def test_powershell_purge_guard_refuses_roots_and_the_profile(layout, shell):
    profile = str(layout.home)
    unsafe = ["C:\\", "\\\\server\\share", "\\\\server\\share\\", profile, str(layout.home.parent)]
    safe = [str(layout.avibe_home), "\\\\server\\share\\avibe"]
    listed = ", ".join("'" + path.replace("'", "''") + "'" for path in unsafe + safe)

    verdicts = _powershell(layout, shell, f"foreach ($path in @({listed})) {{ Test-PathHoldsHome $path }}")

    assert verdicts == ["True"] * len(unsafe) + ["False"] * len(safe)


posix_only = pytest.mark.skipif(WINDOWS, reason="the Windows owner runs end to end in windows-install-smoke")


def _stop_logging_generation(layout: Layout, exported: Path) -> None:
    """Make a generation's launcher record each stop and what still existed then."""
    exported.resolve().write_text(
        "#!/bin/sh\n"
        f"# generation {exported.parent.parent.name}\n"
        'if [ "$1" = stop ]; then\n'
        '  state=""\n'
        '  for path in $VIBE_TEST_WATCH; do [ -e "$path" ] && state="$state+" || state="$state-"; done\n'
        '  printf "stop %s %s %s\\n" "$0" "$AVIBE_HOME" "$state" >> "$VIBE_TEST_LOG"\n'
        '  exit "${VIBE_TEST_STOP_STATUS:-0}"\n'
        "fi\n",
        encoding="utf-8",
    )


@pytest.fixture
def installed(layout):
    """Two managed launchers, one foreign launcher, and the user's data."""
    for exported in (layout.current, layout.older):
        _stop_logging_generation(layout, exported)
    first = _symlink(layout.path_dirs[0] / LAUNCHER, layout.current)
    (first.parent / MARKER).write_text(str(layout.current.parent.parent.resolve()), encoding="utf-8")
    # A root host's second launcher: the installer's fixed location, copied.
    second = _copy(layout.installer_dir / LAUNCHER, layout.current, marker=True)
    foreign = _foreign(layout.path_dirs[1] / LAUNCHER)
    # A marker naming a generation this home deletes, beside a launcher that
    # has since been replaced by something else.
    (foreign.parent / MARKER).write_text(str(layout.older.parent.parent.resolve()), encoding="utf-8")
    legacy_home = _symlink(layout.home / ".vibe_remote", layout.avibe_home)
    log = layout.tmp / "calls.log"
    watch = " ".join(str(path) for path in (layout.root, first, second))
    return layout, first, second, foreign, legacy_home, {"VIBE_TEST_LOG": str(log), "VIBE_TEST_WATCH": watch}


def _calls(layout: Layout) -> list[str]:
    log = layout.tmp / "calls.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _assert_untouched(layout: Layout, first: Path, second: Path, foreign: Path, legacy_home: Path) -> None:
    assert first.is_symlink() and second.is_file() and foreign.is_file()
    assert (first.parent / MARKER).is_file() and (foreign.parent / MARKER).is_file()
    assert {path.name for path in layout.root.iterdir()} == {"current", "older"}
    assert (layout.avibe_home / "state" / "vibe.sqlite").read_bytes() == b"user data"
    assert legacy_home.is_symlink()


@posix_only
def test_uninstall_removes_avibe_and_keeps_user_data(installed):
    layout, first, second, foreign, legacy_home, env = installed
    foreign_bytes = foreign.read_bytes()

    result = _installer_shell(layout, "main --uninstall", **env)

    assert result.returncode == 0, result.stdout + result.stderr
    # One stop, through a managed launcher, while every file was still there.
    assert _calls(layout) == [f"stop {first} {layout.avibe_home} +++"]
    for removed in (first, second, first.parent / MARKER, second.parent / MARKER, foreign.parent / MARKER):
        assert not removed.exists() and not removed.is_symlink()
    assert not layout.root.exists()
    assert foreign.read_bytes() == foreign_bytes
    assert (layout.avibe_home / "config" / "config.json").is_file()
    assert (layout.avibe_home / "state" / "vibe.sqlite").read_bytes() == b"user data"
    assert legacy_home.is_symlink()
    assert f"Another vibe command remains at {foreign}" in result.stdout
    assert "Avibe was removed." in result.stdout
    kept = result.stdout.split("Your data was kept in:\n", 1)[1]
    assert kept.startswith(f"  {layout.avibe_home}\n  {legacy_home} -> {layout.avibe_home}\n")
    assert "  bash -o pipefail -c 'curl -fsSL https://avibe.bot/install.sh | bash -s -- --uninstall --purge'\n" in kept


@posix_only
def test_a_purge_without_a_terminal_needs_yes_and_deletes_nothing(installed):
    layout, first, second, foreign, legacy_home, env = installed

    result = _installer_shell(layout, "main --uninstall --purge", **env)

    assert result.returncode == 1
    listed = result.stdout.split("This permanently deletes:", 1)[1]
    for path in (first, second, layout.root, layout.avibe_home, legacy_home):
        assert f"  {path}" in listed
    assert "bash -s -- --uninstall --purge --yes'" in result.stdout
    assert "Nothing was removed." in result.stdout
    assert _calls(layout) == []
    _assert_untouched(layout, first, second, foreign, legacy_home)


@posix_only
def test_a_confirmed_purge_lists_then_deletes_the_data(installed):
    layout, first, second, foreign, legacy_home, env = installed

    result = _installer_shell(layout, "main --uninstall --purge --yes", **env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.index("This permanently deletes:") < result.stdout.index("Removed")
    assert not layout.avibe_home.exists() and not legacy_home.is_symlink()
    assert not first.is_symlink() and not second.exists()
    assert foreign.is_file()
    assert "Avibe and its data were removed." in result.stdout
    assert "Your data was kept" not in result.stdout


def _read_terminal(terminal: int, *, until: bytes | None = None, timeout: float = 30) -> bytes:
    output = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and (until is None or until not in output):
        if not select.select([terminal], [], [], 0.1)[0]:
            continue
        try:
            chunk = os.read(terminal, 4096)
        except OSError:  # the child closed the terminal
            break
        if not chunk:
            break
        output += chunk
    return output


@posix_only
@pytest.mark.parametrize("answer, confirmed", [("n", False), ("", False), ("y", True)])
def test_a_purge_piped_into_bash_confirms_on_the_terminal(installed, answer, confirmed):
    """`curl ... | bash` feeds the script through stdin, so the answer comes from /dev/tty."""
    import pty

    layout, first, second, foreign, legacy_home, env = installed
    source = INSTALL_SCRIPT.read_text(encoding="utf-8").rsplit('\nmain "$@"', 1)[0]
    piped = layout.tmp / "piped-install.sh"
    piped.write_text(
        f'{source}\nINSTALLER_LAUNCHER_DIRS=({shlex.quote(str(layout.installer_dir))})\nmain "$@"\n', encoding="utf-8"
    )
    bash = shutil.which("bash", path=layout.search_path)
    child, terminal = pty.fork()
    if child == 0:  # pragma: no cover - replaced by exec
        os.execve(bash, [bash, "-c", f"bash -s -- --uninstall --purge < {shlex.quote(str(piped))}"], layout.env(**env))
    output = _read_terminal(terminal, until=b"[y/N]")
    os.write(terminal, f"{answer}\n".encode())
    output += _read_terminal(terminal)
    _, status = os.waitpid(child, 0)
    text = output.decode(errors="replace")

    assert b"[y/N]" in output, text
    if confirmed:
        assert os.waitstatus_to_exitcode(status) == 0, text
        assert not layout.avibe_home.exists() and not layout.root.exists()
    else:
        assert os.waitstatus_to_exitcode(status) == 1, text
        assert "Purge cancelled. Nothing was removed." in text
        _assert_untouched(layout, first, second, foreign, legacy_home)


@posix_only
@pytest.mark.parametrize(
    "shape, options, stop_status, blocks",
    [
        ("connector-in-install-generations", "--uninstall", "0", True),
        ("child-exiting-after-a-clean-stop", "--uninstall", "0", False),
        ("mentions-an-unrelated-path", "--uninstall", "0", False),
        ("connector-in-the-kept-home", "--uninstall", "0", False),
        ("connector-in-the-purged-home", "--uninstall --purge --yes", "0", True),
        ("connector-in-the-home-after-a-failed-stop", "--uninstall", "1", True),
        ("nothing-left-after-a-failed-stop", "--uninstall", "1", False),
    ],
)
def test_uninstall_waits_for_any_process_using_what_it_deletes(installed, shape, options, stop_status, blocks):
    """No process may run from, or name, what goes; without a confirmed stop, the home counts too."""
    layout, first, second, foreign, legacy_home, env = installed
    unrelated = layout.tmp / "unrelated" / "runtime" / "install-generations"
    command, seconds = {
        "connector-in-install-generations": (f"{layout.root}/current/bin/cloudflared tunnel run", 60),
        "child-exiting-after-a-clean-stop": (f"{layout.root}/current/bin/python -m vibe.log_sink", 2),
        "mentions-an-unrelated-path": (f"harmless --config={unrelated}/current", 60),
        "connector-in-the-kept-home": (f"{layout.avibe_home}/bin/cloudflared tunnel run", 60),
        "connector-in-the-purged-home": (f"{layout.avibe_home}/bin/cloudflared tunnel run", 60),
        "connector-in-the-home-after-a-failed-stop": (f"{layout.avibe_home}/bin/cloudflared tunnel run", 60),
        "nothing-left-after-a-failed-stop": ("harmless", 60),
    }[shape]
    process = subprocess.Popen(["bash", "-c", f"exec -a {shlex.quote(command)} sleep {seconds}"])
    try:
        result = _installer_shell(layout, f"main {options}", VIBE_TEST_STOP_STATUS=stop_status, **env)
    finally:
        process.kill()
        process.wait()

    # A failed stop asks every managed launcher; a clean one stops at the first.
    assert len(_calls(layout)) == (1 if stop_status == "0" else 2)
    if blocks:
        assert result.returncode == 1
        assert f"  pid {process.pid} {command} {seconds}" in result.stdout
        assert "Stop them, then run the uninstall again." in result.stdout
        _assert_untouched(layout, first, second, foreign, legacy_home)
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert not layout.root.exists() and not first.is_symlink()
    if stop_status == "0":
        assert "Stopped the Avibe service" in result.stdout
    else:
        assert "The installed vibe could not stop the service." in result.stdout
        assert "such as a managed OpenCode server, could not be confirmed stopped" in result.stdout


@posix_only
def test_uninstall_removes_legacy_uv_tools_unless_uv_would_delete_a_foreign_launcher(layout):
    tool_dir = layout.tmp / "uv-tools"
    legacy_bin = layout.path_dirs[2]
    log = layout.tmp / "calls.log"
    current = tool_dir / "avibe-os"
    (current / "bin").mkdir(parents=True)
    _stop_logging_generation(layout, _foreign(current / "bin" / "vibe"))
    own = _symlink(legacy_bin / "vibe", current / "bin" / "vibe")
    (current / "uv-receipt.toml").write_text(f'[tool]\nentrypoints = [{{ name = "vibe", install-path = "{own}" }}]\n')
    legacy = tool_dir / "vibe-remote"
    legacy.mkdir()
    foreign = _foreign(layout.path_dirs[1] / "vibe")
    (legacy / "uv-receipt.toml").write_text(f'[tool]\nentrypoints = [{{ name = "vibe", install-path = "{foreign}" }}]\n')
    # uv removes the tool environment and every launcher its receipt records.
    _foreign(
        layout.path_dirs[0] / "uv",
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        f'  "tool dir") echo {shlex.quote(str(tool_dir))} ;;\n'
        f'  "tool uninstall") echo "uv uninstall $3" >> {shlex.quote(str(log))}\n'
        f'    rm -rf {shlex.quote(str(tool_dir))}/"$3" {shlex.quote(str(own))} ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n",
    )

    result = _installer_shell(layout, "main --uninstall", VIBE_TEST_LOG=str(log), VIBE_TEST_WATCH="")

    assert result.returncode == 1
    # With no managed launcher left, the legacy install's own launcher stops Avibe.
    assert _calls(layout) == [f"stop {current / 'bin' / 'vibe'} {layout.avibe_home} ", "uv uninstall avibe-os"]
    assert not current.exists() and not own.is_symlink()
    assert legacy.is_dir() and foreign.is_file()
    assert "Left the uv tool install vibe-remote in place" in result.stdout
    assert "Avibe was not completely removed." in result.stdout


@posix_only
def test_uninstall_without_an_installation_says_so(layout):
    shutil.rmtree(layout.root)

    result = _installer_shell(layout, "main --uninstall")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "No Avibe installation was found, so nothing was removed." in result.stdout
    assert "Avibe was removed." not in result.stdout
    assert f"Your data was kept in:\n  {layout.avibe_home}\n" in result.stdout


@posix_only
@pytest.mark.parametrize(
    "options, message",
    [
        ("--purge", "use it with --uninstall"),
        ("--uninstall --yes", "use it with --uninstall --purge"),
        ("--uninstall --launch", "cannot be combined"),
    ],
)
def test_uninstall_options_never_imply_one_another(installed, options, message):
    layout, first, second, foreign, legacy_home, env = installed

    result = _installer_shell(layout, f"main {options}", **env)

    assert result.returncode == 1
    assert message in result.stdout
    assert _calls(layout) == []
    _assert_untouched(layout, first, second, foreign, legacy_home)


@posix_only
@pytest.mark.parametrize(
    "home, reason",
    [("tmp", "it holds your home directory"), ("project", "it does not look like an Avibe home")],
)
def test_a_purge_refuses_a_home_that_is_not_safe_to_delete(installed, home, reason):
    layout, first, second, foreign, legacy_home, env = installed
    target = layout.tmp if home == "tmp" else layout.tmp / "project"
    (target / "runtime" if home == "tmp" else target / "config").mkdir(parents=True)

    result = _installer_shell(layout, "main --uninstall --purge --yes", **{**env, "AVIBE_HOME": str(target)})

    assert result.returncode == 1
    assert f"Refusing to purge {target}" in result.stdout and reason in result.stdout
    assert target.is_dir() and _calls(layout) == []
    _assert_untouched(layout, first, second, foreign, legacy_home)


def _linked_home(layout: Layout, shape: str, *, windows: bool) -> tuple[Path, list[Path], list[Path]]:
    """Build a home reached through links; return the profile, the links, and what they name."""
    profile = layout.home
    shutil.rmtree(layout.avibe_home)
    (profile / "keep.txt").write_text("the user's own file", encoding="utf-8")
    if shape == "chain":
        # ~/.avibe reaches the data through a link to a link.
        data = layout.tmp / "data" / "avibe"
        (data / "runtime").mkdir(parents=True)
        (data / "state").mkdir()
        hop = _symlink(layout.tmp / "hop", data)
        if windows:
            import _winapi

            _winapi.CreateJunction(str(hop), str(layout.avibe_home))
        else:
            _symlink(layout.avibe_home, hop)
        legacy = _symlink(profile / ".vibe_remote", layout.avibe_home)
        return profile, [layout.avibe_home, legacy], [data]
    # The profile itself is reached through a link, and ~/.vibe_remote names it.
    linked_profile = layout.tmp / "profile-link"
    if windows:
        import _winapi

        _winapi.CreateJunction(str(profile), str(linked_profile))
    else:
        _symlink(linked_profile, profile)
    legacy = _symlink(profile / ".vibe_remote", linked_profile)
    return linked_profile, [legacy], [profile]


@posix_only
@pytest.mark.parametrize("shape", ["chain", "link-to-the-profile"])
def test_a_purge_removes_links_and_never_what_they_name(layout, shape):
    profile, links, targets = _linked_home(layout, shape, windows=False)

    result = _installer_shell(layout, "main --uninstall --purge --yes", HOME=str(profile))

    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(link.is_symlink() for link in links)
    assert all(target.is_dir() for target in targets)
    assert (layout.home / "keep.txt").read_text(encoding="utf-8") == "the user's own file"
    assert "It removes these links, not what they point to:" in result.stdout
    assert "pointed to. Delete it by hand if it is yours." in result.stdout


@pytest.mark.skipif(not WINDOWS, reason="junctions")
@pytest.mark.parametrize("shell", _rule_implementations())
@pytest.mark.parametrize("shape", ["chain", "link-to-the-profile"])
def test_powershell_purge_removes_links_and_never_what_they_name(layout, shell, shape):
    profile, links, targets = _linked_home(layout, shape, windows=True)

    _powershell(
        layout, shell, "$Purge = $true; $Yes = $true; Uninstall-Avibe | Out-Null", USERPROFILE=str(profile), AVIBE_HOME=""
    )

    assert not any(os.path.lexists(link) for link in links)
    assert all(target.is_dir() for target in targets)
    assert (layout.home / "keep.txt").read_text(encoding="utf-8") == "the user's own file"


@pytest.mark.skipif(not WINDOWS, reason="Windows process table")
@pytest.mark.parametrize("shell", _rule_implementations())
def test_powershell_waits_for_a_process_using_what_it_deletes(layout, shell):
    unrelated = layout.tmp / "unrelated" / "runtime" / "install-generations"
    sleeper = "import time; time.sleep(120)"
    using = subprocess.Popen([sys.executable, "-c", sleeper, str(layout.root / "current" / "bin" / "cloudflared.exe")])
    mentioning = subprocess.Popen([sys.executable, "-c", sleeper, f"--config={unrelated}"])
    try:
        root = str(layout.root).replace("'", "''")
        blocking = _powershell(layout, shell, f"Get-BlockingProcesses -Paths @('{root}')")
    finally:
        for process in (using, mentioning):
            process.kill()
            process.wait()

    pids = {int(line.split(" ", 1)[0]) for line in blocking}
    assert using.pid in pids and mentioning.pid not in pids


def _spelled_home(layout: Layout, spelling: str) -> tuple[str, Path]:
    """Return an AVIBE_HOME spelling and the physical home it reaches."""
    def home_at(path: Path) -> Path:
        (path / "runtime" / "install-generations" / "g").mkdir(parents=True)
        (path / "state").mkdir()
        return path

    if spelling == "linked-ancestor":
        # The home is a real directory under a linked parent, as with /home -> /var/home.
        real = home_at(layout.tmp / "var-home" / "instance")
        _symlink(layout.tmp / "home-link", real.parent)
        return str(layout.tmp / "home-link" / "instance"), real
    if spelling == "dot-dot-through-a-link":
        # Spelled x/../instance, where x is a link: bash reads it as one directory,
        # and the kernel resolves another.
        home_at(layout.tmp / "sub" / "instance")
        reached = home_at(layout.tmp / "elsewhere" / "instance")
        (layout.tmp / "elsewhere" / "deep").mkdir()
        _symlink(layout.tmp / "sub" / "x", layout.tmp / "elsewhere" / "deep")
        return str(layout.tmp / "sub" / "x" / ".." / "instance"), reached
    real = home_at(layout.tmp / "instance")
    link = _symlink(layout.tmp / "instance-link", real)
    suffix = {"trailing-dot": "/.", "double-slash": "//", "trailing-slash": "/"}[spelling]
    return f"{link}{suffix}", real


@posix_only
@pytest.mark.parametrize(
    "spelling, succeeds",
    [
        ("trailing-dot", False),
        ("double-slash", False),
        ("trailing-slash", False),
        ("dot-dot-through-a-link", False),
        ("linked-ancestor", True),
    ],
)
@pytest.mark.parametrize("options", ["--uninstall", "--uninstall --purge --yes"])
def test_uninstall_deletes_directories_only_by_their_verified_physical_path(layout, spelling, succeeds, options):
    avibe_home, reached = _spelled_home(layout, spelling)
    purge = "--purge" in options

    result = _installer_shell(layout, f"main {options}", AVIBE_HOME=avibe_home)

    if succeeds:
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (reached / "runtime" / "install-generations").exists()
        assert reached.exists() is not purge
        return
    assert result.returncode == 1
    # The directory the spelling reaches keeps everything.
    assert (reached / "runtime" / "install-generations" / "g").is_dir()
    assert (reached / "state").is_dir()


@posix_only
def test_a_purge_unlinks_an_explicit_home_named_with_trailing_slashes(layout):
    target = layout.tmp / "instance"
    (target / "runtime").mkdir(parents=True)
    (target / "state").mkdir()
    link = _symlink(layout.tmp / "instance-link", target)

    result = _installer_shell(layout, "main --uninstall --purge --yes", AVIBE_HOME=f"{link}///")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not link.is_symlink()
    assert (target / "runtime").is_dir() and (target / "state").is_dir()
    assert f"Kept {target}, which {link} pointed to." in result.stdout


@posix_only
def test_a_purge_leaves_a_file_that_merely_has_a_home_name(layout):
    shutil.rmtree(layout.avibe_home)
    layout.avibe_home.write_text("someone else's file", encoding="utf-8")

    result = _installer_shell(layout, "main --uninstall --purge --yes")

    assert result.returncode == 0, result.stdout + result.stderr
    assert layout.avibe_home.read_text(encoding="utf-8") == "someone else's file"


@posix_only
def test_uninstall_removes_nothing_when_no_process_list_is_available(installed):
    layout, first, second, foreign, legacy_home, env = installed

    # As on a system with neither ps nor /proc.
    result = _installer_shell(layout, "process_table() { :; }\nmain --uninstall", **env)

    assert result.returncode == 1
    assert "No process list is available here" in result.stdout
    assert "Nothing was removed." in result.stdout
    _assert_untouched(layout, first, second, foreign, legacy_home)


def _link_directory(link: Path, target: Path) -> None:
    if WINDOWS:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        _symlink(link, target)


def _generations_behind_a_link(layout: Layout, shape: str) -> tuple[Path, dict[str, str]]:
    """Move part of the home elsewhere and reach it through a link; return the real root."""
    if shape == "linked-runtime":
        external = layout.tmp / "external-runtime"
        shutil.move(str(layout.avibe_home / "runtime"), str(external))
        _link_directory(layout.avibe_home / "runtime", external)
        return external / "install-generations", {}
    if shape == "non-directory-root":
        shutil.rmtree(layout.root)
        layout.root.write_text("not a generation root", encoding="utf-8")
        return layout.root, {}
    external = layout.tmp / "external-home"
    shutil.move(str(layout.avibe_home), str(external))
    if shape == "linked-home":
        _link_directory(layout.avibe_home, external)
    else:
        # No ~/.avibe, so the installer selects this legacy alias as the home.
        _link_directory(layout.home / ".vibe_remote", external)
    return external / "runtime" / "install-generations", {}


GENERATION_GUARD_SHAPES = ["linked-runtime", "linked-home", "stale-legacy-alias", "non-directory-root"]


def _assert_generations_kept(root: Path, shape: str) -> None:
    if shape == "non-directory-root":
        assert root.read_text(encoding="utf-8") == "not a generation root"
    else:
        assert {path.name for path in root.iterdir()} == {"current", "older"}


@posix_only
@pytest.mark.parametrize("shape", GENERATION_GUARD_SHAPES)
def test_uninstall_deletes_the_generation_root_only_through_real_directories(layout, shape):
    external_root, _ = _generations_behind_a_link(layout, shape)
    # The fixture's generation exports link by the old path, so name the file itself.
    launcher = _symlink(layout.path_dirs[0] / LAUNCHER, external_root / "current" / "uv" / "tools" / "avibe-os" / "bin" / "vibe")

    result = _installer_shell(layout, "main --uninstall")

    assert result.returncode == 1
    _assert_generations_kept(external_root, shape)
    # Launchers are links or files themselves, so they still go.
    assert not launcher.is_symlink()
    if shape == "non-directory-root":
        assert "is not a directory, and the uninstaller deletes only real directories." in result.stdout
    else:
        assert "and the uninstaller never deletes through a link." in result.stdout
    assert f"If it is yours, remove it with: rm -rf -- {shlex.quote(str(external_root.resolve()))}" in result.stdout
    assert "Avibe was not completely removed." in result.stdout


@posix_only
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root can remove from a read-only directory")
def test_uninstall_keeps_the_generation_root_while_a_launcher_could_not_be_removed(installed):
    layout, first, second, foreign, legacy_home, env = installed
    first.parent.chmod(0o555)  # as a system directory the user cannot write
    try:
        result = _installer_shell(layout, "main --uninstall", **env)
    finally:
        first.parent.chmod(0o755)

    assert result.returncode == 1
    assert first.is_symlink() and not second.exists()
    assert {path.name for path in layout.root.iterdir()} == {"current", "older"}
    assert f"Left {layout.root} in place: {first} could not be removed" in result.stdout
    # It is reported once, as the managed launcher it is, not as another install.
    assert f"remains at {first}" not in result.stdout


@pytest.mark.skipif(not os.path.exists("/proc/self/exe"), reason="Linux names each process's executable")
def test_uninstall_waits_for_a_process_started_by_a_relative_path(installed):
    layout, first, second, foreign, legacy_home, env = installed
    binary = layout.root / "current" / "bin" / "cloudflared"
    shutil.copy2(shutil.which("sleep"), binary)
    process = subprocess.Popen(["./cloudflared", "60"], cwd=binary.parent)
    try:
        result = _installer_shell(layout, "main --uninstall", **env)
    finally:
        process.kill()
        process.wait()

    assert result.returncode == 1
    # The command line shows only ./cloudflared; the executable names the generation.
    assert f"  pid {process.pid} ./cloudflared 60 ({binary})" in result.stdout
    _assert_untouched(layout, first, second, foreign, legacy_home)


@pytest.mark.skipif(not WINDOWS, reason="junctions")
@pytest.mark.parametrize("shell", _rule_implementations())
@pytest.mark.parametrize("shape", GENERATION_GUARD_SHAPES)
def test_powershell_deletes_the_generation_root_only_through_real_directories(layout, shell, shape):
    external_root, _ = _generations_behind_a_link(layout, shape)
    env = {} if shape in ("linked-runtime", "non-directory-root") else {"AVIBE_HOME": ""}

    output = "\n".join(_powershell(layout, shell, "Uninstall-Avibe | Out-Null", **env))

    _assert_generations_kept(external_root, shape)
    if shape == "non-directory-root":
        assert "is not a directory, and the uninstaller deletes only real directories." in output
    else:
        assert "and the uninstaller never deletes through a link." in output
    assert "If it is yours, remove it with:" in output


@pytest.mark.skipif(not WINDOWS, reason="an open file cannot be deleted on Windows")
@pytest.mark.parametrize("shell", _rule_implementations())
def test_powershell_keeps_the_generation_root_while_a_launcher_could_not_be_removed(layout, shell):
    launcher = _copy(layout.path_dirs[0] / LAUNCHER, layout.current, marker=True)
    with launcher.open("rb"):  # held open, so Windows refuses to delete it
        output = "\n".join(_powershell(layout, shell, "Uninstall-Avibe | Out-Null"))

    assert launcher.is_file()
    assert {path.name for path in layout.root.iterdir()} == {"current", "older"}
    assert f"{launcher} could not be removed" in output


@pytest.mark.skipif(not WINDOWS, reason="Windows process table")
@pytest.mark.parametrize("shell", _rule_implementations())
def test_powershell_removes_nothing_without_a_process_list(layout, shell):
    output = "\n".join(_powershell(
        layout, shell, "function Get-CimInstance { throw 'WMI is unavailable' }; Uninstall-Avibe | Out-Null"
    ))

    assert "No process list is available here" in output
    assert {path.name for path in layout.root.iterdir()} == {"current", "older"}


@posix_only
def test_uninstall_names_the_pip_that_can_remove_an_avibe_it_did_not_install(layout):
    shutil.rmtree(layout.root)
    pip_launcher = _foreign(
        layout.path_dirs[0] / "vibe",
        "#!/opt/python/bin/python3\nimport sys\nfrom vibe.cli import main\nif __name__ == '__main__':\n    sys.exit(main())\n",
    )

    result = _installer_shell(layout, "main --uninstall")

    assert result.returncode == 0, result.stdout + result.stderr
    assert pip_launcher.is_file()
    assert f"remains at {pip_launcher}, such as a pip install" in result.stdout
    assert "/opt/python/bin/python3 -m pip uninstall avibe-os vibe-remote" in result.stdout


def _fake_uv(layout: Layout, tool_dir: Path) -> None:
    """uv removes a tool's environment; `uv tool dir` names where they live."""
    log = shlex.quote(str(layout.tmp / "calls.log"))
    _foreign(
        layout.path_dirs[0] / "uv",
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        f'  "tool dir") echo {shlex.quote(str(tool_dir))} ;;\n'
        f'  "tool uninstall") echo "uv uninstall $3" >> {log}; rm -rf {shlex.quote(str(tool_dir))}/"$3" ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n",
    )


@posix_only
def test_an_explicit_avibe_home_purges_only_that_home(installed):
    layout, first, second, foreign, legacy_home, env = installed
    instance = layout.tmp / "instance"
    (instance / "runtime").mkdir(parents=True)
    # A global uv tool install belongs to no chosen home.
    tool = layout.tmp / "uv-tools" / "avibe-os"
    tool.mkdir(parents=True)
    _fake_uv(layout, tool.parent)

    result = _installer_shell(layout, "main --uninstall --purge --yes", **{**env, "AVIBE_HOME": str(instance)})

    assert result.returncode == 0, result.stdout + result.stderr
    assert not instance.exists()
    # The default home's launchers, data, and uv tool belong to another installation.
    _assert_untouched(layout, first, second, foreign, legacy_home)
    assert tool.is_dir() and _calls(layout) == []
    assert "Leaving the uv tool install avibe-os in place" in result.stdout


@posix_only
@pytest.mark.parametrize("shape", ["wrong-legacy-link", "wrong-legacy-link-without-avibe-home", "explicit-legacy-home"])
def test_a_purge_deletes_only_the_selected_homes_data(installed, shape):
    layout, first, second, foreign, legacy_home, env = installed
    legacy_home.unlink()
    unrelated = layout.tmp / "unrelated"
    unrelated.mkdir()
    (unrelated / "notes.txt").write_text("not Avibe's", encoding="utf-8")
    if shape.startswith("wrong-legacy-link"):
        # Doctor reports this as a wrong link; without ~/.avibe it is also the home.
        legacy_home.symlink_to(unrelated)
        if shape.endswith("without-avibe-home"):
            shutil.rmtree(layout.avibe_home)
    else:
        (legacy_home / "runtime").mkdir(parents=True)
        env = {**env, "AVIBE_HOME": str(legacy_home)}

    result = _installer_shell(layout, "main --uninstall --purge --yes", **env)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (unrelated / "notes.txt").read_text(encoding="utf-8") == "not Avibe's"
    if shape.startswith("wrong-legacy-link"):
        assert not layout.avibe_home.exists()
        assert not legacy_home.is_symlink()
        assert f"  {legacy_home} -> {unrelated}" in result.stdout
        assert f"Kept {unrelated}, which {legacy_home} pointed to." in result.stdout
    else:
        assert not legacy_home.exists()
        # ~/.avibe is the home Avibe uses without AVIBE_HOME, another installation.
        assert (layout.avibe_home / "state" / "vibe.sqlite").read_bytes() == b"user data"
        assert first.is_symlink() and layout.root.is_dir()


@pytest.mark.parametrize(
    "document, native_windows",
    [
        ("README.md", False),
        ("README_ZH.md", False),
        ("docs/INSTALL_FOR_AI.md", True),
        ("docs/INSTALL_FOR_AI_ZH.md", True),
    ],
)
def test_uninstall_recipes_point_to_the_installer(document, native_windows):
    text = (REPO_ROOT / document).read_text(encoding="utf-8")

    # Like the install command, a failed download fails the command.
    assert "bash -o pipefail -c 'curl -fsSL https://avibe.bot/install.sh | bash -s -- --uninstall'\n" in text
    assert "bash -o pipefail -c 'curl -fsSL https://avibe.bot/install.sh | bash -s -- --uninstall --purge'\n" in text
    powershell = "& ([scriptblock]::Create((irm https://raw.githubusercontent.com/avibe-bot/avibe/master/install.ps1))) -Uninstall"
    assert (f"{powershell}\n" in text and f"{powershell} -Purge\n" in text) is native_windows
    assert "uv tool uninstall" not in text and "rm -rf" not in text
