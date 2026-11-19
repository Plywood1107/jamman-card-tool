#!/bin/bash
# JamMan Card Tool starten (macOS). Doppelklick im Finder.
#
# Gesucht wird ein Python 3 mit Tk 8.6 oder neuer. Apples Python 3.9
# bringt Tk 8.5 mit; das zeichnet auf macOS 26/27 Beschriftungen und
# Listen nicht - das Fenster bleibt leer, nur die Knöpfe sind zu sehen.
# Deshalb: Tk 8.5 wird für das Fenster übergangen, und es geht im
# Textmenü weiter (gleiche Funktionen, nur mit Tastatur).
#
# NEU (18.11.2026): Nach dem Schließen des Werkzeugs schließt sich auch
# dieses Terminalfenster. macOS fragt beim ersten Mal, ob das Skript
# „Terminal" steuern darf - einmal bestätigen, danach ist Ruhe. Wer das
# nicht möchte, drückt „Nicht erlauben"; dann bleibt das Fenster wie
# bisher stehen, und in den Terminal-Einstellungen läßt sich unter
# Profile → Shell → „Wenn die Shell beendet wird: Fenster schließen"
# dasselbe erreichen.

cd "$(dirname "$0")" || exit 1
KARTE="${KARTE:-}"
MEIN_TTY="$(tty 2>/dev/null)"

# Fenster nach erfolgreichem Lauf schließen (nur, wenn wir in einem
# Terminal laufen und nicht etwa aus einem anderen Skript heraus).
fenster_zu() {
    case "$MEIN_TTY" in
        /dev/ttys*) ;;
        *) return 0 ;;
    esac
    # Nur Terminal.app wird per AppleScript geschlossen. iTerm, kitty, WezTerm
    # und andere bleiben unangetastet — dort schließt die eigene Einstellung
    # des Terminals das Fenster (bei iTerm ist „When the session ends: Close"
    # im Profil „Default" ab Werk an). Anlaß für die Rücknahme (19.11.2026):
    # der iTerm-Weg hätte beim ersten Start eine Rückfrage „iTerm möchte iTerm
    # steuern" erzeugt — mehr Störung als Nutzen, weil die Einstellung reicht.
    case "${TERM_PROGRAM:-}" in
        Apple_Terminal|"")
            osascript -e "tell application \"Terminal\" to close (every window whose tty is \"$MEIN_TTY\")" >/dev/null 2>&1
            ;;
    esac
}
for P in /usr/local/bin/python3 /opt/homebrew/bin/python3 \
         /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
         /usr/bin/python3 "$(command -v python3 2>/dev/null)"; do
    [ -x "$P" ] || continue
    if "$P" -c "import tkinter,sys; sys.exit(0 if tkinter.TkVersion >= 8.6 else 1)" >/dev/null 2>&1; then
        if [ -n "$KARTE" ]; then
            "$P" jamman.py gui --karte "$KARTE"
        else
            "$P" jamman.py gui
        fi
        if [ $? -eq 0 ]; then
            fenster_zu
            exit 0
        fi
        echo
        echo "Das Werkzeug wurde mit einem Fehler beendet."
        echo "Zum Schließen die Eingabetaste drücken ..."
        read -r _
        exit 1
    fi
done

echo "Hinweis: Es wurde kein Python mit Tk 8.6 oder neuer gefunden."
echo "Apples Tk 8.5 zeichnet auf diesem macOS keinen Text im Fenster."
echo "Es geht im Textmenü weiter - mit denselben Funktionen."
echo "Für ein Fenster: Python von python.org installieren."
echo
exec /usr/bin/python3 jamman.py menu ${KARTE:+--karte "$KARTE"}
