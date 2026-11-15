# Narrow observer helpers; never invoke the production updater transaction.
function Test-UpdateSupervisorParent($Row, [int] $OriginalPid) {
    $shell = $env:SystemRoot.TrimEnd('\') + '\System32\WindowsPowerShell\v1.0\powershell.exe'
    return [int]$Row.ParentProcessId -eq $OriginalPid -and $Row.ExecutablePath -eq $shell
}
function Get-UpdateSupervisorStage($Row, [int] $OriginalPid, [string] $Temp) {
    if (-not (Test-UpdateSupervisorParent $Row $OriginalPid) -or -not $Row.CommandLine) { return $null }
    $prefix = [regex]::Escape($Temp.TrimEnd('\') + '\')
    $path = $prefix + 'avibe-update-[^\\"\s]+'
    $command = ([string]$Row.CommandLine).Replace('\\?\', '')
    if ($command -notmatch ('(?i)(?:^|\s)-File\s+"?(?<script>' + $path + '\\install\.ps1)"?\s+-Request\s+"?(?<request>' + $path + '\\request\.json)"?\s*$')) { return $null }
    $directory = $Matches.script.Substring(0, $Matches.script.Length - '\install.ps1'.Length)
    if ($Matches.request -ne ($directory + '\request.json')) { return $null }
    return $directory
}
function Stop-UpdateHandle($Process) {
    if (-not $Process -or $Process.HasExited) { return }
    $Process.Kill()
    if (-not $Process.WaitForExit(10000)) { throw 'update_process_stop_unproven' }
}
function Stop-UpdateTransaction([hashtable] $Context, $Original, $Replacement, $Supervisor, $Installer, [datetime] $Since) {
    # Stop producers first so none can create a later transaction. Killing the
    # original may release Wait-Process, so then discover/stop its supervisor
    # BEFORE the installer. Only after both are gone may uninstall touch files.
    $failed = $false
    foreach ($producer in @($Original, $Replacement)) {
        try { Stop-UpdateHandle $producer } catch { $failed = $true }
    }
    try { Stop-UpdateHandle $Supervisor } catch { $failed = $true }
    $discovered = [Collections.Generic.List[object]]::new()
    try {
        foreach ($row in @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -OperationTimeoutSec 5)) {
            if (-not (Test-UpdateSupervisorParent $row $Original.Id)) { continue }
            $process = Open-UpdateProcess $row $Since
            if ($process) { $discovered.Add($process); Stop-UpdateHandle $process } else { $failed = $true }
        }
    } catch { $failed = $true }
    # Keep these attempts independent of supervisor errors. If any supervisor
    # could not be stopped, Quiesced stays false and worker uninstall is skipped.
    try { Stop-UpdateHandle $Installer } catch { $failed = $true }
    $pattern = '^' + [regex]::Escape($env:TEMP.TrimEnd('\') + '\') + 'avibe-update-[^\\]+\\update\.exe$'
    try {
        foreach ($row in @(Get-CimInstance Win32_Process -Filter "Name='update.exe'" -OperationTimeoutSec 5)) {
            if ($row.ExecutablePath -notmatch $pattern) { continue }
            $process = Open-UpdateProcess $row $Since
            if ($process) { $discovered.Add($process); Stop-UpdateHandle $process } else { $failed = $true }
        }
    } catch { $failed = $true }
    foreach ($process in $discovered) { $process.Dispose() }
    # A fresh inventory catches processes launched just as a supervisor stopped.
    try {
        foreach ($row in @(Get-CimInstance Win32_Process -OperationTimeoutSec 5)) {
            if (-not ((Test-UpdateSupervisorParent $row $Original.Id) -or $row.ExecutablePath -match $pattern)) { continue }
            $process = Open-UpdateProcess $row $Since
            if ($process) {
                try { if (-not $process.HasExited) { $failed = $true } } finally { $process.Dispose() }
            } else { $failed = $true }
        }
    } catch { $failed = $true }
    $Context.Quiesced = -not $failed
    if ($failed) { throw 'update_transaction_cleanup_unproven' }
}
function Pin-UpdateDebugger($Desktop, [int] $Port) {
    $handles = [Collections.Generic.List[object]]::new()
    try {
        if ($Desktop.HasExited) { throw 'update_replacement_generation_exited' }
        $rows = @{}; foreach ($row in @(Get-CimInstance Win32_Process -OperationTimeoutSec 5)) { $rows[[int]$row.ProcessId] = $row }
        $root = $rows[$Desktop.Id]
        $created = $Desktop.StartTime.ToUniversalTime(); $created = $created.AddTicks(-($created.Ticks % 10))
        if (-not $root -or $root.CreationDate.ToUniversalTime() -ne $created) { throw 'update_replacement_generation_changed' }
        $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
        if (-not $listeners.Count) { throw 'update_cdp_loopback_unproven' }
        $owners = [Collections.Generic.HashSet[int]]::new()
        foreach ($listener in $listeners) {
            if ($listener.LocalAddress -notin @('127.0.0.1', '::1')) { throw 'update_cdp_loopback_unproven' }
            $id = [int]$listener.OwningProcess
            $visited = [Collections.Generic.HashSet[int]]::new()
            while ($id -ne $Desktop.Id) {
                if (-not $visited.Add($id) -or -not $rows.ContainsKey($id)) { throw 'update_cdp_generation_unproven' }
                $child = $rows[$id]; $parent = $rows[[int]$child.ParentProcessId]
                if (-not $parent -or $child.CreationDate -lt $parent.CreationDate) { throw 'update_cdp_generation_unproven' }
                $id = [int]$parent.ProcessId
            }
            if ($owners.Add([int]$listener.OwningProcess)) {
                $process = Open-UpdateProcess $rows[[int]$listener.OwningProcess] $Desktop.StartTime.ToUniversalTime()
                if (-not $process) { throw 'update_cdp_generation_unproven' }
                $handles.Add($process)
            }
        }
        return ,$handles
    } catch { foreach ($handle in $handles) { $handle.Dispose() }; throw }
}
