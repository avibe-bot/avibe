# Enabled-RC scaffolding. The frozen activation installer is the only product
# under test. This file never patches it or manufactures a Turn/session token.
. (Join-Path $PSScriptRoot 'evidence.ps1')
function New-InputDiagnostics {
    return [ordered]@{
        phase = 'not_started'; first_error = $null; secondary_errors = @()
        ready_stable = $null; ready_usable = $null; ready = $null
        activation = $null; native_focus_before_type = $null
        typed_attempted = $false; type_attempt_count = 0
        type = $null; type_error = $null; after = $null; observation_changed = $null
    }
}
function Invoke-EnabledCases([hashtable] $Context) {
    $targetPath = $inputData.target_app
    $driverPath = Join-Path $install 'computer-use\cua-driver.exe'
    $currentCase = 'settings_enable'
    function Status {
        return Invoke-RestMethod -Uri ($origin.AbsoluteUri.TrimEnd('/') + '/api/desktop/computer-use/status') -TimeoutSec 5
    }
    function Wait-Status([string] $Expected, [int] $Seconds = 60) {
        Set-CasePhase ('status_wait_' + $Expected)
        $deadline = (Get-Date).AddSeconds($Seconds)
        do {
            $answer = Status
            if ($answer.status -eq $Expected) { return }
            Start-Sleep -Milliseconds 300
        } while ((Get-Date) -lt $deadline)
        throw 'expected_computer_use_status_not_reached'
    }
    function Enable-FromSettings {
        Set-CasePhase 'settings_status'
        $answer = Status
        if ($answer.status -eq 'ready') {
            if ($answer.enabled -ne $true) { throw 'settings_enabled_not_confirmed' }
            return
        }
        if ($answer.status -ne 'off') { Wait-Status 'off' }
        Set-CasePhase 'settings_enable_click'
        $null = Run-Process $node @((Join-Path $Root 'cdp.mjs'), '--port', [string]$cdpPort, '--origin', $origin.GetLeftPart([UriPartial]::Authority), '--action', 'enable') 90
        Wait-Status 'ready'
        # The authoritative opt-in, not readiness alone (#3815 switch contract).
        if ((Status).enabled -ne $true) { throw 'settings_enabled_not_confirmed' }
    }
    # Off through the real Settings switch: the page shows the change only once
    # a later status read reports enabled=false; then the endpoint must agree.
    function Disable-FromSettings {
        Set-CasePhase 'settings_disable_click'
        $null = Run-Process $node @((Join-Path $Root 'cdp.mjs'), '--port', [string]$cdpPort, '--origin', $origin.GetLeftPart([UriPartial]::Authority), '--action', 'disable') 90
        Wait-Status 'off' 30
        if ((Status).enabled -ne $false) { throw 'settings_disabled_not_confirmed' }
    }
    function Run-CaseTurn([string] $Case) {
        if (-not $Case.StartsWith('launch-')) {
            $operationId = [guid]::NewGuid().ToString('N')
            $receiptPath = Join-Path $work ("input-$operationId.json")
            $result.cu_input_diagnostics[$operationId] = [ordered]@{
                operation_id=$operationId; operation=$Case; phase='agent_turn'; first_error=$null; wrapper=$null; input=(New-InputDiagnostics)
            }
            @{operation_id=$operationId} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $work ("$Case-context.json")) -Encoding utf8
        }
        $record = $result.cu_input_diagnostics[$operationId]
        $record.phase = 'agent_turn'
        $answer = $null; $operationError = $null
        try {
            $null = Run-Process $python @('-I', '-B', '-m', 'vibe', 'agent', 'run', '--agent', 'vibey', '--model', 'cu-acceptance', '--reasoning-effort', 'low', '--cwd', $work, '--message', ('AVIBE_CU_CASE:' + $Case), '--sync', '--wait-timeout', '180') 210
            $record.phase = 'case_result_read'
            $answer = Get-Content -LiteralPath (Join-Path $work ($Case + '.json')) -Raw | ConvertFrom-Json
            if ($answer.operation_id -ne $operationId) { throw 'input_result_identity_mismatch' }
            if ($answer.kind -ne 'computer_js') { throw 'shipped_cli_case_failed' }
            if ($answer.ok -ne $true) { throw 'input_wrapper_failed' }
        } catch {
            $operationError = $_
            Add-AcceptanceFailure $result $record.phase $_
            $record.first_error = $result.failure
        } finally {
            # Each collector failure is separate from the original operation;
            # invalid/missing current receipts still invalidate a prior success.
            foreach ($item in @(
                @{field='dispatch'; path=(Join-Path $work ("dispatch-$operationId.json")); required=$true},
                @{field='stub'; path=(Join-Path $work ("stub-$operationId.json")); required=$false},
                @{field='wrapper'; path=$receiptPath; required=$Case.StartsWith('launch-')}
            )) {
                try {
                    if (-not (Test-Path -LiteralPath $item.path)) {
                        if ($item.required -and $answer -and $answer.ok) { throw 'input_receipt_missing' }
                        continue
                    }
                    $receipt = Get-Content -LiteralPath $item.path -Raw | ConvertFrom-Json -AsHashtable
                    if ($receipt.operation_id -ne $operationId) { throw 'input_receipt_identity_mismatch' }
                    $record[$item.field] = $receipt
                    if ($item.field -eq 'wrapper') { $record.input = $receipt.diagnostics }
                } catch {
                    Add-AcceptanceFailure $result ('collect_' + $item.field) $_
                    if (-not $operationError) { $operationError = $_; $record.first_error = $result.failure }
                }
            }
            # Final stdout receipts are independent of sidecar persistence.
            # They restore known diagnostics, never forgive a missing/invalid
            # result file or a collector/persistence failure.
            $envelopes = @()
            if ($answer) {
                try { $envelopes += ($answer | ConvertTo-Json -Depth 30 | ConvertFrom-Json -AsHashtable) } catch {
                    Add-AcceptanceFailure $result 'envelope_evidence_decode' $_
                    if (-not $operationError) { $operationError=$_ }
                }
            }
            if ($record.Contains('stub') -and $record.stub.Contains('output_evidence')) { $envelopes += $record.stub.output_evidence }
            foreach ($envelope in $envelopes) {
                try {
                    if ($envelope.operation_id -ne $operationId) { throw 'input_envelope_identity_mismatch' }
                    $reportedFailure = -not $envelope.ok
                    foreach ($mapping in @(@('input_receipt','wrapper'), @('dispatch_receipt','dispatch'))) {
                        if (-not $envelope.Contains($mapping[0]) -or -not $envelope[$mapping[0]]) { continue }
                        $embedded = $envelope[$mapping[0]]
                        if ($embedded.operation_id -ne $operationId) { throw 'input_embedded_receipt_identity_mismatch' }
                        $record[$mapping[1]] = $embedded
                        if ($mapping[1] -eq 'wrapper') { $record.input = $embedded.diagnostics }
                        if ($embedded.first_error) {
                            $reportedFailure = $true
                            if (-not $record.Contains('original_operation_error')) { $record.original_operation_error=$embedded.first_error }
                        }
                        foreach ($field in @('cleanup_errors', 'persistence_errors')) {
                            if ($embedded.Contains($field) -and $embedded[$field].Count) { $reportedFailure=$true }
                        }
                    }
                    if ($reportedFailure) { throw 'input_operation_reported_failure' }
                } catch {
                    Add-AcceptanceFailure $result 'envelope_evidence' $_
                    if (-not $operationError) { $operationError=$_; $record.first_error=$result.failure }
                }
            }
            try { Write-AcceptanceEvidence $result (Join-Path $work 'result.json') } catch {
                Add-AcceptanceFailure $result 'input_evidence_write' $_
                if (-not $operationError) { $operationError = $_; $record.first_error = $result.failure }
            }
        }
        if ($operationError) { throw $operationError }
        return $answer.result
    }
    function Set-CasePhase([string] $Phase) {
        if ($operationId) {
            $result.cu_input_diagnostics[$operationId].phase = $Phase
            Write-AcceptanceEvidence $result (Join-Path $work 'result.json')
        }
    }
    . (Join-Path $Root 'inventory.ps1')
    function Driver-Handles([int] $AppPid) {
        $inventory = [ordered]@{}
        $result.cu_input_diagnostics[$operationId].helper_inventory = $inventory
        try {
            Get-AcceptanceHelpers -DriverPath $driverPath -DriverHash $inputData.driver_hash -OwnerSid $inputData.expected_sid -AppPid $AppPid -DesktopPid $Context.Desktop.Id -Known ([Collections.Generic.HashSet[int]]::new()) -Evidence $inventory
        } finally { Write-AcceptanceEvidence $result (Join-Path $work 'result.json') }
    }
    function Assert-NoReplacementDriver($Prior, [int] $AppPid) {
        $known = [Collections.Generic.HashSet[int]]::new()
        foreach ($process in $Prior) { $null = $known.Add($process.Id) }
        $result.cu_input_diagnostics[$operationId].replacement_inventory = @()
        # The same image/hash/owner observer covers proxies and late descendants.
        $quietUntil = (Get-Date).AddSeconds(6)
        do {
            $inventory = [ordered]@{}; $live = $null
            $result.cu_input_diagnostics[$operationId].replacement_inventory += $inventory
            try {
                $live = Get-AcceptanceHelpers -DriverPath $driverPath -DriverHash $inputData.driver_hash -OwnerSid $inputData.expected_sid -AppPid $AppPid -DesktopPid $Context.Desktop.Id -Known $known -Evidence $inventory -AllowEmpty
                if ($live.Count) { throw 'replacement_driver_or_late_helper_survived_shutdown' }
            } finally {
                if ($null -ne $live) { foreach ($process in $live) { $process.Dispose() } }
                Write-AcceptanceEvidence $result (Join-Path $work 'result.json')
            }
            Start-Sleep -Milliseconds 500
        } while ((Get-Date) -lt $quietUntil)
    }
    function Record-EnabledCaseFailure($original) {
        Add-AcceptanceFailure $result $currentCase $original
        if ($operationId -and $result.cu_input_diagnostics.Contains($operationId)) {
            $record = $result.cu_input_diagnostics[$operationId]
            if (-not $record.first_error) {
                $record.first_error = @{
                    call = $record.phase
                    error_class = $original.Exception.GetType().FullName
                    script_line = $original.InvocationInfo.ScriptLineNumber
                    exit_code = if ($original.Exception.Message -match '^child_process_exit_(-?\d+)$') { [int]$Matches[1] } else { $null }
                }
            }
            Write-AcceptanceEvidence $result (Join-Path $work 'result.json')
        }
        $reason = if ($original.Exception.Message -match '^[a-z][a-z0-9_]+$') { $original.Exception.Message } else { 'enabled_acceptance_operation_failed' }
        # A generic worker error must not replace a specific parent case failure.
        if (-not $result.cases.Contains($currentCase) -or $result.cases[$currentCase].verdict -ne 'FAIL') {
            Verdict $currentCase 'FAIL' $reason
        }
    }
    # Elevated-app decision from evidence the case reads itself. The parent
    # independently re-reads the elevated document afterwards; PASS here is
    # conditional on that empty-document check.
    function Resolve-ElevatedAppVerdict($Evidence) {
        if (-not $Evidence) { return @{verdict='FAIL'; reason='elevated_app_evidence_missing'} }
        $elevated = $Evidence.elevated
        if ($elevated.type_ok -eq $true) { return @{verdict='FAIL'; reason='elevated_type_accepted_product_failure'} }
        if (-not ($Evidence.control.type_ok -eq $true -and $Evidence.control_text_verified)) { return @{verdict='FAIL'; reason='ordinary_control_window_unproven'} }
        # #3817 contract: code protected_window with params.reason=administrator.
        $refused = ($elevated.type_code -eq 'protected_window' -and $elevated.type_reason -eq 'administrator') -or
            ($elevated.error_code -eq 'protected_window' -and $elevated.error_reason -eq 'administrator')
        if ($elevated.pid_match -eq $true -and $refused) {
            return @{verdict='PASS'; reason='real_elevated_notepad_refused_with_protected_window_administrator'}
        }
        return @{verdict='FAIL'; reason='explicit_protected_window_refusal_missing'}
    }
    $operationId = [guid]::NewGuid().ToString('N')
    if (-not $result.ContainsKey('cu_input_diagnostics')) { $result.cu_input_diagnostics = [ordered]@{} }
    $result.cu_input_diagnostics[$operationId] = [ordered]@{
        operation_id=$operationId; operation='settings'; phase='native_observer_load'; first_error=$null; wrapper=$null; input=(New-InputDiagnostics)
    }
    try {
        Set-CasePhase 'native_observer_load'
        Add-Type -Path (Join-Path $Root 'Native.cs')
        Set-CasePhase 'initial_status'
        $initial = Status
        if ($initial.supported -ne $true -or $initial.status -ne 'off' -or $initial.host_platform -ne 'windows') { throw 'activation_default_off_not_proven' }
        Set-CasePhase 'settings_enable'
        Enable-FromSettings
        Verdict 'settings_enable' 'PASS' 'real_settings_switch_turned_on_enabled_true_and_ready'
        $inputOnly = $Context.ContainsKey('InputOnly') -and $Context.InputOnly
        $securityOnly = $Context.ContainsKey('SecurityOnly') -and $Context.SecurityOnly
        if ($securityOnly) {
            foreach ($id in @('survival_cu_off', 'survival_desktop_quit', 'survival_desktop_crash')) {
                Verdict $id 'NOT RUN' 'security_only_scope'
            }
        }
        # rc7 scope (Product): Off via the Settings switch, then the security
        # cases. Quit and crash survival were proven on signed rc4 (43731926013).
        if (-not $inputOnly -and -not $securityOnly) {
            foreach ($id in @('survival_desktop_quit', 'survival_desktop_crash')) { Verdict $id 'NOT RUN' 'proven_on_signed_rc4_43731926013_outside_rc7_scope' }
        }
        $operations = if ($securityOnly) { @() } else { @('off') }
        foreach ($operation in $operations) {
            $currentCase = @{off='survival_cu_off'; quit='survival_desktop_quit'; crash='survival_desktop_crash'}[$operation]
            # Safety is established by owned-target cleanup, not by reaching a catch.
            $cleanupSafe = $false
            try {
            $operationId = [guid]::NewGuid().ToString('N')
            $receiptPath = Join-Path $work ("input-$operationId.json")
            if (-not $result.ContainsKey('cu_input_diagnostics')) { $result.cu_input_diagnostics = [ordered]@{} }
            $result.cu_input_diagnostics[$operationId] = [ordered]@{
                operation_id = $operationId; operation = $operation; phase = 'generate_scripts'
                first_error = $null; wrapper = $null; input = (New-InputDiagnostics)
            }
            @{ operation_id = $operationId; receipt_path = $receiptPath } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $work ("launch-$operation-context.json")) -Encoding utf8
            Write-AcceptanceEvidence $result (Join-Path $work 'result.json')
            Enable-FromSettings
            Set-CasePhase 'generate_scripts'
            $text = 'Avibe signed acceptance unsaved draft ' + $operation
            $targetSettings = Join-Path $work ("editor-$operation-settings")
            New-Item -ItemType Directory -Path $targetSettings -Force | Out-Null
            $targetArguments = @('-multiInst', '-nosession', '-noPlugin', ('-settingsDir=' + $targetSettings))
            $launchSource = @"
let started = null;
let win = null;
const summarize = (observation) => ({
  has_tree: typeof observation?.tree_markdown === 'string' && observation.tree_markdown.length > 0,
  element_count: Array.isArray(observation?.elements) ? observation.elements.length : 0,
  exact_handle_observed: true,
});
const stableProjection = (observation) => ({
  app_name: observation?.app_name ?? null,
  window_bounds: observation?.window_bounds ?? null,
  tree_markdown: observation?.tree_markdown ?? null,
  elements: Array.isArray(observation?.elements) ? observation.elements.map((element) => ({
    role: element?.role ?? null,
    label: element?.label ?? null,
    value: element?.value ?? null,
    enabled: element?.enabled ?? null,
    selected: element?.selected ?? null,
  })) : null,
});
const sameProjection = (left, right) => JSON.stringify(stableProjection(left)) === JSON.stringify(stableProjection(right));
const observeStable = async () => {
  let previous = await win.observe({include_screenshot: false});
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const current = await win.observe({include_screenshot: false});
    if (sameProjection(previous, current)) return {observation: current, stable: true};
    previous = current;
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return {observation: previous, stable: false};
};
const effectSummary = (effect) => ({
  effect: effect?.effect ?? null,
  status: effect?.status ?? null,
  route: effect?.route ?? null,
  delivery: effect?.delivery?.mode ?? null,
});
const diagnostics = {
  phase: 'app', first_error: null, secondary_errors: [],
  ready_stable: null, ready_usable: null, ready: null,
  activation: null, native_focus_before_type: null,
  typed_attempted: false, type_attempt_count: 0,
  type: null, type_error: null, after: null, observation_changed: null,
};
const safeJsError = (error, call) => ({
  call, code: typeof error?.code === 'string' && /^[a-z_]{1,64}$/.test(error.code) ? error.code : 'js_exception',
  error_class: ['Error', 'TypeError', 'ReferenceError', 'SyntaxError', 'RangeError'].includes(error?.name) ? error.name : 'Error',
  exit_code: null,
});
try {
  const target = await computer.app({path: $(ConvertTo-Json -InputObject $targetPath -Compress)});
  diagnostics.phase = 'launch';
  started = await target.launch({additional_arguments: $(ConvertTo-Json -InputObject $targetArguments -Compress)});
  diagnostics.phase = 'wait_for';
  win = await started.app.waitFor({onScreen: true}, {timeout: 15000});
  diagnostics.phase = 'observe_ready';
  const ready = await observeStable();
  diagnostics.ready = summarize(ready.observation);
  diagnostics.ready_stable = ready.stable;
  diagnostics.ready_usable = diagnostics.ready.has_tree || diagnostics.ready.element_count > 0;
  if (diagnostics.ready_stable && diagnostics.ready_usable) {
    diagnostics.phase = 'activate';
    diagnostics.activation = effectSummary(await win.activate());
  }
} catch (error) { diagnostics.first_error = safeJsError(error, diagnostics.phase); }
({pid: started?.app?.pid ?? null, hwnd: win?.windowId ?? null, diagnostics})
"@
            $typeSource = @"
const typeDiagnostics = {
  phase: 'kernel_binding', first_error: null, secondary_errors: [],
  typed_attempted: false, type_attempt_count: 0,
  type: null, type_error: null, after: null, observation_changed: null,
};
const effectSummary = (effect) => ({
  effect: effect?.effect ?? null,
  status: effect?.status ?? null,
  route: effect?.route ?? null,
  delivery: effect?.delivery?.mode ?? null,
});
try {
  if (typeof win === 'undefined' || !win) throw new Error('computer_kernel_not_persistent');
  typeDiagnostics.phase = 'observe_before';
  const before = await win.observe({include_screenshot: false});
  typeDiagnostics.phase = 'type';
  typeDiagnostics.typed_attempted = true;
  typeDiagnostics.type_attempt_count = 1;
  try {
    typeDiagnostics.type = effectSummary(await win.type($(ConvertTo-Json -InputObject $text -Compress)));
  } catch (error) {
    typeDiagnostics.type_error = safeJsError(error, 'type');
    typeDiagnostics.first_error = typeDiagnostics.type_error;
  }
  typeDiagnostics.phase = 'observe_after';
  const after = await win.observe({include_screenshot: false});
  typeDiagnostics.after = summarize(after);
  typeDiagnostics.observation_changed = !sameProjection(before, after);
} catch (error) {
  const failure = typeof safeJsError === 'function' ? safeJsError(error, typeDiagnostics.phase) : {call: typeDiagnostics.phase, code: 'js_exception', error_class: 'Error', exit_code: null};
  if (!typeDiagnostics.first_error) typeDiagnostics.first_error = failure;
  else typeDiagnostics.secondary_errors.push(failure);
}
({diagnostics: typeDiagnostics})
"@
            $nativePath = (Join-Path $Root 'Native.cs').Replace("'", "''")
            $wrapperSource = @'
param(
    [Parameter(Mandatory)][string] $Python,
    [Parameter(Mandatory)][string] $LaunchScript,
    [Parameter(Mandatory)][string] $TypeScript,
    [Parameter(Mandatory)][string] $OperationId,
    [Parameter(Mandatory)][string] $ReceiptPath,
    [ValidateRange(1,110000)][int] $BudgetMs = 110000
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$receipt = [ordered]@{
    operation_id = $OperationId; phase = 'wrapper_start'; first_error = $null
    powershell_version = $PSVersionTable.PSVersion.ToString()
    calls = @(); cleanup_errors = @(); persistence_errors = @(); diagnostics = ('__EMPTY_DIAGNOSTICS__' | ConvertFrom-Json)
}
$finalResult = $null
$clock = [Diagnostics.Stopwatch]::StartNew()
function Save-Receipt([switch] $Required) {
    try {
        $receipt | ConvertTo-Json -Depth 15 -Compress | Set-Content -LiteralPath ($ReceiptPath + '.tmp') -Encoding utf8
        if (Test-Path -LiteralPath $ReceiptPath -PathType Container) { throw 'input_receipt_path_is_directory' }
        Move-Item -LiteralPath ($ReceiptPath + '.tmp') -Destination $ReceiptPath -Force
    } catch {
        $failure = @{call='wrapper_receipt_write'; exit_code=$null; code='input_receipt_persistence_failed'; error_class=$_.Exception.GetType().FullName}
        $receipt.persistence_errors += $failure
        if (-not $receipt.first_error) { $receipt.first_error = $failure }
    }
    if ($Required -and $receipt.persistence_errors.Count) { throw 'input_receipt_persistence_failed' }
}
function Invoke-ComputerJs([string] $Script, [string] $Call) {
    $receipt.phase = $Call
    $entry = [ordered]@{ call = $Call; exit_code = $null; error_class = $null; code = $null; cli_code = $null }
    $receipt.calls += $entry
    Save-Receipt -Required
    $child = $null
    try {
        $psi = New-Object Diagnostics.ProcessStartInfo
        $psi.FileName = $Python; $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
        # Script paths are generated under the fixture root, never user input.
        $remaining = [int][Math]::Min(60000, $BudgetMs - $clock.ElapsedMilliseconds - 1000)
        if ($remaining -le 0) { throw 'input_operation_timeout' }
        $psi.Arguments = '-I -B -m vibe computer js --code "@' + $Script + '" --timeout-ms ' + $remaining
        $child = [Diagnostics.Process]::Start($psi)
        $stdout = $child.StandardOutput.ReadToEndAsync(); $stderr = $child.StandardError.ReadToEndAsync()
        if (-not $child.WaitForExit($remaining + 1000)) { throw 'input_cli_timeout' }
        $entry.exit_code = $child.ExitCode
        if (-not $stdout.Wait(2000) -or -not $stderr.Wait(2000)) { throw 'input_cli_drain_timeout' }
        $text = $stdout.GetAwaiter().GetResult(); $null = $stderr.GetAwaiter().GetResult()
        if ($text) {
            try {
                $candidate = $text | ConvertFrom-Json
                $codeProperty = $candidate.PSObject.Properties['code']
                if ($codeProperty -and $codeProperty.Value -is [string] -and $codeProperty.Value -match '^[a-z_]{1,64}$') { $entry.cli_code = $codeProperty.Value }
            } catch { } # Parsing is judged below; never replace the real exit.
        }
        if ($child.ExitCode -ne 0) { throw 'input_cli_nonzero' }
        if (-not $text) { throw 'input_cli_empty_result' }
        $answer = $text | ConvertFrom-Json
        if (-not $answer.ok -or $answer.kind -ne 'computer_js') { throw 'input_cli_envelope_failed' }
        # Adopt the returned operation evidence at its producer boundary,
        # BEFORE finally can add a cleanup/persistence failure. Both launch
        # and type intentionally return exit0 for caught JavaScript errors.
        $incoming = $answer.result.diagnostics
        if (-not $incoming) { throw 'input_cli_diagnostics_missing' }
        $merged = [ordered]@{}
        if ($receipt.diagnostics -is [Collections.IDictionary]) {
            foreach ($key in $receipt.diagnostics.Keys) { $merged[$key]=$receipt.diagnostics[$key] }
        } else {
            foreach ($property in $receipt.diagnostics.PSObject.Properties) { $merged[$property.Name]=$property.Value }
        }
        foreach ($property in $incoming.PSObject.Properties) { $merged[$property.Name]=$property.Value }
        $receipt.diagnostics = $merged
        if ($merged.first_error -and -not $receipt.first_error) { $receipt.first_error=$merged.first_error }
        return $answer
    } catch {
        $entry.error_class = $_.Exception.GetType().FullName
        $entry.code = if ($_.Exception.Message -match '^input_[a-z_]+$') { $_.Exception.Message } else { 'input_cli_exception' }
        if (-not $receipt.first_error) { $receipt.first_error = $entry }
        Save-Receipt
        throw
    } finally {
        # Cleanup errors are separate; they cannot replace the original call.
        if ($child) {
            try { if (-not $child.HasExited) { $child.Kill(); $null = $child.WaitForExit(5000) }; $child.Dispose() }
            catch {
                $cleanupError = @{ call = 'cli_cleanup'; exit_code = $null; code = 'input_cleanup_failed'; error_class = $_.Exception.GetType().FullName }
                $receipt.cleanup_errors += $cleanupError
                if (-not $receipt.first_error) { $receipt.first_error = $cleanupError }
            }
        }
        Save-Receipt
    }
}
try {
    Save-Receipt -Required
    $receipt.phase = 'native_observer_load'; Save-Receipt -Required
    Add-Type -Path '__NATIVE_PATH__'
    $launch = Invoke-ComputerJs $LaunchScript 'launch_js'
    $finalResult = $launch.result
    Save-Receipt -Required
    if ($receipt.first_error) { throw 'input_launch_failed' }
    if (-not $launch.result.diagnostics.ready_stable -or -not $launch.result.diagnostics.ready_usable) {
        throw 'input_readiness_unproven'
    }
    $receipt.phase = 'native_focus'; Save-Receipt -Required
    $focus = [AcceptanceNative]::IsExactWindowVisibleAndForeground([IntPtr][long]$launch.result.hwnd)
    $diagnostics = [ordered]@{}
    foreach ($property in $launch.result.diagnostics.PSObject.Properties) { $diagnostics[$property.Name] = $property.Value }
    $diagnostics.native_focus_before_type = $focus
    $receipt.diagnostics = $diagnostics
    Save-Receipt -Required
    if (-not $focus) { throw 'input_focus_unproven' }
    $diagnostics.typed_attempted = $null
    $diagnostics.type_attempt_count = $null
    Save-Receipt -Required
    $typed = Invoke-ComputerJs $TypeScript 'type_js'
    foreach ($property in $typed.result.diagnostics.PSObject.Properties) { $diagnostics[$property.Name] = $property.Value }
    $finalResult = [ordered]@{ pid = $launch.result.pid; hwnd = $launch.result.hwnd; diagnostics = $diagnostics }
    if ($diagnostics.first_error) { $receipt.first_error = $diagnostics.first_error }
    Save-Receipt -Required
    if ($diagnostics.first_error) { throw 'input_type_failed' }
    $receipt.phase = 'wrapper_complete'
} catch {
    if (-not $receipt.first_error) {
        $receipt.first_error = @{
            call = $receipt.phase; error_class = $_.Exception.GetType().FullName
            code = if ($_.Exception.Message -match '^input_[a-z_]+$') { $_.Exception.Message } else { 'input_wrapper_exception' }
            exit_code = $null
        }
    }
} finally {
    Save-Receipt
    [ordered]@{ ok = ($null -eq $receipt.first_error); kind = 'computer_js'; operation_id = $OperationId; result = $finalResult; input_receipt = $receipt } | ConvertTo-Json -Depth 15 -Compress
}
'@.Replace('__NATIVE_PATH__', $nativePath).Replace('__EMPTY_DIAGNOSTICS__', ((New-InputDiagnostics) | ConvertTo-Json -Depth 10 -Compress))
            Set-CasePhase 'write_launch_script'
            Set-Content -LiteralPath (Join-Path $work ("launch-$operation.js")) -Value $launchSource -Encoding utf8
            Set-CasePhase 'write_type_script'
            Set-Content -LiteralPath (Join-Path $work ("launch-$operation-type.js")) -Value $typeSource -Encoding utf8
            Set-CasePhase 'write_wrapper_script'
            Set-Content -LiteralPath (Join-Path $work ("launch-$operation.ps1")) -Value $wrapperSource -Encoding utf8BOM
            $appResult = Run-CaseTurn "launch-$operation"
            if (-not $appResult.diagnostics) { throw 'shipped_cu_diagnostics_missing' }
            $diagnostics = [ordered]@{}
            foreach ($property in $appResult.diagnostics.PSObject.Properties) { $diagnostics[$property.Name] = $property.Value }
            $diagnostics.app_pid = [int]$appResult.pid
            $diagnostics.window_id = [long]$appResult.hwnd
            $result.cu_input_diagnostics[$operationId].input = $diagnostics
            $result.cu_input_diagnostics[$operationId].phase = 'input_diagnostics_emitted'
            Write-AcceptanceEvidence $result (Join-Path $work 'result.json')
            if ($Context.ContainsKey('InputOnly') -and $Context.InputOnly) {
                # Harness-only dry gate. Not a lifecycle or product verdict.
                Verdict 'dry_input_pipeline' 'PASS' 'inert_boundaries_only_not_product_acceptance'
                return
            }
            Set-CasePhase 'target_open'
            $target = [Diagnostics.Process]::GetProcessById([int]$appResult.pid)
            $null = $target.SafeHandle
            $helpers = $null
            try {
                Set-CasePhase 'target_identity'
                if ($target.HasExited -or $target.MainModule.FileName -ne $targetPath -or [AcceptanceNative]::IsElevated($target.Id)) { throw 'real_created_standard_editor_not_proven' }
                $window = [IntPtr][long]$appResult.hwnd
                Set-CasePhase 'hwnd_owner'
                if (-not [AcceptanceNative]::IsExactWindowOwnedByProcess($window, $target.Id)) { throw 'created_window_owner_unproven' }
                $diagnostics.target_identity = 'created_process_path_nonelevated_and_hwnd_owner_verified'
                Write-AcceptanceEvidence $result (Join-Path $work 'result.json')
                Set-CasePhase 'unsaved_text_before'
                if (-not $diagnostics.typed_attempted) { throw 'shipped_type_not_attempted_readiness_or_focus_unproven' }
                if (-not [AcceptanceNative]::UnsavedTextEquals($window, $text)) { throw 'shipped_type_did_not_create_unsaved_text' }
                Set-CasePhase 'helper_inventory'
                try {
                    $helpers = Driver-Handles $target.Id
                    $result.cu_input_diagnostics[$operationId].helper_identity = @{status='VERIFIED'; reason_class=$null}
                } catch {
                    if (-not (Test-AcceptanceHelperIdentityOptional $inputData)) { throw }
                    $result.helper_identity_verified = $false
                    $result.cu_input_diagnostics[$operationId].helper_identity = @{status='NOT VERIFIED'; reason_class=$_.Exception.GetBaseException().GetType().FullName}
                    Write-AcceptanceEvidence $result (Join-Path $work 'result.json')
                }
                Set-CasePhase ('lifecycle_' + $operation)
                if ($operation -eq 'off') {
                    Disable-FromSettings
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
                if ($null -ne $helpers) {
                    Set-CasePhase 'helper_exit'
                    $deadline = (Get-Date).AddSeconds(30)
                    foreach ($helper in $helpers) {
                        $remaining = [Math]::Max(0, [int]($deadline - (Get-Date)).TotalMilliseconds)
                        if (-not $helper.WaitForExit($remaining)) { throw 'driver_or_helper_survived_shutdown' }
                    }
                    Set-CasePhase 'replacement_driver_probe'
                    Assert-NoReplacementDriver $helpers $target.Id
                }
                Set-CasePhase 'unsaved_text_after'
                if ($target.HasExited -or -not [AcceptanceNative]::UnsavedTextEquals($window, $text)) { throw 'original_app_or_unsaved_text_did_not_survive' }
                $survivalReason = if ($null -eq $helpers) { 'original_app_and_unsaved_text_survived_helper_identity_and_exit_NOT_VERIFIED' } else { 'original_app_and_unsaved_text_survived_pinned_helpers_exited_and_no_replacement_generation' }
                Verdict $currentCase 'PASS' $survivalReason
            } catch {
                Add-AcceptanceFailure $result $result.cu_input_diagnostics[$operationId].phase $_
                throw
            } finally {
                $cleanupFailed = $false
                if ($helpers) { foreach ($helper in $helpers) {
                    try { $helper.Dispose() } catch { Add-AcceptanceFailure $result 'helper_handle_cleanup' $_ -Cleanup; $cleanupFailed = $true }
                } }
                try {
                    if (-not $target.HasExited) { $target.Kill(); if (-not $target.WaitForExit(10000)) { throw 'target_cleanup_exit_unproven' } }
                } catch { Add-AcceptanceFailure $result 'target_stop_cleanup' $_ -Cleanup; $cleanupFailed = $true }
                try { $target.Dispose() } catch { Add-AcceptanceFailure $result 'target_handle_cleanup' $_ -Cleanup; $cleanupFailed = $true }
                Write-AcceptanceEvidence $result (Join-Path $work 'result.json')
                $cleanupSafe = -not $cleanupFailed
                if ($cleanupFailed) { throw 'input_cleanup_failed' }
            }
            } catch {
                if ($inputOnly) { throw }
                Record-EnabledCaseFailure $_
            }
            if (-not $cleanupSafe) {
                Write-AcceptanceEvidence $result (Join-Path $work 'result.json')
                return # Unknown fixture cleanup is not a safe baseline for another case.
            }
            if ($operation -ne 'off') {
                try {
                    Set-CasePhase 'desktop_relaunch'
                    # Each case gets a fresh owned Desktop. A failed Quit must
                    # not make the following crash reuse the previous instance.
                    if (-not $Context.Desktop.HasExited) {
                        $Context.Desktop.Kill()
                        if (-not $Context.Desktop.WaitForExit(10000)) { throw 'desktop_recovery_stop_failed' }
                    }
                    $Context.Desktop.Dispose()
                    $Context.Desktop = Start-Process -FilePath $apps[0].FullName -PassThru
                    $null = $Context.Desktop.SafeHandle
                    Wait-Status 'ready' 90
                } catch {
                    Add-AcceptanceFailure $result 'desktop_recovery' $_
                    $result.cu_input_diagnostics[$operationId].recovery_error = @{call='desktop_recovery'; error_class=$_.Exception.GetBaseException().GetType().FullName}
                    Write-AcceptanceEvidence $result (Join-Path $work 'result.json')
                    return # Later cases remain NOT RUN, never inherit an unready runtime.
                }
            } else {
                # Off leaves Computer Use off; the security cases need it on.
                # Re-enable through the same Settings switch and require the
                # authoritative enabled=true and ready, else security is NOT RUN.
                try {
                    Set-CasePhase 'reenable_after_off'
                    Enable-FromSettings
                    $answer = Status
                    if ($answer.enabled -ne $true -or $answer.status -ne 'ready') { throw 'settings_reenable_not_confirmed' }
                } catch {
                    Add-AcceptanceFailure $result 'reenable_after_off' $_
                    $result.cu_input_diagnostics[$operationId].recovery_error = @{call='reenable_after_off'; error_class=$_.Exception.GetBaseException().GetType().FullName}
                    Write-AcceptanceEvidence $result (Join-Path $work 'result.json')
                    return # Security cases remain NOT RUN; never run them with Computer Use off.
                }
            }
        }
        $operationId = [guid]::NewGuid().ToString('N')
        $result.cu_input_diagnostics[$operationId] = [ordered]@{
            operation_id=$operationId; operation='security'; phase='pipe_state'; first_error=$null; wrapper=$null; input=(New-InputDiagnostics)
        }
        $currentCase = 'second_user_pipe_lock'
        Set-CasePhase 'pipe_state'
        $state = Get-Content -LiteralPath (Join-Path $env:APPDATA 'bot.avibe.desktop\computer-use.json') -Raw | ConvertFrom-Json
        if ($state.state -ne 'ready' -or -not $state.socket_path.StartsWith('\\.\pipe\')) { throw 'ready_primary_pipe_missing' }
        # Positive control: the actual primary ordinary user can open its pipe.
        Set-CasePhase 'owner_pipe_positive_control'
        $ownerPipe = [IO.Pipes.NamedPipeClientStream]::new('.', $state.socket_path.Substring(9), [IO.Pipes.PipeDirection]::InOut)
        try { $ownerPipe.Connect(3000) } finally { $ownerPipe.Dispose() }
        # The owner (not the elevated parent) proves its own lock exists and
        # hands the parent only the pipe leaf and fixed lock path.
        Set-CasePhase 'owner_lock_present'
        $lockPath = Join-Path $env:APPDATA 'bot.avibe.desktop\computer-use.lock'
        if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { throw 'ready_primary_lock_missing' }
        Set-CasePhase 'security_fixture_request'
        $request = Join-Path $work 'security-request.json'
        @{action='prepare-security-fixtures'; pipe_leaf=$state.socket_path.Substring(9); lock_path=$lockPath} | ConvertTo-Json | Set-Content -LiteralPath ($request + '.tmp')
        Move-Item -LiteralPath ($request + '.tmp') -Destination $request
        $response = Join-Path $work 'security-response.json'
        $deadline = (Get-Date).AddSeconds(120)
        while (-not (Test-Path -LiteralPath $response) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
        $currentCase = 'security_fixture'
        Set-CasePhase 'security_fixture_response'
        $securityResult = Get-Content -LiteralPath $response -Raw | ConvertFrom-Json -AsHashtable
        $result.security_fixture_diagnostics = $securityResult.diagnostics
        foreach ($id in $securityResult.cases.Keys) {
            if ($id -notin @('security_fixture', 'elevated_driver_refused', 'second_user_pipe_lock')) { throw 'unknown_security_verdict' }
            Verdict $id $securityResult.cases[$id].verdict $securityResult.cases[$id].reason
        }
        # Earlier security case failures stay in their own verdicts. Only a
        # missing elevated fixture prevents the separate elevated-app probe.
        if (-not $securityResult.ok) { throw 'security_fixture_failed_see_individual_verdicts' }
        Set-CasePhase 'elevated_query'
        try {
            if (-not [AcceptanceNative]::IsElevated([int]$securityResult.elevated_pid)) { throw 'elevated_target_query_access_unproven' }
        } catch { throw 'elevated_target_query_access_unproven' }
        $currentCase = 'elevated_app_refused'
        Set-CasePhase 'elevated_script_generation'
        # The script never throws: it returns the shipped error codes it saw, so
        # the case is decided by evidence, not by a CLI exit status. The elevated
        # attempt runs first (one notepad++ instance), then an ordinary control.
        $controlSettings = Join-Path $work 'editor-control-settings'
        New-Item -ItemType Directory -Path $controlSettings -Force | Out-Null
        $controlArguments = @('-multiInst', '-nosession', '-noPlugin', ('-settingsDir=' + $controlSettings))
        $elevatedScript = @"
const code = (error) => typeof error?.code === 'string' && /^[a-z_]{1,64}$/.test(error.code) ? error.code : 'js_exception';
const reasonOf = (error) => typeof error?.params?.reason === 'string' && /^[a-z_]{1,64}$/.test(error.params.reason) ? error.params.reason : null;
const out = {
  elevated: {phase: 'lookup', pid_match: null, type_attempted: false, type_ok: null, type_code: null, type_reason: null, error_code: null, error_reason: null},
  control: {phase: 'app', pid: null, hwnd: null, type_ok: null, type_code: null, error_code: null},
};
try {
  const app = await computer.app({nameContains: 'notepad++'});
  out.elevated.pid_match = app.pid === $($securityResult.elevated_pid);
  if (!out.elevated.pid_match) throw Object.assign(new Error('wrong target'), {code: 'wrong_elevated_target'});
  out.elevated.phase = 'window';
  const win = await app.window({windowId: $($securityResult.elevated_hwnd)});
  out.elevated.phase = 'observe';
  await win.observe({include_screenshot: false});
  out.elevated.phase = 'type';
  out.elevated.type_attempted = true;
  try { await win.type('must-not-be-inserted'); out.elevated.type_ok = true; }
  catch (error) { out.elevated.type_ok = false; out.elevated.type_code = code(error); out.elevated.type_reason = reasonOf(error); }
  out.elevated.phase = 'done';
} catch (error) { out.elevated.error_code = code(error); out.elevated.error_reason = reasonOf(error); }
try {
  const target = await computer.app({path: $(ConvertTo-Json -InputObject $targetPath -Compress)});
  out.control.phase = 'launch';
  const started = await target.launch({additional_arguments: $(ConvertTo-Json -InputObject $controlArguments -Compress)});
  out.control.pid = started?.app?.pid ?? null;
  out.control.phase = 'wait_for';
  const win = await started.app.waitFor({onScreen: true}, {timeout: 15000});
  out.control.hwnd = win?.windowId ?? null;
  out.control.phase = 'observe';
  await win.observe({include_screenshot: false});
  // No activate(): like the lifecycle typing, the control uses default
  // background delivery (rc7 43766745164: activate() was foreground_unavailable).
  out.control.phase = 'type';
  try { await win.type('control-ok'); out.control.type_ok = true; }
  catch (error) { out.control.type_ok = false; out.control.type_code = code(error); }
  out.control.phase = 'done';
} catch (error) { out.control.error_code = code(error); }
out
"@
        Set-Content -LiteralPath (Join-Path $work 'attach-elevated.js') -Value $elevatedScript -Encoding utf8
        $evidence = $null
        try {
            $refusal = Run-CaseTurn 'attach-elevated'
            Set-CasePhase 'protected_window_result'
            $evidence = [ordered]@{ elevated = $refusal.elevated; control = $refusal.control; control_text_verified = $false; control_cleanup = $null }
            $result.elevated_app_evidence = $evidence
            # Independent native read of the ordinary control window, then
            # cleanup of exactly that created, non-elevated editor.
            if ($refusal.control.pid -and $refusal.control.hwnd) {
                $control = $null
                try {
                    $control = [Diagnostics.Process]::GetProcessById([int]$refusal.control.pid)
                    $null = $control.SafeHandle
                    if ($control.MainModule.FileName -ne $targetPath -or [AcceptanceNative]::IsElevated($control.Id)) { throw 'created_control_identity_unproven' }
                    $deadline = (Get-Date).AddSeconds(5)
                    do {
                        if ([AcceptanceNative]::UnsavedTextEquals([IntPtr][long]$refusal.control.hwnd, 'control-ok')) { $evidence.control_text_verified = $true; break }
                        Start-Sleep -Milliseconds 250
                    } while ((Get-Date) -lt $deadline)
                } finally {
                    if ($control) {
                        try {
                            if (-not $control.HasExited) { $control.Kill(); if (-not $control.WaitForExit(10000)) { throw 'created_control_cleanup_unproven' } }
                            $evidence.control_cleanup = 'exited'
                        } catch { $evidence.control_cleanup = 'failed'; Add-AcceptanceFailure $result 'control_cleanup' $_ -Cleanup }
                        finally { $control.Dispose() }
                    }
                }
            }
        } finally { if ($evidence) { Write-AcceptanceEvidence $result (Join-Path $work 'result.json') } }
        $decision = Resolve-ElevatedAppVerdict $evidence
        Verdict 'elevated_app_refused' $decision.verdict $decision.reason
        if ($decision.verdict -ne 'PASS') { throw $decision.reason }
    } catch {
        Record-EnabledCaseFailure $_
        throw 'enabled_acceptance_case_failed'
    }
}
