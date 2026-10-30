@echo off
set "ROOT=%~dp0"
set "PYTHON=%ROOT%runtime\python\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"
set "PYTHONPATH=%ROOT%src;%ROOT%web_v1"
"%PYTHON%" -X utf8 -m unittest discover -s "%ROOT%web_v1\tests" -v
if errorlevel 1 goto :failed
"%PYTHON%" -X utf8 -m unittest discover -s "%ROOT%tests" -p "test_phase3_pipeline.py" -v
if errorlevel 1 goto :failed
echo All offline tests passed.
pause
exit /b 0

:failed
echo Offline tests failed.
pause
exit /b 1
