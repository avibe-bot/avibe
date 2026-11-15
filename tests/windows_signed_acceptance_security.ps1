param([Parameter(Mandatory)][string] $Source, [Parameter(Mandatory)][string] $Work)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
. $Source
New-Item -ItemType Directory -Path $Work -Force | Out-Null
# Inert native boundary. No GUI, processes, accounts, clipboard or keychain.
Add-Type -TypeDefinition @'
public static class AcceptanceNative {
    public static bool FailToken;
    public static bool IsElevated(int pid) {
        if (FailToken) throw new System.InvalidOperationException("private token detail");
        return true;
    }
}
'@
function Fail-Boundary([string] $Phase) {
    if ($Phase -eq $script:failPhase) { throw 'private-account-path-token-must-not-escape' }
}
function Add-Type { param($Path); Fail-Boundary 'compile_native' }
function Get-CimInstance { param($ClassName,$Filter); Fail-Boundary 'primary_profile'; return @{LocalPath=$Work} }
function Get-FileHash { param($LiteralPath,$Algorithm); Fail-Boundary 'installed_driver_hash'; return @{Hash=('a'*64)} }
# Regression for signed run 43738110889: the elevated parent was denied reading
# the primary user's private state. It must never read it or probe its lock.
function Get-Content { param($LiteralPath,[switch]$Raw); throw 'parent_read_primary_private_state' }
function Test-Path { param($LiteralPath,$PathType); throw 'parent_probed_primary_private_lock' }
function New-Item { param($ItemType,$Path,[switch]$Force); Fail-Boundary 'security_directory'; throw 'test_must_stop_before_native_probe' }
$phases=@('compile_native','runner_token','primary_profile','installed_driver_hash','primary_request:pipe','primary_request:lock','security_directory')
$cases=@()
$validLeaf='avibe-cua-0123abcd-0123-4567-89ab-0123456789ab-1'
$validLock=Join-Path (Join-Path $Work 'AppData\Roaming\bot.avibe.desktop') 'computer-use.lock'
foreach ($entry in $phases) {
    $phase,$variant=$entry.Split(':')
    $script:failPhase=$phase
    [AcceptanceNative]::FailToken=($phase -eq 'runner_token')
    $request=[pscustomobject]@{action='prepare-security-fixtures'; pipe_leaf=$validLeaf; lock_path=$validLock}
    if ($variant -eq 'pipe') { $request.pipe_leaf='..\other-pipe' }
    if ($variant -eq 'lock') { $request.lock_path=(Join-Path $Work 'elsewhere.lock') }
    $context=@{Root=$Work; User=@{SID=@{Value='fixture'}}; DriverHash=('a'*64); Stage='security_fixture'; Cases=@{}; Request=$request}
    $reply=Invoke-SecurityFixtureResponse $context
    if ($reply.ok -or $reply.diagnostics.security_fixture.failure.call -ne $phase) { throw "wrong_failure_boundary_$phase" }
    if ($reply.diagnostics.security_fixture.failure.script_line -le 0) { throw 'source_line_missing' }
    if ($reply.cases.security_fixture.verdict -ne 'FAIL') { throw 'case_failure_missing' }
    if (($reply | ConvertTo-Json -Depth 20) -match 'private-account|private token') { throw 'private_exception_leaked' }
    $first=$reply.diagnostics.security_fixture.failure | ConvertTo-Json -Compress
    $context.Phase='cleanup'
    try { throw 'security_cleanup_failed' } catch { $null=Add-SecurityFailure $context $_ }
    if (($reply.diagnostics.security_fixture.failure | ConvertTo-Json -Compress) -ne $first) { throw 'first_error_overwritten' }
    if ($reply.diagnostics.security_fixture.secondary_errors.Count -ne 1) { throw 'secondary_error_missing' }
    $cases += @{phase=$entry; passed=$true}
}
# Test serialization and parent/worker merge using the actual shared merger.
foreach ($name in @('Add-Type','Get-CimInstance','Get-FileHash','Get-Content','Test-Path','New-Item')) { Remove-Item -LiteralPath "Function:\$name" }
$record=[ordered]@{cases=[ordered]@{security_fixture=@{verdict='FAIL';reason='parent'}}; security_fixture_receipt=$reply}
$worker=@{cases=@{security_fixture=@{verdict='FAIL';reason='generic_worker'}}; failure=@{code='generic_worker'}}
Merge-AcceptanceWorker $record $worker 1
if ($record.security_fixture_receipt.diagnostics.security_fixture.failure.call -ne 'security_directory') { throw 'parent_receipt_overwritten' }
Write-AcceptanceEvidence $record (Join-Path $Work 'security-dry.json')
$restored=[IO.File]::ReadAllText((Join-Path $Work 'security-dry.json')) | ConvertFrom-Json -AsHashtable
if ($restored.security_fixture_receipt.diagnostics.security_fixture.failure.call -ne 'security_directory') { throw 'parent_receipt_not_serialized' }
# Execute the actual lifecycle-selection expression, not a copied implementation.
$tokens=$null; $errors=$null
$enabledAst=[Management.Automation.Language.Parser]::ParseFile((Join-Path (Split-Path $Source) 'enabled.ps1'),[ref]$tokens,[ref]$errors)
$selection=$enabledAst.Find({param($n) $n -is [Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$operations'},$true)
foreach ($shape in @(@($false,$true,0),@($false,$false,2),@($true,$false,1))) {
    $inputOnly=$shape[0]; $securityOnly=$shape[1]
    . ([scriptblock]::Create($selection.Extent.Text))
    if (@($operations).Count -ne $shape[2]) { throw 'security_scope_selection_wrong' }
}
# Execute the real worker failure recorder: its generic failure cannot erase
# the parent's concrete case verdict.
$failureFunction=$enabledAst.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Record-EnabledCaseFailure'},$true)
. ([scriptblock]::Create($failureFunction.Extent.Text))
$currentCase='security_fixture'; $operationId=$null
$result=@{cases=[ordered]@{security_fixture=@{verdict='FAIL';reason='primary_lock_missing'}}}
function Verdict($Id,$Value,$Reason) { $result.cases[$Id]=@{verdict=$Value;reason=$Reason} }
try { throw 'security_fixture_failed_see_individual_verdicts' } catch { Record-EnabledCaseFailure $_ }
if ($result.cases.security_fixture.reason -ne 'primary_lock_missing') { throw 'worker_overwrote_parent_reason' }
$cases += @{phase='security_only_selection_and_preserved_case';passed=$true}
# Execute the real elevated-app decision over every evidence shape it must
# classify. PASS only for the product's protected_window plus a verified control.
$decisionFunction=$enabledAst.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Resolve-ElevatedAppVerdict'},$true)
. ([scriptblock]::Create($decisionFunction.Extent.Text))
function Shape($TypeOk,$TypeCode,$ErrorCode,$PidMatch,$ControlOk,$Verified) {
    [pscustomobject]@{
        elevated=[pscustomobject]@{pid_match=$PidMatch; type_ok=$TypeOk; type_code=$TypeCode; error_code=$ErrorCode}
        control=[pscustomobject]@{type_ok=$ControlOk}; control_text_verified=$Verified
    }
}
foreach ($expected in @(
    @((Shape $false 'protected_window' $null $true $true $true), 'PASS', 'real_elevated_notepad_control_refused_with_protected_window'),
    @((Shape $null $null 'protected_window' $true $true $true), 'PASS', 'real_elevated_notepad_control_refused_with_protected_window'),
    @((Shape $true $null $null $true $true $true), 'FAIL', 'elevated_type_accepted_product_failure'),
    @((Shape $false 'protected_window' $null $true $true $false), 'FAIL', 'ordinary_control_window_unproven'),
    @((Shape $false 'protected_window' $null $true $false $false), 'FAIL', 'ordinary_control_window_unproven'),
    @((Shape $false 'app_missing' $null $true $true $true), 'FAIL', 'explicit_protected_window_refusal_missing'),
    @((Shape $null $null 'wrong_elevated_target' $false $true $true), 'FAIL', 'explicit_protected_window_refusal_missing'),
    @($null, 'FAIL', 'elevated_app_evidence_missing')
)) {
    $decision = Resolve-ElevatedAppVerdict $expected[0]
    if ($decision.verdict -ne $expected[1] -or $decision.reason -ne $expected[2]) { throw "elevated_decision_wrong_$($expected[2])" }
}
$cases += @{phase='elevated_app_decision_shapes';passed=$true}
$cases | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $Work 'security-cases.json')
'security_fixture_cases_passed'
