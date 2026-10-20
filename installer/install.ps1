# KOMPAS MCP installer. Runs from the folder the setup .exe extracted to (payload.zip next to it).
# Installs for the current user only: no administrator rights needed.
$ErrorActionPreference = 'Stop'
$Host.UI.RawUI.WindowTitle = 'KOMPAS MCP - установка'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $env:LOCALAPPDATA 'Programs\KompasMCP'
# Test mode (used by the build check): other folder, no prompts, shortcuts or registry entry.
$test = [bool]$env:KOMPASMCP_TEST_TARGET
if ($test) { $target = $env:KOMPASMCP_TEST_TARGET }

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }
function Fail($text) { Say "`nОШИБКА: $text" Red; if (-not $test) { Read-Host 'Нажмите Enter, чтобы закрыть' }; exit 1 }

function Find-Python {
    # Real interpreters only: the Microsoft Store "python.exe" alias prints nothing useful.
    $probe = "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')"
    $found = @()
    try { if (Get-Command py -ErrorAction SilentlyContinue) { $found += & py -3 -c $probe 2>$null } } catch { }
    try { if (Get-Command python -ErrorAction SilentlyContinue) { $found += & python -c $probe 2>$null } } catch { }
    foreach ($exe in $found) {
        if ($exe -and (Test-Path $exe.Trim())) { return $exe.Trim() }
    }
    foreach ($p in Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", 'C:\Python*\python.exe', "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue) {
        $ok = & $p.FullName -c "import sys; print(int(sys.version_info >= (3, 10)))" 2>$null
        if ($ok -eq '1') { return $p.FullName }
    }
    return $null
}

$version = (Get-Content (Join-Path $here 'VERSION') -ErrorAction SilentlyContinue | Select-Object -First 1)
Say "КОМПАС MCP $version — установка`n" Cyan

# 1. KOMPAS-3D
$kompas = Get-ItemProperty 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -match '^(КОМПАС-3D|KOMPAS-3D) v\d+' } | Select-Object -First 1
if ($kompas) { Say "[1/5] Найден $($kompas.DisplayName) $($kompas.DisplayVersion)" Green }
else { Say '[1/5] КОМПАС-3D не найден. Программа установится, но строить детали можно только при установленном КОМПАС-3D.' Yellow }

# 2. Python
Say '[2/5] Ищу Python 3.10 или новее…'
$python = Find-Python
if (-not $python) {
    Say 'Python не найден.' Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $ans = Read-Host 'Установить Python 3.13 через winget? (Y/N)'
        if ($ans -match '^[YyДд]') {
            winget install --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
            $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
            $python = Find-Python
        }
    }
    if (-not $python) { Fail 'Установите Python 3.10+ с https://www.python.org/downloads/ (галочка «Add python.exe to PATH») и запустите установку снова.' }
}
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { Fail "Рядом с $python нет pythonw.exe" }
Say "      $python" Green

# 3. Files
Say "[3/5] Копирую программу в $target…"
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$target*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$keep = Join-Path $env:TEMP 'kompasmcp-settings.json'
if (Test-Path "$target\ui\settings.json") { Copy-Item "$target\ui\settings.json" $keep -Force }
if (Test-Path $target) { Remove-Item $target -Recurse -Force }
New-Item -ItemType Directory -Force $target | Out-Null
Expand-Archive (Join-Path $here 'payload.zip') -DestinationPath $target -Force
if (Test-Path $keep) { Move-Item $keep "$target\ui\settings.json" -Force; Say '      Настройки прошлой версии сохранены.' }

# 4. Libraries
Say '[4/5] Устанавливаю библиотеки Python (нужен интернет)…'
& $python -m pip install --user --disable-pip-version-check -q -r "$target\requirements.txt"
if ($LASTEXITCODE -ne 0) { Fail 'pip не смог установить библиотеки. Проверьте подключение к интернету.' }
& $python -c "import mcp, win32com.client, PIL, uvicorn, starlette" 2>$null
if ($LASTEXITCODE -ne 0) { Fail 'Библиотеки установились, но не импортируются.' }

if ($test) { Say "`nТестовая установка в $target завершена." Green; exit 0 }

# 5. Shortcuts and uninstall entry
Say '[5/5] Создаю ярлыки…'
& $python "$target\make_icon.py" | Out-Null
$shell = New-Object -ComObject WScript.Shell
$links = @((Join-Path ([Environment]::GetFolderPath('Desktop')) 'KOMPAS MCP.lnk'),
           (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\KOMPAS MCP.lnk'))
foreach ($path in $links) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = '"' + "$target\KompasMCP.pyw" + '"'
    $lnk.WorkingDirectory = $target
    $lnk.IconLocation = "$target\kompas-mcp.ico,0"
    $lnk.Description = 'ИИ-ассистент для КОМПАС-3D'
    $lnk.Save()
}
$key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\KompasMCP'
New-Item $key -Force | Out-Null
Set-ItemProperty $key DisplayName 'KOMPAS MCP'
Set-ItemProperty $key DisplayVersion $version
Set-ItemProperty $key Publisher 'KOMPAS MCP'
Set-ItemProperty $key DisplayIcon "$target\kompas-mcp.ico"
Set-ItemProperty $key InstallLocation $target
Set-ItemProperty $key UninstallString "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$target\installer\uninstall.ps1`""
Set-ItemProperty $key NoModify 1 -Type DWord
Set-ItemProperty $key NoRepair 1 -Type DWord

Say "`nГотово. Ярлык «KOMPAS MCP» есть на рабочем столе и в меню «Пуск»." Green
Say 'Для работы нужен сервер ИИ: LM Studio (порт 1234) или Ollama (порт 11434) с загруженной моделью.'
$run = Read-Host 'Запустить КОМПАС MCP сейчас? (Y/N)'
if ($run -match '^[YyДд]') { Start-Process $pythonw -ArgumentList "`"$target\KompasMCP.pyw`"" -WorkingDirectory $target }
