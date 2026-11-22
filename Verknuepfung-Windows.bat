@echo off
rem Legt eine Verknuepfung auf dem Desktop an (22.11.2026).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0verknuepfung-anlegen.ps1" "%~dp0."
echo.
echo Fenster mit einer Taste schliessen.
pause
