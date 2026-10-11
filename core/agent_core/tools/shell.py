"""The shell a command job runs in (C-7 tools.md section 5): bash on POSIX, PowerShell on Windows.

The command tool is named after the language its commands are written in, ``bash`` or
``powershell``, so the model writes what the shell runs.
"""

from __future__ import annotations

import ntpath
import os
import shlex
import shutil
from dataclasses import dataclass

WINDOWS = os.name == "nt"
#: The command tool's name, which tells the model the language of its commands.
SHELL_TOOL = "powershell" if WINDOWS else "bash"
#: What other tools' results suggest for work they will not do (C-7 tools.md section 5).
SHELL_ADVICE = "powershell (for example a short script)" if WINDOWS else "bash (for example sed or a short script)"
#: Where a PowerShell job's command waits for the bootstrap, in the job directory.
COMMAND_FILE = "command.ps1"

# Runs ``command.ps1`` (C-7 tools.md section 5). It is passed with ``-Command``, where Windows PowerShell
# writes errors as text (with ``-EncodedCommand`` it writes them as CLIXML), so it holds no double quote,
# which the command line would have to escape. Each top-level statement runs on its own and its output is
# formatted before the next one runs, so an ``exit`` loses none of it; a script with parts that only work
# whole (a param or named block, a top-level trap, using statements) runs whole.
_BOOTSTRAP = """\
$ProgressPreference = 'SilentlyContinue'
if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }
$__avibe_utf8 = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $__avibe_utf8
$OutputEncoding = $__avibe_utf8
$__avibe_errors = $null
$__avibe_ast = [System.Management.Automation.Language.Parser]::ParseInput(\
[System.IO.File]::ReadAllText('{path}', $__avibe_utf8), [ref]$null, [ref]$__avibe_errors)
if ($__avibe_errors) {
    foreach ($__avibe_error in $__avibe_errors) {
        [Console]::Error.WriteLine(('At line:{0} char:{1}' -f $__avibe_error.Extent.StartLineNumber, \
$__avibe_error.Extent.StartColumnNumber) + [Environment]::NewLine + $__avibe_error.Message)
    }
    exit 1
}
$__avibe_whole = $__avibe_ast.ParamBlock -or $__avibe_ast.BeginBlock -or $__avibe_ast.ProcessBlock -or \
$__avibe_ast.DynamicParamBlock -or $__avibe_ast.UsingStatements.Count -or $__avibe_ast.EndBlock.Traps
if ($__avibe_whole) { $__avibe_parts = @($__avibe_ast.Extent.Text) } else { \
$__avibe_parts = @($__avibe_ast.EndBlock.Statements | ForEach-Object { $_.Extent.Text }) }
$__avibe_status = $true
try {
    foreach ($__avibe_part in $__avibe_parts) {
        . ([ScriptBlock]::Create($__avibe_part + [Environment]::NewLine + [Environment]::NewLine + \
'$__avibe_status = $?; $__avibe_code = $global:LASTEXITCODE')) | Out-Default
    }
} catch {
    [Console]::Error.Write(($_ | Out-String))
    exit 1
}
if ($__avibe_status) { exit 0 }
if ($__avibe_code) { exit $__avibe_code }
exit 1
"""


@dataclass(frozen=True)
class Shell:
    """A program that runs a job's command: ``<path> -c <command>`` for a POSIX shell (and Python, in tests),
    or PowerShell through the bootstrap, which reads the command from ``command.ps1``."""

    path: str
    powershell: bool = False

    @property
    def windows_powershell(self) -> bool:
        """Windows PowerShell 5.1, which every Windows 10 and 11 has; else PowerShell 7 (``pwsh``)."""
        return self.powershell and ntpath.basename(self.path).lower() == "powershell.exe"

    def describe(self) -> str:
        """The environment block's ``shell`` field (tools.md section 8)."""
        if not self.powershell:
            return self.path
        return f"{'Windows PowerShell 5.1' if self.windows_powershell else 'PowerShell 7'} ({self.path})"

    def prepare(self, command: str, job_dir: str) -> list[str]:
        """The argv that runs ``command`` in ``job_dir``'s job; for PowerShell, ``command.ps1`` is written first.

        A Windows command line holds 32,767 characters; the file holds any command the job record does.
        """
        if not self.powershell:
            return [self.path, "-c", command]
        path = os.path.join(job_dir, COMMAND_FILE)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(command)
        bootstrap = _BOOTSTRAP.replace("{path}", path.replace("'", "''"))
        return [self.path, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", bootstrap]


def default_shell() -> Shell:
    """Pi's choice on POSIX: ``/bin/bash``, then ``bash`` on PATH, then ``sh``. On Windows, PowerShell 7 when
    ``pwsh`` is on PATH, else Windows PowerShell 5.1."""
    if WINDOWS:
        pwsh = shutil.which("pwsh")
        if pwsh:
            return Shell(pwsh, powershell=True)
        system_root = os.environ.get("SystemRoot") or r"C:\Windows"
        return Shell(
            os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"), powershell=True
        )
    if os.path.exists("/bin/bash"):
        return Shell("/bin/bash")
    return Shell(shutil.which("bash") or "sh")


def read_line_advice(path: str, line: int, max_bytes: int) -> str:
    """How to read one line too long for ``read``, as a command that is safe to run whatever the path holds."""
    if WINDOWS:
        quoted = "'" + path.replace("'", "''") + "'"
        return (
            f"Use powershell: (Get-Content -LiteralPath {quoted} -TotalCount {line})[-1] | "
            f"ForEach-Object {{ $_.Substring(0, [Math]::Min($_.Length, {max_bytes})) }}"
        )
    return f"Use bash: sed -n '{line}p' {shlex.quote(path)} | head -c {max_bytes}"
