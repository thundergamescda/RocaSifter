@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "tools\install_shortcuts.ps1"
if errorlevel 1 goto failed
echo Open Roca Station from your Desktop or Start menu.
pause
exit /b 0
:failed
echo Shortcut setup did not finish. The error above explains why.
pause
exit /b 1
