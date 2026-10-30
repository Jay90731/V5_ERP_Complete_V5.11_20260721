@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_image_cleanup_task.ps1"
if errorlevel 1 (
  echo.
  echo 安裝失敗，請查看上方訊息。
  pause
  exit /b 1
)
echo.
echo 已安裝每日圖片清理排程。
pause

