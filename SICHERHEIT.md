# Was das Werkzeug schreibt — und was nie

**Für wen:** wer prüfen will, ob das Werkzeug Karten oder Pedale beschädigen
kann. Jede Aussage hier ist im Code nachvollziehbar; wo es schnell geht, steht
der Befehl dabei, mit dem man sie selbst prüfen kann.

---

## 1. Wohin geschrieben wird

| Ort | Was | Wann |
|---|---|---|
| Speicherkarte | `patch.xml`, `phrase.xml`, `phrase.wav` in `PatchNN/` | bei Schreibbefehlen des Nutzers |
| Karte, Plätze | Umbenennen, Verschieben, Lücken schließen | auf Befehl |
| Interner Speicher des Pedals | dieselben Dateien über USB | auf Befehl |
| Sicherungsordner | Kopie des betroffenen Teils **vor** dem Schreiben | automatisch bei jeder Änderung |
| `~/.jamman-werkzeug.cnf` | Einstellungen (Pfade, Sprache, Fenstergröße) | bei Änderung |
| `jamman-fehler.log` | Abstürze, Zeitmessungen | bei Fehlern |

**Nicht geschrieben werden:** Dateien außerhalb dieser Orte, Autostart-Einträge,
Netzwerkfreigaben fremder Geräte, Registrierungsschlüssel.

## 2. In welcher Reihenfolge geschrieben wird

```
1  Sicherung anlegen            vollständig, bevor irgendetwas geändert wird
2  in <ziel>.part schreiben      nie direkt in die Zieldatei
3  Größe und Lesbarkeit prüfen
4  os.replace(.part → Ziel)      ein Schritt, unteilbar
5  bei Abbruch: .part löschen,   die Zieldatei bleibt, wie sie war
   Sicherung bleibt liegen
```

**Was das bedeutet:** Ein Abbruch mittendrin (Stromausfall, Kabel, Strg+C)
hinterläßt **keine halbe Datei** — entweder die alte oder die neue.
Prüfbar: `grep -n "\.part" .jamman/jamman.py` — vier Stellen, alle nach
diesem Muster.

## 3. Was passiert, wenn die Karte voll ist oder schreibgeschützt

```
voll            das Schreiben bricht ab, die .part-Datei wird entfernt,
                die Sicherung bleibt; die Meldung nennt den Platz
schreibgeschützt die Meldung kommt vor dem ersten Schreibversuch
kein Platz im Sicherungsordner  Abbruch, bevor die Karte angefaßt wird
```

## 4. Firmware: der einzige heikle Vorgang

**Das Werkzeug kann Firmware-Dateien von einem Pedal auf ein anderes
übertragen** — nötig, weil DigiTech den Update-Server abgeschaltet hat
(`digitech.com/updater/` antwortet mit 404). Das ist der einzige Vorgang, bei
dem ein Abbruch ein Pedal unbrauchbar machen kann.

```
vorher     Sicherung ALLER Systemdateien des Zielgeräts nach
           sicherung-vor-firmware-<Zeitstempel>/
während    nach jeder Datei wird die Größe verglichen
Nachbar    geschrieben wird <datei>.neu, dann umbenannt (QNX läßt eine
           laufende Datei nicht überschreiben: „Resource busy")
unberührt  unit_guid.attr (Kennung DIESES Pedals)
           JamManSoloXT.xml (Einstellungen des Nutzers)
           .filetable (Verzeichnisindex des Pedals)
Bestätigung  ausdrücklich, mit Warnung; im Fenster über „Schreiben ▾ →
           Firmware übernehmen …", in der Befehlszeile mit Rückfrage
```

**Ein Hersteller wird genau hier nachfragen — und die Antwort ist: nur auf
Befehl, nur mit vollständiger Sicherung, mit Größenprüfung, und die drei
Dateien, die das Gerät identifizieren, bleiben unangetastet.**

## 5. Netz: was das Werkzeug nach außen tut

```
Aktualisierung prüfen   eine Abfrage an api.github.com (Release-Liste),
                        nur auf Klick oder wenn „Update beim Start" gesetzt ist
Spenden-Link            öffnet den Browser, nichts weiter
Bericht per Mail        öffnet das Mailprogramm mit eingesetztem Text
sonst                   keine Verbindung — keine Kennung, keine Statistik,
                        kein Server des Autors
```

**Prüfbar:** `grep -n "urllib.request\|urlopen" .jamman/jamman.py` — nur der
Update-Pfad. Es gibt keine Telemetrie.

## 6. Was das Werkzeug nicht kann

```
keine Aufnahmen löschen, ohne daß der Nutzer den Befehl gibt
keine Dateien außerhalb des Karten-/Pedalbereichs ändern
keine Namen auf der Karte erfinden (Regel 3 in ARCHITEKTUR.md)
keine Firmware ohne Bestätigung schreiben
keine Daten ins Netz schicken
```

## 7. Wie man das selbst prüft, in fünf Minuten

```bash
# 1  Schreibmuster ansehen
grep -n "\.part" .jamman/jamman.py
grep -n "os.replace" .jamman/jamman.py

# 2  Sicherung vor dem Schreiben
grep -n "sicherung" .jamman/jamman.py | head -20

# 3  Netzzugriffe
grep -n "urlopen\|urlopen(" .jamman/jamman.py

# 4  Selbsttest laufen lassen
python3 .jamman/jamman.py funktionstest --alle-sprachen
```

**Wer mehr wissen will: `ARCHITEKTUR.md` beschreibt die Schichten,
`README.de.md` den Umgang mit Firmware im Detail.**
