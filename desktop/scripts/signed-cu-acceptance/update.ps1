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
function Invoke-UpdateCase([hashtable] $Context) {
    $phase = 'update_panel_start'; $clicker = $null; $updater = $null; $replacement = $null; $supervisor = $null
    $original = $Context.Desktop; $since = [DateTime]::UtcNow; $completed = $false
    $desktops = [ordered]@{}; $firstDesktopSeen = $null
    $supervisorFound = $false; $installerFound = $false; $installerHashMatches = $null
    $Context.Quiesced = $false
    . (Join-Path $Root 'update-observer.ps1')
    if (-not ('AcceptanceNative' -as [type])) { Add-Type -Path (Join-Path $Root 'Native.cs') }
    try {
        if ($result.cases.webview_cdp_preflight.verdict -ne 'PASS') { throw 'update_shared_cdp_prerequisite_unproven' }
        $executable = $original.MainModule.FileName
        $baseDesktopVersion = $inputData.release_version -replace 'rc', '-rc.'
        if ([Diagnostics.FileVersionInfo]::GetVersionInfo($executable).ProductVersion -ne $baseDesktopVersion) { throw 'update_base_desktop_version_mismatch' }
        $before = Invoke-RestMethod -Uri ($origin.AbsoluteUri.TrimEnd('/') + '/api/version') -TimeoutSec 10
        if ($before.current -ne $inputData.release_version) { throw 'update_base_runtime_version_mismatch' }
        $since = [DateTime]::UtcNow
        $psi = [Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $node; $psi.UseShellExecute = $false; $psi.WorkingDirectory = $work
        $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
        foreach ($arg in @((Join-Path $Root 'cdp.mjs'), '--port', [string]$cdpPort, '--origin', $origin.GetLeftPart([UriPartial]::Authority), '--action', 'update-start', '--current-version', $inputData.release_version, '--target-version', $inputData.update.version)) { $psi.ArgumentList.Add($arg) }
        # Observe while the UI client runs, not only after it returns: a fast
        # installer must not escape the retained-handle observation window.
        $clicker = [Diagnostics.Process]::Start($psi)
        $null = $clicker.SafeHandle
        $stdout = $clicker.StandardOutput.ReadToEndAsync(); $stderr = $clicker.StandardError.ReadToEndAsync()
        $clickDeadline = (Get-Date).AddSeconds(90); $deadline = (Get-Date).AddMinutes(15)
        $receipt = $null
        $installerPattern = '^' + [regex]::Escape($env:TEMP.TrimEnd('\') + '\') + 'avibe-update-[^\\]+\\update\.exe$'
        do {
            if (-not $receipt -and $clicker.HasExited) {
                $phase = 'update_panel_start'
                $text = $stdout.GetAwaiter().GetResult(); $null = $stderr.GetAwaiter().GetResult()
                $receipt = $text | ConvertFrom-Json
                if ($clicker.ExitCode -ne 0) {
                    if ($receipt.reason -match '^update_[a-z_]+$') { throw [string]$receipt.reason }
                    throw 'update_panel_click_failed'
                }
                if (-not ($receipt.ok -and $receipt.badge_clicked -and $receipt.check_clicked -and $receipt.fresh_check -and $receipt.update_clicked) -or $receipt.current -ne $inputData.release_version -or $receipt.offered -ne $inputData.update.version) { throw 'update_exact_offer_click_unproven' }
                $result.update_transition = @{ from = $receipt.current; to = $receipt.offered; auto_update = $receipt.auto_update; prereleases = $receipt.prereleases; fresh_check = $true; panel_steps = $receipt.steps }
                Verdict 'update_panel_start' 'PASS' 'real_badge_check_and_exact_offered_update_clicked_fresh_receipt_not_install_success'
                $phase = 'update_installer'
            }
            if (-not $clicker.HasExited -and (Get-Date) -gt $clickDeadline) { $phase = 'update_panel_start'; throw 'update_panel_client_timeout' }
            $desktopName = [IO.Path]::GetFileNameWithoutExtension($executable)
            $rows = @(Get-UpdateProcessRows @('update', 'powershell', $desktopName))
            foreach ($row in $rows) {
                $staging = Get-UpdateSupervisorStage $row $original.Id $env:TEMP
                if (-not $staging -or ($supervisor -and $supervisor.Id -eq [int]$row.ProcessId)) { continue }
                $phase = 'update_installer'
                $candidate = Open-UpdateProcess $row $since
                if ($candidate) {
                    if ($supervisor) { $candidate.Dispose(); throw 'update_multiple_transaction_supervisors' }
                    $supervisor = $candidate
                    $supervisorFound = $true
                    $request = Get-Content -LiteralPath (Join-Path $staging 'request.json') -Raw | ConvertFrom-Json
                    if ([int]$request.pid -ne $original.Id -or ([string]$request.directory).Replace('\\?\', '') -ne $install -or ([string]$request.executable).Replace('\\?\', '') -ne $executable -or $request.version -ne $inputData.update.desktop_version) { throw 'update_supervisor_request_identity_mismatch' }
                }
            }
            foreach ($row in $rows) {
                if (-not $updater -and $row.ExecutablePath -match $installerPattern) {
                    $phase = 'update_installer'
                    $candidate = Open-UpdateProcess $row $since
                    if ($candidate) {
                        $updater = $candidate
                        $installerFound = $true
                        if (-not $supervisor -or [int]$row.ParentProcessId -ne $supervisor.Id) { throw 'update_installer_supervisor_unproven' }
                        $runningHash = (Get-FileHash -LiteralPath $row.ExecutablePath -Algorithm SHA256).Hash.ToLowerInvariant()
                        $installerHashMatches = $runningHash -eq $inputData.update.installer_hash
                        if ($runningHash -ne $inputData.update.installer_hash) { throw 'update_running_installer_hash_mismatch' }
                    }
                }
                if ($row.ExecutablePath -eq $executable -and [int]$row.ProcessId -ne $original.Id -and -not $desktops.Contains([int]$row.ProcessId)) {
                    $candidate = Open-UpdateProcess $row $since
                    if ($candidate) {
                        $parentPid = if ($null -ne $row.ParentProcessId) { [int]$row.ParentProcessId } else { $null }
                        $relation = if ($null -eq $parentPid) { 'unknown' }
                            elseif ($parentPid -eq $original.Id) { 'original' }
                            elseif ($supervisor -and $parentPid -eq $supervisor.Id) { 'supervisor' }
                            elseif ($updater -and $parentPid -eq $updater.Id) { 'installer' }
                            elseif ($desktops.Contains($parentPid)) { 'replacement' } else { 'other' }
                        if (-not $firstDesktopSeen) { $firstDesktopSeen = Get-Date }
                        $desktops[[int]$row.ProcessId] = @{ process = $candidate; record = [ordered]@{
                            pid = [int]$row.ProcessId; parent_pid = $parentPid; parent_relation = $relation
                            started_after_click_ms = [int]($row.CreationDate - $since).TotalMilliseconds
                            exited = $false; lifetime_ms = $null; exit_code = $null } }
                        # Every candidate is owned for cleanup from first sight.
                        if (-not $replacement) { $replacement = $candidate; $Context.Desktop = $candidate }
                    }
                }
            }
            foreach ($entry in $desktops.Values) {
                if (-not $entry.record.exited -and $entry.process.HasExited) {
                    $entry.record.exited = $true; $entry.record.exit_code = $entry.process.ExitCode
                    $entry.record.lifetime_ms = [int]($entry.process.ExitTime.ToUniversalTime() - $entry.process.StartTime.ToUniversalTime()).TotalMilliseconds
                }
            }
            # Settle: observe every post-click Desktop for 60 s after the first.
            if ($receipt -and $supervisor -and $supervisor.HasExited -and $updater -and $updater.HasExited -and $original.HasExited -and $firstDesktopSeen -and ((Get-Date) - $firstDesktopSeen).TotalSeconds -ge 60) { break }
            Start-Sleep -Milliseconds 100
        } while ((Get-Date) -lt $deadline)
        if (-not $result.Contains('update_transition')) { $result.update_transition = @{} }
        $result.update_transition.supervisor_found = $supervisorFound
        $result.update_transition.installer_found = $installerFound
        $result.update_transition.installer_hash_matches_release = $installerHashMatches
        $result.update_transition.desktop_processes = @($desktops.Values | ForEach-Object { $_.record })
        $phase = 'update_installer'
        if (-not $supervisor -or -not $supervisor.HasExited -or $supervisor.ExitCode -ne 0) { throw 'update_supervisor_settled_unproven' }
        if (-not $updater -or -not $updater.HasExited -or $updater.ExitCode -ne 0) { throw 'update_installer_exit_zero_unproven' }
        Verdict 'update_installer' 'PASS' 'observed_owned_updater_process_matching_signed_target_installer_hash_and_exit_zero'
        $phase = 'update_relaunch'
        if (-not $original.HasExited -or $original.ExitCode -ne 0) { throw 'update_real_relaunch_unproven' }
        $decision = Resolve-UpdateDesktops @($desktops.Values | ForEach-Object { $_.record })
        $result.update_transition.single_instance_handoffs = @($desktops.Values | Where-Object { $_.record.exited } | ForEach-Object { $_.record.pid }).Count
        if ($decision.reason) { throw $decision.reason }
        $replacement = $desktops[$decision.survivor].process
        $Context.Desktop = $replacement
        $deadline = (Get-Date).AddMinutes(3)
        do {
            $replacement.Refresh()
            if ($replacement.HasExited) { throw 'update_relaunched_desktop_exited' }
            if ($replacement.MainWindowHandle -ne 0) { break }
            Start-Sleep -Milliseconds 200
        } while ((Get-Date) -lt $deadline)
        if ($replacement.MainWindowHandle -eq 0) { throw 'update_relaunched_window_unproven' }
        Verdict 'update_relaunch' 'PASS' 'retained_original_desktop_exited_and_distinct_owned_exact_path_desktop_window_relaunched'
        $phase = 'update_versions'
        $desktopVersion = $replacement.MainModule.FileVersionInfo.ProductVersion
        if ($desktopVersion -ne $inputData.update.desktop_version) { throw 'update_desktop_version_mismatch' }
        $driverProof = Assert-InstalledDriver $install $inputData.update.driver_hash $inputData.update.driver_size $inputData.update.driver_provenance
        $driverHash = $driverProof.Hash
        # Resolve the NEW installed runtime manifest, never retain the rc3 slot.
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
        if (-not $runtimeVersion -or $runtimeVersion.current -ne $inputData.update.version) { throw 'update_runtime_version_mismatch' }
        $debuggers = Pin-UpdateDebugger $replacement $cdpPort
        try {
            $panel = Run-Process $newNode @((Join-Path $Root 'cdp.mjs'), '--port', [string]$cdpPort, '--origin', $newOrigin.GetLeftPart([UriPartial]::Authority), '--action', 'update-verify', '--current-version', $inputData.update.version) 90 | ConvertFrom-Json
            foreach ($debugger in $debuggers) { if ($debugger.HasExited) { throw 'update_cdp_generation_changed_during_proof' } }
            $after = Pin-UpdateDebugger $replacement $cdpPort
            try {
                if (@($after | Where-Object { $_.Id -notin @($debuggers | ForEach-Object { $_.Id }) }).Count) { throw 'update_cdp_generation_changed_during_proof' }
            } finally { foreach ($handle in $after) { $handle.Dispose() } }
            $replacement.Refresh()
            if ($replacement.HasExited -or $replacement.MainWindowHandle -eq 0) { throw 'update_replacement_generation_exited' }
        } finally { foreach ($handle in $debuggers) { $handle.Dispose() } }
        if (-not ($panel.ok -and $panel.badge_clicked -and $panel.current_row_verified) -or $panel.current -ne $inputData.update.version -or $panel.auto_update -ne $receipt.auto_update -or $panel.prereleases -ne $receipt.prereleases) { throw 'update_current_row_or_settings_preservation_unproven' }
        $result.update_transition.desktop_version = $desktopVersion
        $result.update_transition.runtime_version = $runtimeVersion.current
        $result.update_transition.installed_driver_sha256 = $driverHash
        $result.update_transition.installed_driver_size = $driverProof.Size
        $result.update_transition.installed_provenance_verified = $driverProof.ProvenanceVerified
        $result.update_transition.current_row_verified = $true
        $result.update_transition.panel_after_relaunch = $panel.steps.opened
        Verdict 'update_versions' 'PASS' 'relaunched_desktop_runtime_current_panel_row_and_installed_stage2_driver_match_target_settings_preserved'
        $completed = $true; $Context.Quiesced = $true
    } catch {
        $reason = if ($_.Exception.Message -match '^update_[a-z_]+$') { $_.Exception.Message } else { 'update_observation_failed' }
        $verdict = if ($reason -in @('update_target_not_discoverable', 'update_shared_cdp_prerequisite_unproven')) { 'NOT RUN' } else { 'FAIL' }
        Verdict $phase $verdict $reason
        throw 'update_acceptance_case_failed'
    } finally {
        try { Stop-UpdateHandle $clicker } finally {
            if ($clicker) { $clicker.Dispose() }
            if (-not $completed) {
                # Stop every post-click Desktop, not only the chosen one.
                $extraFailed = $false
                foreach ($entry in $desktops.Values) { if ($entry.process -ne $replacement) { try { Stop-UpdateHandle $entry.process } catch { $extraFailed = $true } } }
                try {
                    Stop-UpdateTransaction $Context $original $replacement $supervisor $updater $since
                    if ($extraFailed) { $Context.Quiesced = $false; throw 'update_transaction_cleanup_unproven' }
                } catch { Verdict 'update_installer' 'FAIL' 'update_transaction_cleanup_unproven_uninstall_suppressed' }
            }
            foreach ($process in @($supervisor, $updater)) { if ($process) { $process.Dispose() } }
            foreach ($entry in $desktops.Values) { if ($entry.process -ne $Context.Desktop) { $entry.process.Dispose() } }
            if ($replacement) { $original.Dispose() }
        }
    }
}
