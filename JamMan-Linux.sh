#!/bin/sh
# JamMan Card Tool starten (Linux). Ausführbar machen: chmod +x JamMan.sh
#
# Ohne Fenster (Doppelklick im Dateimanager, „Ausführen") gibt es kein
# Terminal, in dem eine Meldung gelesen werden könnte. Deshalb werden Fehler
# über zenity gezeigt, wenn stdin kein Terminal ist (18.11.2026).
cd "$(dirname "$0")" || exit 1

melden() {
    if [ -t 0 ]; then
        printf '%s\n' "$1"
        printf 'Zum Schliessen die Eingabetaste druecken ... '
        read -r _
    elif command -v zenity >/dev/null 2>&1; then
        zenity --error --title="JamMan Card Tool" --text="$1" 2>/dev/null
    elif command -v notify-send >/dev/null 2>&1; then
        notify-send "JamMan Card Tool" "$1"
    fi
}

for P in python3 /usr/bin/python3 /usr/local/bin/python3; do
    command -v "$P" >/dev/null 2>&1 || continue
    if "$P" -c "import tkinter" >/dev/null 2>&1; then
        # Mit Argumenten NICHT das Fenster erzwingen — sonst wird aus
        # "--version" ein GUI-Start (19.11.2026 gemerkt).
        if [ $# -gt 0 ]; then
            "$P" .jamman/jamman.py "$@"
        else
            "$P" .jamman/jamman.py gui
        fi
        if [ $? -ne 0 ]; then
            melden "Das Werkzeug wurde mit einem Fehler beendet.
Starte es im Terminal, um die Meldung zu sehen:
    cd $(pwd) && ./JamMan-Linux.sh"
        fi
        exit 0
    fi
done

if [ -t 0 ]; then
    echo "Hinweis: tkinter fehlt (Paket python3-tk). Es geht im Textmenue weiter."
    echo
    exec python3 .jamman/jamman.py menu
fi

melden "tkinter fehlt (Paket python3-tk).
Installieren:  sudo apt install python3-tk
Danach das Werkzeug erneut starten."
exit 1
