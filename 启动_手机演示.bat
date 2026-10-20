@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PY=python"
if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
echo ============================================================
echo  Phone demo: PC and phone must be on the same Wi-Fi.
echo  Keep this window open. Close it to stop.
echo ============================================================
"%PY%" -X utf8 run_my_demo.py --port 0 --secret 123456 --wifi-address auto --wifi-prefix 24 --phone-port 18705
echo.
echo Demo stopped.
pause
