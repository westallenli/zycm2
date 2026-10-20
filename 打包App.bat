@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=python"
if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
set "OUT=%~dp0pack-result.txt"
echo ============================================================
echo  Packaging the captain App (map cache improvements).
echo  Takes 2-5 minutes. Do not close this window.
echo ============================================================
echo.
"%PY%" -X utf8 tools\build_android_map_report.py > "%OUT%" 2>&1
set "RC=%ERRORLEVEL%"
echo "==== EXIT %RC% ====" >> "%OUT%"
echo.
type "%OUT%"
echo.
echo ============================================================
if "%RC%"=="0" (
  echo  DONE. New APK is in: code\android-captain\out\
  echo  Next: uninstall the old App on the phone, then scan the
  echo  download QR again to install the new one.
) else (
  echo  FAILED. Please send pack-result.txt to the assistant.
)
echo ============================================================
pause
