@echo off
rem This file is UTF-8: switch the console code page so Russian messages are readable
chcp 65001 >nul
cd /d "%~dp0"
"C:\Python\python.exe" build_installer.py
if errorlevel 1 (
 echo.
 echo Сборка не завершена. Смотрите текст ошибки выше.
) else (
 echo Установщик: release\installer\NEST_Setup.exe
)
pause
