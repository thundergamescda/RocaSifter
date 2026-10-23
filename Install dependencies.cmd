@echo off
setlocal
cd /d "%~dp0"
echo.
echo Roca Station 2.0.3 - Windows setup
echo.
if not exist "requirements.txt" goto missing_files
if not exist "station_store.py" goto missing_files
if exist ".venv\Scripts\python.exe" goto install
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto python_required
py -3 -m venv .venv
if errorlevel 1 goto failed
goto install
:use_python
where python >nul 2>nul
if errorlevel 1 goto python_required
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto python_required
python -m venv .venv
if errorlevel 1 goto failed
:install
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto python_required
".venv\Scripts\python.exe" -c "import sys,platform; print('Python:', platform.python_version(), '|', platform.machine()); print('Environment:', sys.executable)"
echo Updating pip...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto failed
echo Installing prebuilt packages. No Rust or C compiler is needed.
".venv\Scripts\python.exe" -m pip install --only-binary=:all: -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -c "import roca_to_square; print('Roca Station', roca_to_square.APP_VERSION, 'import check passed.')"
if errorlevel 1 goto failed
echo.
echo Setup complete. Close this window and double-click Start Roca.vbs.
set "ROCA_INSTALL_RESULT=0"
goto finished
:python_required
echo.
echo Python 3.10 or newer is required. We recommend 64-bit Python 3.12 or 3.13.
echo Install it from python.org, then run this setup again.
goto failed
:missing_files
echo.
echo Extract the complete application ZIP before running setup.
goto failed
:failed
echo.
echo Setup did not finish. Please keep the error above for troubleshooting.
echo If a prebuilt package is unavailable, use 64-bit Python 3.12 or 3.13.
echo Do not delete station_data or outputs; they contain your saved work.
set "ROCA_INSTALL_RESULT=1"
:finished
echo.
pause
exit /b %ROCA_INSTALL_RESULT%
