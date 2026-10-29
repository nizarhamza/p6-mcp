# Authenticode signing with a code signing certificate in a .pfx file (signtool from the Windows SDK).
#   $env:P6MCP_PFX_PATH      path of the .pfx file
#   $env:P6MCP_PFX_PASSWORD  its password
#   $env:P6MCP_TIMESTAMP_URL RFC 3161 timestamp server (default: http://timestamp.digicert.com)
# Azure Artifact Signing is done by the release workflow instead (azure/artifact-signing-action).
param([Parameter(Mandatory = $true)][string[]]$Files)
$ErrorActionPreference = "Stop"
if (-not $env:P6MCP_PFX_PATH -or -not (Test-Path $env:P6MCP_PFX_PATH)) { throw "P6MCP_PFX_PATH is not set or not found" }
if (-not $env:P6MCP_PFX_PASSWORD) { throw "P6MCP_PFX_PASSWORD is not set" }
$ts = if ($env:P6MCP_TIMESTAMP_URL) { $env:P6MCP_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }
$signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $signtool) { throw "signtool.exe not found (install the Windows SDK)" }
foreach ($f in $Files) {
    & $signtool sign /fd SHA256 /td SHA256 /tr $ts /f $env:P6MCP_PFX_PATH /p $env:P6MCP_PFX_PASSWORD /d "MCP for Primavera P6" $f
    if ($LASTEXITCODE -ne 0) { throw "signing $f failed" }
    & $signtool verify /pa /q $f
    if ($LASTEXITCODE -ne 0) { throw "signature of $f does not verify" }
}
