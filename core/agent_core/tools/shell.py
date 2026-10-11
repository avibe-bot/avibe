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
# which the command line would have to escape. The command runs whole, as one script block, so PowerShell's own
# semantics hold. Only the last statement of its end block is wrapped, found by its place in the text: a native
# program sets ``$LASTEXITCODE`` as an int, so it is kept as a long of the same value just before that statement,
# and the exit code is one the last statement set, never a code an earlier one left there. The loop around the
# script block is where a ``break`` or ``continue`` outside any loop ends it.
_BOOTSTRAP = """\
$ProgressPreference = 'SilentlyContinue'
if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }
$__avibe_utf8 = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $__avibe_utf8
$OutputEncoding = $__avibe_utf8
$__avibe_errors = $null
$__avibe_script = [System.IO.File]::ReadAllText('{path}', $__avibe_utf8)
$__avibe_ast = [System.Management.Automation.Language.Parser]::ParseInput(\
$__avibe_script, [ref]$null, [ref]$__avibe_errors)
if ($__avibe_errors) {
    foreach ($__avibe_error in $__avibe_errors) {
        [Console]::Error.WriteLine(('At line:{0} char:{1}' -f $__avibe_error.Extent.StartLineNumber, \
$__avibe_error.Extent.StartColumnNumber) + [Environment]::NewLine + $__avibe_error.Message)
    }
    exit 1
}
$__avibe_status = $true
if ($__avibe_ast.EndBlock -and $__avibe_ast.EndBlock.Statements.Count) {
    $__avibe_last = $__avibe_ast.EndBlock.Statements[$__avibe_ast.EndBlock.Statements.Count - 1].Extent
    $__avibe_script = $__avibe_script.Insert($__avibe_last.EndOffset, [Environment]::NewLine + \
'$__avibe_status = $?; $__avibe_code = $global:LASTEXITCODE; $__avibe_set = $__avibe_code -is [int]').Insert(\
$__avibe_last.StartOffset, 'if ($global:LASTEXITCODE -is [int]) { $global:LASTEXITCODE = [long]$global:LASTEXITCODE }' + \
[Environment]::NewLine)
}
try {
    foreach ($__avibe_once in 1) { . ([ScriptBlock]::Create($__avibe_script)) | Out-Default }
} catch {
    [Console]::Error.Write(($_ | Out-String))
    exit 1
}
if ($__avibe_status) { exit 0 }
if ($__avibe_set -and $__avibe_code) { exit $__avibe_code }
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
        # What sed and head -c print: the line as UTF-8, at most max_bytes of it, cut at a character boundary.
        return (
            f"Use powershell: @(Get-Content -LiteralPath {quoted} -TotalCount {line} -Encoding UTF8)[-1] | "
            f"ForEach-Object {{ $b = [Text.Encoding]::UTF8.GetBytes($_); $n = [Math]::Min($b.Length, {max_bytes}); "
            "while ($n -lt $b.Length -and ($b[$n] -band 0xC0) -eq 0x80) { $n-- }; "
            "[Console]::Out.Write([Text.Encoding]::UTF8.GetString($b, 0, $n)) }"
        )
    return f"Use bash: sed -n '{line}p' {shlex.quote(path)} | head -c {max_bytes}"
