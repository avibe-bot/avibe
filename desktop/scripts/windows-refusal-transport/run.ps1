# Called only by the source-build branch of test-windows-driver-source.ps1.
# Reuses its account, ACLs, environment and final profile/file cleanup.
function Invoke-WindowsRefusalTransport {
    param(
        [string] $Repo, [string] $Binary, [string] $Bin, [string] $Temp,
        [string] $Logs, $User, [Security.SecureString] $Password,
        [hashtable] $Environment
    )
    if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or $env:RUNNER_OS -ne 'Windows') {
        throw 'Native refusal transport requires disposable hosted Windows.'
    }
    $evidence = Join-Path $Logs 'refusal-transport'
    $work = Join-Path $Temp 'refusal-transport'
    New-Item -ItemType Directory -Force -Path $evidence, $work | Out-Null
    $processes = [Collections.Generic.List[Diagnostics.Process]]::new()
    $cleanup = [Collections.Generic.List[string]]::new()
    $receipt = [ordered]@{
        schema_version = 1; verdict = 'FAIL'; locked_desktop = 'not exercised; hosted cannot prove it'
        clipboard_isolation = 'No owner desktop execution; hosted disposable account only'
        images = 'No screenshots requested or saved'
        account = 'Reused Users-only source-test fixture, not a filtered admin token'
        binary_sha256 = (Get-FileHash -LiteralPath $Binary -Algorithm SHA256).Hash.ToLowerInvariant()
        native_elevated_empty = $false; native_elevated_input_messages = $null
        native_control_text_verified = $false; native_control_input_messages = $null
    }
    $failure = $null
    $elevated = $control = $worker = $null
    $started = Get-Date
    try {
        $python = Join-Path $Repo '.venv\Scripts\python.exe'
        $node = (Get-Command node -ErrorAction Stop).Source
        if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
            throw 'Existing hosted runtime .venv is required; this harness never installs dependencies.'
        }
        Add-Type -Path (Join-Path $PSScriptRoot 'Native.cs')
        if (-not [RefusalNative]::IsElevated($PID)) { throw 'Hosted fixture parent elevation not proven.' }
        $csc = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
        $suffix = [guid]::NewGuid().ToString('N').Substring(0, 12)
        $elevatedExe = Join-Path $Bin ('avibe-elevated-' + $suffix + '.exe')
        $controlExe = Join-Path $Bin ('avibe-control-' + $suffix + '.exe')
        $compile = & $csc /nologo /target:winexe /reference:System.Windows.Forms.dll /reference:System.Drawing.dll "/out:$elevatedExe" (Join-Path $PSScriptRoot 'Editor.cs') 2>&1
        $compile | Set-Content -LiteralPath (Join-Path $evidence 'compile.log') -Encoding UTF8
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $elevatedExe)) { throw 'Native EDIT fixture compilation failed.' }
        Copy-Item -LiteralPath $elevatedExe -Destination $controlExe
        # Elevated receipt inherits bin's ordinary-user ReadAndExecute only.
        # The ordinary worker cannot overwrite the independent no-input proof.
        $elevatedInput = Join-Path $Bin 'elevated-input.json'
        $controlInput = Join-Path $work 'control-input.json'
        foreach ($target in @(
            @{ Path = $elevatedExe; Elevated = $true; Receipt = $elevatedInput },
            @{ Path = $controlExe; Elevated = $false; Receipt = $controlInput }
        )) {
            $psi = [Diagnostics.ProcessStartInfo]::new()
            $psi.FileName = $target.Path; $psi.WorkingDirectory = $Bin
            $psi.UseShellExecute = $false
            $psi.Environment.Clear()
            foreach ($entry in $Environment.GetEnumerator()) { $psi.Environment[$entry.Key] = [string]$entry.Value }
            foreach ($key in @('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA')) { $psi.Environment[$key] = $work }
            $psi.ArgumentList.Add($target.Receipt)
            # Same inherited-desktop provisioning as the exercised signed lane.
            # No desktop/window-station ACL repair: readiness must fail closed
            # if this hosted image cannot create the ordinary fixture window.
            if (-not $target.Elevated) {
                $psi.LoadUserProfile = $true; $psi.UserName = $User.Name
                $psi.Domain = $env:COMPUTERNAME; $psi.Password = $Password
            }
            $process = [Diagnostics.Process]::Start($psi)
            $processes.Add($process)
            $null = $process.SafeHandle # Retain the original object, not only a PID.
            $deadline = (Get-Date).AddSeconds(20)
            do {
                $process.Refresh()
                if ($process.HasExited) { throw "Fixture editor exited with code $($process.ExitCode) before its window was ready." }
                if ($process.MainWindowHandle -ne 0 -and (Test-Path -LiteralPath $target.Receipt)) { break }
                Start-Sleep -Milliseconds 100
            } while ((Get-Date) -lt $deadline)
            if ($process.MainWindowHandle -eq 0 -or $process.MainModule.FileName -ne $target.Path -or
                [RefusalNative]::IsElevated($process.Id) -ne $target.Elevated -or $process.SessionId -ne [Diagnostics.Process]::GetCurrentProcess().SessionId) {
                throw 'Fixture exact path, elevation, HWND or desktop session not proven.'
            }
            if ([RefusalNative]::ReadEditor($process.MainWindowHandle, $process.Id) -ne '') { throw 'Fixture editor was not initially empty.' }
            if ((Get-Content -LiteralPath $target.Receipt -Raw | ConvertFrom-Json).input_messages -ne 0) { throw 'Fixture received input before test.' }
            if ($target.Elevated) { $elevated = $process } else { $control = $process }
        }
        [RefusalNative]::GrantFixtureQuery($elevated.SafeHandle.DangerousGetHandle(), $User.SID.Value)
        $receipt.query_grant = 'Only target PROCESS_QUERY_LIMITED_INFORMATION and TOKEN_QUERY; parent/shared token refused'
        $controlText = 'ordinary-control-' + $suffix
        $inputPath = Join-Path $Bin 'refusal-transport-input.json'
        @{
            expected_sid = $User.SID.Value; binary = $Binary; binary_sha256 = $receipt.binary_sha256
            node = $node; control_text = $controlText
            elevated = @{ pid = $elevated.Id; hwnd = $elevated.MainWindowHandle.ToInt64(); name = [IO.Path]::GetFileNameWithoutExtension($elevatedExe) }
            control = @{ pid = $control.Id; hwnd = $control.MainWindowHandle.ToInt64(); name = [IO.Path]::GetFileNameWithoutExtension($controlExe) }
        } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $inputPath -Encoding UTF8
        $psi = [Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = $python; $psi.WorkingDirectory = $Repo
        $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true; $psi.LoadUserProfile = $true
        $psi.UserName = $User.Name; $psi.Domain = $env:COMPUTERNAME; $psi.Password = $Password
        $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
        $psi.Environment.Clear()
        foreach ($entry in $Environment.GetEnumerator()) { $psi.Environment[$entry.Key] = [string]$entry.Value }
        $psi.Environment['PYTHONIOENCODING'] = 'utf-8'; $psi.Environment['PYTHONUTF8'] = '1'
        foreach ($arg in @('-I', (Join-Path $PSScriptRoot 'worker.py'), '--repo', $Repo, '--input', $inputPath, '--work', $work)) { $psi.ArgumentList.Add($arg) }
        $worker = [Diagnostics.Process]::Start($psi)
        $processes.Add($worker)
        $null = $worker.SafeHandle
        $stdout = $worker.StandardOutput.ReadToEndAsync(); $stderr = $worker.StandardError.ReadToEndAsync()
        if (-not $worker.WaitForExit(240000)) { throw 'Native transport worker exceeded 240 seconds.' }
        Set-Content -LiteralPath (Join-Path $evidence 'worker.stdout') -Value $stdout.GetAwaiter().GetResult() -Encoding UTF8
        Set-Content -LiteralPath (Join-Path $evidence 'worker.stderr') -Value $stderr.GetAwaiter().GetResult() -Encoding UTF8
        $receipt.worker_exit = $worker.ExitCode
        if ($worker.ExitCode -ne 0) { throw "Native transport worker failed with exit $($worker.ExitCode)." }
        $transport = Get-Content -LiteralPath (Join-Path $work 'transport.json') -Raw | ConvertFrom-Json
        if ($transport.transport_assertions_passed -ne $true) { throw 'Native-to-SDK transport assertions did not pass.' }
        $deadline = (Get-Date).AddSeconds(5)
        do {
            if ($control.HasExited) { throw 'Ordinary editor exited before independent readback.' }
            $receipt.native_control_text_verified = [RefusalNative]::ReadEditor($control.MainWindowHandle, $control.Id) -ceq $controlText
            if ($receipt.native_control_text_verified) { break }
            Start-Sleep -Milliseconds 100
        } while ((Get-Date) -lt $deadline)
        $receipt.native_control_input_messages = (Get-Content -LiteralPath $controlInput -Raw | ConvertFrom-Json).input_messages
        if (-not $receipt.native_control_text_verified -or $receipt.native_control_input_messages -le 0) {
            throw 'SDK ordinary input did not independently read back in the native EDIT.'
        }
    } catch { $failure = $_ }
    finally {
        # Stop the request producer before independent final readback; a timed
        # out worker must not be able to mutate the target after we inspected it.
        try {
            if ($worker -and -not $worker.HasExited) {
                $worker.Kill($true)
                if (-not $worker.WaitForExit(10000)) { throw 'Owned worker did not exit.' }
            }
        } catch { $cleanup.Add($_.Exception.Message) }
        # Elevated text/input evidence is checked even when transport assertions
        # fail. Do not hide a mutation behind the expected refusal's assertion.
        try {
            if ($elevated) {
                if ($elevated.HasExited) { throw 'Elevated editor exited before independent readback.' }
                $receipt.native_elevated_empty = [RefusalNative]::ReadEditor($elevated.MainWindowHandle, $elevated.Id) -ceq ''
                $receipt.native_elevated_input_messages = (Get-Content -LiteralPath $elevatedInput -Raw | ConvertFrom-Json).input_messages
                if (-not $receipt.native_elevated_empty -or $receipt.native_elevated_input_messages -ne 0) { throw 'Elevated editor received input.' }
            } else { throw 'Elevated fixture was never established.' }
        } catch { $cleanup.Add($_.Exception.Message) }
        # Only retained processes created above, plus their owned child trees.
        for ($i = $processes.Count - 1; $i -ge 0; $i--) {
            $process = $processes[$i]
            try {
                if (-not $process.HasExited) { $process.Kill($true); if (-not $process.WaitForExit(10000)) { throw 'Owned process did not exit.' } }
            } catch { $cleanup.Add($_.Exception.Message) }
            finally { $process.Dispose() }
        }
        foreach ($name in @('transport.json', 'sdk-result.json', 'sdk.stdout', 'sdk.stderr', 'daemon.stdout', 'daemon.stderr')) {
            $path = Join-Path $work $name
            if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination $evidence -Force }
        }
        if ($failure) { $receipt.failure = $failure.Exception.ToString() }
        $receipt.cleanup_failures = @($cleanup)
        if (-not $failure -and $cleanup.Count -eq 0) { $receipt.verdict = 'PASS' }
        $receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $evidence 'verdict.json') -Encoding UTF8
        $xmlFailure = if ($receipt.verdict -eq 'PASS') { '' } else {
            '<failure message="native refusal transport failed">' + [Security.SecurityElement]::Escape(($receipt | ConvertTo-Json -Depth 8)) + '</failure>'
        }
        $seconds = ((Get-Date) - $started).TotalSeconds.ToString('F3', [Globalization.CultureInfo]::InvariantCulture)
        @"
<?xml version="1.0" encoding="utf-8"?>
<testsuite name="avibe.windows-native-refusal-transport" tests="1" failures="$(if ($receipt.verdict -eq 'PASS') { 0 } else { 1 })" skipped="0" time="$seconds">
  <testcase classname="native.transport" name="protected-window-daemon-core-sdk-and-positive-readback">$xmlFailure</testcase>
</testsuite>
"@ | Set-Content -LiteralPath (Join-Path $evidence 'junit.xml') -Encoding UTF8
    }
    if ($cleanup.Count) { throw ($cleanup -join '; ') }
    if ($failure) { throw $failure }
    return $receipt
}
