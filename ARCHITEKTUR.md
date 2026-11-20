# Aufbau des Werkzeugs

**Für wen dieses Blatt ist:** wer den Code zum ersten Mal liest — besonders
Ingenieure, die prüfen wollen, ob das Werkzeug in ihr Produkt paßt.

**Was es nicht ist:** eine Rechtfertigung. Es beschreibt, wie der Code gebaut
ist, welche drei Regeln überall gelten und wo man anfangen sollte zu lesen.

---

## 1. Die drei Regeln, die überall gelten

```
1  Nie ohne Sicherung schreiben        vor jeder Änderung an Karte oder Pedal
                                       wird der betroffene Teil gesichert
2  Nie halb schreiben                  in eine .part-Datei schreiben, prüfen,
                                       dann umbenennen (os.replace)
3  Vorhandene Namen gehören dem Gerät  Ordnernamen auf der Karte werden nicht
                                       umbenannt; geändert wird nur die Zuordnung
```

Regel 3 kam nach einem Schaden: am 26.10.2026 hat eine frühe Fassung die
Patch-Ordner nach eigenem Schema umbenannt, und das Pedal erkannte keinen
Patch mehr. Das Werkzeug benennt seitdem keine Patch-Ordner um, sondern
schreibt die Platzzuordnung in `patch.xml`.

## 2. Aufbau in Schichten

```
jamman.py                       alles in einer Datei (Gründe unten)
 │
 ├─ Kartenzugriff (Dateisystem)  patches() · patch_name() · patch_einstellungen()
 │                               kopiere_baum() · luecken_in() · sicherung_*
 │
 ├─ Kartenzugriff (USB)          greenglass.py — Protokoll des Pedals über
 │                               den seriellen Anschluß, lesen und schreiben
 │
 ├─ Audio                       wav_info() · wellenform() · normieren() ·
 │                               teilen() · ffmpeg nur zum Umwandeln
 │
 ├─ Textoberfläche              menu() — braucht kein Tk, für alte Macs
 │
 └─ Fenster                     gui() — Kompositionswurzel: baut 62 Widgets,
                                 verbindet 13 Knöpfe und 20 Tastenkürzel mit
                                 den Funktionen darüber
```

**Die Richtung ist immer gleich:** das Fenster ruft Funktionen auf, nie
umgekehrt. Keine Kartenlogik steckt in einem Rückruf — sie ist in benannte
Funktionen ausgelagert, die auch das Textmenü und die Befehlszeile nutzen.

## 3. Warum eine Datei

```
Vorteil   herunterladen, entpacken, doppelklicken — kein Installer, keine
          Bibliothek, keine Abhängigkeit außer Python selbst
Nachteil  gui() ist 2400 Zeilen und 102 innere Funktionen; ein Umbau in Module
          ist ein eigener Vorgang mit eigenem Risiko
```

**Diese Entscheidung ist bewußt und dokumentiert.** Für ein Werkzeug, das
Nutzer auf drei Systemen ohne Installation einsetzen sollen, wiegt der Vorteil
schwerer als die Form. Wer den Code ändern will, findet in
`ENTWICKLER.md` den Einstieg und die Prüfungen.

## 4. Wo Zustand liegt

```
zustand{}      Laufzeitgedächtnis des Fensters: Karte, Liste, Auswahl,
               Warteschlange für Hintergrundarbeit
~/.jamman-werkzeug.cnf   Einstellungen des Nutzers (Pfade, Sprache, Fenster)
jamman-fehler.log        Protokoll: Abstürze und Zeitmessungen
```

**Nichts davon liegt auf der Karte oder im Pedal.** Wer das Werkzeug löscht,
löscht keine Aufnahmen.

## 5. Die drei Schreibwege

```
Karte im Kartenleser   normale Dateioperationen, atomar, mit Sicherung
Pedal über USB         greenglass.py spricht das Protokoll; nach jeder Datei
                       wird die Größe verglichen
Firmware übertragen    nur auf ausdrückliche Bestätigung, vorher Sicherung
                       ALLER Systemdateien des Zielgeräts, danach Größenprüfung;
                       unit_guid.attr, JamManSoloXT.xml und .filetable bleiben
                       unangetastet (Einzelheiten: README, Abschnitt Firmware)
```

## 6. Was wo geprüft wird

```
Selbsttest im Werkzeug   Knöpfe, Dialoge, Dateibefehle, fünf Sprachen
                         (jamman.py funktionstest --alle-sprachen)
ruff                     Namen, Importe, tote Stellen (ruff.toml)
texte_pruefen.py         Vollständigkeit der Übersetzungen
usb_pruefen.py           USB-Fenster und Pflichtfunktionen
alles_pruefen.py         Dokumente, Anleitungen, Arbeitsordner
GitHub Actions           ruff + Selbsttest unter xvfb bei jedem Push
```

## 7. Wenn Sie nur fünf Minuten haben

```
1  jamman.py funktionstest --alle-sprachen     laufen lassen
2  SICHERHEIT.md lesen                          was geschrieben wird
3  README, Abschnitt „Firmware"                 was ausdrücklich nicht passiert
```

**Alles andere ist lesbar, wenn es nötig ist — die Reihenfolge oben ist die
sinnvollste.**

<!-- Name geprüft -->

<!-- Absenderadresse: suilui@julius-k.de (20.11.2026) -->
