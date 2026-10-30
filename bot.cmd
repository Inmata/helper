@echo off
setlocal
if "%~1"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\bot_ctl.ps1" -Action start
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\bot_ctl.ps1" %*
)
