param(
    [ValidatePattern('^gh-v3\.2\.7rc[0-9]+$')][string] $ReleaseTag = 'gh-v3.2.7rc3',
    [ValidateSet('preparation', 'enabled', 'security', 'update')][string] $Mode = 'preparation',
    [ValidatePattern('^gh-v3\.2\.7rc[0-9]+$')][string] $UpdateTag = 'gh-v3.2.7rc4',
    [Parameter(Mandatory)][ValidatePattern('^[a-f0-9]{40}$')][string] $CandidateSourceSha,
    [Parameter(Mandatory)][ValidatePattern('^[a-f0-9]{64}$')][string] $CandidateDriverHash,
    [Parameter(Mandatory)][ValidateRange(1, 2147483647)][long] $CandidateDriverSize,
    [switch] $AllowUnverifiedHelpers
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'evidence.ps1')
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:RUNNER_OS -ne 'Windows') {
    throw 'Only a disposable GitHub-hosted Windows runner is permitted.'
}
$runDeadline = (Get-Date).AddMinutes(40) # Leave five minutes for teardown/artifact upload.
$evidence = Join-Path $env:RUNNER_TEMP 'signed-cu-evidence'
$root = Join-Path $env:RUNNER_TEMP ('signed-cu-' + [guid]::NewGuid().ToString('N'))
$work = Join-Path $root 'work'
New-Item -ItemType Directory -Path $evidence, $root, $work -Force | Out-Null
$cases = [ordered]@{}
foreach ($id in @('release_integrity', 'target_fixture_integrity', 'real_win32_fixture_preflight', 'standard_user', 'silent_install', 'driver_provenance', 'desktop_launch', 'webview_cdp_preflight', 'unified_update_panel_preflight', 'update_release_integrity', 'update_panel_start', 'update_installer', 'update_relaunch', 'update_versions', 'model_stub_tool_path', 'native_menu_quit_preflight', 'feature_disabled', 'settings_enable', 'survival_cu_off', 'survival_desktop_quit', 'survival_desktop_crash', 'elevated_app_refused', 'elevated_driver_refused', 'second_user_pipe_lock', 'security_fixture', 'elevated_target_query_preflight', 'locked_desktop', 'second_interactive_session', 'real_agent_turn', 'uninstall', 'cleanup')) {
    $cases[$id] = @{ verdict = 'NOT RUN'; reason = 'prerequisite_not_reached' }
}
$cases.locked_desktop.reason = 'hosted_runner_cannot_prove_locked_interactive_desktop'
$cases.second_interactive_session.reason = 'second_local_account_is_not_a_second_interactive_session'
$cases.real_agent_turn.reason = 'no_model_credentials_owner_real_machine_check_required'
$requiredCases = @('release_integrity', 'standard_user', 'silent_install', 'driver_provenance', 'desktop_launch', 'uninstall', 'cleanup')
if ($Mode -in @('enabled', 'security')) { $requiredCases += @('webview_cdp_preflight', 'model_stub_tool_path', 'settings_enable', 'survival_cu_off', 'survival_desktop_quit', 'survival_desktop_crash', 'elevated_app_refused', 'elevated_driver_refused', 'second_user_pipe_lock', 'security_fixture') }
elseif ($Mode -eq 'update') { $requiredCases += @('webview_cdp_preflight', 'update_release_integrity', 'update_panel_start', 'update_installer', 'update_relaunch', 'update_versions') }
else { $requiredCases += 'feature_disabled' }
$record = [ordered]@{
    required_cases = $requiredCases; required_cases_passed = $false; missing_required_cases = @()
    schema_version = 1; release_tag = $ReleaseTag; mode = $Mode
    candidate_source_sha = $CandidateSourceSha; candidate_driver_sha256 = $CandidateDriverHash; candidate_driver_size = $CandidateDriverSize
    runner = 'GitHub-hosted windows-latest / Windows Server 2025 x64'
    actual_os_build = [Environment]::OSVersion.Version.ToString()
    launch_method = 'ProcessStartInfo with UserName, LoadUserProfile (CreateProcessWithLogonW); not a second interactive session'
    screenshots = $false; clipboard_capture = $false; model_credentials = $false
    turn_evidence = 'deterministic model stub, not an LLM; real LLM Agent Turn NOT RUN'
    installer_sha256 = $null; installed_driver_sha256 = $null; stage2_driver_sha256 = $null
    cases = $cases
}
$user = $null; $password = $null; $worker = $null; $result = $null; $security = $null; $failed = $false; $stage = 'release_integrity'

function Verdict([string] $Id, [string] $Result, [string] $Reason) {
    $cases[$Id] = @{ verdict = $Result; reason = $Reason }
}
function Save-Report {
    Write-AcceptanceEvidence $record (Join-Path $evidence 'verdict.json')
    $lines = @('# Signed Windows Computer Use acceptance', '', "Release: $ReleaseTag; mode: $Mode.", '',
        'GitHub-hosted **Windows Server 2025 x64** (`windows-latest`); not Windows 11.', '',
        'No screenshots, clipboard capture, secrets, raw process logs or LLM Turn. Preparation PASS is not enabled-feature acceptance.', '',
        "Required cases passed: **$($record.required_cases_passed)**. Unexecuted cases keep NOT RUN and cannot satisfy the enabled gate.", '',
        '| Case | Verdict | Reason |', '| --- | --- | --- |')
    foreach ($entry in $cases.GetEnumerator()) { $lines += "| $($entry.Key) | $($entry.Value.verdict) | $($entry.Value.reason) |" }
    $lines += @('', 'Launch: ordinary Users-only local account via CreateProcessWithLogonW/profile loading, not a second interactive session.',
        '', "Installed driver SHA256: ``$($record.installed_driver_sha256)``", "Stage 2 driver SHA256: ``$($record.stage2_driver_sha256)``")
    $lines | Set-Content -LiteralPath (Join-Path $evidence 'verdict.md') -Encoding utf8
}
function Assert-ReleaseIdle {
    # Read all active pages. Fail closed if the API cannot be read. Never cancel
    # another run or contend on a release's concurrency group.
    $headers = @{ Authorization = "Bearer $env:GH_TOKEN"; 'X-GitHub-Api-Version' = '2022-11-28' }
    foreach ($status in @('queued', 'in_progress', 'waiting', 'pending', 'requested')) {
        for ($page = 1; ; $page++) {
            if ((Get-Date) -ge $runDeadline.AddMinutes(-3)) { throw 'acceptance_budget_exhausted' }
            $uri = "https://api.github.com/repos/avibe-bot/avibe/actions/runs?status=$status&per_page=100&page=$page"
            $data = Invoke-RestMethod -Uri $uri -Headers $headers -TimeoutSec 30
            foreach ($run in $data.workflow_runs) {
                # Match the release-producing workflows, not every workflow
                # whose name contains "release" (mirror schedules are unrelated).
                $releaseWorkflow = $run.path -match '^\.github/workflows/(release_ai|publish|publish_npm|desktop-release|desktop-package|desktop-native-update-acceptance)\.yml$'
                if ([string]$run.id -ne $env:GITHUB_RUN_ID -and $releaseWorkflow) {
                    throw 'release_priority_yield'
                }
            }
            if (@($data.workflow_runs).Count -lt 100) { break }
            if ($page -ge 20) { throw 'release_admission_unreadable' }
        }
    }
}
function Set-FixtureAccess([string] $Path, [string] $Rights) {
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($entry in @(@('S-1-5-18', 'FullControl'), @('S-1-5-32-544', 'FullControl'), @($user.SID.Value, $Rights))) {
        $sid = [Security.Principal.SecurityIdentifier]::new($entry[0])
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, $entry[1], 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}
function Stop-FixtureProcesses([string] $OwnerSid) {
    # Every process of this unique account was created by this task. Pin its
    # handle and check creation identity and SID before terminating it.
    foreach ($row in @(Get-CimInstance Win32_Process -OperationTimeoutSec 15)) {
        $owner = Invoke-CimMethod -InputObject $row -MethodName GetOwnerSid -ErrorAction SilentlyContinue
        if (-not $owner -or $owner.ReturnValue -ne 0 -or $owner.Sid -ne $OwnerSid) { continue }
        $process = $null
        try {
            $process = [Diagnostics.Process]::GetProcessById([int]$row.ProcessId)
            $null = $process.SafeHandle
            if ($process.HasExited) { continue }
            $created = $process.StartTime.ToUniversalTime()
            $created = $created.AddTicks(-($created.Ticks % 10))
            if ($created -ne $row.CreationDate.ToUniversalTime()) { throw 'cleanup_process_identity_changed' }
            $process.Kill()
            if (-not $process.WaitForExit(10000)) { throw 'cleanup_process_timeout' }
        } catch [ArgumentException] { } finally { if ($process) { $process.Dispose() } }
    }
}

Save-Report
try {
    Assert-ReleaseIdle
    # Lead 02:40: the published rc5 -> rc6 pair (both carry the fixed panel).
    if ($Mode -eq 'update' -and ($ReleaseTag -ne 'gh-v3.2.7rc5' -or $UpdateTag -ne 'gh-v3.2.7rc6')) { throw 'only_authorized_rc5_to_rc6_update_allowed' }
    . (Join-Path $PSScriptRoot 'release.ps1')
    $sourcePin = if ($Mode -eq 'update') { '' } else { $CandidateSourceSha }
    $driverPin = if ($Mode -eq 'update') { '' } else { $CandidateDriverHash }
    $release = Get-VerifiedRelease $ReleaseTag (Join-Path $root 'base-release') $runDeadline { Assert-ReleaseIdle } $sourcePin $driverPin
    $installer = $release.Installer; $hash = $release.DriverHash
    $record.installer_sha256 = $release.InstallerHash
    $record.stage2_driver_sha256 = $hash
    Verdict 'release_integrity' 'PASS' 'sha256sums_and_timestamped_certum_authenticode_valid'
    Save-Report
    $updateRelease = $null
    if ($Mode -eq 'update') {
        $stage = 'update_release_integrity'
        # Reference bytes only: the target installer is NEVER launched by the
        # harness. The real updater must discover/download/install its offer.
        $updateRelease = Get-VerifiedRelease $UpdateTag (Join-Path $root 'update-reference') $runDeadline { Assert-ReleaseIdle } $CandidateSourceSha $CandidateDriverHash
        $record.update_release = @{ tag = $UpdateTag; installer_sha256 = $updateRelease.InstallerHash; stage2_driver_sha256 = $updateRelease.DriverHash }
        Verdict 'update_release_integrity' 'PASS' 'target_reference_sha256sums_timestamped_certum_and_stage2_provenance_verified_not_executed'
    }

    $targetApp = $null; $targetHash = $null
    if ($Mode -ne 'update') {
    $stage = 'target_fixture_integrity'
    # A pinned real Win32 editor, not Windows' brokered Notepad launcher.
    # Binary files remain read/execute-only for the ordinary account; each
    # launch has its own settings directory and disables plugin/session reuse.
    $targetUrl = 'https://github.com/notepad-plus-plus/notepad-plus-plus/releases/download/v8.9.9.1/npp.8.9.9.1.portable.x64.zip'
    $targetArchiveHash = 'cbcefa34677e10fe6dd19802f7d6b3b9860cd6de62afeaae7680e09c51b47779'
    $targetArchive = Join-Path $root 'real-editor.zip'
    Assert-ReleaseIdle
    Invoke-WebRequest -Uri $targetUrl -OutFile $targetArchive -TimeoutSec 180
    if ((Get-FileHash -LiteralPath $targetArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $targetArchiveHash) { throw 'target_fixture_archive_hash_mismatch' }
    $targetDirectory = Join-Path $root 'real-editor'
    Expand-Archive -LiteralPath $targetArchive -DestinationPath $targetDirectory
    $targetApp = Join-Path $targetDirectory 'notepad++.exe'
    $targetSignature = Get-AuthenticodeSignature -LiteralPath $targetApp
    if ($targetSignature.Status -ne 'Valid' -or -not $targetSignature.SignerCertificate -or -not $targetSignature.TimeStamperCertificate) { throw 'target_fixture_signature_invalid' }
    $targetHash = (Get-FileHash -LiteralPath $targetApp -Algorithm SHA256).Hash.ToLowerInvariant()
    $targetVersion = [Diagnostics.FileVersionInfo]::GetVersionInfo($targetApp).ProductVersion
    if ($targetVersion -ne '8.9.9.1') { throw 'target_fixture_version_mismatch' }
    $record.target_fixture = @{ name = 'Notepad++'; version = $targetVersion; source = $targetUrl; archive_sha256 = $targetArchiveHash; executable_sha256 = $targetHash; signature_valid = $true; signer_subject = $targetSignature.SignerCertificate.Subject; signer_thumbprint = $targetSignature.SignerCertificate.Thumbprint }
    Verdict 'target_fixture_integrity' 'PASS' 'pinned_official_portable_editor_archive_hash_and_timestamped_authenticode_valid'
    Save-Report
    }

    $stage = 'standard_user'
    if ((Get-Date) -gt $runDeadline.AddMinutes(-5)) { throw 'acceptance_budget_exhausted' }
    $password = [Security.SecureString]::new()
    foreach ($character in 'aA1!'.ToCharArray()) { $password.AppendChar($character) }
    $alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%'
    for ($i = 0; $i -lt 40; $i++) { $password.AppendChar($alphabet[[Security.Cryptography.RandomNumberGenerator]::GetInt32($alphabet.Length)]) }
    $password.MakeReadOnly()
    $user = New-LocalUser -Name ('avicua' + [guid]::NewGuid().ToString('N').Substring(0, 10)) -Password $password -AccountExpires (Get-Date).AddHours(2)
    $group = Get-LocalGroup -SID 'S-1-5-32-545'
    if (-not (Get-LocalGroupMember -Group $group | Where-Object { $_.SID -eq $user.SID })) { Add-LocalGroupMember -Group $group -Member $user }
    $groups = @(Get-LocalGroup | Where-Object { @(Get-LocalGroupMember -Group $_ | Where-Object { $_.SID -eq $user.SID }).Count -gt 0 })
    if ($groups.Count -ne 1 -or $groups[0].SID.Value -ne 'S-1-5-32-545') { throw 'account_not_users_only' }
    Copy-AcceptanceHarness $PSScriptRoot $root
    Set-FixtureAccess $root 'ReadAndExecute'
    Set-FixtureAccess $work 'Modify'
    $config = @{ allow_unverified_helpers = [bool]$AllowUnverifiedHelpers; installer = $installer; driver_hash = $hash; driver_provenance = $release.Provenance; driver_size = if ($Mode -eq 'update') { 0 } else { $CandidateDriverSize }; mode = $Mode; release_version = ($ReleaseTag -replace '^gh-v', ''); expected_sid = $user.SID.Value; target_app = $targetApp; target_app_hash = $targetHash }
    if ($updateRelease) { $config.update = @{ version = $updateRelease.Version; desktop_version = $updateRelease.DesktopVersion; installer_hash = $updateRelease.InstallerHash; driver_hash = $updateRelease.DriverHash; driver_size = $CandidateDriverSize; driver_provenance = $updateRelease.Provenance } }
    $config | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $root 'input.json')
    Assert-ReleaseIdle
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = (Get-Command pwsh).Source
    $psi.WorkingDirectory = $root
    $psi.UseShellExecute = $false
    $psi.LoadUserProfile = $true
    $psi.UserName = $user.Name; $psi.Domain = $env:COMPUTERNAME; $psi.Password = $password
    $psi.Environment.Clear()
    foreach ($key in @('SystemRoot', 'SystemDrive', 'COMPUTERNAME', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData')) {
        $psi.Environment[$key] = [Environment]::GetEnvironmentVariable($key)
    }
    $psi.Environment['PATH'] = "$env:SystemRoot\System32;$env:SystemRoot;$(Split-Path $psi.FileName)"
    foreach ($arg in @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', (Join-Path $root 'user.ps1'), '-Root', $root)) { $psi.ArgumentList.Add($arg) }
    $worker = [Diagnostics.Process]::Start($psi)
    $null = $worker.SafeHandle
    $deadline = (Get-Date).AddMinutes(32)
    if ($deadline -gt $runDeadline.AddMinutes(-3)) { $deadline = $runDeadline.AddMinutes(-3) }
    $nextAdmission = (Get-Date).AddSeconds(180)
    . (Join-Path $PSScriptRoot 'security.ps1')
    $security = @{ Root = $root; User = $user; SecondUser = $null; TargetApp = $targetApp; TargetHash = $targetHash; DriverHash = $hash; ElevatedApp = $null; Completed = $false; QueryPrepared = $false; Stage = 'security_fixture'; Cases = @{} }
    while (-not $worker.WaitForExit(1000)) {
        $queryRequest = Join-Path $work 'query-fixture-request.json'
        if ($Mode -eq 'preparation' -and -not $security.QueryPrepared -and (Test-Path -LiteralPath $queryRequest)) {
            $request = Get-Content -LiteralPath $queryRequest -Raw | ConvertFrom-Json
            if ($request.action -ne 'prepare-elevated-query-fixture') { throw 'unknown_query_fixture_request' }
            try {
                Initialize-ElevatedQueryTarget $security
                $queryReply = @{ ok = $true; elevated_pid = $security.ElevatedApp.Id }
            } catch {
                $message = $_.Exception.GetBaseException().Message
                $reason = if ($message -match '^[a-z][a-z0-9_]+$') { $message } else { 'elevated_query_fixture_failed' }
                $queryReply = @{ ok = $false; reason = $reason }
            }
            $security.QueryPrepared = $true
            $queryReplyPath = Join-Path $work 'query-fixture-response.json'
            $queryReply | ConvertTo-Json | Set-Content -LiteralPath ($queryReplyPath + '.tmp') -Encoding utf8
            Move-Item -LiteralPath ($queryReplyPath + '.tmp') -Destination $queryReplyPath
        }
        $securityRequest = Join-Path $work 'security-request.json'
        if ($Mode -in @('enabled', 'security') -and -not $security.Completed -and (Test-Path -LiteralPath $securityRequest)) {
            $request = Get-Content -LiteralPath $securityRequest -Raw | ConvertFrom-Json
            if ($request.action -ne 'prepare-security-fixtures') { throw 'unknown_security_fixture_request' }
            $security.Request = $request
            $securityReply = Invoke-SecurityFixtureResponse $security
            # Parent-owned immutable diagnostics survive worker overwrite or loss.
            $record.security_fixture_receipt = $securityReply
            Save-Report
            $security.Completed = $true
            $replyPath = Join-Path $work 'security-response.json'
            $securityReply | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath ($replyPath + '.tmp') -Encoding utf8
            Move-Item -LiteralPath ($replyPath + '.tmp') -Destination $replyPath
        }
        if ((Get-Date) -gt $deadline) { throw 'ordinary_user_worker_timeout' }
        if ((Get-Date) -ge $nextAdmission) { Assert-ReleaseIdle; $nextAdmission = (Get-Date).AddSeconds(180) }
    }
    $result = Get-Content -LiteralPath (Join-Path $work 'result.json') -Raw | ConvertFrom-Json -AsHashtable
    Merge-AcceptanceWorker $record $result $worker.ExitCode
    if ($worker.ExitCode -ne 0) { $failed = $true }
    if ($cases.elevated_target_query_preflight.verdict -eq 'PASS') {
        try {
            if (-not $security.ElevatedApp -or $security.ElevatedApp.HasExited -or -not [AcceptanceNative]::IsElevated($security.ElevatedApp.Id) -or -not [AcceptanceNative]::UnsavedTextEquals($security.ElevatedApp.MainWindowHandle, '')) { throw 'original_elevated_target_not_preserved' }
        } catch {
            Add-AcceptanceFailure $record 'elevated_target_query_postcheck' $_
            Verdict 'elevated_target_query_preflight' 'FAIL' 'original_elevated_target_postcheck_failed'
            $failed = $true
        }
    }
    # Independent elevated read of the administrator document, recorded
    # whatever the worker decided, so a product failure is distinguishable.
    if ($security.ElevatedApp) {
        $documentEmpty = $null
        try {
            if ($security.ElevatedApp.HasExited) { throw 'elevated_target_exited' }
            $documentEmpty = [AcceptanceNative]::UnsavedTextEquals($security.ElevatedApp.MainWindowHandle, '')
        } catch { Add-AcceptanceFailure $record 'elevated_document_postcheck' $_ }
        $record.elevated_document_empty = $documentEmpty
        if ($cases.elevated_app_refused.verdict -eq 'PASS' -and $documentEmpty -ne $true) {
            Verdict 'elevated_app_refused' 'FAIL' 'elevated_notepad_empty_document_postcheck_failed'
            $failed = $true
        }
    }
} catch {
    $failed = $true
    # Only harness-owned reason codes escape; never serialize an exception,
    # credential-bearing HTTP request, process environment or native output.
    Add-AcceptanceFailure $record $stage $_
    $reason = if ($_.Exception.Message -match '^[a-z][a-z0-9_]+$') { $_.Exception.Message } else { 'harness_operation_failed' }
    $verdict = if ($reason -eq 'release_priority_yield') { 'NOT RUN' } else { 'FAIL' }
    Verdict $stage $verdict $reason
} finally {
    $cleanupFailed = $false
    try { if ($worker) { if (-not $worker.HasExited) { $worker.Kill(); $null = $worker.WaitForExit(10000) }; $worker.Dispose() } } catch { Add-AcceptanceFailure $record 'worker_cleanup' $_ -Cleanup; $cleanupFailed = $true }
    try {
        if ($security -and $security.ElevatedApp) {
            if (-not $security.ElevatedApp.HasExited) { $security.ElevatedApp.Kill(); $null = $security.ElevatedApp.WaitForExit(10000) }
            $security.ElevatedApp.Dispose()
        }
    } catch { Add-AcceptanceFailure $record 'security_target_cleanup' $_ -Cleanup; $cleanupFailed = $true }
    $accounts = @($user)
    if ($security -and $security.SecondUser) { $accounts += $security.SecondUser }
    foreach ($account in $accounts) {
        if (-not $account) { continue }
        try { Stop-FixtureProcesses $account.SID.Value } catch { Add-AcceptanceFailure $record 'fixture_process_cleanup' $_ -Cleanup; $cleanupFailed = $true }
        try {
            # A late process (e.g. an updater relaunch) can start after the first
            # sweep and keep the profile loaded; sweep again while waiting.
            $deadline = (Get-Date).AddSeconds(90)
            do {
                $profile = Get-CimInstance Win32_UserProfile -Filter "SID='$($account.SID.Value)'"
                if (-not $profile -or -not $profile.Loaded) { break }
                Stop-FixtureProcesses $account.SID.Value
                Start-Sleep -Milliseconds 1000
            } while ((Get-Date) -lt $deadline)
            if ($profile -and $profile.Loaded) { throw 'profile_still_loaded' }
            if ($profile) { $profile | Remove-CimInstance }
        } catch { Add-AcceptanceFailure $record 'fixture_profile_cleanup' $_ -Cleanup; $cleanupFailed = $true }
        try {
            if (Get-LocalUser -SID $account.SID -ErrorAction SilentlyContinue) { Remove-LocalUser -SID $account.SID }
            if ((Get-LocalUser -SID $account.SID -ErrorAction SilentlyContinue) -or (Get-CimInstance Win32_UserProfile -Filter "SID='$($account.SID.Value)'")) { throw 'fixture_identity_remains' }
        } catch { Add-AcceptanceFailure $record 'fixture_account_cleanup' $_ -Cleanup; $cleanupFailed = $true }
    }
    try {
        foreach ($portField in @('cdp_port', 'stub_port')) {
            if ($result -and $result.Contains($portField)) {
                $remaining = @(Get-NetTCPConnection -LocalPort $result[$portField] -State Listen -ErrorAction SilentlyContinue)
                if ($remaining.Count) { throw 'test_port_still_listening' }
            }
        }
    } catch { Add-AcceptanceFailure $record 'fixture_listener_cleanup' $_ -Cleanup; $cleanupFailed = $true }
    try { Remove-Item -LiteralPath $root -Recurse -Force } catch { Add-AcceptanceFailure $record 'fixture_files_cleanup' $_ -Cleanup; $cleanupFailed = $true }
    if ($cleanupFailed) { Verdict 'cleanup' 'FAIL' 'fixture_cleanup_failed'; $failed = $true }
    else { Verdict 'cleanup' 'PASS' 'owned_processes_profiles_accounts_and_work_removed' }
    if ($password) { $password.Dispose() }
    Complete-AcceptanceGate $record
    if (-not $record.required_cases_passed) { $failed = $true }
    Save-Report
}
if ($failed) { throw 'Signed acceptance did not pass; see value-free verdict artifacts.' }
