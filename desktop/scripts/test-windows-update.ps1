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
        if (File.Exists(Path.Combine(root, "fail"))) {
            Directory.Delete(destination, true);
            return 1;
        }
        File.Copy(Path.Combine(root, "next.exe"), Path.Combine(destination, "app.exe"), true);
        return 0;
    }
}
'@ -OutputAssembly $installer -OutputType ConsoleApplication
    foreach ($failure in @($false, $true)) {
        $directory = Join-Path $root ("installation space 中文 " + $failure)
        $staging = Join-Path $root ("stage-" + $failure)
        [IO.Directory]::CreateDirectory($directory) | Out-Null
        [IO.Directory]::CreateDirectory($staging) | Out-Null
        Copy-Item -LiteralPath $old -Destination (Join-Path $directory 'app.exe')
        [IO.File]::WriteAllText((Join-Path $directory 'resources.txt'), 'previous resources')
        if ($failure) { [IO.File]::WriteAllText((Join-Path $root 'fail'), '') }
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
