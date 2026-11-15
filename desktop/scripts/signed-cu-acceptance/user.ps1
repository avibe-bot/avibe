param([Parameter(Mandatory)][string] $Root)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$inputData = Get-Content -LiteralPath (Join-Path $Root 'input.json') -Raw | ConvertFrom-Json
$work = Join-Path $Root 'work'
$result = @{ installed_driver_sha256 = $null; cases = [ordered]@{} }
$stage = 'standard_user'; $failed = $false; $desktop = $null; $install = $null; $python = $null
function Verdict([string] $Id, [string] $Verdict, [string] $Reason) {
    $result.cases[$Id] = @{ verdict = $Verdict; reason = $Reason }
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $work 'result.json') -Encoding utf8
}
function Run-Process([string] $File, [string[]] $Arguments, [int] $Seconds = 180) {
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $File; $psi.UseShellExecute = $false; $psi.WorkingDirectory = $work
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    foreach ($argument in $Arguments) { $psi.ArgumentList.Add($argument) }
    $p = [Diagnostics.Process]::Start($psi)
    $null = $p.SafeHandle
    try {
        $stdout = $p.StandardOutput.ReadToEndAsync(); $stderr = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit($Seconds * 1000)) { throw 'child_process_timeout' }
        $text = $stdout.GetAwaiter().GetResult(); $null = $stderr.GetAwaiter().GetResult()
        if ($p.ExitCode -ne 0) { throw 'child_process_nonzero_exit' }
        return $text
    } finally {
        if (-not $p.HasExited) { $p.Kill(); $null = $p.WaitForExit(10000) }
        $p.Dispose()
    }
}
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if ($identity.User.Value -ne $inputData.expected_sid -or $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'ordinary_user_token_required'
    }
    # The parent supplied a minimal environment, not the runner administrator's
    # HOME/APPDATA/token stores. Resolve the loaded user's known folders now.
    $env:USERPROFILE = [Environment]::GetFolderPath('UserProfile')
    $env:HOME = $env:USERPROFILE
    $env:LOCALAPPDATA = [Environment]::GetFolderPath('LocalApplicationData')
    $env:APPDATA = [Environment]::GetFolderPath('ApplicationData')
    $env:TEMP = Join-Path $work 'temp'; $env:TMP = $env:TEMP
    New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
    if (-not $env:USERPROFILE -or -not $env:LOCALAPPDATA) { throw 'loaded_user_profile_missing' }
    Verdict 'standard_user' 'PASS' 'users_only_account_and_nonelevated_worker_identity_verified'

    $stage = 'silent_install'
    $install = Join-Path $env:LOCALAPPDATA 'Programs\AvibeAcceptance'
    # NSIS /D consumes the remainder; it must be last and not quoted.
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $inputData.installer; $psi.UseShellExecute = $false
    $psi.Arguments = "/S /D=$install"
    $installer = [Diagnostics.Process]::Start($psi)
    $null = $installer.SafeHandle
    try {
        if (-not $installer.WaitForExit(600000)) { throw 'silent_install_timeout' }
        if ($installer.ExitCode -ne 0) { throw 'silent_install_nonzero_exit' }
    } finally {
        if (-not $installer.HasExited) { $installer.Kill(); $null = $installer.WaitForExit(10000) }
        $installer.Dispose()
    }
    $apps = @(Get-ChildItem -LiteralPath $install -File -Filter '*.exe' | Where-Object { $_.Name -notmatch '(?i)uninstall' })
    if ($apps.Count -ne 1) { throw 'installed_desktop_executable_ambiguous' }
    Verdict 'silent_install' 'PASS' 'signed_nsis_installed_in_standard_user_profile'

    $stage = 'driver_provenance'
    $driver = Join-Path $install 'computer-use\cua-driver.exe'
    $hash = (Get-FileHash -LiteralPath $driver -Algorithm SHA256).Hash.ToLowerInvariant()
    $result.installed_driver_sha256 = $hash
    if ($hash -ne $inputData.driver_hash) { throw 'installed_driver_stage2_mismatch' }
    Verdict 'driver_provenance' 'PASS' 'installed_bytes_equal_prepared_and_packaged_stage2_hashes'

    $stage = 'desktop_launch'
    $desktop = Start-Process -FilePath $apps[0].FullName -PassThru
    $null = $desktop.SafeHandle
    $runtime = Join-Path $env:LOCALAPPDATA 'bot.avibe.desktop\runtime'
    $interpreters = @(Get-ChildItem -LiteralPath $runtime -Recurse -File -Filter 'python.exe')
    if ($interpreters.Count -ne 1) { throw 'installed_runtime_interpreter_ambiguous' }
    $python = $interpreters[0].FullName
    $descriptor = Run-Process $python @('-I', '-B', '-m', 'vibe', 'desktop', 'endpoint', '--json') 180 | ConvertFrom-Json
    $origin = [uri]$descriptor.origin
    if ($origin.Scheme -ne 'http' -or $origin.Host -ne '127.0.0.1') { throw 'unexpected_runtime_origin' }
    $deadline = (Get-Date).AddMinutes(4)
    $status = $null
    do {
        $desktop.Refresh()
        if ($desktop.HasExited) { throw 'desktop_exited_during_startup' }
        try { $status = Invoke-RestMethod -Uri ($origin.AbsoluteUri.TrimEnd('/') + '/api/desktop/computer-use/status') -TimeoutSec 5 } catch { $status = $null }
        if ($status -and $desktop.MainWindowHandle -ne 0) { break }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    if (-not $status -or $desktop.MainWindowHandle -eq 0) { throw 'desktop_window_or_runtime_not_ready' }
    Verdict 'desktop_launch' 'PASS' 'installed_desktop_window_and_its_runtime_status_ready_under_standard_user'

    if ($inputData.mode -eq 'preparation') {
        $stage = 'feature_disabled'
        if ($status.supported -ne $false -or $status.status -ne 'unavailable' -or $status.reason -ne 'unsupported_host' -or $status.host_platform -ne 'windows') {
            throw 'preparation_expected_windows_computer_use_disabled'
        }
        $driverProcesses = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $driver })
        if ($driverProcesses.Count -ne 0) { throw 'disabled_feature_started_driver' }
        Verdict 'feature_disabled' 'PASS' 'shipped_status_unsupported_host_and_no_installed_driver_process'
        foreach ($id in @('settings_enable', 'survival_cu_off', 'survival_desktop_quit', 'survival_desktop_crash', 'elevated_app_refused', 'elevated_driver_refused', 'second_user_pipe_lock')) {
            Verdict $id 'NOT RUN' 'rc3_feature_disabled_preparation_only'
        }
    } else {
        $stage = 'settings_enable'
        throw 'enabled_acceptance_not_implemented_yet'
    }
} catch {
    $failed = $true
    $reason = if ($_.Exception.Message -match '^[a-z][a-z0-9_]+$') { $_.Exception.Message } else { 'ordinary_user_operation_failed' }
    Verdict $stage 'FAIL' $reason
} finally {
    try {
        # This is preparation teardown, NOT the Desktop-quit acceptance case.
        if ($desktop -and -not $desktop.HasExited) { $desktop.Kill(); $null = $desktop.WaitForExit(10000) }
        if ($desktop) { $desktop.Dispose() }
        if ($python) { $null = Run-Process $python @('-I', '-B', '-m', 'vibe', 'stop') 120 }
        if ($install -and (Test-Path -LiteralPath $install)) {
            $uninstallers = @(Get-ChildItem -LiteralPath $install -File -Filter '*uninstall*.exe')
            if ($uninstallers.Count -ne 1) { throw 'uninstaller_missing_or_ambiguous' }
            # _?= prevents NSIS self-copying into a detached process and lets us
            # wait for the actual uninstaller. It must be the last argument.
            $psi = [Diagnostics.ProcessStartInfo]::new()
            $psi.FileName = $uninstallers[0].FullName; $psi.UseShellExecute = $false
            $psi.Arguments = "/S _?=$install"
            $uninstaller = [Diagnostics.Process]::Start($psi)
            try {
                if (-not $uninstaller.WaitForExit(120000) -or $uninstaller.ExitCode -ne 0) { throw 'uninstall_failed' }
            } finally { if (-not $uninstaller.HasExited) { $uninstaller.Kill() }; $uninstaller.Dispose() }
            if (Test-Path -LiteralPath (Join-Path $install 'computer-use\cua-driver.exe')) { throw 'uninstall_left_driver' }
            Verdict 'uninstall' 'PASS' 'standard_user_silent_uninstall_removed_installed_driver'
        }
    } catch { Verdict 'uninstall' 'FAIL' 'ordinary_user_uninstall_failed'; $failed = $true }
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $work 'result.json') -Encoding utf8
}
if ($failed) { exit 1 }
