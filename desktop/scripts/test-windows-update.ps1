# Hermetic Windows process/filesystem acceptance of the real install supervisor.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/../src-tauri/src/update_windows.ps1"
$root = Join-Path ([IO.Path]::GetTempPath()) ('avibe-update-test-' + [Guid]::NewGuid())
[IO.Directory]::CreateDirectory($root) | Out-Null
try {
    function Build-App($path, $version, $marker, $class) {
        $source = @"
using System;
using System.IO;
using System.Reflection;
[assembly: AssemblyProduct("Avibe")]
[assembly: AssemblyInformationalVersion("$version")]
public class $class {
    public static void Main() {
        File.WriteAllText(Path.Combine(Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location), "$marker"), "started");
    }
}
"@
        Add-Type -TypeDefinition $source -OutputAssembly $path -OutputType ConsoleApplication
    }
    $old = Join-Path $root 'old.exe'
    $next = Join-Path $root 'next.exe'
    Build-App $old '3.1.1' 'ran-old' 'OldApp'
    Build-App $next '3.1.2' 'ran-new' 'NewApp'
    $installer = Join-Path $root 'installer.exe'
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Reflection;
[assembly: AssemblyProduct("Avibe")]
[assembly: AssemblyInformationalVersion("3.1.2")]
public class Installer {
    public static int Main(string[] args) {
        var root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        var joined = String.Join(" ", args);
        var destination = joined.Substring(joined.IndexOf("/D=") + 3);
        var mode = File.ReadAllText(Path.Combine(root, "mode"));
        if (mode == "deleted") {
            Directory.Delete(destination, true);
            return 1;
        }
        if (mode == "partial" || mode == "wrong-version") {
            File.WriteAllText(Path.Combine(destination, "new-only.dll"), "failed version bytes");
            Directory.CreateDirectory(Path.Combine(destination, "new-resources"));
            File.WriteAllText(Path.Combine(destination, "new-resources", "partial.txt"), "partial");
            File.WriteAllText(Path.Combine(destination, "resources.txt"), "damaged resources");
            return mode == "partial" ? 1 : 0;
        }
        File.Copy(Path.Combine(root, "next.exe"), Path.Combine(destination, "app.exe"), true);
        return 0;
    }
}
'@ -OutputAssembly $installer -OutputType ConsoleApplication
    foreach ($mode in @('success', 'deleted', 'partial', 'wrong-version')) {
        $failure = $mode -ne 'success'
        $directory = Join-Path $root ("installation space 中文 " + $mode)
        $staging = Join-Path $root ("stage-" + $mode)
        [IO.Directory]::CreateDirectory($directory) | Out-Null
        [IO.Directory]::CreateDirectory($staging) | Out-Null
        Copy-Item -LiteralPath $old -Destination (Join-Path $directory 'app.exe')
        [IO.File]::WriteAllText((Join-Path $directory 'resources.txt'), 'previous resources')
        [IO.File]::WriteAllText((Join-Path $root 'mode'), $mode)
        $requestData = [pscustomobject]@{
            directory=$directory; staging=$staging; executable=(Join-Path $directory 'app.exe');
            installer=$installer; version='3.1.2'; pid=2147483647
        }
        $threw = $false
        try { Invoke-AvibeReplacement $requestData } catch { $threw = $true }
        if ($threw -ne $failure) { throw "Unexpected transaction outcome: $threw / $failure" }
        $expected = if ($failure) { $old } else { $next }
        if ((Get-FileHash (Join-Path $directory 'app.exe')).Hash -ne (Get-FileHash $expected).Hash) {
            throw 'Application bytes did not match the successful or restored version'
        }
        if ([IO.File]::ReadAllText((Join-Path $directory 'resources.txt')) -ne 'previous resources') {
            throw 'Previous application resources were not preserved'
        }
        if ((Test-Path -LiteralPath (Join-Path $directory 'new-only.dll')) -or
            (Test-Path -LiteralPath (Join-Path $directory 'new-resources'))) {
            throw 'Failed installation left new files mixed into the restored application'
        }
        $marker = Join-Path $directory $(if ($failure) { 'ran-old' } else { 'ran-new' })
        $deadline = [DateTime]::UtcNow.AddSeconds(10)
        while (-not (Test-Path -LiteralPath $marker)) {
            if ([DateTime]::UtcNow -gt $deadline) { throw 'Application was not relaunched' }
            Start-Sleep -Milliseconds 100
        }
    }
    Write-Output 'Native Windows update success and rollback fixtures passed.'
} finally {
    Remove-Item -LiteralPath $root -Recurse -Force
}
