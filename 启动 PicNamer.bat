@echo off
rem PicNamer one-click launcher (double-click friendly). ASCII only on purpose.
chcp 65001 >nul
set "ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%run.ps1" %*
pause
