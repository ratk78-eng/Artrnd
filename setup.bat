@echo off
setlocal
cd /d "%~dp0"
set "PYCMD=%~1"
if not defined PYCMD set "PYCMD=python"
"%PYCMD%" -c "import sys,tkinter; assert (3,11)<=sys.version_info[:2]<(3,15); print(sys.version,sys.executable)"
if errorlevel 1 goto :fail
if exist ".venv" rmdir /s /q ".venv"
"%PYCMD%" -m venv .venv
if errorlevel 1 goto :fallback
".venv\Scripts\python.exe" -m pip install cadquery-ocp
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -c "from OCP.STEPCAFControl import STEPCAFControl_Writer; print('OCP OK')"
if errorlevel 1 goto :fail
echo Installed in .venv. Run start.bat
pause
exit /b 0
:fallback
if exist ".venv" rmdir /s /q ".venv"
echo Virtual environment failed. Checking base Python pip...
"%PYCMD%" -m pip --version
if errorlevel 1 goto :fail
"%PYCMD%" -m pip install --user cadquery-ocp
if errorlevel 1 goto :fail
"%PYCMD%" -c "from OCP.STEPCAFControl import STEPCAFControl_Writer; print('OCP OK')"
if errorlevel 1 goto :fail
"%PYCMD%" -c "import sys,pathlib;pathlib.Path('python_path.txt').write_text(sys.executable,encoding='utf8')"
echo Installed for this Python user. Run start.bat
pause
exit /b 0
:fail
echo OCP setup failed. The GUI can still be opened with python main.py; CAD functions require OCP.
pause
exit /b 1
