param([Parameter(Mandatory)][string] $Source, [Parameter(Mandatory)][string] $Work)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. $Source
# Portable behavioral tests of the actual observer helpers. Every process,
# inventory and socket below is a fixture object; no native or network calls.
$env:SystemRoot = 'C:\Windows'; $env:TEMP = 'C:\fixture'
$script:Events = [Collections.Generic.List[string]]::new()
$script:Rows = @(); $script:Processes = @{}; $script:Listeners = @(); $script:Mode = 'cleanup'
$now = [DateTime]::UtcNow.AddSeconds(-10)
function New-FakeProcess([int] $Id, [string] $Label, [datetime] $Created, [bool] $Fail = $false) {
    $p = [pscustomobject]@{ Id = $Id; Label = $Label; HasExited = $false; StartTime = $Created; Fail = $Fail; Disposed = $false }
    $p | Add-Member ScriptMethod Kill { $script:Events.Add($this.Label); if ($this.Fail) { throw 'fixture_stop_refused' }; $this.HasExited = $true }
    $p | Add-Member ScriptMethod WaitForExit { param($Milliseconds) return $this.HasExited }
    $p | Add-Member ScriptMethod Dispose { $this.Disposed = $true }
    $script:Processes[$Id] = $p
    return $p
}
function New-Row([int] $Id, [int] $Parent, [datetime] $Created, [string] $Image = 'C:\app\webview.exe', [string] $Command = '') {
    return [pscustomobject]@{ ProcessId = $Id; ParentProcessId = $Parent; CreationDate = $Created; ExecutablePath = $Image; CommandLine = $Command }
}
function Get-CimInstance($Class, $Filter, $OperationTimeoutSec) {
    if ($script:Mode -eq 'lineage') { return $script:Rows }
    return @($script:Rows | Where-Object {
        -not $script:Processes[[int]$_.ProcessId].HasExited -and
        (-not $Filter -or ($Filter -eq "Name='powershell.exe'" -and $_.ExecutablePath.EndsWith('powershell.exe')) -or ($Filter -eq "Name='update.exe'" -and $_.ExecutablePath.EndsWith('update.exe')))
    })
}
function Get-NetTCPConnection($LocalPort, $State, $ErrorAction) { return $script:Listeners }
function Open-UpdateProcess($Row, $Since) { return $script:Processes[[int]$Row.ProcessId] }
function Expect-Error([scriptblock] $Body, [string] $Code) {
    $observed = $null
    try { & $Body } catch { $observed = $_.Exception.Message }
    if ($observed -ne $Code) { throw "fixture_expected_$Code" }
}
$shell = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
$command = 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "C:\fixture\avibe-update-abc\install.ps1" -Request "C:\fixture\avibe-update-abc\request.json"'
$row = New-Row 20 10 $now $shell $command
if ((Get-UpdateSupervisorStage $row 10 'C:\fixture') -ne 'C:\fixture\avibe-update-abc') { throw 'fixture_supervisor_positive_failed' }
if (Get-UpdateSupervisorStage $row 99 'C:\fixture') { throw 'fixture_wrong_parent_accepted' }
$row.CommandLine = $command.Replace('abc\request', 'other\request')
if (Get-UpdateSupervisorStage $row 10 'C:\fixture') { throw 'fixture_mixed_transactions_accepted' }
$row.CommandLine = $command.Replace('C:\fixture\', 'C:\outside\')
if (Get-UpdateSupervisorStage $row 10 'C:\fixture') { throw 'fixture_outside_staging_accepted' }

foreach ($scenario in @('known', 'discovered_before_installer', 'supervisor_refuses_stop')) {
    $script:Events.Clear(); $script:Rows = @(); $script:Processes = @{}
    $original = New-FakeProcess 10 'original' $now
    $supervisor = New-FakeProcess 20 'supervisor' $now ($scenario -eq 'supervisor_refuses_stop')
    $installer = New-FakeProcess 30 'installer' $now
    $context = @{ Quiesced = $false }
    $known = $supervisor
    if ($scenario -eq 'discovered_before_installer') {
        $known = $null
        # Even missing command metadata cannot hide an owned direct supervisor
        # from failure cleanup. It cannot count as acceptance provenance.
        $script:Rows = @(New-Row 20 10 $now $shell)
    }
    if ($scenario -eq 'supervisor_refuses_stop') {
        Expect-Error { Stop-UpdateTransaction $context $original $null $known $installer $now } 'update_transaction_cleanup_unproven'
        if ($context.Quiesced) { throw 'fixture_unsafe_uninstall_gate_opened' }
    } else {
        Stop-UpdateTransaction $context $original $null $known $installer $now
        if (-not $context.Quiesced) { throw 'fixture_quiescence_not_recorded' }
    }
    if (($script:Events -join ',') -ne 'original,supervisor,installer') { throw 'fixture_supervisor_not_stopped_before_installer' }
}

$script:Mode = 'lineage'
foreach ($scenario in @('new_generation', 'stale_generation', 'mixed_listeners', 'exited_replacement', 'reused_root_pid', 'child_predates_parent')) {
    $script:Processes = @{}
    $root = New-FakeProcess 10 'replacement' $now
    $child = New-FakeProcess 11 'new_webview' $now.AddSeconds(1)
    $script:Rows = @((New-Row 10 1 $now), (New-Row 11 10 $now.AddSeconds(1)))
    $script:Listeners = @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 11 })
    $errorCode = 'update_cdp_generation_unproven'
    switch ($scenario) {
        'stale_generation' { $script:Rows[1].ParentProcessId = 9 }
        'mixed_listeners' {
            $script:Rows += New-Row 12 9 $now.AddSeconds(-1)
            $script:Listeners += [pscustomobject]@{ LocalAddress = '::1'; OwningProcess = 12 }
        }
        'exited_replacement' { $root.HasExited = $true; $errorCode = 'update_replacement_generation_exited' }
        'reused_root_pid' { $script:Rows[0].CreationDate = $now.AddSeconds(1); $errorCode = 'update_replacement_generation_changed' }
        'child_predates_parent' { $script:Rows[1].CreationDate = $now.AddSeconds(-1) }
    }
    if ($scenario -eq 'new_generation') {
        $handles = Pin-UpdateDebugger $root 9222
        if ($handles.Count -ne 1 -or $handles[0].Id -ne 11) { throw 'fixture_new_generation_not_pinned' }
        foreach ($handle in $handles) { $handle.Dispose() }
    } else { Expect-Error { $null = Pin-UpdateDebugger $root 9222 } $errorCode }
}
Write-Output 'update_observer_fixture_cases_passed'
