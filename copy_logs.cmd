@echo off
rem Copies the program's journals (outside the Claude app's file virtualization) next to the code.
mkdir "%~dp0logs_copy" 2>nul
copy /y "%LOCALAPPDATA%\kompas-mcp\app*.log" "%~dp0logs_copy\" >nul
copy /y "%LOCALAPPDATA%\kompas-mcp\calls*.jsonl" "%~dp0logs_copy\" >nul
copy /y "%LOCALAPPDATA%\kompas-mcp\server.log" "%~dp0logs_copy\" >nul
dir "%LOCALAPPDATA%\kompas-mcp" > "%~dp0logs_copy\dir.txt"
