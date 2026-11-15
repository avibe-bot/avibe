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
                if ($target.HasExited -or -not [AcceptanceNative]::UnsavedTextEquals($window, $text)) { throw 'original_app_or_unsaved_text_did_not_survive' }
                Verdict $currentCase 'PASS' 'same_real_created_app_and_unsaved_text_survived_all_recorded_driver_helpers_exited'
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
        $securityResult = Get-Content -LiteralPath $response -Raw | ConvertFrom-Json
        if (-not ($securityResult.ok -and $securityResult.second_user_pipe_denied -and $securityResult.second_user_lock_denied)) { throw 'second_user_pipe_lock_not_denied' }
        Verdict 'second_user_pipe_lock' 'PASS' 'owner_pipe_open_positive_control_second_users_only_account_denied_pipe_and_lock'
        if (-not $securityResult.elevated_driver_refused) { throw 'elevated_driver_refusal_missing' }
        Verdict 'elevated_driver_refused' 'PASS' 'exact_installed_driver_refused_elevated_serve_before_binding'
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
