@echo off
REM ============================================================================
REM  Mission Control - one-click dashboard launcher (Windows)
REM  Double-click this file. It starts the server and opens your browser.
REM  Close this window to stop the dashboard.
REM ============================================================================
setlocal
title Mission Control dashboard

REM --- go to the project folder (this .bat lives one level above it) ---
cd /d "%~dp0mission-control"
if not exist "ui\server.py" (
  echo Could not find mission-control\ui\server.py next to this .bat file.
  echo Put run-dashboard.bat in the same folder that contains "mission-control".
  pause
  exit /b 1
)

REM --- make sure Python exists ---
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo  [X] Python was not found on your PATH.
  echo      Install Python 3.11+ from https://python.org and re-run this file.
  echo.
  pause
  exit /b 1
)

REM --- keep posting safe: dashboard runs stay dry-run (never tweet for real) ---
set DRY_RUN=1

REM --- first launch: make sure the web libs are installed ---
python -c "import fastapi, uvicorn" 1>nul 2>nul
if errorlevel 1 (
  echo  Installing dependencies ^(first run only^)...
  python -m pip install -r requirements.txt
)

REM --- first launch: seed a few demo runs so there is something to look at ---
if not exist "mission.db" (
  echo  Generating a few demo runs ^(first run only^)...
  python harness.py --mission missions\lumora.yaml --reject-demo --yes 1>nul 2>nul
  python harness.py --mission missions\lumora.yaml --block-demo  --yes 1>nul 2>nul
  python harness.py --mission missions\lumora.yaml               --yes 1>nul 2>nul
)

REM --- open the browser a moment after the server starts ---
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start "" http://127.0.0.1:8000"

echo.
echo  ============================================================
echo    MISSION CONTROL  ^>  http://127.0.0.1:8000
echo    Pick a mission + agent + scenario and click RUN MISSION.
echo    (Close this window to stop the server.)
echo  ============================================================
echo.

REM --- run the server (blocks until you close the window) ---
python ui\server.py

echo.
echo  Server stopped. Press any key to close.
pause >nul
endlocal
