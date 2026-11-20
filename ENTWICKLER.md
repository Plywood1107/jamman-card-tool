# Entwickler: prüfen, bauen, ändern

**Für wen:** wer am Code etwas ändern will. Alles hier ist ein Befehl, den man
kopieren kann; jeder endet mit einem Ergebnis, das man lesen kann.

---

## 1. Voraussetzungen

```
Python 3.8 oder neuer — mehr nicht. Kein ffmpeg (nur zum Umwandeln von
MP3/M4A), keine Bibliothek, kein Build.
Für das Fenster: Tk 8.6 oder neuer (Apples /usr/bin/python3 hat nur Tk 8.5;
dann läuft das Textmenü).
```

## 2. Die Prüfungen, in dieser Reihenfolge

```bash
# 1  Selbsttest: drückt jeden Knopf, prüft Dialoge und Dateibefehle,
#    in fünf Sprachen. Rückgabe 0 = in Ordnung
python3 .jamman/jamman.py funktionstest --alle-sprachen

# 2  Übersetzungen: fehlt ein Text, wird es hier sichtbar
python3 ../werkzeuge/texte_pruefen.py

# 3  USB-Fenster und Pflichtfunktionen
python3 ../werkzeuge/usb_pruefen.py

# 4  Linter (Regeln und begründete Ausnahmen in ruff.toml)
python3 -m ruff check .jamman/jamman.py .jamman/greenglass.py

# 5  Der ganze Arbeitsordner (Dokumente, Anleitungen, Websites)
python3 ../werkzeuge/alles_pruefen.py
```

**Auf GitHub Actions laufen 1 und 4 bei jedem Push** (`.github/workflows/
pruefen.yml`, mit virtuellem Bildschirm unter Linux).

## 3. Eine Fassung bauen und veröffentlichen

**Am einfachsten mit einem Befehl** (bricht beim ersten Befund ab, dann wird
nichts veröffentlicht):

```bash
# im Arbeitsordner, vorher notizen/release-<fassung>.md anlegen und den
# Eintrag "## <fassung> …" in der Änderungsliste schreiben
python3 werkzeuge/veroeffentliche_fassung.py 3.128
python3 werkzeuge/veroeffentliche_fassung.py 3.128 --probe   # nur prüfen
```

Das Skript prüft die Dateilisten beider Pakete gegen einen Manifest (die drei
Prüf-Dokumente müssen drin sein, `ruff.toml` und `.ruff_cache` nicht), läuft
alle Prüfungen, committet, veröffentlicht, lädt zum Vergleich herunter, setzt
die Werkzeugseiten und schreibt Prüfsumme und Größe ins Protokoll.

**Von Hand geht es weiterhin:**

```bash
# Paket für die aufgeräumte Aufteilung
python3 werkzeuge/baue_jamman_zip.py

# Flache „Brückenfassung" für Fassungen bis 3.117 (können .jamman/ nicht
# installieren, brauchen jamman.py an der Wurzel)
python3 werkzeuge/baue_jamman_bruecke.py 3.126

# Veröffentlichen (gh ist eingerichtet)
cd jamman-card-tool
gh release create v3.126 jamman-bruecke-3.126.zip \
   --title "…" --notes-file ../notizen/release-3.126.md --latest

# Danach IMMER: herunterladen, Byte vergleichen, entpacken, Selbsttest
```

## 3a. Ankündigungen: deutsch und englisch

**GitHub ist international** — dort steht die englische Fassung, die deutsche
bleibt für den eigenen Gebrauch:

```
notizen/release-<fassung>-en.md   geht auf GitHub (Skript nimmt sie zuerst)
notizen/release-<fassung>.md      deutsche Fassung, bleibt im Arbeitsordner
Titel                             = erste Überschrift der englischen Datei
```

Fehlt die englische Datei, nimmt das Skript die deutsche — es bricht nicht ab.

## 3b. Wie Fassungen gezählt werden

```
3.131.1   kleine Änderung an einer schon veröffentlichten Fassung
          (Beispiel: das Häkchen „Selbsttest nach Update" kam nach der
          Veröffentlichung von 3.131 — der Aktualisierer sieht (3,131,1) als
          neuer an und holt sie)
3.133     größere Änderung, neue Funktion, Umbau: die nächste volle Zahl
```

**Nie eine Nummer wiederverwenden**, auch wenn eine Veröffentlichung
zurückgezogen wurde — jemand kann sie in der Zwischenzeit geladen haben.
3.132 wurde am 19.11.2026 veröffentlicht und wieder zurückgenommen; sie bleibt
deshalb gesperrt, die nächste volle Fassung ist 3.133.

## 4. Regeln beim Ändern

```
Texte            nie im Code hart hinschreiben, immer T("…"); dann in alle
                 fünf Kataloge (de, en, fr, es, zh) eintragen — texte_pruefen
                 meldet fehlende
Sichtbare Texte  auch Listboxen und Labels gehen durch T(); der Selbsttest
                 sucht deutsche Reste, wenn die Sprache nicht deutsch ist
Namen auf Karten niemals umbenennen (siehe ARCHITEKTUR.md, Regel 3)
Schreiben        immer .part → prüfen → os.replace; vorher sichern
Fassung          FASSUNG erhöhen und oben in der Änderungsliste eintragen —
                 alles_pruefen prüft beides gegeneinander
Breite           Knöpfe nicht in feste Breiten zwingen (knopf_text() bricht um,
                 die Schrift folgt der Bildschirmbreite)
```

## 5. Wo was liegt

```
.jamman/jamman.py          Programm, Oberfläche, Kartenlogik
.jamman/greenglass.py      USB-Protokoll (Herkunft: RenanDiaz/jamman-manager,
                           MIT; Änderungen sind gekennzeichnet)
.jamman/docs/              Fensterbilder für die README
Hilfe-…/                   Starter, Änderungsliste, Spenden-Dateien
ARCHITEKTUR.md             Aufbau und Schichten
SICHERHEIT.md              was geschrieben wird und was nie
```

## 6. Fragen, die im Code beantwortet sind

```
„Warum eine Datei?"              ARCHITEKTUR.md, Abschnitt 3
„Warum blinde except?"           ruff.toml — damit ein Klick nie ein Fenster
                                 mit Traceback beendet; der Selbsttest prüft
                                 zusätzlich auf Ausnahmen
„Warum .part?"                   SICHERHEIT.md, Abschnitt 2
„Warum keine Namen umbenennen?"  ARCHITEKTUR.md, Regel 3 (Anlaß 26.10.2026)
„Was passiert beim Abbruch?"     SICHERHEIT.md, Abschnitt 2 und 3
```

## 7. Woher das Wissen stammt

**Das Kartenformat und das USB-Protokoll sind nicht dokumentiert.** Was hier
steht, stammt aus Proben von Nutzerkarten, aus dem offenen Projekt
`RenanDiaz/jamman-manager` (MIT) und aus Versuchen, die im Änderungsprotokoll
und in `notizen/JamMan-Speicherkarte.txt` festgehalten sind. **Wo etwas nur
vermutet ist, steht es als Vermutung da** — das ist Absicht: eine Vermutung,
die als Tatsache auftritt, beschädigt Karten.
