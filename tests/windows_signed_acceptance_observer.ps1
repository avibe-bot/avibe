param([Parameter(Mandatory)][string] $Source, [Parameter(Mandatory)][string] $Work)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# Outcome-only update helpers, executed from the real update.ps1 without
# running its case. No native, network, process-kill or installer action.
$tokens = $null; $errors = $null
$updateAst = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'fixture_update_source_parse_failed' }
foreach ($name in @('Get-InstalledDesktops', 'Resolve-UpdateOutcomeCount')) {
    $definition = $updateAst.Find({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    if (-not $definition) { throw "fixture_missing_$name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}
function D([bool] $Original, [bool] $PostClick, [bool] $Window) { [pscustomobject]@{ is_original = $Original; post_click = $PostClick; has_window = $Window } }
foreach ($case in @(
    @(@((D $false $true $true)), $null),
    @(@((D $false $true $true), (D $false $true $true)), 'update_product_double_launch'),
    @(@(), 'update_relaunched_desktop_exited'),
    @(@((D $true $false $true)), 'update_original_desktop_not_exited'),
    @(@((D $true $false $true), (D $false $true $true)), 'update_original_desktop_not_exited'),
    @(@((D $false $false $true)), 'update_relaunched_desktop_unproven'),
    @(@((D $false $true $false)), 'update_relaunched_desktop_unproven')
)) {
    if ((Resolve-UpdateOutcomeCount $case[0]) -ne $case[1]) { throw "fixture_outcome_wrong_$($case[1])" }
}
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
Write-Output 'update_outcome_fixture_cases_passed'
