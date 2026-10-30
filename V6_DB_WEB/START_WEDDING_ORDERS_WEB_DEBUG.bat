@echo off
setlocal
set "WEDDING_WEB_DEBUG=1"
call "%~dp0START_WEDDING_ORDERS_WEB.bat"
exit /b %ERRORLEVEL%
