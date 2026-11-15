# Enabled-RC scaffolding. The frozen activation installer is the only product
# under test. This file never patches it or manufactures a Turn/session token.
function Invoke-EnabledCases([hashtable] $Context) {
    Add-Type -Path (Join-Path $Root 'Native.cs')
    $targetPath = Join-Path $env:SystemRoot 'System32\notepad.exe'
    $driverPath = Join-Path $install 'computer-use\cua-driver.exe'
    $currentCase = 'settings_enable'
    function Status {
        return Invoke-RestMethod -Uri ($origin.AbsoluteUri.TrimEnd('/') + '/api/desktop/computer-use/status') -TimeoutSec 5
    }
    function Wait-Status([string] $Expected, [int] $Seconds = 60) {
        $deadline = (Get-Date).AddSeconds($Seconds)
        do {
            $answer = Status
            if ($answer.status -eq $Expected) { return }
            Start-Sleep -Milliseconds 300
        } while ((Get-Date) -lt $deadline)
        throw 'expected_computer_use_status_not_reached'
    }
    function Enable-FromSettings {
        $answer = Status
        if ($answer.status -eq 'ready') { return }
        if ($answer.status -ne 'off') { Wait-Status 'off' }
        $null = Run-Process $node @((Join-Path $Root 'cdp.mjs'), '--port', [string]$cdpPort, '--origin', $origin.GetLeftPart([UriPartial]::Authority), '--action', 'enable') 90
        Wait-Status 'ready'
    }
    function Run-CaseTurn([string] $Case) {
        $null = Run-Process $python @('-I', '-B', '-m', 'vibe', 'agent', 'run', '--agent', 'vibey', '--model', 'cu-acceptance', '--reasoning-effort', 'low', '--cwd', $work, '--message', ('AVIBE_CU_CASE:' + $Case), '--sync', '--wait-timeout', '120') 180
        $answer = Get-Content -LiteralPath (Join-Path $work ($Case + '.json')) -Raw | ConvertFrom-Json
        if ($answer.ok -ne $true -or $answer.kind -ne 'computer_js') { throw 'shipped_cli_case_failed' }
        return $answer.result
    }
    function Driver-Handles([int] $AppPid) {
        $rows = @(Get-CimInstance Win32_Process)
        $ids = [Collections.Generic.HashSet[int]]::new()
        foreach ($row in $rows) {
            if ($row.ExecutablePath -eq $driverPath) {
                $owner = Invoke-CimMethod -InputObject $row -MethodName GetOwnerSid
                if ($owner.ReturnValue -eq 0 -and $owner.Sid -eq $inputData.expected_sid) { $null = $ids.Add([int]$row.ProcessId) }
            }
        }
        if (-not $ids.Count) { throw 'live_installed_driver_missing' }
        do {
            $changed = $false
            foreach ($row in $rows) {
                if ($ids.Contains([int]$row.ParentProcessId) -and [int]$row.ProcessId -ne $AppPid) {
                    if ($ids.Add([int]$row.ProcessId)) { $changed = $true }
                }
            }
        } while ($changed)
        $handles = [Collections.Generic.List[Diagnostics.Process]]::new()
        foreach ($id in $ids) {
            try { $p = [Diagnostics.Process]::GetProcessById($id); $null = $p.SafeHandle; $handles.Add($p) } catch [ArgumentException] { }
        }
        if (-not $handles.Count) { throw 'live_driver_handles_missing' }
        return ,$handles
    }
    function Assert-NoReplacementDriver([Collections.Generic.List[Diagnostics.Process]] $Prior, [int] $AppPid) {
        $known = [Collections.Generic.HashSet[int]]::new()
        foreach ($process in $Prior) { $null = $known.Add($process.Id) }
        # Include late descendants and new driver generations, not just the
        # pre-action handles. Stay quiet across the native five-second reprobe.
        $quietUntil = (Get-Date).AddSeconds(6)
        do {
            $rows = @(Get-CimInstance Win32_Process)
            do {
                $changed = $false
                foreach ($row in $rows) {
                    if ([int]$row.ProcessId -ne $AppPid -and ($row.ExecutablePath -eq $driverPath -or $known.Contains([int]$row.ParentProcessId))) {
                        if ($known.Add([int]$row.ProcessId)) { $changed = $true }
                    }
                }
            } while ($changed)
            foreach ($row in $rows) {
                if (-not $known.Contains([int]$row.ProcessId) -or [int]$row.ProcessId -eq $AppPid) { continue }
                $owner = Invoke-CimMethod -InputObject $row -MethodName GetOwnerSid -ErrorAction SilentlyContinue
                if (-not $owner -or $owner.ReturnValue -ne 0 -or $owner.Sid -ne $inputData.expected_sid) { continue }
                $live = $null
                try {
                    $live = [Diagnostics.Process]::GetProcessById([int]$row.ProcessId)
                    $null = $live.SafeHandle
                    if (-not $live.HasExited) { throw 'replacement_driver_or_late_helper_survived_shutdown' }
                } catch [ArgumentException] { } finally { if ($live) { $live.Dispose() } }
            }
            Start-Sleep -Milliseconds 500
        } while ((Get-Date) -lt $quietUntil)
    }
    try {
        $initial = Status
        if ($initial.supported -ne $true -or $initial.status -ne 'off' -or $initial.host_platform -ne 'windows') { throw 'activation_default_off_not_proven' }
        Enable-FromSettings
        Verdict 'settings_enable' 'PASS' 'real_settings_preview_turn_on_button_reached_ready'
        foreach ($operation in @('off', 'quit', 'crash')) {
            $currentCase = @{off='survival_cu_off'; quit='survival_desktop_quit'; crash='survival_desktop_crash'}[$operation]
            Enable-FromSettings
            $text = 'Avibe signed acceptance unsaved draft ' + $operation
            $source = @"
const target = await computer.app({path: $(ConvertTo-Json -InputObject $targetPath -Compress)});
const started = await target.launch();
const win = await started.app.waitFor({onScreen: true}, {timeout: 15000});
await win.observe({include_screenshot: false});
await win.type($(ConvertTo-Json -InputObject $text -Compress));
await win.observe({include_screenshot: false});
({pid: started.app.pid, hwnd: win.windowId})
"@
            Set-Content -LiteralPath (Join-Path $work ("launch-$operation.js")) -Value $source -Encoding utf8
            $appResult = Run-CaseTurn "launch-$operation"
            $target = [Diagnostics.Process]::GetProcessById([int]$appResult.pid)
            $null = $target.SafeHandle
            $helpers = $null
            try {
                if ($target.HasExited -or $target.MainModule.FileName -ne $targetPath -or [AcceptanceNative]::IsElevated($target.Id)) { throw 'real_created_standard_notepad_not_proven' }
                $window = [IntPtr][long]$appResult.hwnd
                if (-not [AcceptanceNative]::UnsavedTextEquals($window, $text)) { throw 'shipped_type_did_not_create_unsaved_text' }
                $helpers = Driver-Handles $target.Id
                if ($operation -eq 'off') {
                    $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
                    $null = Run-Process $powershell @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Root 'tray-off.ps1')) 30
                    Wait-Status 'off' 30
                } elseif ($operation -eq 'quit') {
                    $Context.Desktop.Refresh()
                    [AcceptanceNative]::InvokeMenu($Context.Desktop.MainWindowHandle, 'Quit', $false)
                    $deadline = (Get-Date).AddSeconds(30)
                    while (-not $Context.Desktop.WaitForExit(200) -and (Get-Date) -lt $deadline) {
                        $null = [AcceptanceNative]::ClickDialogButton($Context.Desktop.Id, 'Keep Runtime running')
                    }
                    if (-not $Context.Desktop.HasExited -or $Context.Desktop.ExitCode -ne 0) { throw 'real_native_quit_not_confirmed' }
                } else {
                    $Context.Desktop.Kill()
                    if (-not $Context.Desktop.WaitForExit(10000)) { throw 'desktop_crash_not_observed' }
                }
                $deadline = (Get-Date).AddSeconds(30)
                foreach ($helper in $helpers) {
                    $remaining = [Math]::Max(0, [int]($deadline - (Get-Date)).TotalMilliseconds)
                    if (-not $helper.WaitForExit($remaining)) { throw 'driver_or_helper_survived_shutdown' }
                }
                Assert-NoReplacementDriver $helpers $target.Id
                if ($target.HasExited -or -not [AcceptanceNative]::UnsavedTextEquals($window, $text)) { throw 'original_app_or_unsaved_text_did_not_survive' }
                Verdict $currentCase 'PASS' 'original_app_and_unsaved_text_survived_pinned_helpers_exited_and_no_replacement_generation'
            } finally {
                if ($helpers) { foreach ($helper in $helpers) { $helper.Dispose() } }
                if (-not $target.HasExited) { $target.Kill(); $null = $target.WaitForExit(10000) }
                $target.Dispose()
            }
            if ($operation -ne 'off') {
                $Context.Desktop.Dispose()
                $Context.Desktop = Start-Process -FilePath $apps[0].FullName -PassThru
                $null = $Context.Desktop.SafeHandle
                Wait-Status 'ready' 90
            }
        }
        $currentCase = 'second_user_pipe_lock'
        $state = Get-Content -LiteralPath (Join-Path $env:APPDATA 'bot.avibe.desktop\computer-use.json') -Raw | ConvertFrom-Json
        if ($state.state -ne 'ready' -or -not $state.socket_path.StartsWith('\\.\pipe\')) { throw 'ready_primary_pipe_missing' }
        # Positive control: the actual primary ordinary user can open its pipe.
        $ownerPipe = [IO.Pipes.NamedPipeClientStream]::new('.', $state.socket_path.Substring(9), [IO.Pipes.PipeDirection]::InOut)
        try { $ownerPipe.Connect(3000) } finally { $ownerPipe.Dispose() }
        $request = Join-Path $work 'security-request.json'
        @{action='prepare-security-fixtures'} | ConvertTo-Json | Set-Content -LiteralPath ($request + '.tmp')
        Move-Item -LiteralPath ($request + '.tmp') -Destination $request
        $response = Join-Path $work 'security-response.json'
        $deadline = (Get-Date).AddSeconds(120)
        while (-not (Test-Path -LiteralPath $response) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
        $currentCase = 'security_fixture'
        $securityResult = Get-Content -LiteralPath $response -Raw | ConvertFrom-Json -AsHashtable
        foreach ($id in $securityResult.cases.Keys) {
            if ($id -notin @('security_fixture', 'elevated_driver_refused', 'second_user_pipe_lock')) { throw 'unknown_security_verdict' }
            Verdict $id $securityResult.cases[$id].verdict $securityResult.cases[$id].reason
        }
        if (-not $securityResult.ok) { throw 'security_fixture_failed_see_individual_verdicts' }
        if ($securityResult.cases.second_user_pipe_lock.verdict -ne 'PASS' -or $securityResult.cases.elevated_driver_refused.verdict -ne 'PASS') { throw 'security_verdict_missing' }
        try {
            if (-not [AcceptanceNative]::IsElevated([int]$securityResult.elevated_pid)) { throw 'elevated_target_query_access_unproven' }
        } catch { throw 'elevated_target_query_access_unproven' }
        $currentCase = 'elevated_app_refused'
        $elevatedScript = @"
const app = await computer.app({nameContains: 'notepad'});
if (app.pid !== $($securityResult.elevated_pid)) throw new Error('wrong_elevated_target');
const win = await app.window({windowId: $($securityResult.elevated_hwnd)});
await win.observe({include_screenshot: false});
let refused = false;
try { await win.type('must-not-be-inserted'); } catch (error) { if (error.code === 'protected_window') refused = true; else throw error; }
({protected_window: refused})
"@
        Set-Content -LiteralPath (Join-Path $work 'attach-elevated.js') -Value $elevatedScript -Encoding utf8
        $refusal = Run-CaseTurn 'attach-elevated'
        if ($refusal.protected_window -ne $true) { throw 'explicit_protected_window_refusal_missing' }
        Verdict 'elevated_app_refused' 'PASS' 'real_elevated_notepad_control_refused_with_protected_window'
    } catch {
        $reason = if ($_.Exception.Message -match '^[a-z][a-z0-9_]+$') { $_.Exception.Message } else { 'enabled_acceptance_operation_failed' }
        Verdict $currentCase 'FAIL' $reason
        throw 'enabled_acceptance_case_failed'
    }
}
