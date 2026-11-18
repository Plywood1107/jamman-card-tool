@echo off
rem JamMan Card Tool MIT Konsolenfenster starten (Windows).
rem
rem Für die Fehlersuche: Hier bleibt die Ausgabe sichtbar. Bleibt das Fenster
rem mit einer Meldung stehen, bitte den Text abschreiben oder als Bild
rem schicken. (18.11.2026, nachdem der fensterlose Start nicht ansprang.)
cd /d "%~dp0"
echo %DATE% %TIME% Start mit Konsole >> jamman-start.log

where py >nul 2>&1
if not errorlevel 1 (
    py -3 jamman.py gui
    goto :ende
)

where python >nul 2>&1
if not errorlevel 1 (
    python jamman.py gui
    goto :ende
)

echo.
echo Python 3 wurde nicht gefunden.
echo Bitte von https://www.python.org/downloads/ installieren
echo (im Installer "Add python.exe to PATH" anhaken).

:ende
echo.
echo --- Fenster bleibt offen. Mit einer beliebigen Taste schliessen. ---
pause >nul
