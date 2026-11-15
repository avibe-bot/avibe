param([Parameter(Mandatory)][string] $Source, [Parameter(Mandatory)][string] $Work)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. $Source
$record = [ordered]@{required_cases=@('case'); cases=[ordered]@{case=@{verdict='PASS'}}}
try { throw 'input_original_failure' } catch { Add-AcceptanceFailure $record 'type_js' $_ }
$original = $record.failure | ConvertTo-Json -Compress
try { throw 'input_cleanup_failure' } catch { Add-AcceptanceFailure $record 'target_cleanup' $_ -Cleanup }
try { throw 'input_outer_abort' } catch { Add-AcceptanceFailure $record 'worker_abort' $_ }
if (($record.failure | ConvertTo-Json -Compress) -ne $original -or $record.cleanup_errors.Count -ne 1 -or $record.secondary_errors.Count -ne 1) { throw 'first_failure_not_immutable' }
Write-AcceptanceEvidence $record (Join-Path $Work 'result.json')
$worker = Get-Content -LiteralPath (Join-Path $Work 'result.json') -Raw | ConvertFrom-Json -AsHashtable
$parent = [ordered]@{required_cases=@('case'); cases=[ordered]@{case=@{verdict='NOT RUN'}}}
Merge-AcceptanceWorker $parent $worker 1
try { throw 'input_parent_postcheck' } catch { Add-AcceptanceFailure $parent 'postcheck' $_ }
Complete-AcceptanceGate $parent
if ($parent.required_cases_passed -or $parent.failure.call -ne 'type_js' -or $parent.cleanup_errors.Count -ne 1) { throw 'aggregate_lost_failure' }
foreach ($scenario in @('worker_nonzero', 'required_missing', 'cleanup_only')) {
    $parent = [ordered]@{required_cases=@('case'); cases=[ordered]@{case=@{verdict='NOT RUN'}}}
    $worker = @{cases=@{case=@{verdict=$(if($scenario -eq 'required_missing'){'NOT RUN'}else{'PASS'})}}}
    if ($scenario -eq 'cleanup_only') { try { throw 'input_cleanup_failure' } catch { Add-AcceptanceFailure $worker 'cleanup' $_ -Cleanup } }
    Merge-AcceptanceWorker $parent $worker $(if($scenario -eq 'worker_nonzero'){1}else{0})
    Complete-AcceptanceGate $parent
    if ($parent.required_cases_passed) { throw "aggregate_false_green_$scenario" }
}
foreach ($bad in @(@{}, @{cases=@{unknown=@{verdict='PASS'}}}, @{cases=@{case=@{verdict='pretend'}}})) {
    $rejected = $false
    try { Merge-AcceptanceWorker $parent $bad 0 } catch { $rejected = $true }
    if (-not $rejected) { throw 'invalid_worker_accepted' }
}
$blocked = Join-Path $Work 'blocked.json'
New-Item -ItemType Directory -Path ($blocked + '.tmp') -Force | Out-Null
$writeFailed = $false
try { Write-AcceptanceEvidence $parent $blocked } catch { $writeFailed = $true; Add-AcceptanceFailure $parent 'report_write' $_ }
if (-not $writeFailed -or (Test-Path -LiteralPath $blocked)) { throw 'failed_write_claimed_published' }
Write-Output 'evidence_fixture_cases_passed'
