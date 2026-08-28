@echo off
setlocal EnableExtensions

rem 将工作目录固定到脚本所在目录，兼容包含空格的路径。
cd /d "%~dp0" || exit /b 1

where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo 停止 Qoder Cockpit 需要 PowerShell。
  exit /b 1
)

set "QODER_COCKPIT_ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$root=[IO.Path]::GetFullPath((Join-Path $env:QODER_COCKPIT_ROOT 'server.py')); $escaped=[regex]::Escape($root); $procs=Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -and ($_.CommandLine -match ('(?i)' + $escaped)) }; if($procs){$ids=@($procs | Select-Object -ExpandProperty ProcessId); $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Write-Host ('已停止 Qoder Cockpit 进程: ' + ($ids -join ', '))} else {Write-Host '未找到运行中的 Qoder Cockpit 服务。'}"
if errorlevel 1 (
  echo 停止 Qoder Cockpit 失败。
  exit /b 1
)

endlocal
