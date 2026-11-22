#!/bin/sh
# Legt einen Eintrag im Anwendungsmenue und auf dem Schreibtisch an (22.11.2026).
cd "$(dirname "$0")" || exit 1
ORDNER="$(pwd)"
ANWENDUNGEN="${HOME}/.local/share/applications"
mkdir -p "$ANWENDUNGEN" || exit 1
DATEI="$ANWENDUNGEN/jamman-card-tool.desktop"

cat > "$DATEI" <<EOF
[Desktop Entry]
Type=Application
Name=JamMan Card Tool
Comment=Speicherkarte des JamMan Solo XT verwalten
Exec=$ORDNER/JamMan-Linux.sh
Path=$ORDNER
Icon=$ORDNER/jamman.png
Terminal=false
Categories=AudioVideo;Audio;
EOF
chmod 755 "$DATEI"

SCHREIBTISCH="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$SCHREIBTISCH" ]; then
    cp "$DATEI" "$SCHREIBTISCH/jamman-card-tool.desktop"
    chmod 755 "$SCHREIBTISCH/jamman-card-tool.desktop"
    command -v gio >/dev/null 2>&1 && \
        gio set "$SCHREIBTISCH/jamman-card-tool.desktop" metadata::trusted true 2>/dev/null
fi

echo ""
echo "Angelegt: $DATEI"
[ -d "$SCHREIBTISCH" ] && echo "und auf dem Schreibtisch: $SCHREIBTISCH/jamman-card-tool.desktop"
