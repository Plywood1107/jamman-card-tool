#!/bin/bash
# Legt "JamMan Card Tool.app" auf dem Schreibtisch an (22.11.2026).
# Doppelklick im Finder. Das Programmbuendel startet JamMan-macOS.command;
# es zeigt unser Symbol statt des allgemeinen Terminalsymbols.
cd "$(dirname "$0")" || exit 1
ORDNER="$(pwd)"
ZIEL="$HOME/Desktop/JamMan Card Tool.app"

rm -rf "$ZIEL"
mkdir -p "$ZIEL/Contents/MacOS" "$ZIEL/Contents/Resources" || exit 1

# Symbol: am liebsten das fertige jamman.icns aus dem Paket nehmen.
# Anlaß (22.11.2026): Auf einem Intel-Mac blieb das Bündel weiß, weil
# iconutil dort nichts brauchbares erzeugte. Das fertige Symbol liegt im
# Paket — es wird nur kopiert.
SYMBOL_OK=0
if [ -f "$ORDNER/jamman.icns" ]; then
    cp "$ORDNER/jamman.icns" "$ZIEL/Contents/Resources/jamman.icns" && SYMBOL_OK=1
elif [ -f "$ORDNER/JamMan Card Tool.app/Contents/Resources/jamman.icns" ]; then
    cp "$ORDNER/JamMan Card Tool.app/Contents/Resources/jamman.icns" \
       "$ZIEL/Contents/Resources/jamman.icns" && SYMBOL_OK=1
fi

# Sonst aus jamman.png bauen (iconutil braucht einen iconset-Ordner)
if [ "$SYMBOL_OK" -eq 0 ] && [ -f "$ORDNER/jamman.png" ] && command -v iconutil >/dev/null 2>&1; then
    TMP="$(mktemp -d)"
    mkdir -p "$TMP/icon.iconset"
    for g in 16 32 128 256 512; do
        sips -z "$g" "$g" "$ORDNER/jamman.png" \
             --out "$TMP/icon.iconset/icon_${g}x${g}.png" >/dev/null 2>&1
        d=$((g * 2))
        sips -z "$d" "$d" "$ORDNER/jamman.png" \
             --out "$TMP/icon.iconset/icon_${g}x${g}@2x.png" >/dev/null 2>&1
    done
    iconutil -c icns "$TMP/icon.iconset" -o "$ZIEL/Contents/Resources/jamman.icns" >/dev/null 2>&1
    rm -rf "$TMP"
fi

cat > "$ZIEL/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>JamMan Card Tool</string>
    <key>CFBundleDisplayName</key><string>JamMan Card Tool</string>
    <key>CFBundleIdentifier</key><string>de.julius-k.jamman-card-tool</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>start</string>
    <key>CFBundleIconFile</key><string>jamman.icns</string>
    <key>LSMinimumSystemVersion</key><string>10.13</string>
</dict>
</plist>
PLIST

cat > "$ZIEL/Contents/MacOS/start" <<START
#!/bin/bash
cd "$ORDNER" || exit 1
exec "$ORDNER/JamMan-macOS.command"
START
chmod +x "$ZIEL/Contents/MacOS/start"

echo ""
if [ -f "$ZIEL/Contents/Resources/jamman.icns" ]; then
    echo "Symbol: jamman.icns, $(stat -f%z "$ZIEL/Contents/Resources/jamman.icns" 2>/dev/null || echo "?") Bytes"
else
    echo "Symbol: FEHLT — bitte jamman.icns in den Werkzeugordner legen und erneut starten"
fi
echo "Angelegt: $ZIEL"
echo "Das Buendel laesst sich ins Dock ziehen oder in den Ordner /Programme verschieben."
