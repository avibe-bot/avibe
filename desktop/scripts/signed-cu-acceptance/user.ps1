param([Parameter(Mandatory)][string] $Root)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$inputData = Get-Content -LiteralPath (Join-Path $Root 'input.json') -Raw | ConvertFrom-Json
$work = Join-Path $Root 'work'
$result = @{ installed_driver_sha256 = $null; cases = [ordered]@{} }
$stage = 'standard_user'; $failed = $false; $desktop = $null; $install = $null; $python = $null; $stub = $null; $caseContext = $null; $updateContext = $null
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
        $text = $stdout.GetAwaiter().GetResult(); $errors = $stderr.GetAwaiter().GetResult()
        if ($p.ExitCode -ne 0) {
            if ($Arguments -contains (Join-Path $Root 'cdp.mjs')) {
                try {
                    $detail = $text | ConvertFrom-Json -AsHashtable
                    if ($detail['reason'] -match '^(cdp|settings|update)_[a-z_]+$') { $result.cdp_detail = $detail['reason'] }
                } catch { }
            }
            # Classify only known shipped diagnostics; never publish their raw
            # text, paths, PIDs or arbitrary command output.
            foreach ($entry in @(
                @('Avibe service did not stop', 'runtime_service_stop_failed'),
                @('Avibe UI did not stop', 'runtime_ui_stop_failed'),
                @('Desktop backend installer processes did not stop', 'runtime_backend_install_stop_failed'),
                @('remote access tunnel could not be stopped', 'runtime_tunnel_stop_failed')
            )) { if ($errors.Contains($entry[0])) { throw $entry[1] } }
            throw "child_process_exit_$($p.ExitCode)"
        }
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
    $portProbe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $portProbe.Start(); $cdpPort = $portProbe.LocalEndpoint.Port; $portProbe.Stop()
    $result.cdp_port = $cdpPort
    $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "--remote-debugging-port=$cdpPort --remote-debugging-address=127.0.0.1"
    $desktop = Start-Process -FilePath $apps[0].FullName -PassThru
    $null = $desktop.SafeHandle
    $runtime = Join-Path $env:LOCALAPPDATA 'bot.avibe.desktop\runtime'
    $manifest = Get-Content -LiteralPath (Join-Path $install 'runtime\runtime-manifest.json') -Raw | ConvertFrom-Json
    $runtimeSlot = Join-Path (Join-Path $runtime $manifest.runtime_version) $manifest.archive_sha256.Substring(0, 16)
    $python = Join-Path $runtimeSlot $manifest.python_entrypoint
    $node = Join-Path $runtimeSlot $manifest.node_entrypoint
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'manifest_runtime_interpreter_missing' }
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
    $null = Run-Process $python @('-I', '-B', (Join-Path $Root 'configure_stub.py'), '--origin', $origin.GetLeftPart([UriPartial]::Authority), '--port', '0', '--work', $work, '--setup-only') 120
    $cdpPhase = 'http'
    try {
        $cdpDeadline = (Get-Date).AddSeconds(45)
        do {
            $targets = Invoke-RestMethod -Uri "http://127.0.0.1:$cdpPort/json/list" -TimeoutSec 5
            $cdpPhase = 'workbench_target'
            $workbench = @($targets | Where-Object { $_.type -eq 'page' -and $_.url.StartsWith($origin.AbsoluteUri.TrimEnd('/') + '/') })
            if ($workbench.Count) { break }
            Start-Sleep -Milliseconds 500
        } while ((Get-Date) -lt $cdpDeadline)
        $result.cdp_diagnostics = @{ targets = @($targets).Count; workbench_targets = $workbench.Count; target_metadata = @($targets | ForEach-Object {
            $targetUri = $null
            $validUri = [Uri]::TryCreate([string]$_.url, [UriKind]::Absolute, [ref]$targetUri)
            @{ type = if ($_.type -in @('page', 'webview', 'iframe', 'service_worker')) { $_.type } else { 'other' }; loopback = $validUri -and $targetUri.IsLoopback; runtime_port = $validUri -and $targetUri.Port -eq $origin.Port }
        }) }
        $cdpPhase = 'listener'
        $listeners = @(Get-NetTCPConnection -LocalPort $cdpPort -State Listen -ErrorAction Stop)
        if ($workbench.Count -lt 1 -or $listeners.Count -lt 1 -or @($listeners | Where-Object { $_.LocalAddress -notin @('127.0.0.1', '::1') }).Count) {
            throw 'cdp_target_or_loopback_binding_unproven'
        }
        $cdpPhase = 'settings_page'
        $page = Run-Process $node @((Join-Path $Root 'cdp.mjs'), '--port', [string]$cdpPort, '--origin', $origin.GetLeftPart([UriPartial]::Authority), '--action', 'inspect') 90 | ConvertFrom-Json
        if ($page.ok -ne $true) { throw 'cdp_settings_page_unproven' }
        Verdict 'webview_cdp_preflight' 'PASS' 'real_settings_rail_link_clicked_and_installed_webview_dom_inspected_over_loopback_cdp'
    } catch {
        $result.cdp_failure = @{ phase = $cdpPhase; exception_type = $_.Exception.GetType().FullName; script_line = $_.InvocationInfo.ScriptLineNumber }
        Verdict 'webview_cdp_preflight' 'NOT RUN' ('signed_webview_cdp_unproven_' + $cdpPhase)
    }

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
    }

    if ($inputData.mode -eq 'update') {
        $stage = 'update_panel_start'
        . (Join-Path $Root 'update.ps1')
        $updateContext = @{ Desktop = $desktop; Python = $python }
        try { Invoke-UpdateCase $updateContext } finally { $desktop = $updateContext.Desktop; $python = $updateContext.Python }
    } else {
    $stage = 'model_stub_tool_path'
    $portProbe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $portProbe.Start(); $stubPort = $portProbe.LocalEndpoint.Port; $portProbe.Stop()
    $result.stub_port = $stubPort
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $python; $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    foreach ($arg in @('-I', '-B', (Join-Path $Root 'model_stub.py'), '--port', [string]$stubPort, '--python', $python, '--work', $work)) { $psi.ArgumentList.Add($arg) }
    $stub = [Diagnostics.Process]::Start($psi)
    $null = $stub.SafeHandle
    $deadline = (Get-Date).AddSeconds(15)
    do {
        try { $catalog = Invoke-RestMethod -Uri "http://127.0.0.1:$stubPort/v1/models" -TimeoutSec 2; break } catch { Start-Sleep -Milliseconds 200 }
    } while ((Get-Date) -lt $deadline)
    if ($stub.HasExited) { throw 'deterministic_upstream_exited' }
    $null = Run-Process $python @('-I', '-B', (Join-Path $Root 'configure_stub.py'), '--origin', $origin.GetLeftPart([UriPartial]::Authority), '--port', [string]$stubPort, '--work', $work) 480
    Set-Content -LiteralPath (Join-Path $work 'probe-context.js') -Value '({fixture_admission: true})' -Encoding utf8
    $turn = Run-Process $python @('-I', '-B', '-m', 'vibe', 'agent', 'run', '--agent', 'vibey', '--model', 'cu-acceptance', '--reasoning-effort', 'low', '--cwd', $work, '--message', 'AVIBE_CU_CASE:probe-context', '--sync', '--wait-timeout', '120') 180 | ConvertFrom-Json -AsHashtable
    $result.stub_turn_diagnostics = @{ ok = $turn['ok']; kind = $turn['kind']; fields = @($turn.Keys) }
    if ($turn.Contains('run')) {
        $result.stub_turn_diagnostics.run_status = $turn.run['status']
        $result.stub_turn_diagnostics.run_fields = @($turn.run.Keys)
    }
    $receiptPath = Join-Path $work 'stub-receipt.json'
    if (Test-Path -LiteralPath $receiptPath) { $result.stub_receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json -AsHashtable }
    $probe = Get-Content -LiteralPath (Join-Path $work 'probe-context.json') -Raw | ConvertFrom-Json -AsHashtable
    # This is deliberately BEFORE Settings enable. The genuine live Turn is
    # admitted first, then the shipped JS endpoint refuses its default-off state.
    # Requiring JS success here incorrectly treats the safe guard as a failure.
    if ($probe['ok'] -ne $false -or $probe['code'] -ne 'computer_use_unavailable' -or $probe['details']['error']['params']['status'] -ne 'off') { throw 'shipped_cli_default_off_guard_not_proven' }
    Verdict 'model_stub_tool_path' 'PASS' 'real_controller_turn_shipped_cli_reached_default_off_guard_deterministic_stub_not_llm_js_not_executed'
    $stage = 'real_win32_fixture_preflight'
    Add-Type -Path (Join-Path $Root 'Native.cs')
    if ((Get-FileHash -LiteralPath $inputData.target_app -Algorithm SHA256).Hash.ToLowerInvariant() -ne $inputData.target_app_hash) { throw 'target_fixture_executable_changed' }
    $writeHandle = $null
    try {
        $writeHandle = [IO.File]::Open($inputData.target_app, [IO.FileMode]::Open, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
        throw 'target_fixture_binary_writable_by_standard_user'
    } catch [UnauthorizedAccessException] { } finally { if ($writeHandle) { $writeHandle.Dispose() } }
    $targetSettings = Join-Path $work 'editor-preflight-settings'
    New-Item -ItemType Directory -Path $targetSettings -Force | Out-Null
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $inputData.target_app; $psi.UseShellExecute = $false
    foreach ($arg in @('-multiInst', '-nosession', '-noPlugin', ('-settingsDir=' + $targetSettings))) { $psi.ArgumentList.Add($arg) }
    $realEditor = [Diagnostics.Process]::Start($psi)
    $null = $realEditor.SafeHandle
    try {
        $deadline = (Get-Date).AddSeconds(20)
        do {
            $realEditor.Refresh()
            if ($realEditor.HasExited) { throw 'real_editor_was_launcher_stub' }
            if ($realEditor.MainWindowHandle -ne 0) { break }
            Start-Sleep -Milliseconds 100
        } while ((Get-Date) -lt $deadline)
        if ($realEditor.MainWindowHandle -eq 0 -or $realEditor.MainModule.FileName -ne $inputData.target_app -or [AcceptanceNative]::IsElevated($realEditor.Id)) { throw 'created_standard_editor_identity_unproven' }
        if (-not [AcceptanceNative]::UnsavedTextEquals($realEditor.MainWindowHandle, '')) { throw 'visible_unsaved_editor_readback_unproven' }
        Verdict 'real_win32_fixture_preflight' 'PASS' 'exact_created_standard_editor_pid_window_and_blank_unsaved_document_readable_not_cu_launch_acceptance'
    } finally {
        if (-not $realEditor.HasExited) { $realEditor.Kill(); $null = $realEditor.WaitForExit(10000) }
        $realEditor.Dispose()
    }
    if ($inputData.mode -eq 'preparation') {
        # Fixture feasibility only, NOT elevated-app control acceptance.
        try {
            $request = Join-Path $work 'query-fixture-request.json'
            @{ action = 'prepare-elevated-query-fixture' } | ConvertTo-Json | Set-Content -LiteralPath ($request + '.tmp')
            Move-Item -LiteralPath ($request + '.tmp') -Destination $request
            $response = Join-Path $work 'query-fixture-response.json'
            $deadline = (Get-Date).AddSeconds(45)
            while (-not (Test-Path -LiteralPath $response) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
            $queryFixture = Get-Content -LiteralPath $response -Raw | ConvertFrom-Json -AsHashtable
            if (-not $queryFixture.ok) { $result.query_fixture_detail = $queryFixture.reason; throw 'elevated_target_query_access_unproven' }
            Add-Type -Path (Join-Path $Root 'Native.cs')
            if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne $inputData.expected_sid -or [AcceptanceNative]::IsElevated($PID)) { throw 'ordinary_query_caller_identity_changed' }
            if (-not [AcceptanceNative]::IsElevated([int]$queryFixture.elevated_pid)) { throw 'elevated_target_query_access_unproven' }
            Verdict 'elevated_target_query_preflight' 'PASS' 'users_only_caller_reopened_elevated_target_process_and_token_with_query_only_object_grants_not_control_acceptance'
        } catch { Verdict 'elevated_target_query_preflight' 'NOT RUN' 'elevated_target_query_access_unproven' }
        # Follow-on preparation only, after the CU/shared fixture prerequisites.
        # No Update click occurs until an explicitly selected public candidate.
        if ($result.cases.webview_cdp_preflight.verdict -eq 'PASS' -and $result.cases.real_win32_fixture_preflight.verdict -eq 'PASS' -and $result.cases.elevated_target_query_preflight.verdict -eq 'PASS') {
            try {
                $panel = Run-Process $node @((Join-Path $Root 'cdp.mjs'), '--port', [string]$cdpPort, '--origin', $origin.GetLeftPart([UriPartial]::Authority), '--action', 'update-inspect', '--current-version', $inputData.release_version) 90 | ConvertFrom-Json
                if (-not ($panel.ok -and $panel.badge_clicked -and $panel.check_clicked -and $panel.fresh_check) -or $panel.current -ne $inputData.release_version) { throw 'update_panel_preflight_unproven' }
                $result.update_panel_preflight = @{ current = $panel.current; fresh_check = $panel.fresh_check; panel_view = $panel.panel_view; auto_update = $panel.auto_update; prereleases = $panel.prereleases }
                Verdict 'unified_update_panel_preflight' 'PASS' 'real_version_badge_and_check_clicked_fresh_terminal_receipt_and_current_row_verified_no_update_attempt'
            } catch {
                $reason = if ($result.Contains('cdp_detail') -and $result.cdp_detail -match '^update_[a-z_]+$') { $result.cdp_detail } else { 'unified_update_panel_click_unproven' }
                Verdict 'unified_update_panel_preflight' 'NOT RUN' $reason
            }
        } else { Verdict 'unified_update_panel_preflight' 'NOT RUN' 'shared_cu_fixture_prerequisite_not_proven_on_this_run' }
    }
    if ($inputData.mode -eq 'enabled') {
        $stage = 'settings_enable'
        . (Join-Path $Root 'enabled.ps1')
        $caseContext = @{ Desktop = $desktop }
        try { Invoke-EnabledCases $caseContext } finally { $desktop = $caseContext.Desktop }
    }
    }
} catch {
    $failed = $true
    $result.failure = @{ exception_type = $_.Exception.GetType().FullName; script_line = $_.InvocationInfo.ScriptLineNumber; hresult = $_.Exception.HResult }
    $reason = if ($_.Exception.Message -match '^[a-z][a-z0-9_]+$') { $_.Exception.Message } else { 'ordinary_user_operation_failed' }
    if ($reason -notin @('enabled_acceptance_case_failed', 'update_acceptance_case_failed')) { Verdict $stage 'FAIL' $reason }
} finally {
    try {
        if ($updateContext -and -not $updateContext.Quiesced) {
            # Parent account teardown must first stop every owned process. Do
            # not race an unproven updater transaction with Quit or uninstall.
            $stage = 'uninstall'
            throw 'update_transaction_cleanup_unproven_uninstall_suppressed'
        }
        if ($stub) { if (-not $stub.HasExited) { $stub.Kill(); $null = $stub.WaitForExit(10000) }; $stub.Dispose() }
        # Preflight the real menu route; no target-app survival is claimed here.
        if ($desktop -and -not $desktop.HasExited) {
            try {
                Add-Type -Path (Join-Path $Root 'Native.cs')
                $desktop.Refresh()
                [AcceptanceNative]::InvokeMenu($desktop.MainWindowHandle, 'Quit', $false)
                $deadline = (Get-Date).AddSeconds(30)
                while (-not $desktop.WaitForExit(200) -and (Get-Date) -lt $deadline) {
                    $null = [AcceptanceNative]::ClickDialogButton($desktop.Id, 'Keep Runtime running')
                }
                if (-not $desktop.HasExited -or $desktop.ExitCode -ne 0) { throw 'native_quit_not_confirmed' }
                Verdict 'native_menu_quit_preflight' 'PASS' 'real_native_quit_menu_and_keep_runtime_dialog_exited_desktop'
            } catch { Verdict 'native_menu_quit_preflight' 'NOT RUN' 'native_menu_quit_unproven' }
        }
        # Forced fallback is cleanup only, never a graceful-quit PASS.
        if ($desktop -and -not $desktop.HasExited) { $desktop.Kill(); $null = $desktop.WaitForExit(10000) }
        if ($desktop) { $desktop.Dispose() }
        if ($python) {
            $stage = 'teardown_stop_runtime'
            $null = Run-Process $python @('-I', '-B', '-m', 'vibe', 'stop') 120
        }
        $stage = 'uninstall'
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
    } catch {
        $result.failure = @{ phase = $stage; exception_type = $_.Exception.GetType().FullName; script_line = $_.InvocationInfo.ScriptLineNumber; hresult = $_.Exception.HResult }
        $reason = if ($_.Exception.Message -match '^[a-z][a-z0-9_]+$') { $_.Exception.Message } else { 'ordinary_user_uninstall_failed' }
        Verdict 'uninstall' 'FAIL' $reason
        $failed = $true
    }
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $work 'result.json') -Encoding utf8
}
if ($failed) { exit 1 }
