@echo off
setlocal
"%~dp0V5_daily_full_flow\runtime\python\python.exe" "%~dp0V5_daily_full_flow\reference_data\menuitem_lookup.py" --force
if errorlevel 1 (
  echo.
  echo MENUITEM lookup refresh failed. The existing lookup was not replaced.
) else (
  echo.
  echo MENUITEM lookup refresh completed.
)
pause
exit /b %ERRORLEVEL%
