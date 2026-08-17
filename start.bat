@echo off
title HELIX — Elite Edge FX Academy Trading Agent
cd /d "%~dp0"

echo.
echo  ================================================
echo   HELIX — Elite Edge FX Academy Trading Agent
echo   Dashboard: http://127.0.0.1:9090
echo  ================================================
echo.

start "HELIX Trading Agent" cmd /k python run_dashboard.py --mode live

timeout /t 3 /nobreak >nul

start http://127.0.0.1:9090
