# Build every release artifact into packaging\dist:
#   P6MCP-Setup-<v>.exe, P6MCP-<v>-portable.zip, p6mcp-<v>.mcpb, SHA256SUMS.txt
#
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1            # unsigned
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Sign pfx  # signed with a .pfx (see sign.ps1)
#
# Needs: the dev environment (pip install -e .[dev] pyinstaller) and Inno Setup 6.
param(
    [ValidateSet("none", "pfx")][string]$Sign = $(if ($env:P6MCP_SIGN) { $env:P6MCP_SIGN } else { "none" }),
    [switch]$SkipInstaller
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }

function Step($text) { Write-Host "== $text" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit $LASTEXITCODE)" } }

Step "clean"
Remove-Item -Recurse -Force packaging\dist -ErrorAction SilentlyContinue

Step "assets (icon, version resources)"
& $py packaging\make_assets.py; Check "make_assets"
$version = (Get-Content packaging\build\version.txt -Raw).Trim()

Step "PyInstaller $version"
& $py -m PyInstaller packaging\p6mcp.spec --noconfirm --distpath packaging\dist --workpath packaging\build\pyi `
    --log-level WARN; Check "PyInstaller"

$exes = @("packaging\dist\P6MCP\p6mcp-server.exe", "packaging\dist\P6MCP\p6mcp-settings.exe",
          "packaging\dist\P6MCP-server\p6mcp-server.exe")
if ($Sign -eq "pfx") {
    Step "sign executables"
    & "$PSScriptRoot\sign.ps1" -Files $exes
}

if (-not $SkipInstaller) {
    Step "installer"
    $iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
              "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) { $iscc = (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source }
    if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) not found" }
    $isccArgs = @("/Q", "/DAppVersion=$version")
    if ($env:P6MCP_REPO_URL) { $isccArgs += "/DRepoUrl=$env:P6MCP_REPO_URL" }
    if ($Sign -eq "pfx") {
        # Inno Setup signs the installer and the uninstaller through this command ($f = the file)
        $signCmd = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\sign.ps1`" -Files `$f"
        $isccArgs += @("/DSIGN", "/Sp6sign=$signCmd")
    }
    & $iscc @isccArgs "packaging\installer.iss"; Check "ISCC"
}

Step "portable zip, MCP bundle, checksums"
& $py packaging\bundle.py; Check "bundle"
Get-ChildItem packaging\dist -File | Format-Table Name, @{n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } }
