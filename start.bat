@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
 ".venv\Scripts\python.exe" main.py
) else (
 if exist "python_path.txt" (
   set /p PYCMD=<"python_path.txt"
   call :saved
 ) else (
   python main.py
 )
)
if errorlevel 1 pause
exit /b
:saved
"%PYCMD%" main.py
exit /b %errorlevel%
