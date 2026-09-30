# Avibe Installation Script for Windows
# Usage: irm https://raw.githubusercontent.com/avibe-bot/avibe/master/install.ps1 | iex
# Uninstall, keeping your data:
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/avibe-bot/avibe/master/install.ps1))) -Uninstall
# Add -Purge to also delete your data. A session that cannot prompt also needs -Yes.
# The uninstaller never deletes through a link; anything behind a link is reported for you to remove.
#
# Prerequisites: None! uv will be installed automatically and manages Python for you.

param(
    # Remove Avibe itself and keep your data.
    [switch]$Uninstall,
    # With -Uninstall, also delete your Avibe data. This cannot be undone.
    [switch]$Purge,
    # Confirm -Purge without a prompt.
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

# Configuration
$REPO = "avibe-bot/avibe"
$PACKAGE_NAME = "avibe-os"
$TSINGHUA_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
$NODE_MINIMUM_REQUIREMENT = "20.19+ or 22.12+"
# uv 0.10.8 and later fetch managed Python from Astral's CDN and fall back to
# GitHub; earlier uv fetches it from GitHub only. Get-UvPythonInstallMirror
# gives earlier uv the CDN for the install step unless the user chose a source.
$ASTRAL_PYTHON_INSTALL_MIRROR = "https://releases.astral.sh/github/python-build-standalone/releases/download"
$PUBLIC_INSTALL_SCRIPT_URL = "https://raw.githubusercontent.com/$REPO/master/install.ps1"
# The fixed stable-launcher locations launcher discovery checks beside PATH and
# uv's configured tool bin. Keep in step with INSTALLER_LAUNCHER_DIRS in
# vibe/upgrade.py; entries without a drive do not apply on Windows.
$INSTALLER_LAUNCHER_DIRS = @("~/.local/bin", "~/bin", "/usr/local/bin", "/opt/homebrew/bin")

function Write-Banner {
    Write-Host @"
    ___          _ __
   /   | _   __ (_) /_  ___
  / /| || | / // / __ \/ _ \
 / ___ || |/ // / /_/ /  __/
/_/  |_||___//_/_.___/\___/
"@ -ForegroundColor Blue
    Write-Host "The local-first Agent OS for Web and chat" -ForegroundColor Green
    Write-Host ""
}

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] " -ForegroundColor Blue -NoNewline
    Write-Host $Message
}

function Write-Success {
    param([string]$Message)
    Write-Host "[OK] " -ForegroundColor Green -NoNewline
    Write-Host $Message
}

function Write-Warning {
    param([string]$Message)
    Write-Host "[WARN] " -ForegroundColor Yellow -NoNewline
    Write-Host $Message
}

function Write-Error {
    param([string]$Message)
    Write-Host "[ERROR] " -ForegroundColor Red -NoNewline
    Write-Host $Message
    exit 1
}

function Test-Command {
    param([string]$Command)
    $null = Get-Command $Command -ErrorAction SilentlyContinue
    return $?
}

function Resolve-InstallPath {
    param([string]$Path)

    $expanded = $Path
    if ($expanded -eq "~") {
        $expanded = $env:USERPROFILE
    } elseif ($expanded.StartsWith("~\") -or $expanded.StartsWith("~/")) {
        $expanded = Join-Path $env:USERPROFILE $expanded.Substring(2)
    }
    if (-not [System.IO.Path]::IsPathRooted($expanded)) {
        $expanded = Join-Path (Get-Location) $expanded
    }
    return [System.IO.Path]::GetFullPath($expanded)
}

function Get-StableBinDirectory {
    $configured = $env:UV_TOOL_BIN_DIR
    $directory = if ($configured) { $configured } else { Join-Path $env:USERPROFILE ".local\bin" }
    return Resolve-InstallPath $directory
}

function Get-LauncherState {
    param(
        [string]$Launcher,
        [string]$RuntimeHome
    )

    $state = @{
        Exists = Test-Path -LiteralPath $Launcher
        SourcePath = $null
        ActivationOwner = $null
    }
    if (-not $state.Exists) {
        return $state
    }

    $previousPythonPath = $env:PYTHONPATH
    $previousPythonHome = $env:PYTHONHOME
    $previousAvibeHome = $env:AVIBE_HOME
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    $env:AVIBE_HOME = $RuntimeHome
    Push-Location $RuntimeHome
    try {
        $protocol = Invoke-NativeCommand -FilePath $Launcher -Arguments @("__activate-install", "--protocol-version")
        if ((Get-InstallProtocolVersion $protocol) -ge 2) {
            $snapshot = Invoke-NativeCommand `
                -FilePath $Launcher `
                -Arguments @("__activate-install", "--snapshot", "--launcher", $Launcher)
            if ($snapshot.Success -and $snapshot.Stdout.Trim()) {
                $state.SourcePath = $snapshot.Stdout.Trim()
                $owner = Join-Path $state.SourcePath "bin\vibe.exe"
                if (Test-Path -LiteralPath $owner) {
                    $state.ActivationOwner = $owner
                }
                return $state
            }
        }
    } catch {
        # Released pre-protocol launchers fall through to legacy link discovery.
    } finally {
        Pop-Location
        if ($null -eq $previousPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue } else { $env:PYTHONPATH = $previousPythonPath }
        if ($null -eq $previousPythonHome) { Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue } else { $env:PYTHONHOME = $previousPythonHome }
        if ($null -eq $previousAvibeHome) { Remove-Item Env:AVIBE_HOME -ErrorAction SilentlyContinue } else { $env:AVIBE_HOME = $previousAvibeHome }
    }
    try {
        $item = Get-Item -LiteralPath $Launcher -ErrorAction Stop
        $target = @($item.Target)[0]
        if (-not $target) {
            return $state
        }
        if (-not [System.IO.Path]::IsPathRooted($target)) {
            $target = Join-Path $item.DirectoryName $target
        }
        $state.SourcePath = Resolve-InstallPath $target
        $state.ActivationOwner = $state.SourcePath
    } catch {
        return $state
    }
    return $state
}

function Invoke-WebScriptWithRetry {
    param([string]$Url)

    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            return Invoke-RestMethod -Uri $Url -TimeoutSec 30
        } catch {
            if ($attempt -eq 3) {
                throw
            }
            $delay = [Math]::Pow(2, $attempt - 1)
            Write-Warning "Dependency request failed (attempt $attempt/3); retrying in $delay second(s)."
            Start-Sleep -Seconds $delay
        }
    }
}

function Test-Node {
    if (-not (Test-Command "node")) {
        return $false
    }
    try {
        $version = (& node --version).Trim().TrimStart("v")
        $parts = $version.Split(".")
        $major = [int]$parts[0]
        $minor = [int]$parts[1]
        if ($major -eq 20) {
            return $minor -ge 19
        }
        if ($major -gt 22) {
            return $true
        }
        if ($major -eq 22) {
            return $minor -ge 12
        }
        return $false
    } catch {
        return $false
    }
}

function Install-Node {
    if ($env:VIBE_INSTALL_SKIP_NODE -eq "1") {
        Write-Warning "Skipping Node.js installation because VIBE_INSTALL_SKIP_NODE=1"
        return
    }

    if (Test-Node) {
        Write-Success "Node.js is already installed"
        return
    }

    Write-Info "Installing Node.js $NODE_MINIMUM_REQUIREMENT for Show Pages runtime..."
    if (Test-Command "winget") {
        $result = Invoke-NativeCommand -FilePath "winget" -Arguments @(
            "install",
            "OpenJS.NodeJS.LTS",
            "--accept-source-agreements",
            "--accept-package-agreements",
            "--silent"
        )
        if (-not $result.Success) {
            $message = "Failed to install Node.js with winget"
            if ($result.Output) {
                $message += ":`n$($result.Output)"
            }
            throw $message
        }

        $persistedPath = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        $env:Path = $env:Path + ";" + $persistedPath
        if (Test-Node) {
            Write-Success "Node.js installed successfully"
            return
        }
    }

    throw "Node.js $NODE_MINIMUM_REQUIREMENT is required for Show Pages runtime. Please install Node.js LTS from https://nodejs.org/ if needed."
}

function Install-NodeOptional {
    try {
        Install-Node
    } catch {
        $message = ($_ | Out-String).Trim()
        if ($message) {
            Write-Warning $message
        }
        Write-Warning "Node.js $NODE_MINIMUM_REQUIREMENT is not available, so managed Show Pages may install/start later when first used."
        Write-Warning "Continuing with Avibe installation; install Node.js manually if Show Pages runtime reports it missing."
    }
}

function Install-Uv {
    if (Test-Command "uv") {
        Write-Success "uv is already installed"
        return
    }
    
    Write-Info "Installing uv (will also manage Python automatically)..."
    
    try {
        Invoke-WebScriptWithRetry "https://astral.sh/uv/install.ps1" | iex
        
        # Refresh PATH
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
        
        if (Test-Command "uv") {
            Write-Success "uv installed successfully"
        } else {
            # Check common locations
            $uvPath = "$env:USERPROFILE\.local\bin\uv.exe"
            if (Test-Path $uvPath) {
                $env:Path += ";$env:USERPROFILE\.local\bin"
                Write-Success "uv installed successfully"
            } else {
                throw "uv not found after installation"
            }
        }
    } catch {
        Write-Error "Failed to install uv. Please install it manually: https://docs.astral.sh/uv/"
    }
}

# The Python download mirror the install step should give uv, if any. Newer uv
# is left alone: an explicit mirror turns its GitHub fallback off.
function Get-UvPythonInstallMirror {
    if ((Test-Path Env:UV_PYTHON_INSTALL_MIRROR) -or (Test-Path Env:UV_PYTHON_DOWNLOADS_JSON_URL)) {
        return $null
    }
    $version = try { (& uv --version 2>$null) -join " " } catch { "" }
    if (-not ($version -match '^uv (\d+)\.(\d+)\.(\d+)') -or
        [version]"$($Matches[1]).$($Matches[2]).$($Matches[3])" -ge [version]"0.10.8") {
        return $null
    }

    # uv resolves its own config files, so a Python source the user chose there
    # is kept. A config uv cannot load keeps uv's default too.
    $settings = try { (& uv tool install --show-settings $PACKAGE_NAME 2>$null) -join "`n" } catch { "" }
    if (-not $settings -or $LASTEXITCODE -ne 0 -or
        $settings -match 'python_(install_mirror|downloads_json_url): Some\(') {
        return $null
    }
    return $ASTRAL_PYTHON_INSTALL_MIRROR
}

function Invoke-NativeCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()

    try {
        # PowerShell's call operator preserves the argument vector. In
        # contrast, Start-Process joins ArgumentList with spaces and loses the
        # boundaries around paths containing whitespace.
        & $FilePath @Arguments 1> $stdoutPath 2> $stderrPath
        $exitCode = $LASTEXITCODE

        $stdout = if (Test-Path $stdoutPath) { [System.IO.File]::ReadAllText($stdoutPath) } else { "" }
        $stderr = if (Test-Path $stderrPath) { [System.IO.File]::ReadAllText($stderrPath) } else { "" }
        $capturedOutput = @()

        foreach ($streamOutput in @($stdout, $stderr)) {
            $trimmedOutput = $streamOutput.Trim()
            if ($trimmedOutput) {
                $capturedOutput += $trimmedOutput
            }
        }

        return @{
            Success = ($exitCode -eq 0)
            ExitCode = $exitCode
            Stdout = $stdout.Trim()
            Stderr = $stderr.Trim()
            Output = ($capturedOutput -join [System.Environment]::NewLine).Trim()
        }
    } catch {
        $capturedOutput = @()
        $stdout = ""
        $stderr = ""

        if (Test-Path $stdoutPath) {
            $stdout = [System.IO.File]::ReadAllText($stdoutPath).Trim()
            if ($stdout) { $capturedOutput += $stdout }
        }
        if (Test-Path $stderrPath) {
            $stderr = [System.IO.File]::ReadAllText($stderrPath).Trim()
            if ($stderr) { $capturedOutput += $stderr }
        }

        $errorText = ($_ | Out-String).Trim()
        if ($errorText) {
            $capturedOutput += $errorText
        }
        $capturedStderr = (($stderr, $errorText) | Where-Object { $_ }) -join [System.Environment]::NewLine

        return @{
            Success = $false
            ExitCode = 1
            Stdout = $stdout
            Stderr = $capturedStderr
            Output = ($capturedOutput -join [System.Environment]::NewLine).Trim()
        }
    } finally {
        foreach ($path in @($stdoutPath, $stderrPath)) {
            if (Test-Path $path) {
                Remove-Item $path -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

function Get-InstallProtocolVersion {
    param([object]$Result)

    if (-not $Result.Success) {
        return 0
    }
    [int]$version = 0
    if (-not [int]::TryParse($Result.Stdout.Trim(), [ref]$version)) {
        return 0
    }
    return $version
}

function Activate-LegacyInstallCandidate {
    param(
        [string]$Candidate,
        [string]$StableLauncher,
        [string]$StableBin,
        [string]$GenerationRoot
    )

    $probe = Invoke-NativeCommand -FilePath $Candidate -Arguments @("--help")
    if (-not $probe.Success) {
        return @{
            Success = $false
            ExitCode = $probe.ExitCode
            Output = if ($probe.Output) { $probe.Output } else { "candidate vibe launcher failed its startup probe" }
        }
    }

    $replacement = Join-Path $StableBin ("vibe.exe.avibe-" + [Guid]::NewGuid().ToString("N") + ".new")
    try {
        try {
            New-Item -ItemType SymbolicLink -Path $replacement -Target $Candidate -ErrorAction Stop | Out-Null
        } catch {
            try {
                New-Item -ItemType HardLink -Path $replacement -Target $Candidate -ErrorAction Stop | Out-Null
            } catch {
                Copy-Item -LiteralPath $Candidate -Destination $replacement -Force -ErrorAction Stop
            }
        }
        # This fallback is fresh-install only. File.Move atomically refuses to
        # overwrite a launcher that appeared while the candidate was staging.
        [System.IO.File]::Move($replacement, $StableLauncher)
    } catch {
        Remove-Item -LiteralPath $replacement -Force -ErrorAction SilentlyContinue
        return @{ Success = $false; ExitCode = 1; Output = (($_ | Out-String).Trim()) }
    }

    $markerReplacement = $null
    try {
        $marker = Join-Path $StableBin ".vibe.exe.avibe-generation"
        $markerReplacement = Join-Path $StableBin (".vibe.exe.avibe-generation-" + [Guid]::NewGuid().ToString("N") + ".new")
        Set-Content -LiteralPath $markerReplacement -Value $GenerationRoot -Encoding UTF8
        Move-Item -Force -Path $markerReplacement -Destination $marker
    } catch {
        if ($markerReplacement) {
            Remove-Item -LiteralPath $markerReplacement -Force -ErrorAction SilentlyContinue
        }
        Write-Warning "Could not record the legacy install generation; retained all installed generations."
    }
    return @{ Success = $true; ExitCode = 0; Output = "" }
}

function Get-RuntimeHome {
    $defaultHome = Join-Path $env:USERPROFILE ".avibe"
    $legacyHome = Join-Path $env:USERPROFILE ".vibe_remote"
    $runtimeHome = if ($env:AVIBE_HOME) {
        $env:AVIBE_HOME
    } elseif (Test-Path $defaultHome) {
        $defaultHome
    } elseif (Test-Path $legacyHome) {
        $legacyHome
    } else {
        $defaultHome
    }
    return Resolve-InstallPath $runtimeHome
}

function Invoke-UvToolInstallAttempt {
    param([string[]]$Arguments, [string]$PythonInstallMirror)

    $runtimeHome = Get-RuntimeHome
    $generationRoot = Join-Path (Join-Path $runtimeHome "runtime\install-generations") ([Guid]::NewGuid().ToString("N"))
    # 3.0.13 identifies uv-managed installs from the executable's /uv/tools/
    # path. Keep that released contract in every generation so the old binary
    # can hand the first upgrade to uv instead of falling through to pip.
    $generationTools = Join-Path $generationRoot "uv\tools"
    $generationBin = Join-Path $generationRoot "bin"
    $stableBin = Get-StableBinDirectory
    $stableLauncher = Join-Path $stableBin "vibe.exe"
    New-Item -ItemType Directory -Force -Path $generationTools, $generationBin, $stableBin | Out-Null
    # Protect the source snapshot throughout staging outside the Python lock.
    # The collector ignores this marker only for its own candidate.
    $installerMarker = Join-Path $generationRoot ".avibe-installing"
    $installerMarkerTemporary = "$installerMarker.new"
    $previousToolDir = $env:UV_TOOL_DIR
    $previousToolBinDir = $env:UV_TOOL_BIN_DIR
    try {
        try {
            Set-Content -LiteralPath $installerMarkerTemporary -Value $PID -Encoding UTF8
            [System.IO.File]::Move($installerMarkerTemporary, $installerMarker)
        } catch {
            Remove-Item -LiteralPath $generationRoot -Recurse -Force -ErrorAction SilentlyContinue
            throw
        }
        # The candidate's shared Python activation owner resolves this snapshot to
        # a generation. PowerShell must not duplicate junction/symlink identity.
        $launcherState = Get-LauncherState -Launcher $stableLauncher -RuntimeHome $runtimeHome
        $previousSourcePath = $launcherState.SourcePath
        $env:UV_TOOL_DIR = $generationTools
        $env:UV_TOOL_BIN_DIR = $generationBin
        if ($PythonInstallMirror) {
            $env:UV_PYTHON_INSTALL_MIRROR = $PythonInstallMirror
        }
        try {
            $result = Invoke-NativeCommand -FilePath "uv" -Arguments (@("tool", "install") + $Arguments)
        } finally {
            # Only an unset variable is ever given the mirror, so unset restores it.
            if ($PythonInstallMirror) { Remove-Item Env:UV_PYTHON_INSTALL_MIRROR -ErrorAction SilentlyContinue }
        }
        if (-not $result.Success) {
            Remove-Item -LiteralPath $generationRoot -Recurse -Force -ErrorAction SilentlyContinue
            return $result
        }

        $candidate = Join-Path $generationBin "vibe.exe"
        if (-not (Test-Path $candidate)) {
            Remove-Item -LiteralPath $generationRoot -Recurse -Force -ErrorAction SilentlyContinue
            return @{
                Success = $false
                ExitCode = 1
                Output = "uv completed but the candidate vibe launcher was not created"
            }
        }

        $previousPythonPath = $env:PYTHONPATH
        $previousPythonHome = $env:PYTHONHOME
        $previousAvibeHome = $env:AVIBE_HOME
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
        $env:AVIBE_HOME = $runtimeHome
        Push-Location $runtimeHome
        try {
            $protocol = Invoke-NativeCommand -FilePath $candidate -Arguments @("__activate-install", "--protocol-version")
            $activationOwner = if ((Get-InstallProtocolVersion $protocol) -ge 1) {
                $candidate
            } elseif ($launcherState.ActivationOwner) {
                $ownerProtocol = Invoke-NativeCommand `
                    -FilePath $launcherState.ActivationOwner `
                    -Arguments @("__activate-install", "--protocol-version")
                if ((Get-InstallProtocolVersion $ownerProtocol) -ge 1) {
                    $launcherState.ActivationOwner
                } else {
                    $null
                }
            } else {
                $null
            }
            if ($activationOwner) {
                $activationArguments = @(
                    "__activate-install",
                    "--launcher", $stableLauncher,
                    "--candidate", $candidate
                )
                if ($previousSourcePath) {
                    $activationArguments += @("--source-generation", $previousSourcePath)
                }
                $activation = Invoke-NativeCommand -FilePath $activationOwner -Arguments $activationArguments
            } elseif ($launcherState.Exists) {
                $activation = @{
                    Success = $false
                    ExitCode = 1
                    Output = "legacy candidate cannot safely replace an existing Avibe installation"
                }
            } else {
                # A legacy wheel may bootstrap a fresh machine. Existing
                # installs must route through a protocol-aware current owner.
                $activation = Activate-LegacyInstallCandidate `
                    -Candidate $candidate `
                    -StableLauncher $stableLauncher `
                    -StableBin $stableBin `
                    -GenerationRoot $generationRoot
            }
        } finally {
            Pop-Location
            if ($null -eq $previousPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue } else { $env:PYTHONPATH = $previousPythonPath }
            if ($null -eq $previousPythonHome) { Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue } else { $env:PYTHONHOME = $previousPythonHome }
            if ($null -eq $previousAvibeHome) { Remove-Item Env:AVIBE_HOME -ErrorAction SilentlyContinue } else { $env:AVIBE_HOME = $previousAvibeHome }
        }
        if (-not $activation.Success) {
            Remove-Item -LiteralPath $generationRoot -Recurse -Force -ErrorAction SilentlyContinue
            return @{
                Success = $false
                ExitCode = $activation.ExitCode
                Output = if ($activation.Output) { $activation.Output } else { "candidate Avibe environment could not be activated" }
            }
        }
        if ($activation.Output) {
            # Retention may defer safely while activation succeeds. Do not hide
            # its ownership/visibility diagnostics in captured native output.
            Write-Host $activation.Output
        }
        return $result
    } finally {
        Remove-Item -LiteralPath $installerMarker -Force -ErrorAction SilentlyContinue
        if ($null -eq $previousToolDir) { Remove-Item Env:UV_TOOL_DIR -ErrorAction SilentlyContinue } else { $env:UV_TOOL_DIR = $previousToolDir }
        if ($null -eq $previousToolBinDir) { Remove-Item Env:UV_TOOL_BIN_DIR -ErrorAction SilentlyContinue } else { $env:UV_TOOL_BIN_DIR = $previousToolBinDir }
    }
}

function Install-Vibe {
    Write-Info "Installing avibe-os (Python will be downloaded automatically if needed)..."

    $pythonInstallMirror = Get-UvPythonInstallMirror
    if ($pythonInstallMirror) {
        Write-Info "This uv downloads Python from GitHub; using Astral's CDN for any Python download instead"
    }

    $failure = Install-VibeFromSources -PythonInstallMirror $pythonInstallMirror
    if (-not $failure) {
        return
    }
    # The mirror replaces uv's GitHub source instead of adding one, so when no
    # package source worked through it, try them all once more without it.
    if ($pythonInstallMirror) {
        Write-Info "Retrying without Astral's CDN for the Python download..."
        $failure = Install-VibeFromSources
        if (-not $failure) {
            return
        }
    }

    Write-Error $failure
}

# Try each package source in order. Returns $null once one worked, otherwise the
# failure message to report.
function Install-VibeFromSources {
    param([string]$PythonInstallMirror)

    $customPackageSpec = $env:AVIBE_INSTALL_PACKAGE_SPEC
    if (-not $customPackageSpec) {
        $customPackageSpec = $env:VIBE_INSTALL_PACKAGE_SPEC
    }

    if ($customPackageSpec) {
        Write-Info "Trying custom package spec..."
        $result = Invoke-UvToolInstallAttempt -Arguments @($customPackageSpec, "--force") -PythonInstallMirror $PythonInstallMirror
        if ($result.Success) {
            Write-Success "avibe-os installed successfully (from custom package spec)"
            return $null
        }

        $failureMessage = "Failed to install avibe-os from custom package spec"
        if ($result.ExitCode -ne $null) {
            $failureMessage += " (exit code $($result.ExitCode))"
        }
        if ($result.Output) {
            $failureMessage += ":`n$($result.Output)"
        }

        return $failureMessage
    }

    $attempts = @(
        @{
            Name = "PyPI"
            Arguments = @($PACKAGE_NAME, "--force", "--refresh")
        },
        @{
            Name = "Tsinghua mirror"
            Arguments = @($PACKAGE_NAME, "--force", "--refresh", "--index-url", $TSINGHUA_INDEX_URL)
        },
        @{
            Name = "GitHub"
            Arguments = @("git+https://github.com/$REPO.git", "--force")
        }
    )
    $failures = @()

    foreach ($attempt in $attempts) {
        Write-Info "Trying $($attempt.Name)..."
        $result = Invoke-UvToolInstallAttempt -Arguments $attempt.Arguments -PythonInstallMirror $PythonInstallMirror
        if ($result.Success) {
            Write-Success "avibe-os installed successfully (from $($attempt.Name))"
            return $null
        }

        $failureMessage = "- $($attempt.Name) failed"
        if ($result.ExitCode -ne $null) {
            $failureMessage += " (exit code $($result.ExitCode))"
        }

        if ($result.Output) {
            $failureMessage += ":`n$($result.Output)"
        }

        $failures += $failureMessage
    }

    return "Failed to install avibe-os from all sources.`n$($failures -join "`n`n")"
}

function Test-Installation {
    Write-Info "Verifying installation..."

    $stableBin = Get-StableBinDirectory
    $stableLauncher = Join-Path $stableBin "vibe.exe"
    $persistedPath = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$stableBin;$persistedPath"

    if (Test-Path -LiteralPath $stableLauncher) {
        Write-Success "vibe command is available"
        Write-Host ""
        & $stableLauncher --help
        return $true
    }

    Write-Error "Installation verification failed. vibe command not found."
}

function Prepare-ShowRuntime {
    if ($env:VIBE_INSTALL_SKIP_SHOW_RUNTIME -eq "1") {
        Write-Warning "Skipping Show Runtime preparation because VIBE_INSTALL_SKIP_SHOW_RUNTIME=1"
        return
    }

    $stableLauncher = Join-Path (Get-StableBinDirectory) "vibe.exe"
    if (-not (Test-Path -LiteralPath $stableLauncher)) {
        Write-Warning "Show Runtime was not prepared because the vibe command is not available yet"
        return
    }

    Write-Info "Preparing Show Runtime for this platform..."
    $result = Invoke-NativeCommand -FilePath $stableLauncher -Arguments @("runtime", "prepare", "--strict")
    if ($result.Success) {
        Write-Success "Show Runtime is ready"
        return
    }

    Write-Warning "Show Runtime preparation failed; Avibe installation is still complete"
    if ($result.Output) {
        Write-Warning $result.Output
    }
    Write-Warning "Run 'vibe runtime prepare' after fixing Node.js or network access"
}

function Write-NextSteps {
    Write-Host ""
    Write-Host "Installation complete!" -ForegroundColor Green
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor Blue
    Write-Host "  1. Run 'vibe' to start the setup wizard"
    Write-Host "  2. Configure your Slack app tokens in the web UI"
    Write-Host "  3. Enable channels and start chatting with AI agents"
    Write-Host ""
    Write-Host "Quick commands:" -ForegroundColor Blue
    Write-Host "  vibe          - Start Avibe (service + web UI)"
    Write-Host "  vibe status   - Check service status"
    Write-Host "  vibe stop     - Stop all services"
    Write-Host "  vibe doctor   - Run diagnostics"
    Write-Host ""
    Write-Host "Uninstall:" -ForegroundColor Blue
    Write-Host "  $(Get-UninstallCommand)"
    Write-Host "  Add -Purge to also delete your data in $(Get-RuntimeHome). This cannot be undone."
    Write-Host ""
    Write-Host "Documentation:" -ForegroundColor Blue
    Write-Host "  https://github.com/$REPO#readme"
    Write-Host ""
}

# The one-line uninstall command for this runtime home, with any options
# appended. AVIBE_HOME is repeated when it chose the home.
function Get-UninstallCommand {
    param([string[]]$Options)

    $command = "& ([scriptblock]::Create((irm $PUBLIC_INSTALL_SCRIPT_URL))) -Uninstall"
    if ($Options) {
        $command += " " + ($Options -join " ")
    }
    if ($env:AVIBE_HOME) {
        $command = "`$env:AVIBE_HOME = '$((Get-RuntimeHome).Replace("'", "''"))'; $command"
    }
    return $command
}

# The installer owns uninstall. It runs outside Avibe, so nothing it deletes is
# in use by itself, and it cannot rely on the installed Python, which may come
# from any earlier release or be broken. It therefore carries its own copy of
# the launcher rule in vibe.upgrade.managed_stable_launchers; tests pin the
# copies to the same cases.

function Test-FullyQualifiedPath {
    param([string]$Path)
    return $Path -match '^[A-Za-z]:[\\/]' -or $Path -match '^[\\/]{2}[^\\/]'
}

# The directory entry itself, even for a link whose target is gone.
function Get-DirectoryEntry {
    param([string]$Path)

    $parent = Split-Path -Parent $Path
    $leaf = Split-Path -Leaf $Path
    if (-not $parent -or -not (Test-Path -LiteralPath $parent -PathType Container)) {
        return $null
    }
    return Get-ChildItem -LiteralPath $parent -Force -Filter $leaf -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq $leaf } |
        Select-Object -First 1
}

# Follow a path's chain of symbolic links and junctions at the leaf, as far as
# it goes, to the entry it finally names.
function Resolve-LinkChain {
    param([string]$Path)

    for ($depth = 0; $depth -lt 20; $depth++) {
        $entry = Get-DirectoryEntry $Path
        if (-not $entry -or $entry.LinkType -notin @("SymbolicLink", "Junction")) {
            break
        }
        $target = @($entry.Target)[0]
        if (-not $target) {
            break
        }
        $target = $target -replace '^\\(\\\?|\?\?)\\', ''
        if (-not [System.IO.Path]::IsPathRooted($target)) {
            $target = Join-Path (Split-Path -Parent $Path) $target
        }
        $Path = [System.IO.Path]::GetFullPath($target)
    }
    return $Path
}

function Test-PathInGenerationRoot {
    param([string]$Path, [string]$Root)

    $prefix = [System.IO.Path]::GetFullPath($Root).TrimEnd("\", "/") + "\"
    return [System.IO.Path]::GetFullPath($Path).StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

# A file's SHA-256, or $null when it cannot be read. .NET computes it, so no
# module has to load in a session whose module path came from another shell.
function Get-ContentHash {
    param([string]$Path)

    try {
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            return [System.BitConverter]::ToString($sha256.ComputeHash([System.IO.File]::ReadAllBytes($Path)))
        } finally {
            $sha256.Dispose()
        }
    } catch {
        return $null
    }
}

# Whether deleting install-generations would break this launcher: it resolves
# into the root, or it is a hard link or copy of a generation's launcher.
# vibe.upgrade._launcher_generation recognizes the same shapes, but it moves a
# launcher only into a recognized installation, and a copy only with its
# marker. A launcher missing either would still break, so it goes too.
function Test-LauncherUsesInstallGenerations {
    param([string]$Launcher, [string]$Root)

    if (Test-PathInGenerationRoot (Resolve-LinkChain $Launcher) $Root) {
        return $true
    }
    if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
        return $false
    }
    $hash = Get-ContentHash $Launcher
    if (-not $hash) {
        return $false
    }
    foreach ($generation in @(Get-ChildItem -LiteralPath $Root -Directory -Force -ErrorAction SilentlyContinue)) {
        $exported = Join-Path $generation.FullName "bin\vibe.exe"
        if ((Test-Path -LiteralPath $exported -PathType Leaf) -and (Get-ContentHash $exported) -eq $hash) {
            return $true
        }
    }
    return $false
}

# Each distinct directory launcher discovery searches: PATH, uv's configured
# tool bin, then the installer's fixed locations.
function Get-LauncherSearchDirectories {
    $seen = @{}
    foreach ($directory in @($env:Path -split ";") + @($env:UV_TOOL_BIN_DIR) + $INSTALLER_LAUNCHER_DIRS) {
        if (-not $directory) {
            continue
        }
        if ($directory -eq "~" -or $directory.StartsWith("~\") -or $directory.StartsWith("~/")) {
            $directory = Resolve-InstallPath $directory
        }
        if (-not (Test-FullyQualifiedPath $directory)) {
            continue
        }
        $directory = [System.IO.Path]::GetFullPath($directory).TrimEnd("\", "/")
        $key = $directory.ToLowerInvariant()
        if ($seen.ContainsKey($key)) {
            continue
        }
        $seen[$key] = $true
        $directory
    }
}

# Each launcher this home's uninstall removes. Like the Python owner, it never
# counts one inside a uv tool environment or the generation root.
function Get-ManagedLaunchers {
    param([string]$Root)

    foreach ($directory in Get-LauncherSearchDirectories) {
        $launcher = Join-Path $directory "vibe.exe"
        if ($launcher.Replace("\", "/").ToLowerInvariant().Contains("/uv/tools/") -or
            (Test-PathInGenerationRoot $launcher $Root) -or -not (Get-DirectoryEntry $launcher)) {
            continue
        }
        if (Test-LauncherUsesInstallGenerations $launcher $Root) {
            $launcher
        }
    }
}

# Each launcher marker the uninstall leaves without a purpose: one beside a
# launcher it removes, or one naming a generation in the root it deletes.
function Get-StaleLauncherMarkers {
    param([string]$Root, [string[]]$Launchers)

    foreach ($directory in Get-LauncherSearchDirectories) {
        $marker = Join-Path $directory ".vibe.exe.avibe-generation"
        if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
            continue
        }
        $marked = "$(Get-Content -LiteralPath $marker -Raw -ErrorAction SilentlyContinue)".Trim([char]0xFEFF, " ", "`r", "`n", "`t")
        if (($Launchers -contains (Join-Path $directory "vibe.exe")) -or
            ((Test-FullyQualifiedPath $marked) -and (Test-PathInGenerationRoot $marked $Root))) {
            $marker
        }
    }
}

function Get-UvCommand {
    $command = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) {
        return $command.Source
    }
    $fallback = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path -LiteralPath $fallback -PathType Leaf) {
        return $fallback
    }
    return $null
}

function Get-UvToolDirectory {
    param([string]$Uv)

    if ($Uv) {
        $result = Invoke-NativeCommand -FilePath $Uv -Arguments @("tool", "dir")
        if ($result.Success -and $result.Stdout) {
            return $result.Stdout
        }
    }
    if ($env:UV_TOOL_DIR) {
        return $env:UV_TOOL_DIR
    }
    return Join-Path $env:APPDATA "uv\tools"
}

# Whether uv's own uninstall of a tool removes only that tool's launchers. uv
# deletes every launcher path its receipt records, even one that another tool,
# such as a different vibe, has replaced since.
function Test-UvToolOwnsItsLaunchers {
    param([string]$Environment)

    $receipt = Join-Path $Environment "uv-receipt.toml"
    if (-not (Test-Path -LiteralPath $receipt -PathType Leaf)) {
        return $true
    }
    $pattern = 'install-path\s*=\s*(?:"((?:[^"\\]|\\.)*)"|''([^'']*)'')'
    foreach ($match in [regex]::Matches((Get-Content -LiteralPath $receipt -Raw), $pattern)) {
        $path = if ($match.Groups[1].Success) { [regex]::Unescape($match.Groups[1].Value) } else { $match.Groups[2].Value }
        if (-not (Get-DirectoryEntry $path)) {
            continue
        }
        if (Test-PathInGenerationRoot (Resolve-LinkChain $path) $Environment) {
            continue
        }
        $own = Join-Path $Environment ("Scripts\" + (Split-Path -Leaf $path))
        $hash = Get-ContentHash $path
        if ($hash -and (Test-Path -LiteralPath $own -PathType Leaf) -and $hash -eq (Get-ContentHash $own)) {
            continue
        }
        return $false
    }
    return $true
}

# Each live process that removal must wait for, as "pid command": one that runs
# from, or names on its command line, a path about to be deleted. The
# uninstaller's own process tree never counts.
function Get-BlockingProcesses {
    param([string[]]$Paths)

    $needles = New-Object System.Collections.Generic.List[string]
    foreach ($path in $Paths) {
        if (-not $path) {
            continue
        }
        foreach ($form in @([System.IO.Path]::GetFullPath($path).TrimEnd("\", "/"), (Resolve-LinkChain $path).TrimEnd("\", "/"))) {
            if (-not $needles.Contains($form)) {
                $needles.Add($form)
            }
        }
    }
    # CIM is the one source of command lines; without it nothing is removed.
    try {
        $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop | ForEach-Object {
            [pscustomobject]@{ Id = [int]$_.ProcessId; Parent = [int]$_.ParentProcessId; Text = "$($_.ExecutablePath) $($_.CommandLine)" }
        })
    } catch {
        $processes = @()
    }
    if ($processes.Count -eq 0) {
        throw "no process list is available"
    }
    $parents = @{}
    foreach ($process in $processes) {
        $parents[$process.Id] = $process.Parent
    }
    $own = @{}
    for ($id = $PID; $id -and -not $own.ContainsKey($id); $id = $parents[$id]) {
        $own[$id] = $true
    }
    $descendants = @{ $PID = $true }
    do {
        $grew = $false
        foreach ($process in $processes) {
            if (-not $descendants.ContainsKey($process.Id) -and $descendants.ContainsKey($process.Parent)) {
                $descendants[$process.Id] = $true
                $grew = $true
            }
        }
    } while ($grew)
    foreach ($process in $processes) {
        if ($own.ContainsKey($process.Id) -or $descendants.ContainsKey($process.Id)) {
            continue
        }
        $text = $process.Text + " "
        foreach ($needle in $needles) {
            $at = $text.IndexOf($needle, [System.StringComparison]::OrdinalIgnoreCase)
            $found = $false
            while ($at -ge 0 -and -not $found) {
                $found = $text[$at + $needle.Length] -in @([char]'\', [char]'/', [char]'"', [char]' ')
                $at = $text.IndexOf($needle, $at + 1, [System.StringComparison]::OrdinalIgnoreCase)
            }
            if ($found) {
                "$($process.Id) $($process.Text.Trim())"
                break
            }
        }
    }
}

# Ask an installed Avibe to stop its service; the first that succeeds is enough.
function Stop-AvibeService {
    param([string[]]$Launchers, [string]$RuntimeHome)

    foreach ($launcher in $Launchers) {
        if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
            continue
        }
        $previousPythonPath = $env:PYTHONPATH
        $previousPythonHome = $env:PYTHONHOME
        $previousAvibeHome = $env:AVIBE_HOME
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
        $env:AVIBE_HOME = $RuntimeHome
        Push-Location $(if (Test-Path -LiteralPath $RuntimeHome -PathType Container) { $RuntimeHome } else { [System.IO.Path]::GetTempPath() })
        try {
            $result = Invoke-NativeCommand -FilePath $launcher -Arguments @("stop")
        } finally {
            Pop-Location
            if ($null -eq $previousPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue } else { $env:PYTHONPATH = $previousPythonPath }
            if ($null -eq $previousPythonHome) { Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue } else { $env:PYTHONHOME = $previousPythonHome }
            if ($null -eq $previousAvibeHome) { Remove-Item Env:AVIBE_HOME -ErrorAction SilentlyContinue } else { $env:AVIBE_HOME = $previousAvibeHome }
        }
        if ($result.Success) {
            return $true
        }
        Write-Warning "'$launcher stop' failed: $($result.Output)"
    }
    return $false
}

# Whether this uninstall is for the default home. Global uv tool installs and
# a legacy ~\.vibe_remote directory belong to no chosen AVIBE_HOME, so only the
# default home's uninstall removes them.
function Test-UninstallingDefaultHome {
    param([string]$RuntimeHome)

    return -not $env:AVIBE_HOME -or $RuntimeHome.TrimEnd("\", "/") -eq (Join-Path $env:USERPROFILE ".avibe")
}

# This home's data paths: for the default home both default names, and for an
# explicit AVIBE_HOME that home alone.
function Get-AvibeDataDirectories {
    param([string]$RuntimeHome)

    $candidates = if (Test-UninstallingDefaultHome $RuntimeHome) {
        @((Join-Path $env:USERPROFILE ".avibe"), (Join-Path $env:USERPROFILE ".vibe_remote"))
    } else {
        @($RuntimeHome.TrimEnd("\", "/"))
    }
    # Only a directory or a link is a home; anything else under those names is not Avibe's.
    foreach ($candidate in $candidates) {
        $entry = Get-DirectoryEntry $candidate
        if ($entry -and ($entry.PSIsContainer -or ($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint))) {
            $candidate
        }
    }
}

function Test-IsLink {
    param([string]$Path)

    $entry = Get-DirectoryEntry $Path
    return [bool]($entry -and ($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint))
}

# A data path for the user, naming what a link points to.
function Get-DataPathDescription {
    param([string]$Path)

    $entry = Get-DirectoryEntry $Path
    if ($entry -and ($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        return "$Path -> $(@($entry.Target)[0])"
    }
    return $Path
}

# Whether an explicit AVIBE_HOME shows it is an Avibe home. The installer and
# Avibe both create its runtime directory, and uninstall keeps it until a purge.
function Test-LooksLikeAvibeHome {
    param([string]$Path)

    $entry = Get-DirectoryEntry (Join-Path $Path "runtime")
    return $entry -and $entry.PSIsContainer -and -not ($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
}

# Whether deleting a directory would delete the user's profile or the root of
# a drive or share.
function Test-PathHoldsHome {
    param([string]$Path)

    $full = [System.IO.Path]::GetFullPath($Path).TrimEnd("\", "/")
    $root = "$([System.IO.Path]::GetPathRoot($full))".TrimEnd("\", "/")
    if (-not $root -or $root.Equals($full, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    foreach ($profile in @($env:USERPROFILE, (Resolve-LinkChain $env:USERPROFILE))) {
        $profile = [System.IO.Path]::GetFullPath($profile).TrimEnd("\", "/")
        if ($profile.Equals($full, [System.StringComparison]::OrdinalIgnoreCase) -or
            $profile.StartsWith($full + "\", [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

# Delete a file or directory tree. A link is removed itself and never followed:
# Windows PowerShell's Remove-Item -Recurse deletes what a directory link names.
function Remove-InstalledPath {
    param([string]$Path)

    $entry = Get-DirectoryEntry $Path
    if (-not $entry) {
        return
    }
    if ($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        if ($entry.PSIsContainer) { [System.IO.Directory]::Delete($entry.FullName, $false) } else { [System.IO.File]::Delete($entry.FullName) }
        return
    }
    if ($entry.PSIsContainer) {
        try {
            # This recursion deletes a link it meets and does not follow it.
            [System.IO.Directory]::Delete($entry.FullName, $true)
        } catch {
            # A read-only entry stops it; clear each attribute on the way down.
            foreach ($child in @(Get-ChildItem -LiteralPath $entry.FullName -Force)) {
                Remove-InstalledPath $child.FullName
            }
            [System.IO.Directory]::Delete($entry.FullName, $false)
        }
    } else {
        $entry.Attributes = [System.IO.FileAttributes]::Normal
        [System.IO.File]::Delete($entry.FullName)
    }
}

function Remove-ReportedPath {
    param([string]$Path, [string]$Verb = "Removed")

    try {
        Remove-InstalledPath $Path
    } catch {
        # Reported below from what is left on disk.
    }
    if (Get-DirectoryEntry $Path) {
        Write-Warning "Could not remove $Path"
        return $false
    }
    Write-Success "$Verb $Path"
    return $true
}

function Confirm-Purge {
    if ($Yes) {
        return $true
    }
    $answer = $null
    if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
        try {
            $answer = Read-Host "Delete all of this permanently? [y/N]"
        } catch {
            # A -NonInteractive session refuses to prompt.
        }
    }
    if ($null -eq $answer) {
        Write-Warning "A purge needs confirmation, and this session cannot prompt. Nothing was removed."
        Write-Host "  To confirm without a prompt, run:"
        Write-Host "  $(Get-UninstallCommand -Options @('-Purge', '-Yes'))"
        return $false
    }
    if ($answer -match '^(y|yes)$') {
        return $true
    }
    Write-Info "Purge cancelled. Nothing was removed."
    return $false
}

# Name a vibe left on PATH, other than a managed launcher that could not be
# removed, which is reported as such.
function Write-RemainingVibe {
    param([string[]]$Launchers)

    $remaining = Get-Command vibe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($remaining -and $Launchers -notcontains $remaining.Source) {
        Write-Info "Another vibe command remains at $($remaining.Source). This installer did not install it, so it was left in place."
        Write-Host "  If it is a pip install of Avibe, remove it with that pip: pip uninstall avibe-os vibe-remote"
    }
}

function Write-KeptData {
    param([string[]]$Data)

    if (-not $Data) {
        return
    }
    Write-Host ""
    Write-Host "Your data was kept in:"
    foreach ($item in $Data) { Write-Host "  $(Get-DataPathDescription $item)" }
    Write-Host "To delete it too (this cannot be undone), run:"
    Write-Host "  $(Get-UninstallCommand -Options @('-Purge'))"
}

function Uninstall-Avibe {
    # Every step reports its own failure; one failure must not hide the rest.
    $ErrorActionPreference = "Continue"
    $runtimeHome = Get-RuntimeHome
    $root = Join-Path $runtimeHome "runtime\install-generations"
    $launchers = @(Get-ManagedLaunchers $root)
    $markers = @(Get-StaleLauncherMarkers -Root $root -Launchers $launchers)
    $uv = Get-UvCommand
    $toolDirectory = Get-UvToolDirectory $uv
    $presentUvTools = @($PACKAGE_NAME, "vibe-remote" | Where-Object { Test-Path -LiteralPath (Join-Path $toolDirectory $_) -PathType Container })
    $uvTools = @(if (Test-UninstallingDefaultHome $runtimeHome) { $presentUvTools })
    $data = @(Get-AvibeDataDirectories $runtimeHome)
    $doomedData = @()
    $unlinkedData = @()
    $failed = $false

    Write-Info "Uninstalling Avibe for $runtimeHome"
    if ($uvTools.Count -eq 0) {
        foreach ($package in $presentUvTools) {
            Write-Info "Leaving the uv tool install $package in place: it belongs to no AVIBE_HOME, so only an uninstall of the default home removes it"
        }
    }
    if ($launchers.Count -eq 0 -and $markers.Count -eq 0 -and $uvTools.Count -eq 0 -and -not (Get-DirectoryEntry $root) -and
        (-not $Purge -or $data.Count -eq 0)) {
        Write-Info "No Avibe installation was found, so nothing was removed."
        Write-RemainingVibe
        Write-KeptData $data
        return 0
    }
    if ($Purge) {
        # A purge never deletes through a link. It deletes real directories
        # and removes a link itself, keeping whatever the link points to.
        foreach ($item in $data) {
            if (Test-IsLink $item) {
                $unlinkedData += $item
            } elseif (-not (Test-UninstallingDefaultHome $runtimeHome) -and -not (Test-LooksLikeAvibeHome $item)) {
                Write-Warning "Refusing to purge ${item}: it has no runtime directory, so it does not look like an Avibe home. Nothing was removed."
                return 1
            } elseif (Test-PathHoldsHome $item) {
                Write-Warning "Refusing to purge ${item}: it holds your user profile. Nothing was removed."
                return 1
            } else {
                $doomedData += $item
            }
        }
        $listing = @($launchers) + @($markers)
        if (Get-DirectoryEntry $root) { $listing += $root }
        $listing += @($uvTools | ForEach-Object { "$(Join-Path $toolDirectory $_) (uv tool $_)" })
        $listing += @($doomedData | ForEach-Object { "$_    (your Avibe data)" })
        Write-Host ""
        if ($listing.Count -gt 0) {
            Write-Host "This permanently deletes:" -ForegroundColor Yellow
            foreach ($item in $listing) { Write-Host "  $item" }
        }
        if ($unlinkedData.Count -gt 0) {
            Write-Host "It removes these links, not what they point to:" -ForegroundColor Yellow
            foreach ($item in $unlinkedData) { Write-Host "  $(Get-DataPathDescription $item)" }
        }
        Write-Host ""
        if (-not (Confirm-Purge)) {
            return 1
        }
    }

    # Nothing is removed while Avibe could still be using it: first ask it to
    # stop, then check that no process runs from or names what goes next.
    $stoppers = @($launchers) + @($uvTools | ForEach-Object { Join-Path $toolDirectory "$_\Scripts\vibe.exe" })
    $doomed = @($launchers) + @($root) + @($uvTools | ForEach-Object { Join-Path $toolDirectory $_ }) + @($doomedData)
    $stopped = Stop-AvibeService -Launchers $stoppers -RuntimeHome $runtimeHome
    if ($stopped) {
        Write-Success "Stopped the Avibe service"
    } else {
        # Without a confirmed stop, anything still using the home counts too.
        $doomed += $runtimeHome
    }
    # Children of a stopped service can take a moment to exit.
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        try {
            $running = @(Get-BlockingProcesses -Paths $doomed)
        } catch {
            Write-Warning "No process list is available here, so the uninstall cannot confirm that nothing still uses what it deletes. Nothing was removed."
            return 1
        }
        if ($running.Count -eq 0 -or $attempt -eq 6) {
            break
        }
        Start-Sleep -Seconds 1
    }
    if ($running.Count -gt 0) {
        Write-Warning "These processes still use what the uninstall would delete, so nothing was removed:"
        foreach ($item in $running) {
            Write-Host "  pid $($item.Substring(0, [Math]::Min(200, $item.Length)))"
        }
        Write-Host "  Stop them, then run the uninstall again."
        return 1
    }
    if (-not $stopped) {
        if ($stoppers.Count -gt 0) {
            Write-Warning "The installed vibe could not stop the service. No process runs from or names what the uninstall deletes or this home."
        } else {
            Write-Info "No installed vibe could be asked to stop the service. No process runs from or names what the uninstall deletes or this home."
        }
        Write-Host "  Processes outside those paths, such as a managed OpenCode server, could not be confirmed stopped."
        Write-Host "  To check, run: Get-Process opencode, cloudflared -ErrorAction SilentlyContinue"
    }

    foreach ($item in $launchers + $markers) {
        if (-not (Remove-ReportedPath $item)) { $failed = $true }
    }
    if (Get-DirectoryEntry $root) {
        # The root goes only when the home and every step below it are real
        # directories: never through a link, never any other kind of entry.
        # Nor while a managed launcher it serves is still there to dangle.
        $left = $null
        $behindLink = $root
        foreach ($step in @($runtimeHome.TrimEnd("\", "/"), (Join-Path $runtimeHome "runtime"), $root)) {
            $entry = Get-DirectoryEntry $step
            if ($entry -and ($entry.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
                $left = "$(Get-DataPathDescription $step), and the uninstaller never deletes through a link"
                $behindLink = (Resolve-LinkChain $step) + $root.Substring($step.Length)
                break
            }
            if (-not $entry -or -not $entry.PSIsContainer) {
                $left = "$step is not a directory, and the uninstaller deletes only real directories"
                break
            }
        }
        foreach ($launcher in $launchers) {
            if (-not $left -and (Get-DirectoryEntry $launcher)) {
                $left = "$launcher could not be removed, and removing the root would leave it dangling"
            }
        }
        if ($left) {
            Write-Warning "Left $root in place: $left."
            Write-Host "  If it is yours, remove it with: Remove-Item -Recurse -Force -LiteralPath '$($behindLink.Replace("'", "''"))'"
            $failed = $true
        } elseif (-not (Remove-ReportedPath $root)) {
            $failed = $true
        }
    }
    foreach ($package in $uvTools) {
        $environment = Join-Path $toolDirectory $package
        if (-not $uv) {
            Write-Warning "uv was not found, so the uv tool install $package at $environment was left in place"
            $failed = $true
        } elseif (-not (Test-UvToolOwnsItsLaunchers $environment)) {
            Write-Warning "Left the uv tool install $package in place: a launcher it records now belongs to another program, which 'uv tool uninstall $package' would delete"
            $failed = $true
        } elseif ((Invoke-NativeCommand -FilePath $uv -Arguments @("tool", "uninstall", $package)).Success -or
            -not (Test-Path -LiteralPath $environment)) {
            Write-Success "Removed the uv tool install $package"
        } else {
            Write-Warning "Could not remove the uv tool install $package; run 'uv tool uninstall $package'"
            $failed = $true
        }
    }
    if ($Purge) {
        $keptTargets = @($unlinkedData | ForEach-Object { Get-DataPathDescription $_ })
        foreach ($item in $unlinkedData) {
            if (-not (Remove-ReportedPath $item -Verb "Removed the link")) { $failed = $true }
        }
        foreach ($item in $doomedData) {
            if (-not (Remove-ReportedPath $item -Verb "Deleted")) { $failed = $true }
        }
        foreach ($item in $keptTargets) {
            $link, $target = $item -split ' -> ', 2
            if (-not [System.IO.Path]::IsPathRooted($target)) {
                $target = Join-Path (Split-Path -Parent $link) $target
            }
            if (Get-DirectoryEntry $target) {
                Write-Info "Kept $target, which $link pointed to. Delete it by hand if it is yours."
            }
        }
    }

    Write-RemainingVibe -Launchers $launchers

    Write-Host ""
    if ($failed) {
        Write-Warning "Avibe was not completely removed. See the warnings above."
    } elseif ($doomedData.Count -gt 0) {
        Write-Success "Avibe and its data were removed."
    } else {
        Write-Success "Avibe was removed."
    }
    if (-not $Purge) {
        Write-KeptData $data
    }
    if ($failed) { return 1 }
    return 0
}

# Main installation flow
function Main {
    Write-Banner
    
    Write-Info "Detected OS: Windows"
    
    # Install uv (which manages Python automatically)
    Install-Uv

    # Node.js only powers the optional managed Show Page runtime. Never let it
    # block installation of the main avibe CLI/service.
    Install-NodeOptional
    # Install avibe-os
    Install-Vibe
    
    # Verify
    Test-Installation

    # Pre-download the current platform Show Runtime when possible. This is
    # intentionally warning-only so Node/network issues never break avibe.
    Prepare-ShowRuntime
    
    # Done
    Write-NextSteps
}

# Each option means one thing, so a purge is never implied by another.
$optionError = if ($Purge -and -not $Uninstall) {
    "-Purge deletes your data during an uninstall; use it with -Uninstall"
} elseif ($Yes -and -not $Purge) {
    "-Yes confirms a purge; use it with -Uninstall -Purge"
}
if ($Uninstall -or $optionError) {
    Write-Banner
    if ($optionError) {
        Write-Host "[ERROR] " -ForegroundColor Red -NoNewline
        Write-Host $optionError
        $uninstallStatus = 1
    } else {
        $uninstallStatus = Uninstall-Avibe | Select-Object -Last 1
    }
    # A script file reports its status; a script block run at the prompt must
    # not close the user's session, so it leaves the status in LASTEXITCODE.
    if ($MyInvocation.MyCommand.Path) {
        exit $uninstallStatus
    }
    $global:LASTEXITCODE = $uninstallStatus
    return
}

# Run main
Main
