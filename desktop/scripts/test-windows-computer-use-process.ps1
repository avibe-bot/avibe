# Hermetic process ownership only. Never run against an owner's workstation.
# The account/password exist for this hosted job only; no GUI or driver runs.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:RUNNER_OS -ne 'Windows') {
    throw 'Computer Use process tests require a disposable GitHub-hosted Windows job.'
}
if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'PowerShell 7 is required.' }

$tests = @(
    'ordinary_user_token_evidence',
    'explicit_parent_attribute_inherits_parent_job_not_driver_job',
    'explicit_breakaway_target_survives_driver_job_termination',
    'suspended_child_never_executes_and_drop_terminates_it',
    'failed_start_and_suspended_stop_leave_no_running_child',
    'stop_is_graceful_for_eof_and_forceful_for_remaining_descendants',
    'cancelled_stop_terminates_owned_child_and_allows_fresh_start',
    'killed_shell_cleans_suspended_and_running_children_without_destructors'
)
# Cargo reports the current executable even on a fresh cache hit. Never select
# an old artifact with a filename glob.
$build = & cargo test --locked -p avibe-desktop --all-features --no-run --message-format=json
if ($LASTEXITCODE -ne 0) { throw 'Could not build the current desktop test executable.' }
$artifacts = @($build | ForEach-Object {
    $row = $_ | ConvertFrom-Json
    if ($row.reason -eq 'compiler-artifact' -and $row.target.name -eq 'avibe_desktop_lib' -and $row.profile.test -and $row.executable) {
        $row.executable
    }
} | Select-Object -Unique)
if ($artifacts.Count -ne 1) { throw "Expected one current library test executable, found $($artifacts.Count)." }
$artifact = (Resolve-Path -LiteralPath $artifacts[0]).Path

$name = 'avibecu' + [guid]::NewGuid().ToString('N').Substring(0, 10)
$root = Join-Path $env:RUNNER_TEMP ('avibe-cu-owner-' + [guid]::NewGuid().ToString('N'))
$bin = Join-Path $root 'bin'
$temp = Join-Path $root 'tmp'
$logs = Join-Path $root 'logs'
$fixtureExecutable = Join-Path $bin 'owner-tests.exe'
$user = $null
$credential = $null
$password = $null
$failure = $null
$cleanupFailures = [System.Collections.Generic.List[string]]::new()
$completed = 0

function Set-TestAccess([string] $Path, [System.Security.Principal.SecurityIdentifier] $Sid, [string] $Rights) {
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($entry in @(@('S-1-5-18', 'FullControl'), @('S-1-5-32-544', 'FullControl'), @($Sid.Value, $Rights))) {
        $identity = [System.Security.Principal.SecurityIdentifier]::new($entry[0])
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $identity, $entry[1], 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

try {
    # Generate directly into SecureString: no plaintext password string, command
    # line, environment variable, output or artifact ever contains the password.
    $password = [Security.SecureString]::new()
    foreach ($character in 'aA1!'.ToCharArray()) { $password.AppendChar($character) }
    $alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%'
    for ($i = 0; $i -lt 40; $i++) {
        $password.AppendChar($alphabet[[Security.Cryptography.RandomNumberGenerator]::GetInt32($alphabet.Length)])
    }
    $password.MakeReadOnly()
    $user = New-LocalUser -Name $name -Password $password -AccountExpires (Get-Date).AddHours(2) -Description 'Disposable Avibe process-lifecycle CI fixture'
    # A real standard account, not a filtered or restricted administrator.
    $usersGroup = Get-LocalGroup -SID 'S-1-5-32-545'
    if (-not (Get-LocalGroupMember -Group $usersGroup | Where-Object { $_.SID -eq $user.SID })) {
        Add-LocalGroupMember -Group $usersGroup -Member $user
    }
    $memberships = @(Get-LocalGroup | Where-Object {
        @(Get-LocalGroupMember -Group $_ | Where-Object { $_.SID -eq $user.SID }).Count -gt 0
    })
    if ($memberships.Count -ne 1 -or $memberships[0].SID.Value -ne 'S-1-5-32-545') {
        throw 'The fixture account must belong to Users only.'
    }
    $credential = [Management.Automation.PSCredential]::new("$env:COMPUTERNAME\$name", $password)
    New-Item -ItemType Directory -Path $root, $bin, $temp, $logs | Out-Null
    Set-TestAccess $root $user.SID 'ReadAndExecute'
    Set-TestAccess $temp $user.SID 'Modify'
    Set-TestAccess $logs $user.SID 'Modify'
    Copy-Item -LiteralPath $artifact -Destination $fixtureExecutable
    # Only sibling native DLLs, if any, are needed by this exact Rust artifact.
    Get-ChildItem -LiteralPath (Split-Path $artifact) -Filter '*.dll' -File | Copy-Item -Destination $bin

    $testEnvironment = @{
        AVIBE_TEST_EXPECTED_SID = $user.SID.Value
        TEMP = $temp
        TMP = $temp
        SystemRoot = $env:SystemRoot
        SystemDrive = $env:SystemDrive
    }
    function Start-DisposableTestProcess([string] $TestName) {
        $startInfo = [Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $fixtureExecutable
        $startInfo.WorkingDirectory = $bin
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.LoadUserProfile = $true
        $startInfo.UserName = $name
        $startInfo.Domain = $env:COMPUTERNAME
        $startInfo.Password = $password
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $startInfo.Environment.Clear()
        foreach ($entry in $testEnvironment.GetEnumerator()) {
            $startInfo.Environment[$entry.Key] = [string]$entry.Value
        }
        foreach ($argument in @('--exact', "computer_use_process::tests::$TestName", '--ignored', '--nocapture')) {
            $startInfo.ArgumentList.Add($argument)
        }
        $process = [Diagnostics.Process]::new()
        $process.StartInfo = $startInfo
        if (-not $process.Start()) { throw "Could not start disposable test process: $TestName" }
        return $process
    }

    foreach ($test in $tests) {
        $stdout = Join-Path $logs "$test.out"
        $stderr = Join-Path $logs "$test.err"
        $process = $null
        try {
            $process = Start-DisposableTestProcess $test
            $null = $process.SafeHandle # retain identity; never reopen by PID to kill
            if (-not $process.WaitForExit(30000)) {
                $process.Kill()
                if (-not $process.WaitForExit(5000)) { throw "Timed-out test process did not exit: $test" }
                throw "Process-lifecycle test timed out: $test"
            }
            $output = $process.StandardOutput.ReadToEnd()
            $errors = $process.StandardError.ReadToEnd()
            Set-Content -LiteralPath $stdout -Value $output -NoNewline
            Set-Content -LiteralPath $stderr -Value $errors -NoNewline
            Write-Output $output
            if ($errors) { Write-Output $errors }
            if ($process.ExitCode -ne 0 -or $output -notmatch 'test result: ok\. 1 passed; 0 failed; 0 ignored;') {
                throw "Expected one executed passing test, not a skip: $test (exit $($process.ExitCode))."
            }
            if ($test -eq 'ordinary_user_token_evidence' -and $output -notmatch 'ordinary-token-evidence:') {
                throw 'The ordinary-user token evidence was not emitted.'
            }
            $completed++
        } finally {
            if ($process) {
                if (-not $process.HasExited) {
                    $process.Kill()
                    if (-not $process.WaitForExit(5000)) { throw 'Recorded test process could not be stopped.' }
                }
                $process.Dispose()
            }
        }
    }
    if ($completed -ne 8) { throw 'Token evidence, parent-attribute proof, breakaway proof, and all five lifecycle tests must execute.' }
} catch {
    $failure = $_
} finally {
    if ($user) {
        try {
            # A failed host-crash test can leave its fixture host alive. Record
            # only processes from this unique copied artifact AND this account,
            # pin their handles, compare creation identity, then terminate them.
            # Never pattern-kill a name or touch an existing runner account.
            foreach ($row in @(Get-CimInstance Win32_Process -OperationTimeoutSec 10 | Where-Object { $_.ExecutablePath -eq $fixtureExecutable })) {
                $owned = $null
                try {
                    $owned = [Diagnostics.Process]::GetProcessById([int]$row.ProcessId)
                    $null = $owned.SafeHandle
                    if ($owned.HasExited) { continue }
                    # CIM datetime carries microseconds; native FILETIME can
                    # additionally carry a 100 ns digit. Compare at CIM precision.
                    $created = $owned.StartTime.ToUniversalTime()
                    $created = $created.AddTicks(-($created.Ticks % 10))
                    if ($created -ne $row.CreationDate.ToUniversalTime()) { throw 'Fixture PID identity changed during cleanup.' }
                    $owner = Invoke-CimMethod -InputObject $row -MethodName GetOwnerSid -OperationTimeoutSec 5
                    if ($owned.HasExited) { continue }
                    if ($owner.ReturnValue -ne 0 -or $owner.Sid -ne $user.SID.Value) { throw 'Could not verify fixture process ownership.' }
                    Write-Output "Stopping recorded fixture PID $($owned.Id)."
                    $owned.Kill()
                    if (-not $owned.WaitForExit(5000)) { throw 'Fixture process did not terminate.' }
                } catch [ArgumentException] {
                    # The recorded process has already exited.
                } catch {
                    if (-not $owned -or -not $owned.HasExited) {
                        $cleanupFailures.Add("fixture PID $($row.ProcessId): $($_.Exception.Message)")
                    }
                } finally {
                    if ($owned) { $owned.Dispose() }
                }
            }
        } catch { $cleanupFailures.Add("process cleanup: $($_.Exception.Message)") }
        try {
            $deadline = (Get-Date).AddSeconds(10)
            do {
                $profile = Get-CimInstance Win32_UserProfile -Filter "SID='$($user.SID.Value)'" -OperationTimeoutSec 10
                if (-not $profile -or -not $profile.Loaded) { break }
                Start-Sleep -Milliseconds 200
            } while ((Get-Date) -lt $deadline)
            if ($profile -and $profile.Loaded) { throw 'Disposable profile remained loaded after tests.' }
            if ($profile) { $profile | Remove-CimInstance -OperationTimeoutSec 10 }
        } catch { $cleanupFailures.Add("profile cleanup: $($_.Exception.Message)") }
        try { Remove-LocalUser -SID $user.SID } catch { $cleanupFailures.Add("account cleanup: $($_.Exception.Message)") }
    }
    try { if (Test-Path -LiteralPath $root) { Remove-Item -LiteralPath $root -Recurse -Force } } catch { $cleanupFailures.Add("artifact cleanup: $($_.Exception.Message)") }
    $credential = $null
    if ($password) { $password.Dispose() }
}
if ($cleanupFailures.Count) { throw ($cleanupFailures -join '; ') }
if ($failure) { throw $failure }
Write-Output 'Ordinary-user token evidence, parent-attribute and breakaway Job proofs, and five process-lifecycle tests executed; disposable account/profile/artifacts removed.'
