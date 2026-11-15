# Test-owned observer. CIM supplies candidate names/parent links, never image identity.
# Keep unknown fields null and retain query-only process handles across lifecycle actions.
function Get-AcceptanceHelpers {
    param([string] $DriverPath, [string] $DriverHash, [string] $OwnerSid,
          [int] $AppPid, [int] $DesktopPid, [Collections.Generic.HashSet[int]] $Known,
          [System.Collections.IDictionary] $Evidence, [switch] $AllowEmpty)
    $handles = [Collections.Generic.List[object]]::new()
    $Evidence.candidates = @(); $Evidence.query_error = $null
    $Evidence.candidate_count = 0; $Evidence.enumeration_succeeded = $false; $Evidence.enumerated_process_count = $null
    try {
        $rows = @(Get-CimInstance Win32_Process -ErrorAction Stop)
        # Get-Process is the same-user discovery/image path. CIM is only used
        # for parent relations/descendants; its ExecutablePath is never a gate.
        $named = @(Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($DriverPath)) -ErrorAction SilentlyContinue)
        foreach ($candidate in $named) {
            if (-not @($rows | Where-Object { [int]$_.ProcessId -eq $candidate.Id }).Count) {
                $rows += [pscustomobject]@{ProcessId=$candidate.Id; Name=[IO.Path]::GetFileName($DriverPath); ParentProcessId=0}
            }
            $candidate.Dispose()
        }
        $Evidence.enumeration_succeeded = $true; $Evidence.enumerated_process_count = $rows.Count
        $visited = [Collections.Generic.HashSet[int]]::new()
        $driverName = [IO.Path]::GetFileName($DriverPath)
        do {
            $changed = $false
            foreach ($row in $rows) {
                $id = [int]$row.ProcessId
                $descendant = $Known.Contains([int]$row.ParentProcessId)
                if ($id -eq $AppPid -or $visited.Contains($id) -or
                    (-not $descendant -and $row.Name -ne $driverName -and -not $Known.Contains($id))) { continue }
                $null = $visited.Add($id)
                $Evidence.candidate_count++
                if ($Evidence.candidate_count -gt 64) { throw 'helper_candidate_limit_exceeded' }
                $item = [ordered]@{
                    process_name=[IO.Path]::GetFileName([string]$row.Name); pid=$id
                    path_basename=$null; path_match=$null; hash_match=$null; owner_match=$null
                    has_exited=$null; parent_pid=[int]$row.ParentProcessId
                    parent_relation=$(if ([int]$row.ParentProcessId -eq $DesktopPid) {'desktop'} elseif ($descendant) {'known_helper'} else {'other'})
                    query_errors=@()
                }
                $Evidence.candidates += $item
                $process = $null; $image = $null
                try {
                    $process = [AcceptanceNative]::ObserveProcess($id)
                    try {
                        $lookup = Get-Process -Id $id -ErrorAction Stop
                        try { $image = $lookup.Path } finally { $lookup.Dispose() }
                        if (-not $image) { throw 'same_user_process_path_unavailable' }
                        $item.path_basename = [IO.Path]::GetFileName($image)
                        $item.path_match = [string]::Equals([IO.Path]::GetFullPath($image), [IO.Path]::GetFullPath($DriverPath), [StringComparison]::OrdinalIgnoreCase)
                    } catch { $item.query_errors += @{call='image'; error_class=$_.Exception.GetBaseException().GetType().FullName} }
                    if ($image) {
                        try { $item.hash_match = (Get-FileHash -LiteralPath $image -Algorithm SHA256 -ErrorAction Stop).Hash -eq $DriverHash }
                        catch { $item.query_errors += @{call='image_hash'; error_class=$_.Exception.GetBaseException().GetType().FullName} }
                    }
                    try { $item.owner_match = $process.OwnerSid() -eq $OwnerSid }
                    catch { $item.query_errors += @{call='owner'; error_class=$_.Exception.GetBaseException().GetType().FullName} }
                    try { $item.has_exited = $process.HasExited }
                    catch { $item.query_errors += @{call='liveness'; error_class=$_.Exception.GetBaseException().GetType().FullName} }
                    if ($item.query_errors.Count) { continue }
                    if ($item.has_exited -or -not $item.owner_match) { continue }
                    # Roots must be the actual pinned installed image. Descendants
                    # belong to the retained helper lineage, never the target app.
                    if (($row.Name -eq $driverName -and $item.path_match -and $item.hash_match) -or
                        ($descendant -and $row.Name -ne $driverName)) {
                        if ($Known.Add($id)) { $changed = $true }
                        $handles.Add($process); $process = $null
                    } elseif ($row.Name -eq $driverName) {
                        $item.query_errors += @{call='installed_image'; error_class='PinnedImageMismatch'}
                    }
                } catch { $item.query_errors += @{call='open_process'; error_class=$_.Exception.GetBaseException().GetType().FullName} }
                finally { if ($null -ne $process) { $process.Dispose() } }
            }
        } while ($changed)
        if (@($Evidence.candidates | Where-Object { $_.query_errors.Count }).Count) { throw 'helper_identity_query_failed' }
        if (-not $AllowEmpty -and -not $handles.Count) { throw 'live_installed_driver_missing' }
        return ,$handles
    } catch {
        $Evidence.query_error = @{call='helper_inventory'; error_class=$_.Exception.GetBaseException().GetType().FullName}
        foreach ($process in $handles) { $process.Dispose() }
        throw
    }
}
