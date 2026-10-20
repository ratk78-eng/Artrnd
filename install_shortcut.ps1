# Creates "KOMPAS MCP" shortcuts on the Desktop and in the Start menu.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python).Source
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to $python" }
& $python (Join-Path $here 'make_icon.py') | Out-Null

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    [Environment]::GetFolderPath('Desktop'),
    (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs')
)
foreach ($dir in $targets) {
    $lnk = $shell.CreateShortcut((Join-Path $dir 'KOMPAS MCP.lnk'))
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = '"' + (Join-Path $here 'KompasMCP.pyw') + '"'
    $lnk.WorkingDirectory = $here
    $lnk.IconLocation = (Join-Path $here 'kompas-mcp.ico') + ',0'
    $lnk.Description = 'Local AI assistant for KOMPAS-3D'
    $lnk.Save()
    Write-Output "Shortcut: $($lnk.FullName)"
}
