param([Parameter(Mandatory)][string] $Source, [Parameter(Mandatory)][string] $Work)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. $Source
# Hermetic boundary substitutes: no network, Authenticode/native call, process
# launch or user-state access. These test the actual shared release verifier.
$script:Downloads = 0; $script:Admissions = 0; $script:Signature = 'valid'
$tag = 'gh-v3.2.7rc3'; $stem = 'Avibe_3.2.7-rc.3_x86_64-pc-windows-msvc'
$fixture = Join-Path $Work 'fixture'
New-Item -ItemType Directory -Path $fixture -Force | Out-Null
function Invoke-WebRequest($Uri, $OutFile, $TimeoutSec) {
    if ($Uri -notlike "https://github.com/avibe-bot/avibe/releases/download/$tag/*" -or $TimeoutSec -gt 180) { throw 'fixture_unexpected_download' }
    $script:Downloads++
    Copy-Item -LiteralPath (Join-Path $fixture ([IO.Path]::GetFileName($OutFile))) -Destination $OutFile
}
function Get-AuthenticodeSignature($LiteralPath) {
    if (-not (Test-Path -LiteralPath $LiteralPath)) { throw 'fixture_missing_installer' }
    return [pscustomobject]@{
        Status = if ($script:Signature -eq 'invalid') { 'NotTrusted' } else { 'Valid' }
        SignerCertificate = [pscustomobject]@{ Issuer = if ($script:Signature -eq 'wrong_issuer') { 'Other CA' } else { 'Certum fixture CA' } }
        TimeStamperCertificate = if ($script:Signature -eq 'no_timestamp') { $null } else { [pscustomobject]@{ Present = $true } }
    }
}
function Prepare-Fixture([string] $Scenario) {
    $script:Downloads = 0; $script:Admissions = 0; $script:Signature = $Scenario
    [IO.File]::WriteAllText((Join-Path $fixture "$stem.exe"), 'not-an-executable')
    $sourceData = @{ tag = $tag; source_sha = ('c' * 40); target = 'x86_64-pc-windows-msvc'; computer_use_driver = @{ prepared_binary_sha256 = ('a' * 64); packaged_sha256 = ('a' * 64) } }
    if ($Scenario -eq 'wrong_tag') { $sourceData.tag = 'gh-v3.2.7rc4' }
    if ($Scenario -eq 'wrong_stage2') { $sourceData.computer_use_driver.packaged_sha256 = ('b' * 64) }
    $sourceData | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $fixture "$stem.SOURCE.json")
    $sums = foreach ($suffix in @('.exe', '.SOURCE.json')) {
        $hash = (Get-FileHash -LiteralPath (Join-Path $fixture "$stem$suffix") -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $stem$suffix"
    }
    if ($Scenario -eq 'wrong_checksum') { $sums[0] = ('0' * 64) + "  $stem.exe" }
    if ($Scenario -eq 'duplicate_checksum') { $sums += $sums[0] }
    $sums | Set-Content -LiteralPath (Join-Path $fixture "$stem.SHA256SUMS")
}
$cases = [ordered]@{
    valid = $null
    invalid = 'trusted_timestamped_certum_signature_required'
    wrong_issuer = 'trusted_timestamped_certum_signature_required'
    no_timestamp = 'trusted_timestamped_certum_signature_required'
    wrong_tag = 'release_source_identity_mismatch'
    wrong_stage2 = 'stage2_driver_hash_mismatch'
    wrong_checksum = 'release_checksum_mismatch'
    duplicate_checksum = 'checksum_entry_missing_or_ambiguous'
    budget = 'acceptance_budget_exhausted'
    candidate_commit = 'candidate_source_commit_mismatch'
    candidate_hash = 'candidate_driver_hash_mismatch'
}
foreach ($entry in $cases.GetEnumerator()) {
    Prepare-Fixture $entry.Key
    $deadline = if ($entry.Key -eq 'budget') { (Get-Date).AddSeconds(60) } else { (Get-Date).AddMinutes(10) }
    $errorCode = $null; $proof = $null
    $commitPin = if ($entry.Key -eq 'candidate_commit') { 'd' * 40 } else { 'c' * 40 }
    $hashPin = if ($entry.Key -eq 'candidate_hash') { 'b' * 64 } else { 'a' * 64 }
    try { $proof = Get-VerifiedRelease $tag (Join-Path $Work $entry.Key) $deadline { $script:Admissions++ } $commitPin $hashPin }
    catch { $errorCode = $_.Exception.Message }
    if ($errorCode -ne $entry.Value) { throw "fixture_unexpected_verdict_$($entry.Key)" }
    if ($entry.Key -eq 'valid') {
        if ($proof.Version -ne '3.2.7rc3' -or $proof.DesktopVersion -ne '3.2.7-rc.3' -or $proof.DriverHash -ne ('a' * 64) -or $proof.Tag -ne $tag -or $script:Downloads -ne 3 -or $script:Admissions -ne 3) { throw 'fixture_valid_proof_incomplete' }
    }
    if ($entry.Key -eq 'budget' -and ($script:Downloads -ne 0 -or $script:Admissions -ne 1)) { throw 'fixture_budget_did_not_refuse_before_download' }
}
# Installed-byte/provenance binding is exercised on inert test files only.
$install = Join-Path $Work 'installed'
$computerUse = Join-Path $install 'computer-use'
New-Item -ItemType Directory -Path $computerUse -Force | Out-Null
$binary = Join-Path $computerUse 'cua-driver.exe'
$bytes = [Text.Encoding]::UTF8.GetBytes('not-an-executable')
$expectedHash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
$expected = [pscustomobject]@{ prepared_binary_sha256 = $expectedHash; packaged_sha256 = $expectedHash; target = 'x86_64-pc-windows-msvc'; patch_chain = @([pscustomobject]@{ file = 'fixture.patch'; sha256 = ('e' * 64) }) }
foreach ($scenario in @('valid', 'bytes', 'size', 'provenance', 'shape')) {
    [IO.File]::WriteAllBytes($binary, $bytes)
    $installed = @{ prepared_binary_sha256 = $expectedHash; target = 'x86_64-pc-windows-msvc'; patch_chain = $expected.patch_chain }
    $expectedSize = $bytes.Length
    $wanted = $null
    switch ($scenario) {
        'bytes' { [IO.File]::WriteAllText($binary, 'changed'); $wanted = 'installed_driver_stage2_mismatch' }
        'size' { $expectedSize++; $wanted = 'installed_driver_candidate_size_mismatch' }
        'provenance' { $installed.prepared_binary_sha256 = 'f' * 64; $wanted = 'installed_driver_provenance_mismatch' }
        'shape' { $installed.extra = $true; $wanted = 'installed_driver_provenance_shape_mismatch' }
    }
    $installed | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $computerUse 'driver-provenance.json')
    $observed = $null
    try { $bound = Assert-InstalledDriver $install $expectedHash $expectedSize $expected }
    catch { $observed = $_.Exception.Message }
    if ($observed -ne $wanted) { throw "fixture_installed_binding_$scenario" }
    if ($scenario -eq 'valid' -and ($bound.Hash -ne $expectedHash -or $bound.Size -ne $bytes.Length -or -not $bound.ProvenanceVerified)) { throw 'fixture_installed_binding_incomplete' }
}
Write-Output 'release_proof_fixture_cases_passed'
