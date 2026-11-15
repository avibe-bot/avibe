# One authorized RC -> RC transition (run.ps1 pins the pair), through the real
# unified panel only.
# Public target bytes are a reference, never an installation fallback.
function Open-UpdateProcess($Row, [datetime] $Since) {
    if ($Row.CreationDate -lt $Since) { return $null }
    $process = $null
    try {
        $process = [Diagnostics.Process]::GetProcessById([int]$Row.ProcessId)
        $null = $process.SafeHandle
        if ($process.HasExited) { $process.Dispose(); return $null }
        # Owner from this account's own query-limited token read, not CIM.
        $observed = [AcceptanceNative]::ObserveProcess($process.Id)
        try { $owner = $observed.OwnerSid() } finally { $observed.Dispose() }
        if ($owner -ne $inputData.expected_sid) { $process.Dispose(); return $null }
        $created = $process.StartTime.ToUniversalTime()
        $created = $created.AddTicks(-($created.Ticks % 10))
        if ($created -ne $Row.CreationDate -or $process.MainModule.FileName -ne $Row.ExecutablePath) { throw 'update_process_identity_changed' }
        return $process
    } catch [ArgumentException] { if ($process) { $process.Dispose() }; return $null }
    catch { if ($process) { $process.Dispose() }; throw }
}
# Outcome-only second half (Product 03:51): after the Update click, judge only
# what a user would see. No supervisor/installer inspection, token or module
# reads of updater processes; the rc6 installer bytes were verified before the
# click. Plain same-user process listing (Get-Process .Path/.StartTime), which
# already worked for helper identity.
function Get-InstalledDesktops([string] $Executable, [string] $Name) {
    $found = @()
    foreach ($process in @(Get-Process -Name $Name -ErrorAction SilentlyContinue)) {
        $path = $null; $start = $null
        try { $path = $process.Path; $start = $process.StartTime.ToUniversalTime() } catch { }
        if ($path -eq $Executable -and $start) { $found += $process } else { $process.Dispose() }
    }
    return ,$found
}
# Records after the settle window: is_original, post_click, has_window.
function Resolve-UpdateOutcomeCount($Records) {
    $all = @($Records)
    if (@($all | Where-Object { $_.is_original }).Count) { return 'update_original_desktop_not_exited' }
    if ($all.Count -ge 2) { return 'update_product_double_launch' }
    if ($all.Count -eq 0) { return 'update_relaunched_desktop_exited' }
    if (-not ($all[0].post_click -and $all[0].has_window)) { return 'update_relaunched_desktop_unproven' }
    return $null
}
function Invoke-UpdateCase([hashtable] $Context) {
    $phase = 'update_panel_start'; $clicker = $null; $completed = $false; $survivor = $null
    $original = $Context.Desktop
    $Context.Quiesced = $false
    $observed = [ordered]@{ original_exited = $null; relaunched_window_seen = $false; relaunch_seconds_after_click = $null
        desktop_processes_after_settle = $null; desktop_file_version = $null; runtime_version = $null
        installed_driver_sha256 = $null; installed_driver_size = $null; panel_after_relaunch = $null; settings_unchanged = $null }
    try {
        if ($result.cases.webview_cdp_preflight.verdict -ne 'PASS') { throw 'update_shared_cdp_prerequisite_unproven' }
        $executable = $original.MainModule.FileName
        $desktopName = [IO.Path]::GetFileNameWithoutExtension($executable)
        $baseDesktopVersion = $inputData.release_version -replace 'rc', '-rc.'
        if ([Diagnostics.FileVersionInfo]::GetVersionInfo($executable).ProductVersion -ne $baseDesktopVersion) { throw 'update_base_desktop_version_mismatch' }
        $before = Invoke-RestMethod -Uri ($origin.AbsoluteUri.TrimEnd('/') + '/api/version') -TimeoutSec 10
        if ($before.current -ne $inputData.release_version) { throw 'update_base_runtime_version_mismatch' }
        # Panel half unchanged: real badge -> Check -> exact rc6 offer -> Update.
        $psi = [Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $node; $psi.UseShellExecute = $false; $psi.WorkingDirectory = $work
        $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
        foreach ($arg in @((Join-Path $Root 'cdp.mjs'), '--port', [string]$cdpPort, '--origin', $origin.GetLeftPart([UriPartial]::Authority), '--action', 'update-start', '--current-version', $inputData.release_version, '--target-version', $inputData.update.version)) { $psi.ArgumentList.Add($arg) }
        $clickStartedAt = [DateTime]::UtcNow
        $clicker = [Diagnostics.Process]::Start($psi)
        $null = $clicker.SafeHandle
        $stdout = $clicker.StandardOutput.ReadToEndAsync(); $stderr = $clicker.StandardError.ReadToEndAsync()
        if (-not $clicker.WaitForExit(90000)) { throw 'update_panel_client_timeout' }
        $text = $stdout.GetAwaiter().GetResult(); $null = $stderr.GetAwaiter().GetResult()
        $clickedAt = [DateTime]::UtcNow
        $receipt = $text | ConvertFrom-Json
        if ($clicker.ExitCode -ne 0) {
            if ($receipt.reason -match '^update_[a-z_]+$') { throw [string]$receipt.reason }
            throw 'update_panel_click_failed'
        }
        if (-not ($receipt.ok -and $receipt.badge_clicked -and $receipt.check_clicked -and $receipt.fresh_check -and $receipt.update_clicked) -or $receipt.current -ne $inputData.release_version -or $receipt.offered -ne $inputData.update.version) { throw 'update_exact_offer_click_unproven' }
        $result.update_transition = @{ from = $receipt.current; to = $receipt.offered; auto_update = $receipt.auto_update; prereleases = $receipt.prereleases; fresh_check = $true; panel_steps = $receipt.steps }
        Verdict 'update_panel_start' 'PASS' 'real_badge_check_and_exact_offered_update_clicked_fresh_receipt_not_install_success'
        Verdict 'update_installer' 'NOT RUN' 'outcome_only_run_installer_not_observed_reference_hash_verified_before_click'

        # (1) A Desktop window of the installed app runs again, within 3 min.
        $phase = 'update_relaunch'
        $deadline = (Get-Date).AddMinutes(3); $window = $false
        do {
            $candidates = Get-InstalledDesktops $executable $desktopName
            try {
                foreach ($process in $candidates) {
                    if ($process.Id -ne $original.Id -and $process.StartTime.ToUniversalTime() -gt $clickStartedAt -and $process.MainWindowHandle -ne 0) { $window = $true }
                }
            } finally { foreach ($process in $candidates) { $process.Dispose() } }
            if ($window) { break }
            Start-Sleep -Milliseconds 500
        } while ((Get-Date) -lt $deadline)
        if (-not $window) { throw 'update_relaunched_window_not_seen' }
        $observed.relaunched_window_seen = $true
        $observed.relaunch_seconds_after_click = [int]([DateTime]::UtcNow - $clickedAt).TotalSeconds

        # (2) Sixty seconds later, how many installed Desktops this user has.
        Start-Sleep -Seconds 60
        $final = Get-InstalledDesktops $executable $desktopName
        $observed.original_exited = $original.HasExited
        $observed.desktop_processes_after_settle = @($final | ForEach-Object {
            [ordered]@{ pid = $_.Id; is_original = $_.Id -eq $original.Id; post_click = $_.StartTime.ToUniversalTime() -gt $clickStartedAt
                started_utc = $_.StartTime.ToUniversalTime().ToString('o'); seconds_after_click = [int]($_.StartTime.ToUniversalTime() - $clickedAt).TotalSeconds; has_window = $_.MainWindowHandle -ne 0 }
        })
        $countFailure = Resolve-UpdateOutcomeCount $observed.desktop_processes_after_settle
        if ($countFailure) { foreach ($process in $final) { $process.Dispose() }; throw $countFailure }
        $survivor = $final[0]
        $Context.Desktop = $survivor
        Verdict 'update_relaunch' 'PASS' 'one_installed_desktop_window_running_sixty_seconds_after_relaunch'

        # (3)+(4) Installed files and the panel report rc6; settings unchanged.
        $phase = 'update_versions'
        $observed.desktop_file_version = [Diagnostics.FileVersionInfo]::GetVersionInfo($executable).ProductVersion
        if ($observed.desktop_file_version -ne $inputData.update.desktop_version) { throw 'update_desktop_version_mismatch' }
        $driverProof = Assert-InstalledDriver $install $inputData.update.driver_hash $inputData.update.driver_size $inputData.update.driver_provenance
        $observed.installed_driver_sha256 = $driverProof.Hash; $observed.installed_driver_size = $driverProof.Size
        $deadline = (Get-Date).AddMinutes(3)
        $manifest = Get-Content -LiteralPath (Join-Path $install 'runtime\runtime-manifest.json') -Raw | ConvertFrom-Json
        $slot = Join-Path (Join-Path $runtime $manifest.runtime_version) $manifest.archive_sha256.Substring(0, 16)
        $newPython = Join-Path $slot $manifest.python_entrypoint
        $newNode = Join-Path $slot $manifest.node_entrypoint
        while (-not (Test-Path -LiteralPath $newPython -PathType Leaf) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 250 }
        if (-not (Test-Path -LiteralPath $newPython -PathType Leaf) -or -not (Test-Path -LiteralPath $newNode -PathType Leaf)) { throw 'update_runtime_entrypoints_missing' }
        $Context.Python = $newPython
        $descriptor = Run-Process $newPython @('-I', '-B', '-m', 'vibe', 'desktop', 'endpoint', '--json') 60 | ConvertFrom-Json
        $newOrigin = [uri]$descriptor.origin
        if ($newOrigin.Scheme -ne 'http' -or $newOrigin.Host -ne '127.0.0.1') { throw 'update_runtime_origin_invalid' }
        $runtimeVersion = $null
        do {
            try { $runtimeVersion = Invoke-RestMethod -Uri ($newOrigin.AbsoluteUri.TrimEnd('/') + '/api/version') -TimeoutSec 5 } catch { $runtimeVersion = $null }
            if ($runtimeVersion -and $runtimeVersion.current -eq $inputData.update.version) { break }
            Start-Sleep -Milliseconds 500
        } while ((Get-Date) -lt $deadline)
        if ($runtimeVersion) { $observed.runtime_version = $runtimeVersion.current }
        if (-not $runtimeVersion -or $runtimeVersion.current -ne $inputData.update.version) { throw 'update_runtime_version_mismatch' }
        $panel = Run-Process $newNode @((Join-Path $Root 'cdp.mjs'), '--port', [string]$cdpPort, '--origin', $newOrigin.GetLeftPart([UriPartial]::Authority), '--action', 'update-verify', '--current-version', $inputData.update.version) 90 | ConvertFrom-Json
        $observed.panel_after_relaunch = $panel.steps.opened
        $observed.settings_unchanged = $panel.auto_update -eq $receipt.auto_update -and $panel.prereleases -eq $receipt.prereleases
        if (-not ($panel.ok -and $panel.badge_clicked -and $panel.current_row_verified) -or $panel.current -ne $inputData.update.version) { throw 'update_panel_current_version_unproven' }
        if (-not $observed.settings_unchanged) { throw 'update_settings_not_preserved' }
        Verdict 'update_versions' 'PASS' 'installed_files_runtime_and_panel_report_target_with_settings_unchanged'
        $completed = $true; $Context.Quiesced = $true
    } catch {
        # Keep the original exception class and line before mapping a reason.
        Add-AcceptanceFailure $result $phase $_
        $reason = if ($_.Exception.Message -match '^update_[a-z_]+$') { $_.Exception.Message } else { 'update_observation_failed' }
        $verdict = if ($reason -in @('update_target_not_discoverable', 'update_shared_cdp_prerequisite_unproven')) { 'NOT RUN' } else { 'FAIL' }
        Verdict $phase $verdict $reason
        throw 'update_acceptance_case_failed'
    } finally {
        $result.update_outcome = $observed
        try { Write-AcceptanceEvidence $result (Join-Path $work 'result.json') } catch { }
        if ($clicker) { try { if (-not $clicker.HasExited) { $clicker.Kill(); $null = $clicker.WaitForExit(5000) } } finally { $clicker.Dispose() } }
        # On failure the transaction is not proven quiet: Quiesced stays false,
        # so the worker skips uninstall and the parent removes the whole account.
    }
}
