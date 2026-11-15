# Parent-only security probes on the disposable runner. No credentialed egress.
function Invoke-SecurityFixtures([hashtable] $Context) {
    Add-Type -Path (Join-Path $PSScriptRoot 'Native.cs')
    if (-not [AcceptanceNative]::IsElevated($PID)) { throw 'runner_elevation_not_proven' }
    $profile = Get-CimInstance Win32_UserProfile -Filter "SID='$($Context.User.SID.Value)'"
    if (-not $profile) { throw 'primary_profile_missing' }
    $installed = Join-Path $profile.LocalPath 'AppData\Local\Programs\AvibeAcceptance'
    $driver = Join-Path $installed 'computer-use\cua-driver.exe'
    if ((Get-FileHash -LiteralPath $driver -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Context.DriverHash) { throw 'security_probe_driver_hash_mismatch' }
    $stateRoot = Join-Path $profile.LocalPath 'AppData\Roaming\bot.avibe.desktop'
    $state = Get-Content -LiteralPath (Join-Path $stateRoot 'computer-use.json') -Raw | ConvertFrom-Json
    $pipePrefix = '\\.\pipe\'
    if (-not $state.socket_path.StartsWith($pipePrefix) -or $state.state -ne 'ready') { throw 'primary_driver_not_ready' }
    $pipeLeaf = $state.socket_path.Substring($pipePrefix.Length)
    $lockPath = Join-Path $stateRoot 'computer-use.lock'
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { throw 'primary_lock_missing' }

    $securityRoot = Join-Path $Context.Root 'security'
    New-Item -ItemType Directory -Path $securityRoot -Force | Out-Null
    $refusalPipe = 'avibe-elevated-refusal-' + [guid]::NewGuid().ToString('N')
    $pidFile = Join-Path $securityRoot 'elevated-driver.pid'
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $driver; $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    $psi.Environment.Clear()
    foreach ($key in @('SystemRoot', 'SystemDrive', 'COMPUTERNAME')) { $psi.Environment[$key] = [Environment]::GetEnvironmentVariable($key) }
    foreach ($key in @('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP', 'CUA_DRIVER_RS_HOME', 'CUA_DRIVER_TELEMETRY_HOME')) { $psi.Environment[$key] = $securityRoot }
    $psi.Environment['CUA_DRIVER_EMBEDDED'] = '1'
    $psi.Environment['CUA_DRIVER_RS_TELEMETRY_ENABLED'] = 'false'
    $psi.Environment['CUA_DRIVER_RS_UPDATE_CHECK'] = 'false'
    foreach ($arg in @('serve', '--socket', ($pipePrefix + $refusalPipe), '--pid-file', $pidFile, '--no-overlay')) { $psi.ArgumentList.Add($arg) }
    $process = [Diagnostics.Process]::Start($psi)
    $Context.Stage = 'elevated_driver_refused'
    $null = $process.SafeHandle
    try {
        $stdout = $process.StandardOutput.ReadToEndAsync(); $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(15000)) { throw 'elevated_driver_served_or_hung' }
        $errors = $stderr.GetAwaiter().GetResult(); $null = $stdout.GetAwaiter().GetResult()
        if ($process.ExitCode -eq 0 -or -not $errors.Contains('Computer Use does not run as administrator; start Avibe normally.') -or (Test-Path -LiteralPath $pidFile)) {
            throw 'elevated_driver_explicit_refusal_missing'
        }
    } finally { if (-not $process.HasExited) { $process.Kill(); $null = $process.WaitForExit(10000) }; $process.Dispose() }
    $client = [IO.Pipes.NamedPipeClientStream]::new('.', $refusalPipe, [IO.Pipes.PipeDirection]::InOut)
    try {
        $client.Connect(500)
        throw 'elevated_driver_bound_pipe'
    } catch [TimeoutException] { } finally { $client.Dispose() }

    $Context.Cases.elevated_driver_refused = @{ verdict = 'PASS'; reason = 'installed_driver_explicit_elevated_serve_refusal_and_no_residual_pipe' }
    $Context.Stage = 'security_fixture'
    $second = $null; $password = $null; $probe = $null
    try {
        $password = [Security.SecureString]::new()
        foreach ($c in 'aA1!'.ToCharArray()) { $password.AppendChar($c) }
        $alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%'
        for ($i=0; $i -lt 40; $i++) { $password.AppendChar($alphabet[[Security.Cryptography.RandomNumberGenerator]::GetInt32($alphabet.Length)]) }
        $password.MakeReadOnly()
        $second = New-LocalUser -Name ('avicub' + [guid]::NewGuid().ToString('N').Substring(0,10)) -Password $password -AccountExpires (Get-Date).AddHours(1)
        $Context.SecondUser = $second
        $group = Get-LocalGroup -SID 'S-1-5-32-545'
        if (-not (Get-LocalGroupMember -Group $group | Where-Object { $_.SID -eq $second.SID })) { Add-LocalGroupMember -Group $group -Member $second }
        $groups = @(Get-LocalGroup | Where-Object { @(Get-LocalGroupMember -Group $_ | Where-Object { $_.SID -eq $second.SID }).Count -gt 0 })
        if ($groups.Count -ne 1 -or $groups[0].SID.Value -ne 'S-1-5-32-545') { throw 'second_account_not_users_only' }
        $secondWork = Join-Path $Context.Root 'second-user'
        New-Item -ItemType Directory -Path $secondWork | Out-Null
        foreach ($entry in @(@($Context.Root, 'ReadAndExecute'), @($secondWork, 'Modify'))) {
            $acl = Get-Acl -LiteralPath $entry[0]
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($second.SID, $entry[1], 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
            Set-Acl -LiteralPath $entry[0] -AclObject $acl
        }
        $inputFile = Join-Path $Context.Root 'second-user-input.json'
        $outputFile = Join-Path $secondWork 'result.json'
        @{ expected_sid = $second.SID.Value; pipe_leaf = $pipeLeaf; lock_path = $lockPath } | ConvertTo-Json | Set-Content -LiteralPath $inputFile
        $psi = [Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = (Get-Command pwsh).Source; $psi.UseShellExecute = $false; $psi.LoadUserProfile = $true
        $psi.UserName = $second.Name; $psi.Domain = $env:COMPUTERNAME; $psi.Password = $password
        $psi.Environment.Clear()
        foreach ($key in @('SystemRoot', 'SystemDrive', 'COMPUTERNAME')) { $psi.Environment[$key] = [Environment]::GetEnvironmentVariable($key) }
        foreach ($arg in @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', (Join-Path $Context.Root 'second-user.ps1'), '-InputFile', $inputFile, '-OutputFile', $outputFile)) { $psi.ArgumentList.Add($arg) }
        $Context.Stage = 'second_user_pipe_lock'
        $probe = [Diagnostics.Process]::Start($psi)
        $null = $probe.SafeHandle
        if (-not $probe.WaitForExit(30000) -or $probe.ExitCode -ne 0) { throw 'second_user_boundary_probe_failed' }
        $denial = Get-Content -LiteralPath $outputFile -Raw | ConvertFrom-Json
        if (-not ($denial.standard_user -and $denial.pipe_denied -and $denial.lock_denied)) { throw 'second_user_boundary_not_denied' }
        $Context.Cases.second_user_pipe_lock = @{ verdict = 'PASS'; reason = 'owner_pipe_open_positive_control_second_users_only_account_denied_pipe_and_lock' }
        $Context.Stage = 'security_fixture'
    } finally {
        # Keep the identity in parent state until ALL cleanup succeeds. Each
        # action is attempted even if an earlier profile/process action fails.
        $cleanupFailed = $false
        try { if ($probe) { if (-not $probe.HasExited) { $probe.Kill(); $null = $probe.WaitForExit(10000) }; $probe.Dispose() } } catch { $cleanupFailed = $true }
        if ($second) {
            try {
                $deadline = (Get-Date).AddSeconds(20)
                do {
                    $secondProfile = Get-CimInstance Win32_UserProfile -Filter "SID='$($second.SID.Value)'"
                    if (-not $secondProfile -or -not $secondProfile.Loaded) { break }
                    Start-Sleep -Milliseconds 250
                } while ((Get-Date) -lt $deadline)
                if ($secondProfile -and $secondProfile.Loaded) { throw 'second_profile_still_loaded' }
                if ($secondProfile) { $secondProfile | Remove-CimInstance }
            } catch { $cleanupFailed = $true }
            try {
                Remove-LocalUser -SID $second.SID
                if ((Get-LocalUser -SID $second.SID -ErrorAction SilentlyContinue) -or (Get-CimInstance Win32_UserProfile -Filter "SID='$($second.SID.Value)'")) { throw 'second_fixture_identity_remains' }
            } catch { $cleanupFailed = $true }
        }
        if ($password) { $password.Dispose() }
        if ($cleanupFailed) { throw 'second_fixture_cleanup_failed' }
        $Context.SecondUser = $null
    }
    Initialize-ElevatedQueryTarget $Context
    $Context.Cases.security_fixture = @{ verdict = 'PASS'; reason = 'elevated_target_verified_and_secondary_fixture_removed' }
    return @{ ok = $true; cases = $Context.Cases; elevated_pid = $Context.ElevatedApp.Id; elevated_hwnd = $Context.ElevatedApp.MainWindowHandle.ToInt64() }
}

function Initialize-ElevatedQueryTarget([hashtable] $Context) {
    if (-not ('AcceptanceNative' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'Native.cs') }
    if (-not [AcceptanceNative]::IsElevated($PID)) { throw 'runner_elevation_not_proven' }
    if ($Context.ElevatedApp) { throw 'elevated_fixture_already_created' }
    $securityRoot = Join-Path $Context.Root 'security'
    New-Item -ItemType Directory -Path $securityRoot -Force | Out-Null
    # A real signed Win32 editor, no broker, model key, or inherited GH_TOKEN.
    if ((Get-FileHash -LiteralPath $Context.TargetApp -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Context.TargetHash) { throw 'target_fixture_executable_changed' }
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $Context.TargetApp; $psi.UseShellExecute = $false
    foreach ($arg in @('-multiInst', '-nosession', '-noPlugin', ('-settingsDir=' + $securityRoot))) { $psi.ArgumentList.Add($arg) }
    $psi.Environment.Clear()
    foreach ($key in @('SystemRoot', 'SystemDrive', 'COMPUTERNAME', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData')) { $psi.Environment[$key] = [Environment]::GetEnvironmentVariable($key) }
    foreach ($key in @('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP')) { $psi.Environment[$key] = $securityRoot }
    $Context.ElevatedApp = [Diagnostics.Process]::Start($psi)
    $null = $Context.ElevatedApp.SafeHandle
    $deadline = (Get-Date).AddSeconds(15)
    do {
        $Context.ElevatedApp.Refresh()
        if ($Context.ElevatedApp.HasExited) { throw 'real_editor_was_launcher_stub' }
        if ($Context.ElevatedApp.MainWindowHandle -ne 0) { break }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $deadline)
    if ($Context.ElevatedApp.MainWindowHandle -eq 0 -or $Context.ElevatedApp.MainModule.FileName -ne $Context.TargetApp -or -not [AcceptanceNative]::IsElevated($Context.ElevatedApp.Id)) { throw 'elevated_notepad_identity_unproven' }
    if (-not [AcceptanceNative]::UnsavedTextEquals($Context.ElevatedApp.MainWindowHandle, '')) { throw 'elevated_notepad_not_blank' }
    [AcceptanceNative]::GrantFixtureTargetQuery($Context.ElevatedApp.SafeHandle.DangerousGetHandle(), $Context.User.SID.Value)
    if ($Context.ElevatedApp.HasExited) { throw 'elevated_target_exited_during_query_setup' }
}
