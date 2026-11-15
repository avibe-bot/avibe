# Shared value-free evidence path for the real harness and inert dry runner.
function Copy-AcceptanceHarness([string] $Source, [string] $Destination) {
    foreach ($file in @('user.ps1', 'evidence.ps1', 'inventory.ps1', 'Native.cs', 'model_stub.py', 'configure_stub.py', 'cdp.mjs', 'second-user.ps1', 'enabled.ps1', 'tray-off.ps1', 'update.ps1', 'release.ps1')) {
        Copy-Item -LiteralPath (Join-Path $Source $file) -Destination (Join-Path $Destination $file) -ErrorAction Stop
    }
}
function Write-AcceptanceEvidence([Collections.IDictionary] $Record, [string] $Path) {
    $temporary = $Path + '.tmp'
    $json = $Record | ConvertTo-Json -Depth 30 -Compress
    [IO.File]::WriteAllText($temporary, $json, [Text.UTF8Encoding]::new($false))
    if (Test-Path -LiteralPath $Path -PathType Container) { throw 'input_evidence_path_is_directory' }
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}
function Add-AcceptanceFailure([Collections.IDictionary] $Record, [string] $Call, $Failure, [switch] $Cleanup) {
    $entry = [ordered]@{
        call = $Call; error_class = $Failure.Exception.GetType().FullName
        script_line = $Failure.InvocationInfo.ScriptLineNumber
        script_role = if ([IO.Path]::GetFileName($Failure.InvocationInfo.ScriptName) -in @('enabled.ps1', 'user.ps1', 'run.ps1', 'security.ps1', 'update.ps1', 'evidence.ps1')) { [IO.Path]::GetFileName($Failure.InvocationInfo.ScriptName) } else { 'generated_or_test_script' }
        base_error_class = $Failure.Exception.GetBaseException().GetType().FullName
        exit_code = $null; code = 'harness_operation_failed'
    }
    if ($Failure.Exception.Message -match '^child_process_exit_(-?\d+)$') {
        $entry.exit_code = [int]$Matches[1]
        $entry.code = 'child_process_nonzero'
    }
    # Codes from our fixed throws only; arbitrary messages/values never escape.
    if ($Failure.Exception.Message -match '^(input|shipped|created|real|driver|replacement|original|explicit|expected|ready|security|elevated|fixture|native|uninstall|update|ordinary|harness|child|primary|runner|second)_[a-z_]{1,100}$') {
        $entry.code = $Failure.Exception.Message
    }
    if ($Cleanup) {
        if (-not $Record.Contains('cleanup_errors')) { $Record.cleanup_errors = @() }
        $Record.cleanup_errors += $entry
    } elseif (-not $Record.Contains('failure')) {
        $Record.failure = $entry
    } else {
        if (-not $Record.Contains('secondary_errors')) { $Record.secondary_errors = @() }
        $Record.secondary_errors += $entry
    }
}
function Merge-AcceptanceWorker([Collections.IDictionary] $Record, [Collections.IDictionary] $Worker, [int] $ExitCode) {
    if (-not $Worker.Contains('cases') -or $Worker.cases -isnot [Collections.IDictionary]) { throw 'input_worker_cases_invalid' }
    foreach ($id in $Worker.cases.Keys) {
        if (-not $Record.cases.Contains($id)) { throw 'input_worker_case_unknown' }
        if ($Worker.cases[$id].verdict -notin @('PASS', 'FAIL', 'NOT RUN')) { throw 'input_worker_verdict_invalid' }
        $Record.cases[$id] = $Worker.cases[$id]
    }
    foreach ($field in @('update_outcome', 'elevated_app_evidence', 'security_fixture_diagnostics', 'native_tray_off', 'helper_identity_verified', 'failure', 'cleanup_errors', 'secondary_errors', 'cdp_failure', 'cdp_diagnostics', 'cdp_detail', 'stub_turn_diagnostics', 'default_off_probe_receipt', 'query_fixture_detail', 'cu_input_diagnostics', 'update_panel_preflight', 'update_transition', 'installed_driver_size', 'installed_provenance_verified', 'installed_driver_sha256')) {
        if ($Worker.Contains($field)) { $Record[$field] = $Worker[$field] }
    }
    $Record.worker_exit_code = $ExitCode
}
function Complete-AcceptanceGate([Collections.IDictionary] $Record) {
    $Record.missing_required_cases = @($Record.required_cases | Where-Object { $Record.cases[$_].verdict -ne 'PASS' })
    $Record.required_cases_passed = $Record.missing_required_cases.Count -eq 0 -and
        (-not $Record.Contains('helper_identity_verified') -or $Record.helper_identity_verified -eq $true) -and
        (-not $Record.Contains('worker_exit_code') -or $Record.worker_exit_code -eq 0) -and
        (-not $Record.Contains('failure')) -and
        (-not $Record.Contains('cleanup_errors') -or $Record.cleanup_errors.Count -eq 0)
}
