@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_same_day_replay.ps1"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo SAME DAY REPLAY TEST PASSED
) else (
  echo SAME DAY REPLAY TEST FAILED, code %RC%
)
pause
exit /b %RC%

