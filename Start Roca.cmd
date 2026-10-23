@echo off
setlocal
cd /d "%~dp0"
echo Starting Roca Label Station release 1.2.0 in Google Chrome...
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 roca_to_square.py
goto finished
:use_python
python roca_to_square.py
:finished
echo.
echo If startup failed, read the message above. Close older Roca windows before trying again.
pause
