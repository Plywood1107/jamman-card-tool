# Fremde Quellen zum JamMan: was gefunden wurde, was verloren ist

Stand: 20.11.2026. Grund: https://github.com/silverXnoise/jamman — eine Sammlung ohne
eigenen Code, angelegt und zuletzt geändert am 26.07.2023, **ohne Lizenz**. Aus einem
Repository ohne Lizenz wird hier nichts übernommen; gelesen und ausgewertet werden darf es.

## 1. Die Protokoll-Notizen von fuzzysynth sind verloren

`silverXnoise/jamman` verweist für „Protocol Info (2018)" auf

    http://fuzzysynth.blogspot.com/2015/06/digitech-jam-man.html
    http://fuzzysynth.blogspot.com/2015/07/kitchensync.html

Am 20.11.2026 geprüft: beide Adressen antworten mit 200, liefern aber **nur den Titel**
(„fuzzy music: digitech jam man") — der Inhalt wird von JavaScript nachgeladen, und die
Archivfassungen (2024-08-01 bzw. 2023-12-05) enthalten ihn ebenfalls nicht. Die Notizen sind
damit nicht mehr zu bekommen. Wer dort etwas erwartet, verliert Zeit; das ist hier festgehalten,
damit es niemand zweimal versucht.

## 2. JamSync ist ein eigenes Signal am 1/8-Zoll-Eingang, nicht USB

Quelle: `joeSeggiola/jamman-arduino-sync` (README und `main/main.ino`, keine Lizenz, nur gelesen).
Das Projekt schiebt ein **MIDI-artiges SysEx-Signal** in die Sync-Buchse; unser Werkzeug
spricht dagegen über USB mit dem „Greenglass"-Rahmen (MID + Nutzlast), und dort kommen
`0xF0`/`0xF7` gar nicht vor (0 Treffer in `.jamman/greenglass.py`).

Zwei Rahmen aus dem Code, wörtlich zitiert als Beleg (kein Code übernommen):

    JM_LINK  F0 00 00 10 7F 62 01 00 01 01 F7
    JM_SYNC  F0 00 00 10 7F 62 01 00 01 00 00 00 00 02 04 00 03 00 00 00 00 00 00 F7

Im Sync-Rahmen werden die Felder gesetzt: Tempo in den Bytes 7, 11 und 12, Länge eines Takts
in 15 und 17–20, der Befehl (SYNC) in Byte 21, und Byte 22 ist eine XOR-Prüfsumme über 7…21.

**Folge für unser Werkzeug:** Tempo und Sync lassen sich nicht über USB einstellen. Sie gehen
über die Sync-Buchse, also über Hardware. Ein Eintrag „JamSync einstellen" im Werkzeug wäre
eine falsche Versprechung.

## 3. JamManager XT: das Original, das dieses Werkzeug ersetzt

`resources/JamManager/JamManagerXTSetup_2.3.5.tar.gz` in derselben Sammlung enthält genau eine
Datei, `JamManagerXTSetup_2.3.5.exe` (Windows). Am 20.11.2026 nach `/tmp` geladen und gemessen:

    Größe      26.540.007 Bytes
    SHA-256    ec3390437553971e62b2dcd5700521f9d776518a6dc3bb48473ac5c48e4a2ccd
    Inhalt     1 Datei: JamManagerXTSetup_2.3.5.exe

Die Datei wird hier **nicht** abgelegt: sie ist DigiTechs Software, die der Hersteller nicht mehr
anbietet, und eine Weitergabe in diesem Repository wäre eine Weiterverbreitung fremder Software.
Als Beleg genügen Adresse und Prüfsumme. Wer sie braucht, lädt sie dort und vergleicht die Summe.

Ursprüngliche Herkunft laut `silverXnoise/jamman` (Internet Archive):

    https://web.archive.org/web/20230212041405/https://adn.harmanpro.com/softwares/wares/604_1504020489/JamManagerXTSetup_2.3.5.exe
    https://web.archive.org/web/20170908204550/http://digitech.com/en-US/softwares/jammanager-xt-v2-3-5-mac-os-x-52ee012f-eff3-4a4d-98d4-b507b58b6ee8

## 4. Was aus der Sammlung sonst zu wissen ist

- `resources/jamman-arduino-sync/` enthält zusätzlich eine EasyEDA-Platine und Gerber-Daten
  (`board-easyeda.json`, `board-gerber.zip`) — also einen fertigen Hardware-Weg für Tempo-Sync.
- `resources/Looperino/Looper.ino` ist das Gegenstück zum Projekt `Calde-github/Looperino`.
- `docs/jamMan.pdf` (96.014 Bytes) ist ein **gescanntes** PDF ohne durchsuchbaren Text.

## 5. Firmware kommt über die Karte, nicht über USB (offizielle Anleitung)

DigiTech/Harman, „Manual Firmware Update Procedure for JamMan Solo XT", Technical Support
Guide v1.0, 18.03.2021, 1 Seite, 107.664 Bytes. Die Adresse

    https://help.harmanpro.com/Documents/2878/Manual%20Firmware%20Update%20Procedure%20for%20JamMan%20Solo%20XT.pdf

antwortet am 20.11.2026 mit 404; die Fassung vom 18.06.2024 liegt im Internet Archive und wurde
von dort gelesen (gescannt, daher kein durchsuchbarer Text — Inhalt aus dem Bild entnommen):

- Die Firmware-Dateien kommen **in das Wurzelverzeichnis** einer MicroSD-Karte, die das Pedal
  lesen kann; ausdrücklich **nicht** in Unterordner.
- Pedal ausschalten, Karte einstecken, einschalten. Das Pedal prüft beim Start die Dateien und
  **aktualisiert sich selbst**.
- Wenn das Display die Preset-Nummer **„1"** zeigt, ist der Vorgang fertig; Karte entnehmen.
- **Danach die Dateien von der Karte löschen** — sonst wird das Pedal bei jedem Einschalten mit
  eingelegter Karte erneut aktualisiert.

Folgen für dieses Werkzeug:

1. Eine Firmware-Aktualisierung braucht keine USB-Verbindung und kein Programm — nur die
   richtigen Dateien auf der Karte. Deshalb ist die Anfrage nach den Firmware-Dateien bei
   DigiTech der eigentliche Weg, nicht eine Funktion im Werkzeug.
2. **Gefahr, die das Werkzeug kennen sollte:** liegt eine Karte mit Firmware-Dateien im Pedal,
   schreibt es sich bei jedem Start neu. Wer eine solche Karte einlegt, ohne es zu wissen,
   riskiert einen abgebrochenen Schreibvorgang. Ein Hinweis „diese Karte enthält
   Firmware-Dateien im Wurzelverzeichnis" wäre billig und nützlich.
3. Die Dateinamen selbst stehen in der Anleitung **nicht** — das Dokument sagt nur „diese
   Dateien". Ohne die Dateien von DigiTech läßt sich der Vorgang also nicht nachbauen.

## 6. Weitere Projekte, die gefunden wurden

`davem2/jamman-rip` — MIT, Python, letzter Stand 20.04.2021. Liest die Patch-Struktur
(`JamManStereo/Patch01/PhraseA/phrase.wav` samt `phrase.xml`) und schreibt die Aufnahmen mit dem
Tempo im Dateinamen heraus; optional mit `--roundbpm` zeitgestreckt. Kein Karten-Schreibzugriff,
also kein Ersatz für dieses Werkzeug, aber derselbe Kartenaufbau — ein unabhängiger Beleg für die
Struktur, die wir schreiben.

Hinweis `IsReversed` (audiofanzine, Tipp vom 24.11.2023): in `phrase.xml` schaltet
`IsReversed` von 0 auf 1 die Wiedergabe rückwärts; auf der Solo XT läßt sich das **nicht** am
Pedal einstellen. Dieses Werkzeug kann es seit dem 18.11.2026 — der Tipp bestätigt also nur, daß
die Funktion gebraucht wird.

## 7. Was JamManager XT selbst verrät (statische Untersuchung, 20.11.2026)

Die Windows-Datei wurde mit `innoextract` entpackt (das Setup hing im stillen Lauf) und gelesen:
`app/JamManagerXT.exe`, eine Qt-Anwendung. Aus den Zeichenketten und Klassennamen:

- Zugriff über **QSerialPort**, also über einen COM-Anschluß; der Treiber `JamMan.inf` ist ein
  `usbser.sys`-Treiber für **USB\\VID_1210&PID_0020** — dasselbe CDC-Gerät, das dieses
  Werkzeug als `/dev/tty.usbmodem14301` benutzt.
- Vorgänge im Programm: `JContentView::populateDeviceInternal`, `populateDeviceExternal`,
  `writeToDevice`, `JResourceView::copyListToDevice`, `updateDBWithDeviceLoops`,
  `JDeviceManagerThread::run` — also Verzeichnis lesen, Dateien schreiben, Liste kopieren.
- **Firmware-Aktualisierung über USB** (nicht nur über die Karte): das Programm holt eine
  Firmware-Datei aus dem Netz, überträgt sie auf das Gerät nach **`/mnt/nandfs/usr/`** unter dem
  Namen **`updJamManDelayTar`** und meldet danach „Please power-cycle your JamMan device to
  apply the update." — also eine `.tar`-Datei, die erst nach einem Neustart wirkt.
- Die Update-Quelle war **`http://digitech.com/updater/common/config/UpdateTargets.xml`**; diese
  Adresse antwortet am 20.11.2026 mit 404, und im Internet Archive liegt unter `digitech.com/updater/`
  **nichts**. Damit ist der Weg, auf dem JamManager Firmware holte, tot. Die Dateinamen der
  Firmware stehen nicht im Programm — sie kamen aus jener XML.
- Die Firmware-Version wird als `%d.%d.%d` angezeigt; dieses Werkzeug liest sie als eine Zahl
  (beim angeschlossenen Solo XT: 5632).

**Was damit gewonnen ist:** der Mechanismus der Aktualisierung (Zielpfad, Dateiname, Neustart) und
die Bestätigung, daß JamManager über denselben seriellen Anschluß arbeitet wie dieses Werkzeug.
**Was fehlt:** die eigentlichen Befehlsbytes. Sie stehen als Zahlen im Maschinencode; aus den
Zeichenketten allein sind sie nicht zu holen. Dafür bräuchte es eine Aufzeichnung des Verkehrs —
und die scheiterte an Wine (siehe Notiz vom 20.11.2026): Wine 11 legt keinen COM-Anschluß an.
