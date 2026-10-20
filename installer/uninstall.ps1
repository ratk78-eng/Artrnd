# Removes KOMPAS MCP installed by install.ps1. Python and its libraries are left in place.
$target = Join-Path $env:LOCALAPPDATA 'Programs\KompasMCP'
$ans = Read-Host 'Удалить KOMPAS MCP? Загруженные чертежи и журналы в %LOCALAPPDATA%\kompas-mcp тоже будут удалены. (Y/N)'
if ($ans -notmatch '^[YyДд]') { exit 0 }
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and ($_.CommandLine -like "*$target*" -or $_.CommandLine -like '*kompas-mcp\window*') } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item (Join-Path ([Environment]::GetFolderPath('Desktop')) 'KOMPAS MCP.lnk') -ErrorAction SilentlyContinue
Remove-Item (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\KOMPAS MCP.lnk') -ErrorAction SilentlyContinue
Remove-Item 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\KompasMCP' -Recurse -ErrorAction SilentlyContinue
Remove-Item (Join-Path $env:LOCALAPPDATA 'kompas-mcp') -Recurse -Force -ErrorAction SilentlyContinue
# The script lives inside $target, so the folder is removed by a detached process after we exit.
Start-Process cmd.exe -ArgumentList "/c timeout /t 2 >nul & rmdir /s /q `"$target`"" -WindowStyle Hidden
Write-Host 'KOMPAS MCP удалён.' -ForegroundColor Green
Start-Sleep 2
