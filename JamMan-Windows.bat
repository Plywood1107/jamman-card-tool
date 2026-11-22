@echo off
rem JamMan Card Tool starten (Windows). Doppelklick.
rem
rem 22.11.2026: Der Microsoft-Store-Platzhalter (WindowsApps\python.exe) wurde
rem bisher als Python erkannt - dann startete nichts, ohne Meldung. Jetzt wird
rem jede gefundene Datei geprueft, ob sie wirklich Python ist.
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "PYW="
set "PY="
set "QUELLE="

rem 1) Mitgeliefertes Python neben dem Starter?
if exist "%~dp0pythonw.exe" (
    set "PYW=%~dp0pythonw.exe"
    set "PY=%~dp0python.exe"
    set "QUELLE=neben dem Starter"
)

rem 2) pythonw im PATH (Fensterstart), aber nicht der Store-Platzhalter
if not defined PY (
    for /f "delims=" %%p in ('where pythonw 2^>nul') do (
        echo %%p | find /i "WindowsApps" >nul
        if errorlevel 1 (
            "%%p" -c "import sys" >nul 2>&1
            if not errorlevel 1 (
                set "PYW=%%p"
                set "PY=%%p"
                set "QUELLE=pythonw"
            )
        )
    )
)

rem 3) pyw (Fensterstarter des py-Launchers); "py -3w" gibt es NICHT
if not defined PY (
    where pyw >nul 2>&1
    if not errorlevel 1 (
        set "PYW=pyw -3"
        set "PY=py -3"
        set "QUELLE=pyw"
    )
)

rem 4) py-Launcher im Konsolenmodus (nur als Rückfall)
if not defined PY (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 (
        set "PY=py -3"
        set "QUELLE=py-Launcher (Konsole)"
    )
)

rem 5) python im PATH - aber nicht der Store-Platzhalter
if not defined PY (
    for /f "delims=" %%p in ('where python 2^>nul') do (
        echo %%p | find /i "WindowsApps" >nul
        if errorlevel 1 (
            "%%p" -c "import sys" >nul 2>&1
            if not errorlevel 1 (
                set "PY=%%p"
                set "PYW=%%p"
                set "QUELLE=%%p"
            )
        )
    )
)

rem Aufruf mit Argumenten (z. B. --version) im selben Fenster
if not "%~1"=="" (
    if not defined PY goto :keinpython
    %PY% .jamman\jamman.py %*
    if errorlevel 1 pause
    goto :ende
)

echo %DATE% %TIME% Startversuch Quelle=!QUELLE! >> jamman-start.log

if defined PYW (
    start "" !PYW! .jamman\jamman.py gui
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
