@echo off
setlocal
call "%~dp0V5_daily_full_flow\RUN_V5_DAILY.bat"
exit /b %ERRORLEVEL%

