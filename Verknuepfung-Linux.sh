#!/bin/sh
# Legt einen Eintrag im Anwendungsmenue und auf dem Schreibtisch an (22.11.2026).
#
# 3.136.7: Das Symbol wird nach ~/.local/share/icons/hicolor/... kopiert und im
# Eintrag als Name eingetragen. Ein relativer Pfad funktioniert in .desktop-Dateien
# nicht - deshalb erschien auf Ubuntu nur ein allgemeines Symbol.
cd "$(dirname "$0")" || exit 1
ORDNER="$(pwd)"
ANWENDUNGEN="${HOME}/.local/share/applications"
SYMBOLE="${HOME}/.local/share/icons/hicolor"
mkdir -p "$ANWENDUNGEN" || exit 1

# Symbol an den Platz legen, an dem die Arbeitsumgebung es sucht
if [ -f "$ORDNER/jamman.png" ]; then
    for groesse in 256 128 64 48 32; do
        ziel="${SYMBOLE}/${groesse}x${groesse}/apps"
        mkdir -p "$ziel"
        if command -v convert >/dev/null 2>&1; then
            convert "$ORDNER/jamman.png" -resize "${groesse}x${groesse}" \
                    "$ziel/jamman-card-tool.png" 2>/dev/null
        else
            cp "$ORDNER/jamman.png" "$ziel/jamman-card-tool.png"
            break
        fi
    done
fi
command -v update-icon-caches >/dev/null 2>&1 && \
    update-icon-caches "$HOME/.local/share/icons/hicolor" 2>/dev/null
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -f -t "$SYMBOLE" 2>/dev/null

DATEI="$ANWENDUNGEN/jamman-card-tool.desktop"
cat > "$DATEI" <<EOF
[Desktop Entry]
Type=Application
Name=JamMan Card Tool
Comment=Speicherkarte des JamMan Solo XT verwalten
Exec=$ORDNER/JamMan-Linux.sh
Path=$ORDNER
Icon=jamman-card-tool
Terminal=false
Categories=AudioVideo;Audio;
EOF
chmod 755 "$DATEI"

SCHREIBTISCH="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$SCHREIBTISCH" ]; then
    cp "$DATEI" "$SCHREIBTISCH/jamman-card-tool.desktop"
    chmod 755 "$SCHREIBTISCH/jamman-card-tool.desktop"
    if command -v gio >/dev/null 2>&1; then
        gio set "$SCHREIBTISCH/jamman-card-tool.desktop" metadata::trusted true 2>/dev/null
    fi
fi

echo ""
echo "Angelegt: $DATEI"
[ -d "$SCHREIBTISCH" ] && echo "und auf dem Schreibtisch: $SCHREIBTISCH/jamman-card-tool.desktop"
echo ""
echo "Falls der Schreibtisch-Eintrag nicht startet: einmal mit der rechten"
echo "Maustaste anklicken und 'Zulassen, dass gestartet wird' waehlen."
