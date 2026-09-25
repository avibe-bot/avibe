# Native installer transaction. The signed app owns this script and request.
# Only the app installation directory is copied; user/Runtime data is outside it.
param([string]$Request)
$ErrorActionPreference = 'Stop'

function Invoke-AvibeReplacement($requestData) {
    $backup = Join-Path $requestData.staging 'previous'
    $ready = Join-Path $requestData.staging 'ready'
    $started = $false
    try {
        # Check installer identity before asking the running app to exit.
        $info = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($requestData.installer)
        if ($info.ProductName -ne 'Avibe') { throw 'Installer product mismatch' }
        if ($info.ProductVersion -ne $requestData.version) { throw 'Installer version mismatch' }
        # Per-user NSIS installs are writable. Refuse elevation or a different scope.
        $probe = Join-Path $requestData.directory ([Guid]::NewGuid().ToString() + '.probe')
        [IO.File]::WriteAllText($probe, '')
        Remove-Item -LiteralPath $probe
        Copy-Item -LiteralPath $requestData.directory -Destination $backup -Recurse
        [IO.File]::WriteAllText($ready, 'ready')
        Wait-Process -Id $requestData.pid -ErrorAction SilentlyContinue
        $started = $true
        # /D is the final NSIS argument, with its entire remainder as the path.
        # No /R: this helper alone decides which application to restart.
        $installerProcess = Start-Process -FilePath $requestData.installer -ArgumentList @('/S', '/UPDATE', "/D=$($requestData.directory)") -Wait -PassThru
        if ($installerProcess.ExitCode -ne 0) { throw 'Installer failed' }
        if (-not (Test-Path -LiteralPath $requestData.executable)) { throw 'Installed executable missing' }
        $installed = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($requestData.executable)
        if ($installed.ProductVersion -ne $requestData.version) { throw 'Installed version mismatch' }
        Start-Process -FilePath $requestData.executable
    } catch {
        $failure = $_
        if ($started) {
            try {
                # Keep the backup until a complete copy is back in place.
                [IO.Directory]::CreateDirectory($requestData.directory) | Out-Null
                Copy-Item -Path (Join-Path $backup '*') -Destination $requestData.directory -Recurse -Force
                Start-Process -FilePath $requestData.executable
            } catch {
                $_ | Out-String | Set-Content -LiteralPath (Join-Path $requestData.staging 'restore-error.txt')
            }
        }
        throw $failure
    }
}

# The transaction is dot-sourceable for hermetic native CI fixtures. The entry
# point, not the transaction, owns user dialogs and the temporary directory.
if ($MyInvocation.InvocationName -eq '.') { return }
$requestData = Get-Content -LiteralPath $Request -Raw -Encoding UTF8 | ConvertFrom-Json
try {
    Invoke-AvibeReplacement $requestData
    Remove-Item -LiteralPath $requestData.staging -Recurse -Force
} catch {
    $_ | Out-String | Set-Content -LiteralPath (Join-Path $requestData.staging 'error.txt')
    if (Test-Path -LiteralPath (Join-Path $requestData.staging 'ready')) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show($requestData.errorMessage, $requestData.errorTitle) | Out-Null
    }
    exit 1
}
