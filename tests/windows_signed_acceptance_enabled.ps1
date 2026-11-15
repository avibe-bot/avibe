param(
    [Parameter(Mandatory)][string] $SourceRoot,
    [Parameter(Mandatory)][string] $Work,
    [Parameter(Mandatory)][string] $Evidence,
    [string] $OneCase = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $SourceRoot 'evidence.ps1')
New-Item -ItemType Directory -Force $Work, $Evidence | Out-Null
$windowsHost = [Environment]::OSVersion.Platform -eq 'Win32NT'
if (-not $OneCase) {
    $summaries = @(); $dryFailure = $null; $activeFault = 'evidence_finalization'; $partial = $null
    try {
    & (Join-Path $PSScriptRoot 'windows_signed_acceptance_evidence.ps1') -Source (Join-Path $SourceRoot 'evidence.ps1') -Work $Work
    foreach ($fault in @('none', 'stderr_zero', 'aggregate_timeout', 'turn', 'missing_wrapper', 'wrapper_native', 'missing_result', 'stale_result', 'stale_receipt', 'launch_cli_exit', 'launch_cli_json', 'launch_cli_empty', 'launch_cli_null', 'launch_cli_kind', 'launch_cli_fields', 'app', 'launch', 'wait_for', 'observe', 'activate', 'unusable', 'unstable', 'focus', 'native_focus', 'type_cli_exit', 'type_cli_json', 'type_cli_empty', 'type_cli_null', 'type_cli_kind', 'type_cli_fields', 'lost_binding', 'observe_before', 'type', 'type_after', 'observe_after')) {
        $activeFault = $fault
        $caseWork = Join-Path $Work $fault
        & (Get-Process -Id $PID).Path -NoLogo -NoProfile -NonInteractive -File $PSCommandPath -SourceRoot $SourceRoot -Work $caseWork -Evidence $Evidence -OneCase $fault
        if ($LASTEXITCODE -ne 0) { throw "dry_case_failed_$fault" }
        $summaries += Get-Content -LiteralPath (Join-Path $caseWork 'dry-verdict.json') -Raw | ConvertFrom-Json
    }
    } catch {
        $dryFailure = @{case=$activeFault; call='dry_fixture'; error_class=$_.Exception.GetType().FullName; script_line=$_.InvocationInfo.ScriptLineNumber}
        $partialPath = Join-Path (Join-Path $Work $activeFault) 'result.json'
        if (Test-Path -LiteralPath $partialPath) {
            try { $partial = Get-Content -LiteralPath $partialPath -Raw | ConvertFrom-Json -AsHashtable } catch { }
        }
    } finally {
        Write-AcceptanceEvidence @{ mode='dry'; product_acceptance=$false; hosted_windows=($windowsHost -and $env:GITHUB_ACTIONS -eq 'true' -and $env:RUNNER_ENVIRONMENT -eq 'github-hosted'); dry_gate_passed=($null -eq $dryFailure); failure=$dryFailure; partial_evidence=$partial; cases=$summaries } (Join-Path $Evidence 'verdict.json')
        '# Harness-only dry gate: no signed installer, model, GUI, clipboard or product acceptance' | Set-Content -LiteralPath (Join-Path $Evidence 'verdict.md')
    }
    if ($dryFailure) { throw 'dry_fixture_gate_failed_see_value_free_evidence' }
    Write-Output 'enabled_input_dry_cases_passed'
    exit 0
}
$work = [IO.Path]::GetFullPath($Work)
$Root = Join-Path $work 'fixture-root'
New-Item -ItemType Directory -Force $Root | Out-Null
$testRoot = $PSScriptRoot
$node = (Get-Command node).Source
$hostPython = (Get-Command python3 -ErrorAction SilentlyContinue)
if (-not $hostPython) { $hostPython = Get-Command python }
$hostPython = $hostPython.Source
$nativeSource = @'
using System;
public static class AcceptanceNative {
    public static bool IsExactWindowVisibleAndForeground(IntPtr hwnd) {
        var fault = Environment.GetEnvironmentVariable("CU_DRY_FAULT");
        if (fault == "native_focus") throw new InvalidOperationException("injected_native_failure");
        return fault != "focus";
    }
}
'@
Set-Content -LiteralPath (Join-Path $Root 'Native.cs') -Value $nativeSource
$env:CU_DRY_NODE = $node
$env:CU_DRY_RUNNER = Join-Path $testRoot 'windows_signed_acceptance_input.mjs'
$env:CU_DRY_WORK = $work
$env:CU_DRY_FAULT = $OneCase
$python = Join-Path $work 'inert python.exe'
if ($windowsHost) {
    $compiler = Join-Path $work 'compile.ps1'
    $source = @'
public static class InertCli {
    public static int Main(string[] args) {
        if (System.Environment.GetEnvironmentVariable("CU_DRY_FAULT") == "aggregate_timeout") System.Threading.Thread.Sleep(3300);
        var p = new System.Diagnostics.ProcessStartInfo();
        p.FileName = System.Environment.GetEnvironmentVariable("CU_DRY_NODE");
        p.UseShellExecute = false;
        p.Arguments = "\"" + System.Environment.GetEnvironmentVariable("CU_DRY_RUNNER") + "\" client \"" + System.Environment.GetEnvironmentVariable("CU_DRY_WORK") + "\"";
        foreach (var arg in args) p.Arguments += " \"" + arg.Replace("\"", "\\\"") + "\"";
        using (var child = System.Diagnostics.Process.Start(p)) { child.WaitForExit(); return child.ExitCode; }
    }
}
'@
    Set-Content -LiteralPath (Join-Path $work 'InertCli.cs') -Value $source
    $env:CU_DRY_COMPILE_SOURCE = Join-Path $work 'InertCli.cs'
    $env:CU_DRY_COMPILE_OUTPUT = $python
    $env:CU_DRY_NATIVE_SOURCE = Join-Path $SourceRoot 'Native.cs'
    '$ErrorActionPreference = ''Stop''; Add-Type -Path $env:CU_DRY_NATIVE_SOURCE; Add-Type -Path $env:CU_DRY_COMPILE_SOURCE -OutputAssembly $env:CU_DRY_COMPILE_OUTPUT -OutputType ConsoleApplication' | Set-Content -LiteralPath $compiler
    & "$env:SystemRoot/System32/WindowsPowerShell/v1.0/powershell.exe" -NoProfile -NonInteractive -File $compiler
    if ($LASTEXITCODE -ne 0) { throw 'dry_cli_compile_failed' }
} else {
    '#!/bin/sh', 'exec "$CU_DRY_NODE" "$CU_DRY_RUNNER" client "$CU_DRY_WORK" "$@"' | Set-Content -LiteralPath $python
    & chmod +x $python
    $env:SystemRoot = Join-Path $work 'system-root'
    $wrapperDir = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0'
    New-Item -ItemType Directory -Force $wrapperDir | Out-Null
    New-Item -ItemType SymbolicLink -Path (Join-Path $wrapperDir 'powershell.exe') -Target (Get-Process -Id $PID).Path | Out-Null
}
$psi = [Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $node; $psi.UseShellExecute = $false
foreach ($arg in @($env:CU_DRY_RUNNER, 'server', $work, $OneCase)) { $psi.ArgumentList.Add($arg) }
$server = [Diagnostics.Process]::Start($psi)
$null = $server.SafeHandle
try {
    $deadline = (Get-Date).AddSeconds(10)
    while (-not (Test-Path (Join-Path $work 'port'))) {
        if ($server.HasExited -or (Get-Date) -gt $deadline) { throw 'dry_js_server_not_ready' }
        Start-Sleep -Milliseconds 50
    }
    $env:CU_DRY_STUB = Join-Path $SourceRoot 'model_stub.py'
    $dispatch = @'
import importlib.util, json, os
from pathlib import Path
s = importlib.util.spec_from_file_location("stub", os.environ["CU_DRY_STUB"])
m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
w = Path(os.environ["CU_DRY_WORK"])
r = {"messages":[{"role":"user","content":"AVIBE_CU_CASE:launch-off"}],"tools":[{"type":"function","function":{"name":"powershell"}}]}
reply, _ = m.scripted_reply(r, python=str(w / "inert python.exe"), work=w)
(w / "dispatch.ps1").write_text(json.loads(reply["tool_calls"][0]["function"]["arguments"])["command"], encoding="utf-8-sig")
'@
    Set-Content -LiteralPath (Join-Path $work 'dispatch.py') -Value $dispatch
    $inputData = @{ target_app = $python }
    $install = Join-Path $work 'no-install'
    $origin = [uri]'http://127.0.0.1:1/'
    $cdpPort = 1
    $result = @{cases = [ordered]@{}}
    $script:dryStatus = 'off'
    function Invoke-RestMethod { return @{supported=$true; host_platform='windows'; status=$script:dryStatus} }
    function Verdict([string] $Id, [string] $Verdict, [string] $Reason) {
        $result.cases[$Id] = @{verdict=$Verdict; reason=$Reason}
        Write-AcceptanceEvidence $result (Join-Path $work 'result.json')
    }
    function Run-Process([string] $File, [string[]] $Arguments, [int] $Seconds) {
        if ($File -eq $node) { $script:dryStatus = 'ready'; return '{}' }
        if ($OneCase -eq 'turn') { throw 'child_process_exit_39' }
        if ($OneCase -eq 'aggregate_timeout') {
            $path = Join-Path $work 'launch-off-context.json'
            $context = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json -AsHashtable
            $context.operation_budget_ms = 6000
            $context | ConvertTo-Json | Set-Content -LiteralPath $path
        }
        & $hostPython (Join-Path $work 'dispatch.py')
        if ($LASTEXITCODE -ne 0) { throw 'dry_dispatch_generation_failed' }
        if ($OneCase -eq 'missing_wrapper') { Remove-Item -LiteralPath (Join-Path $work 'launch-off.ps1') }
        if ($OneCase -eq 'wrapper_native') { Set-Content -LiteralPath (Join-Path $Root 'Native.cs') -Value 'not valid C#' }
        & "$env:SystemRoot/System32/WindowsPowerShell/v1.0/powershell.exe" -NoProfile -NonInteractive -File (Join-Path $work 'dispatch.ps1')
        if ($LASTEXITCODE -ne 0) { throw "child_process_exit_$LASTEXITCODE" }
        $context = Get-Content -LiteralPath (Join-Path $work 'launch-off-context.json') -Raw | ConvertFrom-Json
        $casePath = Join-Path $work 'launch-off.json'
        if ($OneCase -eq 'missing_result') { Remove-Item -LiteralPath $casePath }
        if ($OneCase -eq 'stale_result') {
            $stale = Get-Content -LiteralPath $casePath -Raw | ConvertFrom-Json
            $stale.operation_id = '00000000000000000000000000000000'
            $stale | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $casePath
        }
        if ($OneCase -eq 'stale_receipt') {
            $path = Join-Path $work ("input-$($context.operation_id).json")
            $stale = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
            $stale.operation_id = '00000000000000000000000000000000'
            $stale | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $path
        }
    }
    . (Join-Path $SourceRoot 'enabled.ps1')
    try { Invoke-EnabledCases @{InputOnly=$true} } catch {
        if ($OneCase -in @('none', 'stderr_zero')) { throw }
    }
    $saved = Get-Content -LiteralPath (Join-Path $work 'result.json') -Raw | ConvertFrom-Json -AsHashtable
    if (-not $saved.cu_input_diagnostics -or $saved.cu_input_diagnostics.Count -ne 2) { throw 'dry_operation_diagnostics_missing' }
    $record = @($saved.cu_input_diagnostics.Values | Where-Object { $_.operation -eq 'off' })[0]
    if ($windowsHost -and $OneCase -in @('none', 'stderr_zero') -and -not $record.wrapper.powershell_version.StartsWith('5.1.')) { throw 'dry_windows_wrapper_not_ps51' }
    $keys = @('ready_stable', 'ready_usable', 'ready', 'activation', 'native_focus_before_type', 'typed_attempted', 'type_attempt_count', 'type', 'type_error', 'after', 'observation_changed')
    foreach ($key in $keys) { if (-not $record.input.Contains($key)) { throw "dry_diagnostic_field_missing_$key" } }
    if ($OneCase -in @('none', 'stderr_zero')) {
        if ($saved.cases.dry_input_pipeline.verdict -ne 'PASS' -or -not $record.input.typed_attempted -or -not (Test-Path (Join-Path $work 'typed'))) { throw 'dry_success_incomplete' }
    } else {
        if (-not $record.first_error -or $saved.cases.survival_cu_off.verdict -ne 'FAIL') { throw 'dry_failure_not_preserved' }
        if ($OneCase -notin @('observe_after', 'type', 'type_after', 'type_cli_null', 'type_cli_kind', 'type_cli_fields', 'missing_result', 'stale_result', 'stale_receipt') -and (Test-Path (Join-Path $work 'typed'))) { throw 'dry_typed_after_failed_prerequisite' }
    }
    $attempts = if (Test-Path (Join-Path $work 'attempts')) { [int](Get-Content (Join-Path $work 'attempts')) } else { 0 }
    if ($attempts -gt 1) { throw 'dry_input_retried' }
    if ($OneCase -in @('unusable', 'unstable') -and (Test-Path (Join-Path $work 'activations'))) { throw 'dry_activated_before_ready' }
    if ($OneCase -in @('app', 'launch', 'wait_for', 'activate', 'type', 'observe_before', 'observe_after')) {
        $expectedCall = $OneCase
        if ($record.wrapper.diagnostics.first_error.call -ne $expectedCall) { throw 'dry_js_failure_stage_wrong' }
    }
    if ($OneCase -eq 'type_after') {
        if ($record.wrapper.diagnostics.first_error.code -ne 'injected_type_failure' -or $record.wrapper.diagnostics.secondary_errors.Count -ne 1) { throw 'dry_after_observe_masked_type_failure' }
    }
    if ($OneCase -eq 'aggregate_timeout') {
        if ($record.wrapper.calls[0].exit_code -ne 0 -or $record.wrapper.first_error.code -notin @('input_operation_timeout', 'input_cli_timeout')) { throw 'dry_aggregate_budget_not_exercised' }
    }
    if ($OneCase -in @('launch_cli_exit', 'type_cli_exit')) {
        $last = $record.wrapper.calls[-1]
        if ($last.exit_code -ne 37 -or $last.cli_code -ne 'injected_cli_failure') { throw 'dry_cli_exit_or_code_lost' }
    }
    $aggregate = [ordered]@{
        required_cases = @('dry_input_pipeline'); required_cases_passed = $false; missing_required_cases = @()
        cases = [ordered]@{settings_enable=@{verdict='NOT RUN'}; survival_cu_off=@{verdict='NOT RUN'}; dry_input_pipeline=@{verdict='NOT RUN'}}
    }
    Merge-AcceptanceWorker $aggregate $saved $(if ($OneCase -in @('none', 'stderr_zero')) {0} else {1})
    Complete-AcceptanceGate $aggregate
    if ($aggregate.required_cases_passed -ne ($OneCase -in @('none', 'stderr_zero'))) { throw 'dry_final_gate_disagrees' }
    $aggregate.source_hashes = @{}
    foreach ($file in @('enabled.ps1', 'model_stub.py', 'evidence.ps1', 'Native.cs')) {
        $aggregate.source_hashes[$file] = (Get-FileHash -LiteralPath (Join-Path $SourceRoot $file) -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $aggregate.os_version = [Environment]::OSVersion.Version.ToString()
    $aggregate.powershell_version = $PSVersionTable.PSVersion.ToString()
    $aggregate.actual_native_source_compiled_without_invocation = $windowsHost
    $aggregate.mode = 'dry'; $aggregate.product_acceptance = $false
    $aggregate.fault = $OneCase; $aggregate.passed = $true
    Write-AcceptanceEvidence $aggregate (Join-Path $work 'dry-verdict.json')
} finally {
    if (-not $server.HasExited) { $server.Kill(); $null = $server.WaitForExit(5000) }
    $server.Dispose()
}
