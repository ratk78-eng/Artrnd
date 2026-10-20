@echo off
rem Starts the kompas-mcp web UI and opens it in the browser
chcp 65001 >nul
cd /d "%~dp0"
start "" http://127.0.0.1:8765
python ui\app.py
