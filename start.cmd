@echo off
setlocal EnableExtensions

rem 将工作目录固定到脚本所在目录，兼容包含空格的路径。
cd /d "%~dp0" || exit /b 1

where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo 启动 Qoder Cockpit 需要 PowerShell。
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 (
  echo.
  echo Qoder Cockpit 启动失败，按任意键退出。
  pause >nul
  exit /b 1
)

endlocal
