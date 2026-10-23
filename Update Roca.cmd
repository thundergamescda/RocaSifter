@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0roca_update.py" goto missing
if exist ".venv\Scripts\pythonw.exe" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0roca_update.py"
    exit /b 0
)
where pyw >nul 2>nul
if not errorlevel 1 (
    start "" pyw -3 "%~dp0roca_update.py"
    exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%~dp0roca_update.py"
    exit /b 0
)
where py >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)"') do if exist "%%~dpPpythonw.exe" (
        start "" "%%~dpPpythonw.exe" "%~dp0roca_update.py"
        exit /b 0
    )
)
where python >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)"') do if exist "%%~dpPpythonw.exe" (
        start "" "%%~dpPpythonw.exe" "%~dp0roca_update.py"
        exit /b 0
    )
)
echo Install Python 3.10 or newer with the Python launcher and Tcl/Tk support.
echo Then run Update Roca.cmd again.
pause
exit /b 1
:missing
echo Extract both Update Roca.cmd and roca_update.py from the updater ZIP first.
pause
exit /b 1
