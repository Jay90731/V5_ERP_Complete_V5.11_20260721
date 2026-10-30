@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

set "ROOT=%~dp0"
set "PYTHON=%ROOT%runtime\python\python.exe"
set "APP=%ROOT%web_v1\app.py"
set "DATABASE=%ROOT%data\orders.db"
set "LOG_DIR=%ROOT%logs"
set "LOG_FILE=%LOG_DIR%\web_startup_error.log"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1
if not exist "%PYTHON%" goto :missing_python
if not exist "%APP%" goto :missing_app
if not exist "%DATABASE%" goto :missing_database

set "DEBUG_ARGUMENT="
if "%WEDDING_WEB_DEBUG%"=="1" set "DEBUG_ARGUMENT=--debug"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"

echo ================================================
echo Wedding Orders website is starting
echo Python: %PYTHON%
echo App: %APP%
echo Database: %DATABASE%
echo URL: http://127.0.0.1:8765/
echo Keep this window open while using the website.
echo ================================================

"%PYTHON%" -X utf8 "%APP%" --database "%DATABASE%" --host 127.0.0.1 --port 8765 --open-browser --startup-log "%LOG_FILE%" %DEBUG_ARGUMENT%
set "EXIT_CODE=%ERRORLEVEL%"
if "%EXIT_CODE%"=="0" exit /b 0

echo.
echo [ERROR] Website startup failed. Exit code: %EXIT_CODE%
echo Error log: %LOG_FILE%
if exist "%LOG_FILE%" type "%LOG_FILE%"
echo.
pause
exit /b %EXIT_CODE%

:missing_python
set "ERROR_MESSAGE=Missing runtime\python\python.exe: %PYTHON%"
goto :preflight_error

:missing_app
set "ERROR_MESSAGE=Missing web_v1\app.py: %APP%"
goto :preflight_error

:missing_database
set "ERROR_MESSAGE=Missing data\orders.db: %DATABASE%"
goto :preflight_error

:preflight_error
echo [ERROR] %ERROR_MESSAGE%
echo [%DATE% %TIME%] %ERROR_MESSAGE%>>"%LOG_FILE%"
echo Error log: %LOG_FILE%
echo.
pause
exit /b 1
