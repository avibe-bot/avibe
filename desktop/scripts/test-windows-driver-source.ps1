$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:RUNNER_OS -ne 'Windows') {
    throw 'Windows Cua Driver native tests require a disposable GitHub-hosted Windows job.'
}
if ($PSVersionTable.PSVersion.Major -lt 7) { throw 'PowerShell 7 is required.' }

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$desktop = Join-Path $repo 'desktop'
$prepare = Join-Path $desktop 'scripts\prepare-cua-driver.py'
$manifestPath = Join-Path $desktop 'cua-driver\sources.json'
$patch1 = Join-Path $desktop 'cua-driver\patches\0001-macos-raw-single-click.patch'
$patch2 = Join-Path $desktop 'cua-driver\patches\0002-windows-security-boundary.patch'
$artifactRoot = Join-Path $env:RUNNER_TEMP ('avibe-windows-driver-security-' + [guid]::NewGuid().ToString('N'))
$logs = Join-Path $artifactRoot 'logs'
$sourceWork = Join-Path $artifactRoot 'source-work'
$fixtureRoot = Join-Path $artifactRoot 'fixture'
$bin = Join-Path $fixtureRoot 'bin'
$temp = Join-Path $fixtureRoot 'tmp'
$testLogs = Join-Path $logs 'tests'
$provenancePath = Join-Path $artifactRoot 'provenance.json'
$helperPath = Join-Path $artifactRoot 'prepare-windows-source.py'
New-Item -ItemType Directory -Force -Path $artifactRoot, $logs, $sourceWork, $fixtureRoot, $bin, $temp, $testLogs | Out-Null

$tests = @(
    # Keep this list explicit and reviewable. Each entry is checked against the
    # patched source before execution; keep this list synchronized with the
    # final reviewed native source selector set.
    @{ Package = 'cua-driver-core'; ArtifactKey = 'cua-driver-core'; Selector = 'tool::windows_launch_ownership_tests::missing_wrong_nonprocess_or_exited_evidence_never_registers'; Source = 'libs/cua-driver/rust/crates/cua-driver-core/src/windows_launch_ownership_tests.rs'; Function = 'missing_wrong_nonprocess_or_exited_evidence_never_registers' },
    @{ Package = 'cua-driver-core'; ArtifactKey = 'cua-driver-core'; Selector = 'tool::windows_launch_ownership_tests::failed_or_incomplete_attestation_never_registers'; Source = 'libs/cua-driver/rust/crates/cua-driver-core/src/windows_launch_ownership_tests.rs'; Function = 'failed_or_incomplete_attestation_never_registers' },
    @{ Package = 'cua-driver-core'; ArtifactKey = 'cua-driver-core'; Selector = 'tool::windows_launch_ownership_tests::original_handle_lives_across_attestation_and_later_reuse_is_refused'; Source = 'libs/cua-driver/rust/crates/cua-driver-core/src/windows_launch_ownership_tests.rs'; Function = 'original_handle_lives_across_attestation_and_later_reuse_is_refused' },
    @{ Package = 'cua-driver-core'; ArtifactKey = 'cua-driver-core'; Selector = 'tool::windows_launch_ownership_tests::retained_exited_process_is_not_registered_as_running'; Source = 'libs/cua-driver/rust/crates/cua-driver-core/src/windows_launch_ownership_tests.rs'; Function = 'retained_exited_process_is_not_registered_as_running' },
    @{ Package = 'cua-driver-core'; ArtifactKey = 'cua-driver-core'; Selector = 'tool::windows_launch_ownership_tests::original_handle_evidence_never_serializes'; Source = 'libs/cua-driver/rust/crates/cua-driver-core/src/windows_launch_ownership_tests.rs'; Function = 'original_handle_evidence_never_serializes' },
    @{ Package = 'cua-driver-core'; ArtifactKey = 'cua-driver-core'; Selector = 'tool::windows_launch_ownership_tests::registration_future_is_send'; Source = 'libs/cua-driver/rust/crates/cua-driver-core/src/windows_launch_ownership_tests.rs'; Function = 'registration_future_is_send' },
    @{ Package = 'platform-windows'; ArtifactKey = 'platform-windows'; Selector = 'literal_launch::native::tests::literal_native_canonicalizes_existing_regular_executable'; Source = 'libs/cua-driver/rust/crates/platform-windows/src/literal_launch_native_tests.rs'; Function = 'literal_native_canonicalizes_existing_regular_executable' },
    @{ Package = 'platform-windows'; ArtifactKey = 'platform-windows'; Selector = 'literal_launch::native::tests::literal_native_profile_environment_and_job_survival'; Source = 'libs/cua-driver/rust/crates/platform-windows/src/literal_launch_native_tests.rs'; Function = 'literal_native_profile_environment_and_job_survival' },
    @{ Package = 'platform-windows'; ArtifactKey = 'platform-windows'; Selector = 'tools::impl_::literal_launch_callsite_tests::unsupported_routes_refuse_at_actual_launch_tool_before_native_effects'; Source = 'libs/cua-driver/rust/crates/platform-windows/src/tools/impl_.rs'; Function = 'unsupported_routes_refuse_at_actual_launch_tool_before_native_effects' },
    @{ Package = 'platform-windows'; ArtifactKey = 'platform-windows'; Selector = 'tools::impl_::literal_launch_callsite_tests::literal_creation_is_not_advertised_as_idempotent'; Source = 'libs/cua-driver/rust/crates/platform-windows/src/tools/impl_.rs'; Function = 'literal_creation_is_not_advertised_as_idempotent' },
    @{ Package = 'platform-windows'; ArtifactKey = 'platform-windows'; Selector = 'tools::impl_::literal_launch_callsite_tests::owner_only_termination_matches_creation_time'; Source = 'libs/cua-driver/rust/crates/platform-windows/src/tools/impl_.rs'; Function = 'owner_only_termination_matches_creation_time' }
)

$results = [System.Collections.Generic.List[object]]::new()
$recordedProcesses = [System.Collections.Generic.List[Diagnostics.Process]]::new()
$cleanupFailures = [System.Collections.Generic.List[string]]::new()
$user = $null
$password = $null
$fatal = $null
$provenance = [ordered]@{
    schema_version = 1
    avibe_head = $null
    source_commit = '5272e492d61b96caf08e3bf434d91126c1f3dccc'
    source_archive = $null
    source_archive_sha256 = $null
    patch_0001 = [ordered]@{ path = 'desktop/cua-driver/patches/0001-macos-raw-single-click.patch'; sha256 = $null }
    patch_0002 = [ordered]@{ path = 'desktop/cua-driver/patches/0002-windows-security-boundary.patch'; sha256 = $null }
    test_binaries = @()
    expected_test_count = $tests.Count
}

function Sha256([string] $Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Set-TestAccess([string] $Path, [System.Security.Principal.SecurityIdentifier] $Sid, [string] $Rights) {
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($entry in @(@('S-1-5-18', 'FullControl'), @('S-1-5-32-544', 'FullControl'), @($Sid.Value, $Rights))) {
        $identity = [System.Security.Principal.SecurityIdentifier]::new($entry[0])
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $identity, $entry[1], 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function XmlEscape([string] $Value) {
    if ($null -eq $Value) { return '' }
    return [System.Security.SecurityElement]::Escape($Value)
}

function Write-Junit([hashtable] $Test, [string] $Status, [double] $Seconds, [string] $Stdout, [string] $Stderr, [string] $FailureMessage) {
    $safeName = ($Test.Selector -replace '[^A-Za-z0-9_.-]', '_')
    $path = Join-Path $artifactRoot ('junit-' + $safeName + '.xml')
    $failed = if ($Status -eq 'passed') { 0 } else { 1 }
    $failure = if ($failed) { '<failure message="' + (XmlEscape $FailureMessage) + '">' + (XmlEscape $FailureMessage) + '</failure>' } else { '' }
    $xml = @"
<?xml version="1.0" encoding="utf-8"?>
<testsuite name="avibe.windows-driver-security" tests="1" failures="$failed" skipped="0" time="$('{0:F3}' -f $Seconds)">
  <testcase classname="$(XmlEscape $Test.Package)" name="$(XmlEscape $Test.Selector)" time="$('{0:F3}' -f $Seconds)">$failure<system-out>$(XmlEscape $Stdout)</system-out><system-err>$(XmlEscape $Stderr)</system-err></testcase>
</testsuite>
"@
    Set-Content -LiteralPath $path -Value $xml -Encoding UTF8
}

function Write-GuardJunit {
    $passed = @($results | Where-Object { $_.Status -eq 'passed' }).Count
    $failed = @($results | Where-Object { $_.Status -ne 'passed' }).Count
    $ok = ($null -eq $fatal -and $results.Count -eq $tests.Count -and $passed -eq $tests.Count -and $failed -eq 0)
    $message = if ($ok) { 'All expected selectors executed exactly once with 1 passed, 0 failed, 0 ignored.' } else { "Expected $($tests.Count) passing executions; observed $($results.Count) result records, $passed passed, $failed failed." }
    $failure = if ($ok) { '' } else { '<failure message="' + (XmlEscape $message) + '">' + (XmlEscape $message) + '</failure>' }
    $xml = @"
<?xml version="1.0" encoding="utf-8"?>
<testsuite name="avibe.windows-driver-security.no-skip-count-guard" tests="1" failures="$(if ($ok) { 0 } else { 1 })" skipped="0">
  <testcase classname="guard" name="expected-test-count-and-no-skip-guard">$failure</testcase>
</testsuite>
"@
    Set-Content -LiteralPath (Join-Path $artifactRoot 'junit-no-skip-count-guard.xml') -Value $xml -Encoding UTF8
}

function Verify-JunitEvidence {
    $expected = @($tests | ForEach-Object { [string]$_.Selector })
    $files = @(Get-ChildItem -LiteralPath $artifactRoot -Filter 'junit-*.xml' -File | Where-Object { $_.Name -ne 'junit-no-skip-count-guard.xml' })
    if ($files.Count -ne $tests.Count) { throw "Expected $($tests.Count) per-test JUnit files, found $($files.Count)." }
    $names = [System.Collections.Generic.List[string]]::new()
    foreach ($file in $files) {
        [xml]$document = Get-Content -LiteralPath $file.FullName -Raw
        $suite = $document.testsuite
        if (-not $suite -or [int]$suite.failures -ne 0 -or [int]$suite.skipped -ne 0) {
            throw "Per-test JUnit is not a zero-failure, zero-skip result: $($file.Name)."
        }
        $cases = @($suite.testcase)
        if ($cases.Count -ne 1) { throw "Per-test JUnit must contain one testcase: $($file.Name)." }
        $names.Add([string]$cases[0].name)
    }
    if ($names.Count -ne $expected.Count -or @($names | Select-Object -Unique).Count -ne $expected.Count) {
        throw 'Per-test JUnit selector names are not exactly 11 unique cases.'
    }
    $actual = @($names | Sort-Object) -join "`n"
    $wanted = @($expected | Sort-Object) -join "`n"
    if ($actual -ne $wanted) { throw 'Per-test JUnit selector names do not match the expected selector set.' }
    $guardPath = Join-Path $artifactRoot 'junit-no-skip-count-guard.xml'
    if (-not (Test-Path -LiteralPath $guardPath -PathType Leaf)) { throw 'JUnit no-skip/count guard is missing.' }
    [xml]$guard = Get-Content -LiteralPath $guardPath -Raw
    if (-not $guard.testsuite -or [int]$guard.testsuite.failures -ne 0 -or [int]$guard.testsuite.skipped -ne 0) {
        throw 'JUnit no-skip/count guard did not read back as zero failures and zero skips.'
    }
}

function Remove-OwnedPath([string] $Path, [string] $Label) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $deadline = (Get-Date).AddSeconds(20)
    $lastError = $null
    do {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            if (-not (Test-Path -LiteralPath $Path)) { return }
        } catch { $lastError = $_.Exception.Message }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    throw "$Label cleanup did not finish within 20 seconds: $lastError"
}

function Invoke-CargoJson([string] $Workspace, [string[]] $Arguments, [string] $LogName) {
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = 'cargo'
    $psi.WorkingDirectory = $Workspace
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    foreach ($argument in $Arguments) { $psi.ArgumentList.Add($argument) }
    $process = $null
    try {
        $process = [Diagnostics.Process]::new()
        $process.StartInfo = $psi
        if (-not $process.Start()) { throw "Could not start cargo for $LogName." }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(2700000)) {
            try { $process.Kill() } catch { }
            try { $process.WaitForExit(10000) } catch { }
            throw "Cargo timed out for $LogName."
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        Set-Content -LiteralPath (Join-Path $logs "$LogName.stdout") -Value $stdout -Encoding UTF8
        Set-Content -LiteralPath (Join-Path $logs "$LogName.stderr") -Value $stderr -Encoding UTF8
        $exitCode = $process.ExitCode
        if ($exitCode -ne 0) { throw "Cargo failed for $LogName with exit $exitCode." }
        return @($stdout -split "`r?`n" | Where-Object { $_.Trim() } | ForEach-Object {
            try { $_ | ConvertFrom-Json } catch { $null }
        } | Where-Object { $null -ne $_ })
    } finally {
        if ($process) {
            try {
                if (-not $process.HasExited) {
                    $process.Kill()
                    $process.WaitForExit(10000)
                }
            } catch { }
            try { $process.Dispose() } catch { }
        }
    }
}

function Verify-TestSource([string] $SourceRoot) {
    foreach ($test in $tests) {
        $source = Join-Path $SourceRoot ($test.Source -replace '/', '\')
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Expected test source is missing: $($test.Source)" }
        $lines = @(Get-Content -LiteralPath $source)
        $functionLine = $lines | Select-String -SimpleMatch ("fn " + $test.Function + '(') | Select-Object -First 1
        if (-not $functionLine) { throw "Expected test function is missing: $($test.Selector)" }
        $prior = ($lines[0..([Math]::Max(0, $functionLine.LineNumber - 2))] | Select-Object -Last 4) -join "`n"
        if ($prior -match '#\[ignore') { throw "Selected test is ignored in source: $($test.Selector)" }
    }
    $coreSource = Join-Path $SourceRoot 'libs/cua-driver/rust/crates/cua-driver-core/src/windows_launch_ownership_tests.rs'
    $coreText = Get-Content -LiteralPath $coreSource -Raw
    if ($coreText -notmatch 'GetProcessId' -or $coreText -notmatch 'GetProcessTimes') {
        throw 'The current HANDLE attestation test source must retain both GetProcessId and GetProcessTimes validation.'
    }
}

$pythonHelper = @'
from __future__ import annotations
import copy, hashlib, json, runpy, sys
from pathlib import Path

prepare_path, manifest_path, patch1_path, patch2_path, archive_path, source_destination = map(Path, sys.argv[1:])
module = runpy.run_path(str(prepare_path))
def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()
manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
driver = manifest.get('driver', {})
source = driver.get('source', {})
expected_source = '5272e492d61b96caf08e3bf434d91126c1f3dccc'
if driver.get('source_commit') != expected_source or driver.get('tag') != 'cua-driver-rs-v0.31.0':
    raise ValueError('unexpected pinned Cua Driver source identity')
if source.get('archive') != ('cua-' + expected_source + '.tar.gz') or source.get('root') != ('cua-' + expected_source):
    raise ValueError('unexpected pinned source archive name/root')
if source.get('url') != ('https://codeload.github.com/trycua/cua/tar.gz/' + expected_source):
    raise ValueError('unexpected pinned source archive URL')
if digest(patch1_path) != driver.get('patch', {}).get('sha256'):
    raise ValueError('unchanged 0001 patch hash does not match sources.json')
module['download'](source['url'], archive_path)
if digest(archive_path) != source.get('sha256'):
    raise ValueError('downloaded source archive hash does not match sources.json')
custom = copy.deepcopy(manifest)
custom['driver']['patch'] = copy.deepcopy(custom['driver']['patch'])
custom['driver']['patch']['file'] = 'patches/0002-windows-security-boundary.patch'
custom['driver']['patch']['sha256'] = digest(patch2_path)
source_root = module['extract_driver_source'](manifest, archive_path, source_destination)
module['apply_driver_patch'](manifest, source_root)
module['apply_driver_patch'](custom, source_root)
print(json.dumps({
    'source_archive_sha256': digest(archive_path),
    'patch_0001_sha256': digest(patch1_path),
    'patch_0002_sha256': digest(patch2_path),
    'source_root': str(source_root),
}, separators=(',', ':')))
'@

try {
    $provenance.avibe_head = (& git -C $repo rev-parse HEAD).Trim()
    $provenance.patch_0001.sha256 = Sha256 $patch1
    $provenance.patch_0002.sha256 = Sha256 $patch2
    $provenance | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $provenancePath -Encoding UTF8
    Set-Content -LiteralPath $helperPath -Value $pythonHelper -Encoding UTF8
    $archivePath = Join-Path $sourceWork 'cua-driver-source.tar.gz'
    $sourceDestination = Join-Path $sourceWork 'source'
    $prepOutput = & python $helperPath $prepare $manifestPath $patch1 $patch2 $archivePath $sourceDestination 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Pinned source preparation failed: $($prepOutput -join ' ')" }
    $prepRecord = ($prepOutput | Select-Object -Last 1) | ConvertFrom-Json
    $provenance.source_archive = 'cua-driver-source.tar.gz'
    $provenance.source_archive_sha256 = [string]$prepRecord.source_archive_sha256
    $provenance | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $provenancePath -Encoding UTF8
    Verify-TestSource ([string]$prepRecord.source_root)

    $workspace = Join-Path ([string]$prepRecord.source_root) 'libs\cua-driver\rust'
    $targetDir = Join-Path $sourceWork 'target'
    $env:CARGO_TARGET_DIR = $targetDir
    $artifacts = @{}
    foreach ($package in @('cua-driver-core', 'platform-windows')) {
        $rows = Invoke-CargoJson $workspace @('test', '--locked', '-p', $package, '--lib', '--no-run', '--message-format=json') ('build-' + ($package -replace '-', '_'))
        $targetName = $package -replace '-', '_'
        $artifactRows = @($rows | Where-Object { $_.reason -eq 'compiler-artifact' })
        $paths = @($artifactRows | ForEach-Object {
            $row = $_
            $hasTarget = $null -ne $row.PSObject.Properties['target']
            $hasProfile = $null -ne $row.PSObject.Properties['profile']
            $hasExecutable = $null -ne $row.PSObject.Properties['executable']
            if (-not ($hasTarget -and $hasProfile -and $hasExecutable)) { return }
            if ($row.target.name -eq $targetName -and
                $row.profile.test -eq $true -and
                $row.executable -and
                @($row.target.kind) -contains 'lib') {
                [string]$row.executable
            }
        } | Select-Object -Unique)
        if ($paths.Count -ne 1) { throw "Expected exactly one current $package test executable from cargo messages; found $($paths.Count)." }
        $artifacts[$package] = (Resolve-Path -LiteralPath $paths[0]).Path
    }

    $name = 'avibedrv' + [guid]::NewGuid().ToString('N').Substring(0, 10)
    $password = [Security.SecureString]::new()
    foreach ($character in 'aA1!'.ToCharArray()) { $password.AppendChar($character) }
    $alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%'
    for ($i = 0; $i -lt 40; $i++) { $password.AppendChar($alphabet[[Security.Cryptography.RandomNumberGenerator]::GetInt32($alphabet.Length)]) }
    $password.MakeReadOnly()
    $user = New-LocalUser -Name $name -Password $password -AccountExpires (Get-Date).AddHours(2) -Description 'Disposable Avibe Cua Driver security CI fixture'
    $usersGroup = Get-LocalGroup -SID 'S-1-5-32-545'
    if (-not (Get-LocalGroupMember -Group $usersGroup | Where-Object { $_.SID -eq $user.SID })) { Add-LocalGroupMember -Group $usersGroup -Member $user }
    $memberships = @(Get-LocalGroup | Where-Object { @((Get-LocalGroupMember -Group $_ | Where-Object { $_.SID -eq $user.SID })).Count -gt 0 })
    if ($memberships.Count -ne 1 -or $memberships[0].SID.Value -ne 'S-1-5-32-545') { throw 'Disposable account must belong to Users only.' }

    Set-TestAccess $fixtureRoot $user.SID 'ReadAndExecute'
    Set-TestAccess $bin $user.SID 'ReadAndExecute'
    Set-TestAccess $temp $user.SID 'Modify'
    foreach ($package in $artifacts.Keys) {
        $artifact = [string]$artifacts[$package]
        $destination = Join-Path $bin (($package -replace '-', '_') + '-tests.exe')
        Copy-Item -LiteralPath $artifact -Destination $destination
        foreach ($dll in @(Get-ChildItem -LiteralPath (Split-Path $artifact) -Filter '*.dll' -File)) { Copy-Item -LiteralPath $dll.FullName -Destination $bin -Force }
        $provenance.test_binaries += [ordered]@{ package = $package; path = (Split-Path $destination -Leaf); sha256 = Sha256 $destination }
    }
    $provenance | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $provenancePath -Encoding UTF8

    $environment = @{
        AVIBE_TEST_EXPECTED_SID = $user.SID.Value
        AVIBE_LITERAL_TEST_DIR = $temp
        AVIBE_LITERAL_SENTINEL = 'synthetic-not-a-credential'
        CUA_LITERAL_SENTINEL = 'synthetic-not-a-credential'
        AVAULT_LITERAL_SENTINEL = 'synthetic-not-a-credential'
        CI_LITERAL_SENTINEL = 'synthetic-not-a-credential'
        TEMP = $temp
        TMP = $temp
        SystemRoot = $env:SystemRoot
        SystemDrive = $env:SystemDrive
        PATH = $env:PATH
    }

    foreach ($test in $tests) {
        $started = Get-Date
        $process = $null
        $stdout = ''
        $stderr = ''
        $status = 'missing'
        $failureMessage = 'Test process was not started.'
        try {
            $binary = Join-Path $bin (($test.Package -replace '-', '_') + '-tests.exe')
            $psi = [Diagnostics.ProcessStartInfo]::new()
            $psi.FileName = $binary
            $psi.WorkingDirectory = $bin
            $psi.UseShellExecute = $false
            $psi.CreateNoWindow = $true
            $psi.LoadUserProfile = $true
            $psi.UserName = $name
            $psi.Domain = $env:COMPUTERNAME
            $psi.Password = $password
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
            $psi.Environment.Clear()
            foreach ($entry in $environment.GetEnumerator()) { $psi.Environment[$entry.Key] = [string]$entry.Value }
            foreach ($argument in @('--exact', $test.Selector, '--nocapture')) { $psi.ArgumentList.Add($argument) }
            $process = [Diagnostics.Process]::new()
            $process.StartInfo = $psi
            if (-not $process.Start()) { throw "Could not start $($test.Selector)." }
            $recordedProcesses.Add($process)
            $stdoutTask = $process.StandardOutput.ReadToEndAsync()
            $stderrTask = $process.StandardError.ReadToEndAsync()
            if (-not $process.WaitForExit(180000)) {
                $process.Kill()
                $process.WaitForExit(10000)
                $stdout = $stdoutTask.GetAwaiter().GetResult()
                $stderr = $stderrTask.GetAwaiter().GetResult()
                $status = 'timeout'
                $failureMessage = 'Test exceeded the 180 second per-test limit.'
            } else {
                $stdout = $stdoutTask.GetAwaiter().GetResult()
                $stderr = $stderrTask.GetAwaiter().GetResult()
                if ($process.ExitCode -eq 0 -and $stdout -match 'test result:\s+ok\.\s+1 passed;\s+0 failed;\s+0 ignored;') {
                    $status = 'passed'
                    $failureMessage = ''
                } else {
                    $status = 'failed'
                    $failureMessage = "Expected exit 0 and exact 1 passed; 0 failed; 0 ignored; summary (exit $($process.ExitCode))."
                }
            }
        } catch {
            $status = 'missing'
            $failureMessage = $_.Exception.Message
        }
        $seconds = ((Get-Date) - $started).TotalSeconds
        $stdoutPath = Join-Path $testLogs (($test.Selector -replace '[^A-Za-z0-9_.-]', '_') + '.out')
        $stderrPath = Join-Path $testLogs (($test.Selector -replace '[^A-Za-z0-9_.-]', '_') + '.err')
        Set-Content -LiteralPath $stdoutPath -Value $stdout -Encoding UTF8
        Set-Content -LiteralPath $stderrPath -Value $stderr -Encoding UTF8
        Write-Junit $test $status $seconds $stdout $stderr $failureMessage
        $results.Add([pscustomobject]@{ Selector = $test.Selector; Status = $status; Seconds = $seconds; Failure = $failureMessage })
        Write-Output "[$status] $($test.Selector)"
    }
} catch {
    $fatal = $_
    Set-Content -LiteralPath (Join-Path $logs 'fatal.txt') -Value $_.Exception.ToString() -Encoding UTF8
} finally {
    foreach ($process in @($recordedProcesses)) {
        try {
            if (-not $process.HasExited) {
                $process.Kill()
                if (-not $process.WaitForExit(10000)) { $cleanupFailures.Add("recorded test process $($process.Id) did not exit") }
            }
        } catch { $cleanupFailures.Add("test process cleanup: $($_.Exception.Message)") }
        try { $process.Dispose() } catch { $cleanupFailures.Add("test process dispose: $($_.Exception.Message)") }
    }
    if ($user) {
        try {
            $profile = $null
            $deadline = (Get-Date).AddSeconds(20)
            do {
                $profile = Get-CimInstance Win32_UserProfile -Filter "SID='$($user.SID.Value)'" -OperationTimeoutSec 10
                if (-not $profile -or -not $profile.Loaded) { break }
                Start-Sleep -Milliseconds 250
            } while ((Get-Date) -lt $deadline)
            if ($profile -and $profile.Loaded) { throw 'Disposable profile remained loaded.' }
            if ($profile) { $profile | Remove-CimInstance -OperationTimeoutSec 10 }
        } catch { $cleanupFailures.Add("profile cleanup: $($_.Exception.Message)") }
        try { Remove-LocalUser -SID $user.SID } catch { $cleanupFailures.Add("account cleanup: $($_.Exception.Message)") }
    }
    try { if ($password) { $password.Dispose() } } catch { $cleanupFailures.Add("SecureString cleanup: $($_.Exception.Message)") }
    foreach ($ownedPath in @(
        @{ Path = $sourceWork; Label = 'source-work' },
        @{ Path = $fixtureRoot; Label = 'fixture' },
        @{ Path = $helperPath; Label = 'Python helper' }
    )) {
        try { Remove-OwnedPath $ownedPath.Path $ownedPath.Label } catch { $cleanupFailures.Add("$($ownedPath.Label) cleanup: $($_.Exception.Message)") }
    }
    try { $provenance | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $provenancePath -Encoding UTF8 } catch { $cleanupFailures.Add("provenance write: $($_.Exception.Message)") }
}

foreach ($test in $tests) {
    $existing = @($results | Where-Object { $_.Selector -eq $test.Selector })
    if ($existing.Count -eq 0) {
        $message = if ($fatal) { 'Setup failed before this expected selector could execute.' } else { 'Expected selector produced no result record.' }
        Write-Junit $test 'missing' 0 '' '' $message
        $results.Add([pscustomobject]@{ Selector = $test.Selector; Status = 'missing'; Seconds = 0; Failure = $message })
    }
}
Write-GuardJunit
Verify-JunitEvidence
if ($cleanupFailures.Count) { throw ($cleanupFailures -join '; ') }
if ($fatal) { throw $fatal }
$failedResults = @($results | Where-Object { $_.Status -ne 'passed' })
if ($failedResults.Count -ne 0) { throw 'One or more expected Windows Cua Driver tests failed, skipped, timed out, or were missing.' }
Write-Output "Windows Cua Driver source security tests passed; evidence is under $artifactRoot."
