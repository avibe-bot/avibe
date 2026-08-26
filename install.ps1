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

function Get-StableBinDirectory {
    $configured = $env:UV_TOOL_BIN_DIR
    $directory = if ($configured) { $configured } else { Join-Path $env:USERPROFILE ".local\bin" }
    if (-not [System.IO.Path]::IsPathRooted($directory)) {
        $directory = Join-Path (Get-Location) $directory
    }
    return [System.IO.Path]::GetFullPath($directory)
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
        $process = Start-Process -FilePath $FilePath `
            -ArgumentList $Arguments `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath `
            -NoNewWindow `
            -Wait `
            -PassThru `
            -ErrorAction Stop

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
            Success = ($process.ExitCode -eq 0)
            ExitCode = $process.ExitCode
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
    if (-not [System.IO.Path]::IsPathRooted($runtimeHome)) {
        $runtimeHome = Join-Path (Get-Location) $runtimeHome
    }
    $runtimeHome = [System.IO.Path]::GetFullPath($runtimeHome)
    $generationRoot = Join-Path (Join-Path $runtimeHome "runtime\install-generations") ([Guid]::NewGuid().ToString("N"))
    $generationTools = Join-Path $generationRoot "tools"
    $generationBin = Join-Path $generationRoot "bin"
    $stableBin = Get-StableBinDirectory
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

        $integrity = Test-UvCandidate -Candidate $candidate -GenerationTools $generationTools -WorkingDirectory $runtimeHome
        if (-not $integrity.Success) {
            Remove-Item -LiteralPath $generationRoot -Recurse -Force -ErrorAction SilentlyContinue
            return $integrity
        }

        Push-Location $runtimeHome
        try {
            $launcherProbe = Invoke-NativeCommand -FilePath $candidate -Arguments @("--help")
        } finally {
            Pop-Location
        }
        if (-not $launcherProbe.Success) {
            Remove-Item -LiteralPath $generationRoot -Recurse -Force -ErrorAction SilentlyContinue
            return @{
                Success = $false
                ExitCode = $launcherProbe.ExitCode
                Output = if ($launcherProbe.Output) { $launcherProbe.Output } else { "candidate vibe launcher failed its startup probe" }
            }
        }

        $replacement = Join-Path $stableBin ("vibe.exe.avibe-" + [Guid]::NewGuid().ToString("N") + ".new")
        try {
            New-Item -ItemType SymbolicLink -Path $replacement -Target $candidate -ErrorAction Stop | Out-Null
        } catch {
            # Developer mode is not required for a normal Windows install. A
            # hard link preserves the candidate bytes while still allowing an
            # atomic Move-Item activation on the same volume.
            try {
                New-Item -ItemType HardLink -Path $replacement -Target $candidate -ErrorAction Stop | Out-Null
            } catch {
                # Hard links cannot cross volumes. Copying to a temporary file
                # in the stable bin keeps the final Move-Item atomic there.
                Copy-Item -LiteralPath $candidate -Destination $replacement -Force -ErrorAction Stop
            }
        }
        Move-Item -Force -Path $replacement -Destination (Join-Path $stableBin "vibe.exe")
        try {
            $marker = Join-Path $stableBin ".vibe.exe.avibe-generation"
            $markerReplacement = Join-Path $stableBin (".vibe.exe.avibe-generation-" + [Guid]::NewGuid().ToString("N") + ".new")
            Set-Content -LiteralPath $markerReplacement -Value $generationRoot -Encoding UTF8
            Move-Item -Force -Path $markerReplacement -Destination $marker
        } catch {
            Write-Warning "Activated the launcher, but could not record its generation for offline rollback."
        }
        return $result
    } finally {
        if ($null -eq $previousToolDir) { Remove-Item Env:UV_TOOL_DIR -ErrorAction SilentlyContinue } else { $env:UV_TOOL_DIR = $previousToolDir }
        if ($null -eq $previousToolBinDir) { Remove-Item Env:UV_TOOL_BIN_DIR -ErrorAction SilentlyContinue } else { $env:UV_TOOL_BIN_DIR = $previousToolBinDir }
    }
}

function Test-UvCandidate {
    param(
        [string]$Candidate,
        [string]$GenerationTools,
        [string]$WorkingDirectory
    )

    try {
        $candidateTarget = (Resolve-Path -LiteralPath $Candidate -ErrorAction Stop).Path
        $candidateBin = Split-Path -Parent $candidateTarget
        $pythonCandidates = @(
            (Join-Path $candidateBin "python.exe"),
            (Join-Path $candidateBin "python3.exe"),
            (Join-Path $GenerationTools "python.exe"),
            (Join-Path $GenerationTools "python3.exe")
        )
        $candidatePython = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $candidatePython) {
            $candidatePython = Get-ChildItem -LiteralPath $GenerationTools -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -in @("python.exe", "python3.exe") } |
                Select-Object -First 1 -ExpandProperty FullName
        }
        if (-not $candidatePython) {
            return @{
                Success = $false
                ExitCode = 1
                Output = "could not find the candidate Python interpreter"
            }
        }

        $probePath = Join-Path ([System.IO.Path]::GetTempPath()) ("avibe-integrity-" + [Guid]::NewGuid().ToString("N") + ".py")
        $probeCode = @'
from core.install_integrity import verify_python_environment
result = verify_python_environment(__import__("sys").executable)
print(result.detail)
raise SystemExit(0 if result.ok else 1)
'@
        Set-Content -LiteralPath $probePath -Value $probeCode -Encoding utf8
        Push-Location $WorkingDirectory
        try {
            $probe = Invoke-NativeCommand -FilePath $candidatePython -Arguments @($probePath)
        } finally {
            Pop-Location
            Remove-Item -LiteralPath $probePath -Force -ErrorAction SilentlyContinue
        }
        if (-not $probe.Success) {
            return @{
                Success = $false
                ExitCode = $probe.ExitCode
                Output = if ($probe.Output) { $probe.Output } else { "candidate Avibe environment failed integrity checks" }
            }
        }
        return $probe
    } catch {
        return @{
            Success = $false
            ExitCode = 1
            Output = (($_ | Out-String).Trim())
        }
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
    
    # Refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    $stableBin = Get-StableBinDirectory
    $env:Path += ";$stableBin"
    
    if (Test-Command "vibe") {
        Write-Success "vibe command is available"
        Write-Host ""
        & vibe --help
        return $true
    }
    
    # Check common install locations
    $vibeLocations = @(
        (Join-Path $stableBin "vibe.exe")
    )
    
    foreach ($loc in $vibeLocations) {
        if (Test-Path $loc) {
            Write-Warning "vibe installed at $loc but not in PATH"
            Write-Host ""
            Write-Host "Add this directory to your PATH:" -ForegroundColor Yellow
            Write-Host "  $(Split-Path $loc)"
            Write-Host ""
            return $true
        }
    }
    
    Write-Error "Installation verification failed. vibe command not found."
}

function Prepare-ShowRuntime {
    if ($env:VIBE_INSTALL_SKIP_SHOW_RUNTIME -eq "1") {
        Write-Warning "Skipping Show Runtime preparation because VIBE_INSTALL_SKIP_SHOW_RUNTIME=1"
        return
    }

    if (-not (Test-Command "vibe")) {
        Write-Warning "Show Runtime was not prepared because the vibe command is not available yet"
        return
    }

    Write-Info "Preparing Show Runtime for this platform..."
    $result = Invoke-NativeCommand -FilePath "vibe" -Arguments @("runtime", "prepare", "--strict")
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
    Write-Host '  Remove-Item -Recurse -Force (Join-Path $(if ($env:AVIBE_HOME) { $env:AVIBE_HOME } else { "$env:USERPROFILE\.avibe" }) "runtime\install-generations")'
    Write-Host "  Remove-Item -Recurse ~\.avibe, ~\.vibe_remote  # remove config and data"
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
