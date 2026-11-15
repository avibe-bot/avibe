param([Parameter(Mandatory)][string] $Source, [Parameter(Mandatory)][string] $Work,
      [switch] $Hosted, [switch] $NativeWorker, [string] $ExpectedSid = '')
$ErrorActionPreference='Stop'; Set-StrictMode -Version Latest
New-Item -ItemType Directory -Force $Work | Out-Null
. $Source
if ($NativeWorker) {
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    if ($identity.User.Value -ne $ExpectedSid -or ([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'inventory_worker_not_ordinary' }
    $env:TEMP=$Work; $env:TMP=$Work
    Add-Type -Path (Join-Path (Split-Path $Source) 'Native.cs')
    $image=Join-Path $Work 'cua-driver.exe'
    Add-Type -TypeDefinition 'public class IdleFixture { public static void Main() { System.Threading.Thread.Sleep(45000); } }' -OutputAssembly $image -OutputType ConsoleApplication
    $child=$null; $handles=$null
    try {
        $psi=[Diagnostics.ProcessStartInfo]::new(); $psi.FileName=$image; $psi.UseShellExecute=$false; $psi.CreateNoWindow=$true
        $child=[Diagnostics.Process]::Start($psi); $null=$child.SafeHandle
        $hash=(Get-FileHash -LiteralPath $image).Hash
        $e=[ordered]@{}
        $handles=Get-AcceptanceHelpers -DriverPath $image -DriverHash $hash -OwnerSid $ExpectedSid -AppPid 0 -DesktopPid $PID -Known ([Collections.Generic.HashSet[int]]::new()) -Evidence $e
        if ($handles.Count -ne 1 -or $handles[0].Id -ne $child.Id -or $handles[0].HasExited) { throw 'ordinary_user_inventory_positive_failed' }
        $candidate=@($e.candidates | Where-Object { $_.pid -eq $child.Id })[0]
        if ($candidate.hash_match -ne $true -or $candidate.owner_match -ne $true -or $candidate.path_match -ne $true -or $candidate.parent_relation -ne 'desktop') { throw 'ordinary_user_identity_not_bound' }
        $child.Kill(); if (-not $handles[0].WaitForExit(10000)) { throw 'retained_exit_not_observed' }
        @{passed=$true; ordinary_user=$true; powershell_version=$PSVersionTable.PSVersion.ToString(); evidence=$e; retained_exit=$true} | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $Work 'native-inventory.json') -Encoding utf8
    } finally {
        if ($null -ne $handles) { foreach ($p in $handles) { $p.Dispose() } }
        if ($child) { if (-not $child.HasExited) { $child.Kill(); $null=$child.WaitForExit(10000) }; $child.Dispose() }
    }
    exit 0
}
if ($Hosted) {
    if ([Environment]::OSVersion.Platform -ne 'Win32NT' -or $env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted') { throw 'ordinary_inventory_requires_disposable_hosted_windows' }
    $root=Join-Path $Work 'ordinary-inventory'; New-Item -ItemType Directory -Force $root | Out-Null
    $user=$null; $worker=$null
    try {
        $password=[Security.SecureString]::new(); foreach ($c in ('aA1!'+[guid]::NewGuid().ToString('N')+[guid]::NewGuid().ToString('N')).ToCharArray()) { $password.AppendChar($c) }; $password.MakeReadOnly()
        $user=New-LocalUser -Name ('aviinv'+[guid]::NewGuid().ToString('N').Substring(0,10)) -Password $password -AccountExpires (Get-Date).AddHours(1)
        $group=Get-LocalGroup -SID 'S-1-5-32-545'
        if (-not (Get-LocalGroupMember -Group $group | Where-Object { $_.SID -eq $user.SID })) { Add-LocalGroupMember -Group $group -Member $user }
        $groups=@(Get-LocalGroup | Where-Object { @(Get-LocalGroupMember -Group $_ | Where-Object { $_.SID -eq $user.SID }).Count -gt 0 })
        if ($groups.Count -ne 1 -or $groups[0].SID.Value -ne 'S-1-5-32-545') { throw 'inventory_fixture_account_not_users_only' }
        $acl=Get-Acl -LiteralPath $root
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($user.SID,'Modify','ContainerInherit,ObjectInherit','None','Allow'))
        Set-Acl -LiteralPath $root -AclObject $acl
        Copy-Item -LiteralPath $PSCommandPath -Destination (Join-Path $root 'test.ps1')
        Copy-Item -LiteralPath $Source -Destination (Join-Path $root 'inventory.ps1')
        Copy-Item -LiteralPath (Join-Path (Split-Path $Source) 'Native.cs') -Destination $root
        $psi=[Diagnostics.ProcessStartInfo]::new()
        $psi.FileName=Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
        $psi.UseShellExecute=$false; $psi.LoadUserProfile=$true; $psi.CreateNoWindow=$true
        $psi.WorkingDirectory=$root; $psi.UserName=$user.Name; $psi.Domain=$env:COMPUTERNAME; $psi.Password=$password
        $psi.Environment.Clear()
        foreach ($key in @('SystemRoot','SystemDrive','COMPUTERNAME','ProgramFiles','ProgramFiles(x86)','ProgramData')) { $psi.Environment[$key]=[Environment]::GetEnvironmentVariable($key) }
        $psi.Environment['PATH']="$env:SystemRoot\System32;$env:SystemRoot"
        foreach ($arg in @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',(Join-Path $root 'test.ps1'),'-Source',(Join-Path $root 'inventory.ps1'),'-Work',$root,'-NativeWorker','-ExpectedSid',$user.SID.Value)) { $psi.ArgumentList.Add($arg) }
        $worker=[Diagnostics.Process]::Start($psi); $null=$worker.SafeHandle
        if (-not $worker.WaitForExit(60000)) { throw 'ordinary_inventory_worker_timeout' }
        if ($worker.ExitCode -ne 0) { throw 'ordinary_inventory_worker_failed' }
        $proof=Get-Content -LiteralPath (Join-Path $root 'native-inventory.json') -Raw | ConvertFrom-Json
        if (-not $proof.passed -or -not $proof.ordinary_user) { throw 'ordinary_inventory_proof_missing' }
        $proof | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $Work 'inventory-native-proof.json') -Encoding utf8
    } finally {
        if ($worker) { if (-not $worker.HasExited) { $worker.Kill($true); $null=$worker.WaitForExit(10000) }; $worker.Dispose() }
        if ($user) {
            Get-CimInstance Win32_UserProfile | Where-Object { $_.SID -eq $user.SID.Value } | Remove-CimInstance
            Remove-LocalUser -SID $user.SID
        }
        Remove-Item -LiteralPath $root -Recurse -Force
    }
    Write-Output 'inventory_native_ordinary_user_passed'
    exit 0
}
# Contract: inaccessible CIM path must not erase a real same-user pinned helper;
# unknown native query fields must remain unknown and block acceptance. The old
# exact-path CIM filter misses the first case; generated input-only tests never
# execute this inventory boundary. No OS process calls occur in this unit matrix.
$native=@'
using System;
public static class AcceptanceNative {
 public static string Fault;
 public static string Image;
 public sealed class Observed : IDisposable {
  public int Id; public bool Disposed;
  public string ImagePath() { if (Fault=="image") throw new InvalidOperationException(); return Image; }
  public string OwnerSid() { if (Fault=="owner") throw new InvalidOperationException(); return Fault=="wrong_owner" ? "other" : "owner"; }
  public bool HasExited { get { return Fault=="exited"; } }
  public void Dispose() { Disposed=true; }
 }
 public static Observed ObserveProcess(int id) { if (Fault=="open") throw new InvalidOperationException(); return new Observed {Id=id}; }
}
'@
Add-Type -TypeDefinition $native
$image=Join-Path $Work 'cua-driver.exe'; Set-Content -LiteralPath $image -Value 'test image'
[AcceptanceNative]::Image=$image
$hash=(Get-FileHash -LiteralPath $image).Hash
function Get-CimInstance { param($ClassName,$ErrorAction) if ([AcceptanceNative]::Fault -eq 'enumeration') { throw 'injected_enumeration_failure' }; if ([AcceptanceNative]::Fault -eq 'none_found') { return @() }; return @([pscustomobject]@{Name='cua-driver.exe'; ProcessId=201; ParentProcessId=100; ExecutablePath=$null}, [pscustomobject]@{Name='cua-driver.exe'; ProcessId=202; ParentProcessId=101; ExecutablePath=$null}, [pscustomobject]@{Name='helper.exe'; ProcessId=203; ParentProcessId=201; ExecutablePath=$null}, [pscustomobject]@{Name='app.exe'; ProcessId=204; ParentProcessId=201; ExecutablePath=$null}) }
foreach ($fault in @('none','open','image','owner','wrong_owner','exited','hash','replacement','enumeration','none_found')) {
    [AcceptanceNative]::Fault=$fault; $e=[ordered]@{}; $found=$null; $failed=$false
    try {
        $found=Get-AcceptanceHelpers -DriverPath $image -DriverHash $(if($fault -eq 'hash'){'bad'}else{$hash}) -OwnerSid 'owner' -AppPid 204 -DesktopPid 100 -Known ([Collections.Generic.HashSet[int]]::new()) -Evidence $e -AllowEmpty:($fault -eq 'replacement')
    } catch { $failed=$true }
    if ($fault -in @('none','replacement')) {
        if ($failed -or $found.Count -ne 3) { throw "inventory_positive_failed_$fault" }
        foreach ($p in $found) { $p.Dispose() }
    } elseif (-not $failed) { throw "inventory_false_pass_$fault" }
    if ($fault -eq 'open' -and ($null -ne $e.candidates[0].owner_match -or $null -ne $e.candidates[0].hash_match)) { throw 'inventory_unknown_became_false' }
    if ($fault -eq 'image' -and ($null -ne $e.candidates[0].path_match -or $null -ne $e.candidates[0].hash_match)) { throw 'inventory_image_unknown_became_false' }
    if ($fault -eq 'owner' -and $null -ne $e.candidates[0].owner_match) { throw 'inventory_owner_unknown_became_false' }
    if ($fault -eq 'enumeration' -and ($e.enumeration_succeeded -or $null -ne $e.enumerated_process_count)) { throw 'inventory_enumeration_unknown_became_absent' }
    if (($e | ConvertTo-Json -Depth 12).Contains($Work)) { throw 'inventory_full_path_leaked' }
}
Write-Output 'inventory_fixture_cases_passed'
