param([Parameter(Mandatory)][string] $Source, [Parameter(Mandatory)][string] $Work)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. $Source
# Portable behavioral tests of the actual observer helpers. Every process,
# inventory and socket below is a fixture object; no native or network calls.
$env:SystemRoot = 'C:\Windows'; $env:TEMP = 'C:\fixture'
$script:Events = [Collections.Generic.List[string]]::new()
$script:Rows = @(); $script:Processes = @{}; $script:Listeners = @(); $script:Mode = 'cleanup'
# Exercise sub-microsecond Process time explicitly, independently of the host
# clock's resolution; the synthetic CIM rows below have microsecond precision.
$now = [DateTime]::new(2026, 11, 15, 7, 0, 0, [DateTimeKind]::Utc).AddTicks(7)
function New-FakeProcess([int] $Id, [string] $Label, [datetime] $Created, [bool] $Fail = $false) {
    $p = [pscustomobject]@{ Id = $Id; Label = $Label; HasExited = $false; StartTime = $Created; Fail = $Fail; Disposed = $false }
    $p | Add-Member ScriptMethod Kill { $script:Events.Add($this.Label); if ($this.Fail) { throw 'fixture_stop_refused' }; $this.HasExited = $true }
    $p | Add-Member ScriptMethod WaitForExit { param($Milliseconds) return $this.HasExited }
    $p | Add-Member ScriptMethod Dispose { $this.Disposed = $true }
    $script:Processes[$Id] = $p
    return $p
}
function New-Row([int] $Id, [int] $Parent, [datetime] $Created, [string] $Image = 'C:\app\webview.exe', [string] $Command = '') {
    # Every fixture Process time has the literal seven-tick residue above;
    # specify its CIM counterpart without reusing the observer's normalization.
    $cimCreated = $Created.AddTicks(-7)
    return [pscustomobject]@{ ProcessId = $Id; ParentProcessId = $Parent; CreationDate = $cimCreated; ExecutablePath = $Image; CommandLine = $Command }
}
# The observer now reads same-user process rows (handle queries), never CIM.
function Get-CimInstance { throw 'fixture_observer_used_cim' }
function Get-UpdateProcessRows([string[]] $Names) {
    if ($script:Mode -eq 'lineage') { return $script:Rows }
    return @($script:Rows | Where-Object {
        -not $script:Processes[[int]$_.ProcessId].HasExited -and
        (-not $Names -or (($_.ExecutablePath -split '\\')[-1] -replace '\.exe$', '') -in $Names)
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
# Same-user rows carry no command line: bind the supervisor by the single
# staged request whose pid is the original Desktop; refuse ambiguity.
$staging = Join-Path $Work 'temp'
$first = Join-Path $staging 'avibe-update-one'
New-Item -ItemType Directory -Path $first -Force | Out-Null
Set-Content -LiteralPath (Join-Path $first 'install.ps1') -Value '#'
Set-Content -LiteralPath (Join-Path $first 'request.json') -Value '{"pid":10}'
$plain = New-Row 20 10 $now $shell
if ((Get-UpdateSupervisorStage $plain 10 $staging) -ne $first) { throw 'fixture_staged_request_binding_failed' }
if (Get-UpdateSupervisorStage $plain 11 $staging) { throw 'fixture_other_desktop_request_accepted' }
$second = Join-Path $staging 'avibe-update-two'
New-Item -ItemType Directory -Path $second -Force | Out-Null
Set-Content -LiteralPath (Join-Path $second 'install.ps1') -Value '#'
Set-Content -LiteralPath (Join-Path $second 'request.json') -Value '{"pid":10}'
if (Get-UpdateSupervisorStage $plain 10 $staging) { throw 'fixture_ambiguous_staged_requests_accepted' }

# Post-click Desktop classification over the settle window.
$handoff = @([pscustomobject]@{ pid = 40; exited = $false }, [pscustomobject]@{ pid = 41; exited = $true })
$decision = Resolve-UpdateDesktops $handoff
if ($decision.reason -or $decision.survivor -ne 40) { throw 'fixture_single_instance_handoff_rejected' }
$single = Resolve-UpdateDesktops @([pscustomobject]@{ pid = 42; exited = $false })
if ($single.reason -or $single.survivor -ne 42) { throw 'fixture_single_relaunch_rejected' }
$double = Resolve-UpdateDesktops @([pscustomobject]@{ pid = 43; exited = $false }, [pscustomobject]@{ pid = 44; exited = $false })
if ($double.reason -ne 'update_product_double_launch') { throw 'fixture_double_launch_accepted' }
$none = Resolve-UpdateDesktops @([pscustomobject]@{ pid = 45; exited = $true })
if ($none.reason -ne 'update_real_relaunch_unproven') { throw 'fixture_no_survivor_accepted' }
if ((Resolve-UpdateDesktops @()).reason -ne 'update_real_relaunch_unproven') { throw 'fixture_no_desktop_accepted' }
# Outcome-only update helpers, executed from the real update.ps1.
$tokens = $null; $errors = $null
$updateAst = [Management.Automation.Language.Parser]::ParseFile((Join-Path (Split-Path $Source) 'update.ps1'), [ref]$tokens, [ref]$errors)
foreach ($name in @('Get-InstalledDesktops', 'Resolve-UpdateOutcomeCount')) {
    $definition = $updateAst.Find({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    . ([scriptblock]::Create($definition.Extent.Text))
}
if ((Resolve-UpdateOutcomeCount @(1)) -ne $null) { throw 'fixture_single_desktop_rejected' }
if ((Resolve-UpdateOutcomeCount @(1, 2)) -ne 'update_product_double_launch') { throw 'fixture_double_launch_accepted' }
if ((Resolve-UpdateOutcomeCount @()) -ne 'update_relaunched_desktop_exited') { throw 'fixture_no_desktop_accepted' }
# Plain same-user listing against this real process: exact path matches, a
# different path never does, and returned handles carry start time.
$self = Get-Process -Id $PID
$selfPath = $self.Path; $selfName = $self.ProcessName; $self.Dispose()
$listed = Get-InstalledDesktops $selfPath $selfName
try {
    if (-not @($listed | Where-Object { $_.Id -eq $PID }).Count) { throw 'fixture_own_process_not_listed' }
    foreach ($process in $listed) { $null = $process.StartTime }
} finally { foreach ($process in $listed) { $process.Dispose() } }
$none = Get-InstalledDesktops ($selfPath + '.other') $selfName
if (@($none).Count) { throw 'fixture_wrong_path_listed' }
Write-Output 'update_observer_fixture_cases_passed'
