@echo off
rem JamMan Card Tool starten (Windows). Doppelklick.
rem
rem 22.11.2026: Zwei Fehler behoben.
rem  1. Der Microsoft-Store-Platzhalter (WindowsApps\python.exe) wurde als Python
rem     erkannt - dann startete nichts, ohne Meldung.
rem  2. "py -3w" gibt es nicht; der Launcher antwortet "No suitable Python
rem     runtime found". Ausserdem braucht die Konsole python.exe, das Fenster
rem     pythonw.exe - beides wird getrennt gesucht.
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PY="
set "PYW="

rem --- Konsolen-Interpreter (fuer --version, funktionstest, alle Befehle) ---
if exist "%~dp0python.exe" (
    set "PY=%~dp0python.exe"
    set "QUELLE=neben dem Starter"
)
if not defined PY (
    for /f "delims=" %%p in ('where python 2^>nul') do (
        if not defined PY (
            echo %%p | find /i "WindowsApps" >nul
            if errorlevel 1 (
                "%%p" -c "import sys" >nul 2>&1
                if not errorlevel 1 (
                    set "PY=%%p"
                    set "QUELLE=%%p"
                )
            )
        )
    )
)
if not defined PY (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "PY=py -3"
        set "QUELLE=py-Launcher"
    )
)

rem --- Fenster-Interpreter (pythonw, ohne Konsolenfenster) ---
if exist "%~dp0pythonw.exe" set "PYW=%~dp0pythonw.exe"
if not defined PYW (
    for /f "delims=" %%p in ('where pythonw 2^>nul') do (
        if not defined PYW (
            echo %%p | find /i "WindowsApps" >nul
            if errorlevel 1 (
                "%%p" -c "import sys" >nul 2>&1
                if not errorlevel 1 set "PYW=%%p"
            )
        )
    )
)
if not defined PYW (
    where pyw >nul 2>&1
    if not errorlevel 1 set "PYW=pyw -3"
)

rem Aufruf mit Argumenten im selben Fenster (Ausgabe muss sichtbar bleiben)
if not "%~1"=="" (
    if not defined PY goto :keinpython
    %PY% .jamman\jamman.py %*
    if errorlevel 1 pause
    goto :ende
)

echo %DATE% %TIME% Startversuch Quelle=!QUELLE! Fenster=!PYW! >> jamman-start.log

if defined PYW (
    start "" !PYW! .jamman\jamman.py gui
    exit /b
)
if defined PY (
    start "" %PY% .jamman\jamman.py gui
    exit /b
)

:keinpython
echo.
echo Python 3 wurde nicht gefunden.
echo.
echo Falls "python" vorhanden ist: Windows zeigt damit auf den Platzhalter aus
echo dem Microsoft Store, nicht auf Python. Bitte Python installieren:
echo.
echo     https://www.python.org/downloads/windows/
echo.
echo Im Installer "Add python.exe to PATH" anhaken.
echo.
choice /c JN /n /m "Downloadseite jetzt oeffnen? (J/N) "
if errorlevel 2 goto :ende
start "" "https://www.python.org/downloads/windows/"

:ende
endlocal
