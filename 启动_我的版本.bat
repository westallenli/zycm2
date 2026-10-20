@echo off
setlocal
title POJU dev build - port 18706
cd /d "%~dp0"
if errorlevel 1 goto nocd

echo ============================================
echo  dir     : %CD%
echo  port    : 18706
echo  data    : demo_data  (copied from the Codex delivery, original untouched)
echo --------------------------------------------
echo   admin        123456
echo   13900000119  123456   (duty officer)
echo   13900000120  123456   (deputy commander)
echo   13900000031  123456   (team leader)
echo ============================================
echo.
echo Copying demo data on first run, then starting the server.
echo Wait 10-40 seconds. Refresh the browser if it opens too early.
echo.

set PYCMD=
where python >nul 2>nul
if not errorlevel 1 set PYCMD=python
if not defined PYCMD (
  where py >nul 2>nul
  if not errorlevel 1 set PYCMD=py -3
)
if not defined PYCMD if exist "C:\Python314\python.exe" set PYCMD="C:\Python314\python.exe"
if not defined PYCMD if exist "C:\Python313\python.exe" set PYCMD="C:\Python313\python.exe"
if not defined PYCMD if exist "C:\Python312\python.exe" set PYCMD="C:\Python312\python.exe"
if not defined PYCMD goto nopython

echo using: %PYCMD%
echo.
%PYCMD% -X utf8 run_my_demo.py --port 18706 --secret 123456
set RC=%ERRORLEVEL%
if "%RC%"=="2" (
  echo.
  echo Port 18706 is busy - retrying on 18707 ...
  echo.
  %PYCMD% -X utf8 run_my_demo.py --port 18707 --secret 123456
  set RC=%ERRORLEVEL%
)
echo.
echo --------------------------------------------------
echo Exit code: %RC%
if not "%RC%"=="0" echo STARTUP FAILED - screenshot the text above.
echo --------------------------------------------------
goto hold

:nopython
echo.
echo Python not found. Tried: python / py -3 / C:\Python314 / 313 / 312
goto hold

:nocd
echo.
echo Cannot enter folder: %~dp0
goto hold

:hold
echo.
echo ---- press any key to close ----
pause >nul
