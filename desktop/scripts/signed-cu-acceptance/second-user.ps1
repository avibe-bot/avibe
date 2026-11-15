param([Parameter(Mandatory)][string] $InputFile, [Parameter(Mandatory)][string] $OutputFile)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$data = Get-Content -LiteralPath $InputFile -Raw | ConvertFrom-Json
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($identity.User.Value -ne $data.expected_sid -or $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'second_standard_user_required' }
$pipeDenied = $false; $lockDenied = $false
$pipe = [IO.Pipes.NamedPipeClientStream]::new('.', $data.pipe_leaf, [IO.Pipes.PipeDirection]::InOut)
try {
    $pipe.Connect(3000)
} catch [UnauthorizedAccessException] {
    $pipeDenied = $true
} catch [IO.IOException] {
    $pipeDenied = ($_.Exception.HResult -band 0xFFFF) -eq 5
} catch [TimeoutException] {
    # An absent/unresponsive pipe is not permission-denial evidence.
    $pipeDenied = $false
} finally { $pipe.Dispose() }
$stream = $null
try {
    $stream = [IO.File]::Open($data.lock_path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
    $stream.Lock(0, 1)
    $stream.Unlock(0, 1)
} catch [UnauthorizedAccessException] {
    $lockDenied = $true
} catch [IO.IOException] {
    # ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION are real lock refusals;
    # path-not-found and arbitrary I/O failure are not.
    $lockDenied = ($_.Exception.HResult -band 0xFFFF) -in @(5, 32, 33)
} finally { if ($stream) { $stream.Dispose() } }
@{ standard_user = $true; pipe_denied = $pipeDenied; lock_denied = $lockDenied } | ConvertTo-Json | Set-Content -LiteralPath $OutputFile -Encoding utf8
if (-not ($pipeDenied -and $lockDenied)) { exit 1 }
