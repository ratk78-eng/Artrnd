# Builds dist\KompasMCP-<version>-Setup.exe (self-extracting installer) and
# dist\KompasMCP-<version>-Setup.zip (same content, for manual install via install.cmd).
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$version = (Get-Content "$root\VERSION" | Select-Object -First 1).Trim()
$name = "KompasMCP-$version-Setup"
$work = Join-Path $env:TEMP "kompasmcp-build"
$payload = "$work\payload"; $pkg = "$work\pkg"; $dist = "$root\dist"
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $payload, $pkg, $dist | Out-Null

# 1. Program files (no caches, no personal settings)
$files = 'server.py', 'kompas_api.py', 'KompasMCP.pyw', 'make_icon.py', 'requirements.txt', 'VERSION',
         'README.md', 'start_ui.bat', 'install_shortcut.ps1'
foreach ($f in $files) { Copy-Item "$root\$f" $payload }
foreach ($d in 'ui', 'examples', 'tests') {
    Copy-Item "$root\$d" "$payload\$d" -Recurse
}
New-Item -ItemType Directory -Force "$payload\installer" | Out-Null
Copy-Item "$root\installer\uninstall.ps1" "$payload\installer"
Get-ChildItem $payload -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
Remove-Item "$payload\ui\settings.json" -ErrorAction SilentlyContinue

# Windows PowerShell 5.1 reads BOM-less scripts in the ANSI code page, which breaks Cyrillic text.
function Add-Bom($path) {
    $text = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
    [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $true))
}
Get-ChildItem $payload -Recurse -Filter *.ps1 | ForEach-Object { Add-Bom $_.FullName }

# 2. Package folder: installer scripts + zipped program
Compress-Archive "$payload\*" "$pkg\payload.zip" -Force
Copy-Item "$root\installer\install.ps1", "$root\installer\install.cmd", "$root\VERSION" $pkg
Add-Bom "$pkg\install.ps1"

# 3. Self-extracting exe: a small C# bootstrapper (installer\Setup.cs) with the package embedded
#    as resources, compiled by the csc.exe that ships with Windows' .NET Framework 4.x.
$exe = "$work\$name.exe"
$csc = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path $csc)) { $csc = "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe" }
if (-not (Test-Path "$root\kompas-mcp.ico")) { & (Get-Command python).Source "$root\make_icon.py" | Out-Null }
$cscArgs = @('/nologo', '/target:exe', '/optimize+', "/out:$exe", "/win32icon:$root\kompas-mcp.ico",
             "/win32manifest:$root\installer\setup.manifest",
             "/resource:$pkg\install.ps1,install.ps1", "/resource:$pkg\payload.zip,payload.zip",
             "/resource:$pkg\VERSION,VERSION", "$root\installer\Setup.cs")
& $csc @cscArgs
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $exe)) { throw "csc failed (exit $LASTEXITCODE)" }
Copy-Item $exe $dist -Force
Compress-Archive "$pkg\*" "$dist\$name.zip" -Force
Get-ChildItem "$dist\$name.*" | Select-Object Name, @{n = 'KB'; e = { [math]::Round($_.Length / 1KB) } }
