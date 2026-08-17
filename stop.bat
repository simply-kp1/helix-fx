@echo off
title HELIX — Stop Agent

echo.
echo  Stopping Helix Trading Agent...
echo.

wmic process where "name='python.exe' and commandline like '%%run_dashboard%%'" delete >nul 2>&1

if %errorlevel%==0 (
    echo  Agent stopped successfully.
) else (
    echo  No running agent found.
)

echo.
pause
