# Public signed release input only. No signing, release writes, or credentials.
function Get-VerifiedRelease([string] $Tag, [string] $Directory, [datetime] $Deadline, [scriptblock] $AdmissionCheck) {
    if ($Tag -notmatch '^gh-v3\.2\.7rc[0-9]+$') { throw 'acceptance_release_tag_invalid' }
    New-Item -ItemType Directory -Path $Directory -Force | Out-Null
    $base = "https://github.com/avibe-bot/avibe/releases/download/$Tag"
    $version = $Tag -replace '^gh-v', '' -replace 'rc', '-rc.'
    $stem = "Avibe_${version}_x86_64-pc-windows-msvc"
    foreach ($suffix in @('.exe', '.SHA256SUMS', '.SOURCE.json')) {
        & $AdmissionCheck
        $remaining = [int]($Deadline - (Get-Date)).TotalSeconds - 180
        if ($remaining -lt 30) { throw 'acceptance_budget_exhausted' }
        Invoke-WebRequest -Uri "$base/$stem$suffix" -OutFile (Join-Path $Directory "$stem$suffix") -TimeoutSec ([Math]::Min(180, $remaining))
    }
    $sums = Get-Content -LiteralPath (Join-Path $Directory "$stem.SHA256SUMS")
    $installerHash = $null
    foreach ($suffix in @('.exe', '.SOURCE.json')) {
        $name = "$stem$suffix"
        $entries = @($sums | Where-Object { $_ -match ('^[a-fA-F0-9]{64}\s+\*?' + [regex]::Escape($name) + '$') })
        if ($entries.Count -ne 1) { throw 'checksum_entry_missing_or_ambiguous' }
        $actual = (Get-FileHash -LiteralPath (Join-Path $Directory $name) -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $entries[0].Substring(0, 64).ToLowerInvariant()) { throw 'release_checksum_mismatch' }
        if ($suffix -eq '.exe') { $installerHash = $actual }
    }
    $installer = Join-Path $Directory "$stem.exe"
    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate -or $signature.SignerCertificate.Issuer -notmatch '(?i)Certum' -or -not $signature.TimeStamperCertificate) {
        throw 'trusted_timestamped_certum_signature_required'
    }
    $source = Get-Content -LiteralPath (Join-Path $Directory "$stem.SOURCE.json") -Raw | ConvertFrom-Json
    if ($source.tag -ne $Tag -or $source.target -ne 'x86_64-pc-windows-msvc') { throw 'release_source_identity_mismatch' }
    $hash = [string]$source.computer_use_driver.prepared_binary_sha256
    if ($hash -notmatch '^[a-f0-9]{64}$' -or $hash -ne $source.computer_use_driver.packaged_sha256) { throw 'stage2_driver_hash_mismatch' }
    return @{ Installer = $installer; InstallerHash = $installerHash; DriverHash = $hash; Version = ($Tag -replace '^gh-v', ''); DesktopVersion = $version; Tag = $Tag }
}
