# Avibe Installation Script for Windows
# Usage: irm https://raw.githubusercontent.com/avibe-bot/avibe/master/install.ps1 | iex
#
# Prerequisites: None! uv will be installed automatically and manages Python for you.

$ErrorActionPreference = "Stop"

# Configuration
$REPO = "avibe-bot/avibe"
$PACKAGE_NAME = "avibe-os"
$TSINGHUA_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
$NODE_MINIMUM_REQUIREMENT = "20.19+ or 22.12+"

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
        if ($protocol.Success -and [int]$protocol.Output.Trim() -ge 2) {
            $snapshot = Invoke-NativeCommand `
                -FilePath $Launcher `
                -Arguments @("__activate-install", "--snapshot", "--launcher", $Launcher)
            if ($snapshot.Success -and $snapshot.Output.Trim()) {
                $state.SourcePath = $snapshot.Output.Trim()
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

function Warn-IfLibreOfficeMissing {
    Write-Warning "Memory Office attachment capture is unavailable on native Windows. LibreOffice installed on Windows is not used by the managed Memory runtime."
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
            Output = ($capturedOutput -join [System.Environment]::NewLine).Trim()
        }
    } catch {
        $capturedOutput = @()

        foreach ($path in @($stdoutPath, $stderrPath)) {
            if (Test-Path $path) {
                $streamOutput = [System.IO.File]::ReadAllText($path).Trim()
                if ($streamOutput) {
                    $capturedOutput += $streamOutput
                }
            }
        }

        $errorText = ($_ | Out-String).Trim()
        if ($errorText) {
            $capturedOutput += $errorText
        }

        return @{
            Success = $false
            ExitCode = 1
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

function Invoke-UvToolInstallAttempt {
    param([string[]]$Arguments)

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
    $runtimeHome = Resolve-InstallPath $runtimeHome
    $generationRoot = Join-Path (Join-Path $runtimeHome "runtime\install-generations") ([Guid]::NewGuid().ToString("N"))
    $generationTools = Join-Path $generationRoot "tools"
    $generationBin = Join-Path $generationRoot "bin"
    $stableBin = Get-StableBinDirectory
    $stableLauncher = Join-Path $stableBin "vibe.exe"
    # The candidate's shared Python activation owner resolves this snapshot to
    # a generation. PowerShell must not duplicate junction/symlink identity.
    $launcherState = Get-LauncherState -Launcher $stableLauncher -RuntimeHome $runtimeHome
    $previousSourcePath = $launcherState.SourcePath
    New-Item -ItemType Directory -Force -Path $generationTools, $generationBin, $stableBin | Out-Null

    $previousToolDir = $env:UV_TOOL_DIR
    $previousToolBinDir = $env:UV_TOOL_BIN_DIR
    try {
        $env:UV_TOOL_DIR = $generationTools
        $env:UV_TOOL_BIN_DIR = $generationBin
        $result = Invoke-NativeCommand -FilePath "uv" -Arguments (@("tool", "install") + $Arguments)
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
            $activationOwner = if ($protocol.Success -and [int]$protocol.Output.Trim() -ge 1) {
                $candidate
            } elseif ($launcherState.ActivationOwner) {
                $ownerProtocol = Invoke-NativeCommand `
                    -FilePath $launcherState.ActivationOwner `
                    -Arguments @("__activate-install", "--protocol-version")
                if ($ownerProtocol.Success -and [int]$ownerProtocol.Output.Trim() -ge 1) {
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
        return $result
    } finally {
        if ($null -eq $previousToolDir) { Remove-Item Env:UV_TOOL_DIR -ErrorAction SilentlyContinue } else { $env:UV_TOOL_DIR = $previousToolDir }
        if ($null -eq $previousToolBinDir) { Remove-Item Env:UV_TOOL_BIN_DIR -ErrorAction SilentlyContinue } else { $env:UV_TOOL_BIN_DIR = $previousToolBinDir }
    }
}

function Install-Vibe {
    Write-Info "Installing avibe-os (Python will be downloaded automatically if needed)..."

    $customPackageSpec = $env:AVIBE_INSTALL_PACKAGE_SPEC
    if (-not $customPackageSpec) {
        $customPackageSpec = $env:VIBE_INSTALL_PACKAGE_SPEC
    }

    if ($customPackageSpec) {
        Write-Info "Trying custom package spec..."
        $result = Invoke-UvToolInstallAttempt -Arguments @($customPackageSpec, "--force")
        if ($result.Success) {
            Write-Success "avibe-os installed successfully (from custom package spec)"
            return
        }

        $failureMessage = "Failed to install avibe-os from custom package spec"
        if ($result.ExitCode -ne $null) {
            $failureMessage += " (exit code $($result.ExitCode))"
        }
        if ($result.Output) {
            $failureMessage += ":`n$($result.Output)"
        }

        Write-Error $failureMessage
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
        $result = Invoke-UvToolInstallAttempt -Arguments $attempt.Arguments
        if ($result.Success) {
            Write-Success "avibe-os installed successfully (from $($attempt.Name))"
            return
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

    Write-Error "Failed to install avibe-os from all sources.`n$($failures -join "`n`n")"
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
    $stableBin = Get-StableBinDirectory
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
    Write-Host "  uv tool uninstall avibe-os"
    Write-Host "  uv tool uninstall vibe-remote"
    Write-Host "  pip uninstall avibe-os vibe-remote"
    Write-Host ("  Remove-Item -Force `"$(Join-Path $stableBin 'vibe.exe')`"")
    Write-Host ("  Remove-Item -Force `"$(Join-Path $stableBin '.vibe.exe.avibe-generation')`"")
    Write-Host '  $avibeHome = if ($env:AVIBE_HOME) { $env:AVIBE_HOME -replace ''^~(?=[\\/]|$)'', $env:USERPROFILE } else { "$env:USERPROFILE\.avibe" }'
    Write-Host '  Remove-Item -Recurse -Force (Join-Path $avibeHome "runtime\install-generations")'
    Write-Host '  Remove-Item -Recurse $avibeHome, ~\.vibe_remote  # remove config and data'
    Write-Host ""
    Write-Host "Documentation:" -ForegroundColor Blue
    Write-Host "  https://github.com/$REPO#readme"
    Write-Host ""
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
    Warn-IfLibreOfficeMissing
    
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

# Run main
Main
