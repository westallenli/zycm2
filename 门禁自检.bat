@echo off
chcp 65001 >nul
cd /d "%~dp0impl"
set "PY=python"
if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
set "OUT=%~dp0gate-result.txt"
echo ============================================================
echo  Gate check: the trustworthy set (must be green).
echo  Old UI / old permission-model suites are excluded on purpose.
echo  See code\docs for the reason. About 2-3 minutes.
echo ============================================================
echo.
"%PY%" -X utf8 -m compileall -q panda_brain > "%OUT%" 2>&1
echo "==== SYNTAX DONE ====" >> "%OUT%"
"%PY%" -X utf8 -m unittest discover -s tests -t . -p "test_upgrade_sop_ui_v2.py" >> "%OUT%" 2>&1
"%PY%" -X utf8 -m unittest discover -s tests -t . -p "test_upgrade_nine_flow_ui_v2.py" >> "%OUT%" 2>&1
"%PY%" -X utf8 -m unittest discover -s tests -t . -p "test_field_sop.py" >> "%OUT%" 2>&1
"%PY%" -X utf8 -m unittest discover -s tests -t . -p "test_p15_workbench.py" >> "%OUT%" 2>&1
echo "==== ALL DONE ====" >> "%OUT%"
echo.
type "%OUT%"
echo.
echo ============================================================
echo  Finished. Result file: %OUT%
echo  Tell the assistant: done
echo ============================================================
pause
