@echo off
rem JamMan Card Tool starten (Windows). Doppelklick.
rem
rem Startet ohne Konsolenfenster, wenn ein "pythonw" vorhanden ist.
rem Das Eingabeaufforderungsfenster blieb sonst hinter dem Werkzeug stehen
rem (17.11.2026). Jeder Start wird in jamman-start.log vermerkt, damit man
rem später sieht, ob der Startversuch überhaupt stattgefunden hat.
cd /d "%~dp0"
echo %DATE% %TIME% Startversuch >> jamman-start.log

if exist "%~dp0pythonw.exe" (
    start "" "%~dp0pythonw.exe" jamman.py gui
    exit /b
)

where pythonw >nul 2>&1
if not errorlevel 1 (
    start "" pythonw jamman.py gui
    exit /b
)

where py >nul 2>&1
if not errorlevel 1 (
    start "" py -3w jamman.py gui
    exit /b
)

where python >nul 2>&1
if not errorlevel 1 (
    start "" python jamman.py gui
    exit /b
)

echo.
echo Python 3 wurde nicht gefunden.
echo Bitte von https://www.python.org/downloads/ installieren
echo (im Installer "Add python.exe to PATH" anhaken).
echo.
pause
