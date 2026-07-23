@echo off
setlocal
call "%~dp0V5_daily_full_flow\RUN_V5_MONTHLY_FULL.bat"
exit /b %ERRORLEVEL%

