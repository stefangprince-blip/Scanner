@echo off
setlocal
cd /d C:\Scanner
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\Scanner\start_scanner_stack.ps1"
endlocal
