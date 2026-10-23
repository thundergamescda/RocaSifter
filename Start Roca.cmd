@echo off
setlocal
cd /d "%~dp0"
echo Starting Roca Station release 2.0.1 in Google Chrome...
if exist ".venv\Scripts\python.exe" goto local_environment
echo.
echo Run Install dependencies.cmd in this folder first.
goto finished
:local_environment
".venv\Scripts\python.exe" roca_to_square.py
:finished
echo.
echo If startup failed, read the message above. Close older Roca windows before trying again.
pause
