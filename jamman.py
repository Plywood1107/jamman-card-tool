#!/usr/bin/env python3
"""JamMan Solo XT — manage the pedal's SD card from a normal computer.

Replacement for DigiTech "JamManager XT" (2012), which crashes on
recent macOS releases as soon as the pedal is detected, and which is no
longer maintained. This tool works directly on the SD card, which the
pedal exposes as a plain drive.

Card layout (verified on real hardware, 2026-10-26):

    JamManSoloXT/
        Patch01/
            patch.xml            name, rhythm, stop mode, patch UUID
            PhraseA/
                phrase.wav        44.1 kHz, 16 bit, stereo
                phrase.xml        IsLoop (single/loop), tempo, phrase UUID
        Patch02/ ...

Commands (without --schreiben nothing is modified — dry run):

    python3 jamman.py liste                list patches (name, length, format)
    python3 jamman.py abspielen --patch 3  play a patch
    python3 jamman.py aufspielen --wav x.wav [--name N] [--stopp fade|instant]
                          [--wiedergabe loop|single] [--rhythmus silence|kick]
    python3 jamman.py aendern --patch 3 [--stopp …] [--wiedergabe …] [--name …]
    python3 jamman.py verschieben --patch 3 --nach 1
    python3 jamman.py loeschen --patch 3
    python3 jamman.py sichern --ziel ORDNER
    python3 jamman.py saeubern            remove macOS ._* files from the card
    python3 jamman.py auswerfen           unmount/eject the card
    python3 jamman.py gui                 simple window (tkinter)
    python3 jamman.py menu                text menu, no tkinter required

Every writing command copies the affected patches to ~/JamMan-Sicherung
first. Command names are German (that is where the tool was written);
the output is German too. See README.md for details.

--- deutsch ---
Ersetzt „JamManager XT" von DigiTech auf macOS, Windows und Linux.
Arbeitet direkt auf der Speicherkarte des JamMan Solo XT. Ohne
--schreiben wird nichts verändert. Vor jedem Schreibvorgang wird nach
~/JamMan-Sicherung gesichert.
"""

import argparse
import math
import os
import re
import shutil
import stat
import string
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
import xml.etree.ElementTree as ET
from datetime import datetime

# Werte, die auf der Karte tatsaechlich vorkommen (am 26.10.2026 aus allen
# Sicherungen gelesen). Es gibt genau zwei Stopp-Arten und zwei Rhythmen;
# "Finish" aus der alten Oberflaeche ist auf dieser Karte nicht vorhanden und
# wird deshalb NICHT erfunden.
STOPP = {"fade": "FadeOut", "instant": "StopInstantly", "finish": "StopAtEndOfLoop"}
# "Finish" heißt in der Datei StopAtEndOfLoop - gefunden am 10.11.2026 auf
# der Sicherungsplatte (Patch 16, "hot turkey").
RHYTHMUS = {"silence": ("Silence", "0"), "kick": ("StudioKickAndHighHat", "1")}
# "Single" oder "Loop" steht NICHT in patch.xml, sondern in phrase.xml
# unter <IsLoop>. Am 26.10.2026 in allen Sicherungen gefunden: 181× 1,
# 185× 0.
WIEDERGABE = {"loop": "1", "single": "0",
              "loop \u21ba": "1", "single \u21ba": "0"}

# Handbuch JamMan Solo XT, Seite 60 (Spezifikationen):
#   "Max. Individual Loop Time: 10 minutes (stereo)"
#   "Loop File Format: 44.1 kHz, 16-bit (stereo), uncompressed WAV"
# Längere Loops nimmt das Pedal nicht an - am 10.11.2026 mit einer
# 15-Minuten-Datei erlebt: auf der Karte vorhanden, im JamMan nicht,
# danach war der Ordner auf dem Mac verschwunden.
MAX_LOOP_SEKUNDEN = 600

WOLL = (44100, 16, 2)          # was der JamMan erwartet: 44,1 kHz, 16 Bit, Stereo

FASSUNG = "3.114 (18.11.2026)"
WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"


def ist_wechselmedium(mountpunkt):
    """Steckt dort ein Wechseldatenträger? (Karte ja, Sicherungsplatte nein.)"""
    if not MAC:
        return False
    try:
        r = subprocess.run(["diskutil", "info", mountpunkt],
                           capture_output=True, text=True)
        return "Removable Media: Removable" in r.stdout
    except Exception:
        return False


def karten_wurzeln():
    """Ordner, in denen ein eingehängter Datenträger liegen kann."""
    if WINDOWS:
        return ["%s:\\" % b for b in string.ascii_uppercase]
    if MAC:
        return ["/Volumes"]
    benutzer = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    return ["/run/media/" + benutzer, "/media/" + benutzer,
            "/run/media", "/media", "/mnt", "/Volumes"]


def karten_diagnose():
    """Zeigt, wo gesucht wurde und was dort lag. Für Fehlersuche am Telefon."""
    zeilen = []
    for wurzel in karten_wurzeln():
        if not os.path.isdir(wurzel):
            zeilen.append(T("  %-28s  (gibt es nicht)") % wurzel)
            continue
        try:
            namen = sorted(os.listdir(wurzel))
        except OSError as e:
            zeilen.append(T("  %-28s  (nicht lesbar: %s)") % (wurzel, e.strerror))
            continue
        if not namen:
            zeilen.append(T("  %-28s  (leer)") % wurzel)
            continue
        for name in namen:
            p = os.path.join(wurzel, name)
            if not os.path.isdir(p):
                continue
            unter = []
            try:
                unter = os.listdir(p)
            except OSError as e:
                zeilen.append(T("  %-28s  (nicht lesbar: %s)") % (p, e.strerror))
                continue
            treffer_name = kartenordner_in(p)
            if treffer_name:
                ordner = os.path.join(p, treffer_name)
                try:
                    anz = len([x for x in os.listdir(ordner)
                               if re.fullmatch(r"Patch\d+", x, re.IGNORECASE)])
                except OSError:
                    anz = -1
                zeilen.append(T("  %-28s  %s mit %s Patches")
                              % (p, treffer_name, anz if anz >= 0 else "?"))
            elif not MAC and not WINDOWS:
                # Linux hängt manchmal eine Ebene tiefer ein
                for name2 in unter:
                    p2 = os.path.join(p, name2)
                    if not os.path.isdir(p2):
                        continue
                    tiefer = kartenordner_in(p2)
                    if tiefer:
                        zeilen.append("  %-28s  %s gefunden" % (p2, tiefer))
    return zeilen


def karten_kandidaten():
    """Alle Laufwerke mit einem JamManSoloXT-Ordner, das wahrscheinlichste zuerst.

    Anlass 10.11.2026: Auf einer Sicherungsplatte lag ebenfalls ein Ordner
    JamManSoloXT mit 23 alten Patches. Nach dem Alphabet ("2TB-1" vor
    "JAMMAN") wurde die Platte gewählt statt der Karte. Bewertet wird
    deshalb: Name, Wechselmedium, Anzahl der Patches.
    """
    moegliche = []
    for wurzel in karten_wurzeln():
        if not os.path.isdir(wurzel):
            continue
        moegliche.append(wurzel)          # Windows: die Karte IST das Laufwerk
        try:
            namen = sorted(os.listdir(wurzel))
        except OSError:
            continue
        for name in namen:
            p = os.path.join(wurzel, name)
            if not os.path.isdir(p):
                continue
            moegliche.append(p)
            if not WINDOWS and not MAC:
                try:
                    for name2 in sorted(os.listdir(p)):
                        p2 = os.path.join(p, name2)
                        if os.path.isdir(p2):
                            moegliche.append(p2)
                except OSError:
                    pass

    treffer = []
    for p in moegliche:
        try:
            eintraege = os.listdir(p)
        except OSError:
            continue
        for e in eintraege:
            if e.lower() not in [w.lower() for w in KARTENORDNER]:
                continue
            ordner = os.path.join(p, e)
            try:
                anz = len([x for x in os.listdir(ordner)
                           if re.fullmatch(r"Patch\d+", x, re.IGNORECASE)])
            except OSError:
                anz = 0
            if not anz:
                continue
            name = os.path.basename(p.rstrip(os.sep)) or p
            punkte = (100 if "jamman" in name.lower() else 0) \
                   + (50 if ist_wechselmedium(p) else 0) \
                   + min(anz, 40)
            treffer.append((punkte, p, anz, name))
    treffer.sort(reverse=True)
    gesehen = set()
    ergebnis = []
    for _, p, anz, name in treffer:
        if p in gesehen:
            continue
        gesehen.add(p)
        ergebnis.append((p, anz, name))
    return ergebnis


def standard_karte():
    """Wo liegt die Karte?

    macOS    /Volumes/JAMMAN
    Windows  erstes Laufwerk mit einem Ordner JamManSoloXT
    Linux    /media/<benutzer>/<name> oder /run/media/<benutzer>/<name>
    """
    if MAC:
        kandidaten = karten_kandidaten()
        return kandidaten[0][0] if kandidaten else "/Volumes/JAMMAN"
    if WINDOWS:
        for buchstabe in "DEFGHIJKLMNOPQRSTUVWXYZABC":
            p = "%s:\\" % buchstabe
            if kartenordner_in(p):
                return p
        return "D:\\"

    # Linux: die üblichen Einhängepunkte absuchen
    kandidaten = []
    for wurzel in ("/run/media/" + os.environ.get("USER", ""),
                   "/media/" + os.environ.get("USER", ""),
                   "/run/media", "/media", "/mnt"):
        if not os.path.isdir(wurzel):
            continue
        for name in sorted(os.listdir(wurzel)):
            p = os.path.join(wurzel, name)
            if kartenordner_in(p):
                kandidaten.append(p)
    return kandidaten[0] if kandidaten else "/media"


def volumen_pfad(pfad):
    """Wurzel des Datenträgers. Funktioniert auf beiden Systemen und ist
    auch auf dem Mac prüfbar:

        /Volumes/JAMMAN/JamManSoloXT   ->  /Volumes/JAMMAN
        D:\\JamManSoloXT               ->  D:\\
    """
    roh = os.path.expanduser(str(pfad))
    m = re.match(r"^([A-Za-z]):[\\/]", roh)      # Windows-Laufwerk
    if m:
        return m.group(1).upper() + ":" + os.sep
    pfad = os.path.abspath(roh)
    teile = [x for x in pfad.split(os.sep) if x != ""]
    # macOS:      /Volumes/<name>/…
    if len(teile) >= 2 and teile[0] == "Volumes":
        return "/Volumes/" + teile[1]
    # Linux:      /media/<benutzer>/<name>/…
    #             /run/media/<benutzer>/<name>/…
    if len(teile) >= 3 and teile[0] == "media":
        return "/media/%s/%s" % (teile[1], teile[2])
    if len(teile) >= 4 and teile[0] == "run" and teile[1] == "media":
        return "/run/media/%s/%s" % (teile[2], teile[3])
    return pfad


def abspielen_datei(pfad):
    """Ton starten. Gibt ein Objekt zurück, mit dem gestoppt werden kann."""
    if WINDOWS:
        import winsound
        winsound.PlaySound(pfad, winsound.SND_FILENAME | winsound.SND_ASYNC)
        return "winsound"
    if MAC:
        return subprocess.Popen(["afplay", pfad])

    # Linux: das erste Programm, das da ist
    for befehl in (["aplay", "-q", pfad],
                   ["paplay", pfad],
                   ["mpv", "--no-video", "--really-quiet", pfad],
                   ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", pfad],
                   ["cvlc", "--play-and-exit", "--intf", "dummy", pfad]):
        if shutil.which(befehl[0]):
            return subprocess.Popen(befehl, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
    raise SystemExit(T("Kein Abspielprogramm gefunden (aplay, paplay, mpv oder ffplay)."))


def stoppen_ton(griff):
    if guff := griff:
        if guff == "winsound":
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        elif hasattr(guff, "poll") and guff.poll() is None:
            guff.terminate()
            try:
                guff.wait(timeout=2)
            except Exception:
                guff.kill()


def fade_ausblenden(pfad, sekunden):
    """Blendet die letzten Sekunden einer WAV-Datei aus (linear).

    Das Pedal blendet beim Stoppen fest über 10 Sekunden aus (Handbuch
    JamMan Solo XT, Seite 47); eine einstellbare Fade-Dauer kennt es
    nicht. Wer eine andere Länge will, muss sie in die Aufnahme legen -
    dann ist sie unabhängig vom Stopp-Verhalten.
    """
    import wave as _wave
    import array
    if sekunden <= 0:
        return False
    with _wave.open(pfad, "rb") as w:
        kanaele, breite, rate, anz = (w.getnchannels(), w.getsampwidth(),
                                      w.getframerate(), w.getnframes())
        rahmen = w.readframes(anz)
    if breite != 2:
        return False
    a = array.array("h")
    a.frombytes(rahmen)
    pro_frame = len(a) // anz if anz else kanaele
    n = int(rate * sekunden) * pro_frame
    n = min(n, len(a))
    if n <= 0:
        return False
    for i in range(n):
        faktor = (n - i) / float(n)
        stelle = len(a) - n + i
        a[stelle] = int(a[stelle] * faktor)
    with _wave.open(pfad, "wb") as w:
        w.setnchannels(kanaele); w.setsampwidth(breite); w.setframerate(rate)
        w.writeframes(a.tobytes())
    return True


def pegel_info(pfad):
    """Spitzen- und Effektivpegel einer WAV-Datei in dBFS."""
    import wave as _wave
    import array
    import math
    with _wave.open(pfad, "rb") as w:
        rahmen = w.readframes(w.getnframes())
    a = array.array("h"); a.frombytes(rahmen)
    if not len(a):
        return None, None
    spitze = max(abs(x) for x in a)
    summe = 0
    for x in a:
        summe += x * x
    rms = math.sqrt(summe / len(a))
    def db(wert):
        return 20 * math.log10(wert / 32768.0) if wert > 0 else -999.0
    return db(spitze), db(rms)


def pegel_anpassen(pfad, db=None, ziel_spitze_db=None, begrenzen=True,
                   grenze_db=0.0, ziel_rms_db=None):
    """Lautstärke ändern: um db verstärken/absenken oder auf einen
    Spitzenpegel normieren.

    begrenzen=True hält die Spitze unter grenze_db (Vorgabe 0 dBFS):
    Statt zu übersteuern wird die Anhebung zurückgenommen. Sonst würde
    alles über 0 dBFS abgeschnitten - das hört man als Kratzen.
    Gibt ein Wörterbuch mit vorher, nachher, geklippt, begrenzt zurück.
    """
    import wave as _wave
    import array
    import math
    with _wave.open(pfad, "rb") as w:
        kanaele, breite, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        rahmen = w.readframes(w.getnframes())
    if breite != 2:
        return None
    a = array.array("h"); a.frombytes(rahmen)
    if not len(a):
        return None
    alt_spitze = max(abs(x) for x in a)
    if alt_spitze == 0:
        return None
    if ziel_spitze_db is not None:
        ziel = 32768.0 * (10 ** (ziel_spitze_db / 20.0))
        faktor = ziel / alt_spitze
    elif ziel_rms_db is not None:
        # Auf gleiche Lautheit (Effektivwert) normieren - das entspricht
        # eher dem, was man als gleich laut hört, als die Spitze.
        summe = 0
        for x in a:
            summe += x * x
        rms = math.sqrt(summe / len(a))
        if rms == 0:
            return None
        ziel = 32768.0 * (10 ** (ziel_rms_db / 20.0))
        faktor = ziel / rms
    else:
        faktor = 10 ** (db / 20.0)

    begrenzt = False
    if begrenzen:
        grenze = 32767.0 * (10 ** (grenze_db / 20.0))
        if alt_spitze * faktor > grenze:
            faktor = grenze / alt_spitze
            begrenzt = True
    geklippt = 0
    for i in range(len(a)):
        wert = int(a[i] * faktor)
        if wert > 32767:
            wert = 32767; geklippt += 1
        elif wert < -32768:
            wert = -32768; geklippt += 1
        a[i] = wert
    with _wave.open(pfad, "wb") as w:
        w.setnchannels(kanaele); w.setsampwidth(breite); w.setframerate(rate)
        w.writeframes(a.tobytes())
    neu_spitze = max(abs(x) for x in a)
    vorher = 20 * math.log10(alt_spitze / 32768.0)
    nachher = 20 * math.log10(neu_spitze / 32768.0) if neu_spitze else -999
    return {"vorher": vorher, "nachher": nachher,
            "geklippt": geklippt, "begrenzt": begrenzt}


def schreibe_pegelprotokoll(basis, eintraege, grund, wurzel=None):
    """Schreibt auf, was am Pegel geändert wurde.

    Damit lässt sich eine Pegeländerung auch dann zurückrechnen, wenn die
    große Sicherung einmal fehlt: Faktor umkehren, fertig. Die Rundung
    auf 16 Bit kostet dabei höchstens eine Stufe.
    """
    if not eintraege:
        return None
    ordner = os.path.expanduser(wurzel or SICHERUNG_WURZEL)
    try:
        os.makedirs(ordner, exist_ok=True)
        ziel = freier_name(os.path.join(
            ordner, "Pegelprotokoll_%s_%s.txt"
            % (datetime.now().strftime("%Y-%m-%d_%H-%M-%S"), grund)))
        f = open(ziel, "w", encoding="utf-8")
    except OSError:
        print(T("  Hinweis: Pegelprotokoll konnte nicht geschrieben werden."))
        return None
    with f:
        f.write("# Pegeländerung %s (%s)\n" % (
            datetime.now().isoformat(timespec="seconds"), grund))
        f.write("# Karte: %s\n" % basis)
        f.write("# Faktor = Verstärkung, die angewendet wurde.\n")
        f.write("# Ordner\tName\tFaktor\tLautheit vorher\tLautheit nachher\n")
        for e in eintraege:
            f.write("%s\t%s\t%.6f\t%+.2f\t%+.2f\n" % (
                e["ordner"], e["name"], e["faktor"], e["vorher"], e["nachher"]))
    print(T("  Protokoll: %s") % ziel)
    return ziel


def lies_pegelprotokoll(pfad):
    """Liest ein Pegelprotokoll: Liste aus Ordner, Faktor je Zeile.

    Fehler werden als SystemExit gemeldet, nicht als Ausnahme: sonst sah
    der Nutzer bei einer falsch gewählten Datei einen Traceback statt eines
    Satzes (gefunden am 11.11.2026: eine WAV als Protokoll gewählt).
    """
    try:
        with open(pfad, encoding="utf-8") as datei:
            zeilen = datei.readlines()
    except FileNotFoundError:
        raise SystemExit(T("Fehler: '%s' gibt es nicht.") % pfad)
    except IsADirectoryError:
        raise SystemExit(T("Fehler: '%s' ist ein Ordner, keine Datei.") % pfad)
    except UnicodeDecodeError:
        raise SystemExit(T("Fehler: '%s' ist kein Pegelprotokoll "
                         "(keine lesbare Textdatei).") % pfad)
    except PermissionError:
        raise SystemExit(T("Fehler: '%s' darf nicht gelesen werden.") % pfad)
    except OSError as fehler:
        raise SystemExit(T("Fehler: '%s' lässt sich nicht lesen: %s")
                         % (pfad, fehler))
    eintraege = []
    for zeile in zeilen:
        if zeile.startswith("#") or not zeile.strip():
            continue
        teile = zeile.rstrip("\n").split("\t")
        if len(teile) < 3:
            continue
        try:
            eintraege.append({"ordner": teile[0], "name": teile[1],
                              "faktor": float(teile[2])})
        except ValueError:
            continue
    return eintraege


def befehl_pegel_umkehren(basis, protokoll, schreiben, wurzel=None):
    """Pegeländerung aus einem Protokoll zurückrechnen."""
    eintraege = lies_pegelprotokoll(protokoll)
    if not eintraege:
        raise SystemExit(T("Fehler: im Protokoll stehen keine Einträge."))
    print(T("  Protokoll: %s") % protokoll)
    print(T("  %d Einträge") % len(eintraege))
    if not schreiben:
        for e in eintraege:
            print(T("    %-10s %-24s Faktor %.4f → 1/%.4f") % (
                e["ordner"], e["name"][:24], e["faktor"], e["faktor"]))
        print(T("  Trockenlauf — nichts geändert. Mit --schreiben ausführen."))
        return
    ziel = sicher_ablegen(basis, [pf for _, _, pf in patches(basis)],
                          "vor_pegel_umkehren", wurzel)
    gemacht = 0
    for e in eintraege:
        wav = os.path.join(basis, e["ordner"], "PhraseA", "phrase.wav")
        if not os.path.exists(wav) or e["faktor"] <= 0:
            continue
        ergebnis = pegel_anpassen(wav, db=-20.0 * math.log10(e["faktor"]),
                                  begrenzen=False)
        if ergebnis:
            gemacht += 1
            print(T("    %-10s %+6.1f → %+6.1f dB") % (
                e["ordner"], ergebnis["vorher"], ergebnis["nachher"]))
    print(T("  Zurückgerechnet: %d Patches. Sicherung davor: %s") % (gemacht, ziel))


def print_pegel(ergebnis):
    """Meldet, was mit dem Pegel passiert ist."""
    print(T("  Pegel:        %.1f dB → %.1f dB Spitze") % (
        ergebnis["vorher"], ergebnis["nachher"]))
    if ergebnis["begrenzt"]:
        print(T("                Die Anhebung wurde begrenzt, damit nichts"))
        print(T("                über 0 dBFS geht (sonst würde übersteuert)."))
    if ergebnis["geklippt"]:
        print(T("                Achtung: %d Werte liegen an der Grenze.") % ergebnis["geklippt"])


def unlesbar_hinweis(pfad):
    """Erklärt, warum eine Datei nicht lesbar ist - und was hilft."""
    endung = os.path.splitext(str(pfad))[1].lower()
    if endung in (".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"):
        if werkzeug_pfad("ffmpeg") is None:
            return ("\n  Dieses Format kann nur mit ffmpeg gelesen werden.\n"
                    + ffmpeg_hinweis())
        return "\n  ffmpeg ist vorhanden, konnte die Datei aber nicht lesen.\n"
    return ""


def ffmpeg_fehlt():
    """Wird ffmpeg gebraucht und ist es nicht da?

    Auf dem Mac übernimmt afconvert; dort ist ffmpeg nicht nötig.
    """
    if MAC:
        return False
    return werkzeug_pfad("ffmpeg") is None


def ffmpeg_anleitung():
    """Anleitung zum Nachlesen - Bildschirm und Datei nutzen denselben Text."""
    if WINDOWS:
        schritte = r"""MP3 und M4A brauchen ffmpeg

Das JamMan Solo XT nimmt nur WAV (44,1 kHz, 16 Bit, Stereo).
Zum Umwandeln von MP3, M4A, AAC, FLAC oder OGG braucht dieses
Werkzeug unter Windows das Programm ffmpeg.

Weg 1 - mit Windows-Bordmitteln (empfohlen)

  1. Startmenu -> "Eingabeaufforderung" oder "PowerShell" öffnen
  2. Diesen Befehl eintippen und Enter drücken:

         winget install --id Gyan.FFmpeg

  3. Warten, bis "Successfully installed" erscheint
  4. Das JamMan Card Tool neu starten

Weg 2 - ohne Installation

  1. https://www.gyan.dev/ffmpeg/builds/ aufrufen
  2. "ffmpeg-release-essentials.zip" herunterladen
  3. Entpacken, im Ordner bin liegt ffmpeg.exe
  4. Diese ffmpeg.exe neben jamman.py kopieren (gleicher Ordner)

Weg 3 - von Hand

  ffmpeg.exe nach C:\ffmpeg\bin\ legen - dieser Ort wird auch gefunden,
  ohne dass etwas an PATH geändert werden muss.

Prüfen, ob es geklappt hat

  In der Eingabeaufforderung:   ffmpeg -version
  Oder im Werkzeug:             jamman.py selbsttest
  Dort steht dann eine Zeile "ffmpeg: C:\...\ffmpeg.exe".

Ohne ffmpeg geht weiterhin alles mit WAV-Dateien."""
    else:
        schritte = r"""MP3 und M4A brauchen ffmpeg

Das JamMan Solo XT nimmt nur WAV (44,1 kHz, 16 Bit, Stereo).
Zum Umwandeln anderer Formate braucht das Werkzeug ffmpeg.

  Debian/Ubuntu:   sudo apt install ffmpeg
  Fedora:          sudo dnf install ffmpeg
  Arch:            sudo pacman -S ffmpeg
  openSUSE:        sudo zypper install ffmpeg

Ohne ffmpeg geht weiterhin alles mit WAV-Dateien."""
    return schritte


def ffmpeg_hinweis():
    """Sagt, wie ffmpeg auf diesen Rechner kommt."""
    if WINDOWS:
        return ("  Windows:  winget install --id Gyan.FFmpeg\n"
                "            oder ffmpeg.exe neben jamman.py legen\n")
    if MAC:
        return ("  macOS:    brew install ffmpeg   (meist nicht nötig,\n"
                "            afconvert ist eingebaut)\n")
    return "  Linux:    sudo apt install ffmpeg\n"


def werkzeug_pfad(name):
    """Externes Programm finden: PATH, dann der eigene Ordner, dann übliche Orte.

    Windows-Nutzer legen ffmpeg.exe oft einfach neben das Werkzeug oder nach
    C:\ffmpeg\bin - beides wird gefunden, ohne PATH-Umbau.
    """
    endung = ".exe" if WINDOWS else ""
    treffer = shutil.which(name)
    if treffer:
        return treffer
    ordner = os.path.dirname(os.path.abspath(__file__))
    moeglich = [os.path.join(ordner, name + endung),
                os.path.join(ordner, "ffmpeg", "bin", name + endung)]
    if WINDOWS:
        moeglich += [os.path.join(os.environ.get("LOCALAPPDATA", ""),
                                  "Microsoft", "WinGet", "Links", name + ".exe"),
                     "C:\\ffmpeg\\bin\\" + name + ".exe",
                     os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"),
                                  "ffmpeg", "bin", name + ".exe")]
    else:
        moeglich += ["/usr/local/bin/" + name, "/opt/homebrew/bin/" + name,
                     "/usr/bin/" + name, "/snap/bin/" + name]
    for pfad in moeglich:
        if os.path.isfile(pfad):
            return pfad
    return None


def neue_tempdatei(prefix="jamman-", suffix=".wav"):
    """Temporäre Datei anlegen und den Handle sofort schließen.

    tempfile.mkstemp gibt einen offenen Handle zurück. Wird er nicht
    geschlossen, lässt Windows die Datei nicht löschen und sperrt sie
    unter Umständen (am 10.11.2026 auf Windows 10 aufgefallen).
    """
    griff, pfad = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(griff)
    return pfad


def wav_kopf(pfad):
    """Liest die Kopfangaben einer WAV-Datei: Format, Kanäle, Rate, Bits,
    Position und Länge der Audiodaten. Gibt None zurück, wenn es keine
    lesbare WAV-Datei ist."""
    import struct
    try:
        f = open(pfad, "rb")
    except OSError:
        return None
    with f:
        kopf = f.read(12)
        if len(kopf) < 12 or kopf[0:4] != b"RIFF" or kopf[8:12] != b"WAVE":
            return None
        while True:
            k = f.read(8)
            if len(k) < 8:
                return None
            name = k[0:4]
            groesse = struct.unpack("<I", k[4:8])[0]
            if name == b"fmt ":
                fmt = f.read(groesse)
                if len(fmt) < 16:
                    return None
                format_, kanaele, rate, _, block, bits = struct.unpack("<HHIIHH", fmt[0:16])
                return {"format": format_, "kanaele": kanaele, "rate": rate,
                        "bits": bits, "block": block, "fmt_groesse": groesse,
                        "daten_start": None, "daten_laenge": None}
            if name == b"data":
                stelle = f.tell()
                return {"format": None, "daten_start": stelle, "daten_laenge": groesse}
            f.seek(groesse + (groesse % 2), 1)      # gerade ausrichten
    return None


def normalisiere_wav(pfad):
    """Schreibt den WAV-Kopf auf einfaches PCM um (Format 1, 16-Byte-fmt).

    Grund: afconvert erzeugt bei manchen Quellen (z. B. M4A mit
    Kanallayout) WAVE_FORMAT_EXTENSIBLE (0xFFFE, 40-Byte-fmt). Der JamMan
    erkennt solche Patches nicht - am 10.11.2026 bei Patch 29 gesehen.
    Die Audiodaten bleiben unverändert, nur der Kopf wird neu geschrieben.
    Gibt True zurück, wenn umgeschrieben wurde.
    """
    import struct
    angaben = wav_kopf(pfad)
    if not angaben or angaben.get("format") is None:
        return False
    if angaben["format"] == 1 and angaben["fmt_groesse"] == 16:
        return False                                   # schon in Ordnung

    # Datenbereich suchen (fmt kann vor data stehen, muss aber nicht)
    with open(pfad, "rb") as f:
        f.seek(12)
        start = laenge = None
        fmt = None
        while True:
            k = f.read(8)
            if len(k) < 8:
                break
            name, groesse = k[0:4], struct.unpack("<I", k[4:8])[0]
            if name == b"fmt ":
                fmt = f.read(groesse)
            elif name == b"data":
                start, laenge = f.tell(), groesse
                break
            else:
                f.seek(groesse + (groesse % 2), 1)
        if fmt is None or start is None:
            return False
        format_, kanaele, rate, _, block, bits = struct.unpack("<HHIIHH", fmt[0:16])
        if format_ not in (1, 0xFFFE):
            print(T("  Hinweis: unbekanntes WAV-Format 0x%04X - nicht angefasst.") % format_)
            return False

    zwischendatei = pfad + ".pcm"
    with open(pfad, "rb") as quelle, open(zwischendatei, "wb") as ziel:
        datenlaenge = min(laenge, os.path.getsize(pfad) - start)
        kopf = b"RIFF" + struct.pack("<I", 36 + datenlaenge) + b"WAVE"
        kopf += b"fmt " + struct.pack("<IHHIIHH", 16, 1, kanaele, rate,
                                     rate * block, block, bits)
        kopf += b"data" + struct.pack("<I", datenlaenge)
        ziel.write(kopf)
        quelle.seek(start)
        rest = datenlaenge
        while rest > 0:
            stueck = quelle.read(min(1 << 20, rest))
            if not stueck:
                break
            ziel.write(stueck)
            rest -= len(stueck)
    os.replace(zwischendatei, pfad)
    return True


def kuerze_wav(pfad, sekunden, blende=0.012):
    """Schneidet eine WAV-Datei nach der angegebenen Länge ab.

    "Übergangslos" heißt hier: an der Schnittstelle wird nichts eingefügt,
    keine Pause, kein neuer Ton. Damit es dabei nicht knackt, werden die
    letzten Millisekunden kurz ausgeblendet (Vorgabe 12 ms) - das ist
    kürzer als ein Wimpernschlag und hört sich als glatter Übergang an.
    Der WAV-Kopf wird dabei gleich mit auf einfaches PCM gesetzt.
    """
    angaben = wav_kopf(pfad)
    if not angaben or angaben.get("format") is None:
        return False
    # Datenbereich finden
    with open(pfad, "rb") as f:
        f.seek(12)
        fmt = None
        start = laenge = None
        while True:
            k = f.read(8)
            if len(k) < 8:
                break
            name, groesse = k[0:4], struct.unpack("<I", k[4:8])[0]
            if name == b"fmt ":
                fmt = f.read(groesse)
            elif name == b"data":
                start, laenge = f.tell(), groesse
                break
            else:
                f.seek(groesse + (groesse % 2), 1)
        if fmt is None or start is None:
            return False
        format_, kanaele, rate, _, block, bits = struct.unpack("<HHIIHH", fmt[0:16])
        if format_ not in (1, 0xFFFE) or bits != 16:
            return False
        max_bytes = int(sekunden * rate) * block
        behalten = min(laenge, max_bytes)
        behalten -= behalten % block
        quelle_daten = f.read(behalten)

    import array
    werte = array.array("h")
    werte.frombytes(quelle_daten)
    blende_frames = int(rate * blende) * kanaele
    n = min(blende_frames, len(werte))
    for i in range(n):
        faktor = (n - i) / float(n)
        stelle = len(werte) - n + i
        werte[stelle] = int(werte[stelle] * faktor)

    zwischendatei = pfad + ".kurz"
    with open(zwischendatei, "wb") as ziel:
        daten = werte.tobytes()
        kopf = b"RIFF" + struct.pack("<I", 36 + len(daten)) + b"WAVE"
        kopf += b"fmt " + struct.pack("<IHHIIHH", 16, 1, kanaele, rate,
                                     rate * block, block, 16)
        kopf += b"data" + struct.pack("<I", len(daten))
        ziel.write(kopf)
        ziel.write(daten)
    os.replace(zwischendatei, pfad)
    return True


def wandle_nach_44k(quelle, ziel):
    """Auf 44,1 kHz, 16 Bit, Stereo bringen. Reihenfolge:
    afconvert (nur Mac) -> ffmpeg -> reines Python über das wave-Modul."""
    if MAC:
        r = subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@44100",
                            "-c", "2", quelle, ziel], capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(ziel):
            return True
    ffmpeg = werkzeug_pfad("ffmpeg")
    if ffmpeg:
        r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                            "-i", quelle, "-ar", "44100", "-ac", "2",
                            "-c:a", "pcm_s16le", ziel], capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(ziel):
            return True
    # Letzter Weg: selbst umrechnen (nur WAV, nur Kanäle/Dopplung)
    try:
        import wave as _wave
        with _wave.open(quelle, "rb") as q:
            kanaele, breite, rate = q.getnchannels(), q.getsampwidth(), q.getframerate()
            rahmen = q.readframes(q.getnframes())
        if rate != 44100 or breite != 2:
            return False          # ohne Bibliothek nicht sauber möglich
        if kanaele == 1:
            import audioop
            rahmen = audioop.tostereo(rahmen, 2, 1, 1)
        elif kanaele > 2:
            import audioop
            rahmen = audioop.tomono(rahmen, 2, 0.5, 0.5)
        with _wave.open(ziel, "wb") as z:
            z.setnchannels(2); z.setsampwidth(2); z.setframerate(44100)
            z.writeframes(rahmen)
        return True
    except Exception:
        return False


def einhaengepunkt(pfad):
    """Einhängepunkt des Datenträgers, in dem `pfad` liegt.

    Ohne fremde Programme: vom Ordner nach oben gehen, bis sich die
    Gerätekennung (st_dev) ändert. Leerer Rückgabewert heißt: der Pfad ist
    nicht (mehr) eingehängt — dann gibt es nichts auszuwerfen.
    Anlaß (17.11.2026): Bei Karte im Pedal stand hier „/media", und der
    Auswurfversuch scheiterte mit einer langen Fehlerkette.
    """
    try:
        pfad = os.path.abspath(os.path.expanduser(str(pfad)))
        if not os.path.exists(pfad):
            return ""
        kennung = os.stat(pfad).st_dev
    except OSError:
        return ""
    aktuell = pfad
    while True:
        eltern = os.path.dirname(aktuell)
        if eltern == aktuell:
            return ""
        try:
            if os.stat(eltern).st_dev != kennung:
                return aktuell
        except OSError:
            return aktuell
        aktuell = eltern


def auswerfen_datentraeger(karte):
    """Karte auswerfen. Gibt (erfolg, meldung) zurück."""
    wurzel = volumen_pfad(karte)
    if not os.path.isdir(wurzel):
        return False, "ist nicht eingelegt"
    if MAC:
        r = subprocess.run(["diskutil", "eject", wurzel], capture_output=True, text=True)
        return (r.returncode == 0,
                "Karte ausgeworfen." if r.returncode == 0
                else (r.stderr.strip() or r.stdout.strip()))
    if not (MAC or WINDOWS):
        # Linux, drei Wege von „braucht kein Recht" nach „braucht root".
        # Am 17.11.2026 fragte udisksctl nach einem Paßwort und scheiterte
        # danach — deshalb zuerst gio (läuft in der Benutzersitzung) und
        # udisksctl nur ohne Rückfrage.
        punkt = einhaengepunkt(karte)
        if not punkt:
            return False, T("Die Karte hängt nicht im Rechner — nichts "
                            "auszuwerfen. Steckt sie im Pedal, führt das "
                            "Pedal sie nicht als Laufwerk.")

        def lauf(befehl):
            """Befehl ausführen; ein fehlendes Programm ist kein Absturz."""
            try:
                return subprocess.run(befehl, capture_output=True, text=True)
            except FileNotFoundError:
                class _Fehlt:
                    returncode = 127
                    stdout = ""
                    stderr = "%s ist nicht vorhanden" % befehl[0]
                return _Fehlt()

        fehler = []
        if shutil.which("gio"):
            r = lauf(["gio", "mount", "-u", punkt])
            if r.returncode == 0:
                return True, "Karte abgemeldet."
            fehler.append("gio: " + (r.stderr.strip().splitlines() or ["unbekannt"])[0])

        r = lauf(["findmnt", "-n", "-o", "SOURCE", "--target", punkt])
        geraet = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
        if geraet and shutil.which("udisksctl"):
            r = lauf(["udisksctl", "unmount", "-b", geraet,
                      "--no-user-interaction"])
            if r.returncode == 0:
                lauf(["udisksctl", "power-off", "-b", geraet,
                      "--no-user-interaction"])
                return True, "Karte abgemeldet."
            fehler.append("udisksctl: " + (r.stderr.strip().splitlines() or ["unbekannt"])[0])

        r = lauf(["umount", punkt])
        if r.returncode == 0:
            return True, "Karte abgemeldet."
        fehler.append("umount: " + (r.stderr.strip().splitlines() or ["unbekannt"])[0])

        # Nur den ersten Grund nennen — die ganze Kette hilft niemandem
        return False, (T("Auswerfen ging nicht. Bitte im Dateimanager auf "
                         "„Auswerfen\" klicken.") + "\n\n" + fehler[0])

    if WINDOWS:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "(New-Object -ComObject Shell.Application).Namespace(17)"
                            ".ParseName('%s').InvokeVerb('Eject')" % wurzel.rstrip("\\")],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return True, "Karte ausgeworfen."
        r2 = subprocess.run(["mountvol", wurzel.rstrip("\\"), "/P"],
                            capture_output=True, text=True)
        if r2.returncode == 0:
            return True, "Laufwerk abgemeldet. Karte kann abgezogen werden."
        return False, (r2.stderr.strip() or r.stderr.strip() or "unbekannter Fehler")
    return False, "Auswerfen wird auf diesem System nicht unterstützt"

UNTERORDNER = "JamManSoloXT"
# Ein JamMan legt auf der Karte einen Ordner mit seinem Gerätenamen an.
# Solo XT und Stereo sind darunter gleich aufgebaut (Patch01 … / PhraseA /
# patch.xml). Belegt am 12.11.2026: ein Nutzer im Bassic-Forum hat den
# Stereo erreicht, indem er im Skript „SoloXT" durch „Stereo" ersetzte.
KARTENORDNER = ("JamManSoloXT", "JamManStereo")


def schrift(groesse, fest=False, titel=False):
    """Passende Schrift je System.

    Menlo gibt es nur auf dem Mac; Windows und Linux brauchen eigene
    Namen, sonst wählt Tk irgendetwas und die Spalten passen nicht.
    """
    if MAC:
        name = "Helvetica" if titel else "Menlo"
    elif WINDOWS:
        name = "Segoe UI" if titel else "Consolas"
    else:
        name = "DejaVu Sans" if titel else "DejaVu Sans Mono"
    return (name, groesse, "bold") if fest else (name, groesse)


# ---------------------------------------------------------------- Grundlagen

def kartenordner_in(p):
    """Name des JamMan-Ordners in p, oder None. Groß/klein ist egal."""
    try:
        eintraege = os.listdir(p)
    except OSError:
        return None
    for wunsch in KARTENORDNER:
        for e in eintraege:
            if e.lower() == wunsch.lower() and os.path.isdir(os.path.join(p, e)):
                return e
    return None


def karte_pruefen(pfad):
    """Gibt den Ordner des Geräts zurück oder bricht mit klarer Meldung ab.

    Der Ordner heißt je nach Gerät "JamManSoloXT" oder "JamManStereo";
    darunter ist der Aufbau gleich. Welcher gefunden wurde, steht danach
    in UNTERORDNER - alle späteren Pfade bauen darauf auf.
    """
    global UNTERORDNER
    p = os.path.abspath(os.path.expanduser(pfad))
    if not os.path.isdir(p):
        raise SystemExit(T("Fehler: '%s' ist kein Ordner. Ist die Karte eingelegt?") % pfad)
    # Wurde schon der Geräteordner selbst gewählt? Dann direkt nehmen.
    for wunsch in KARTENORDNER:
        if os.path.basename(p).lower() == wunsch.lower():
            UNTERORDNER = os.path.basename(p)
            return p
    gefunden = kartenordner_in(p)
    if gefunden:
        UNTERORDNER = gefunden
        return os.path.join(p, gefunden)
    raise SystemExit(T("Fehler: auf '%s' liegt kein Ordner '%s' oder '%s'. "
                         "Das ist keine JamMan-Karte.") % (p, KARTENORDNER[0], KARTENORDNER[1]))


def ist_ordner(pfad):
    """Ist das ein Ordner? Mit Rückfallweg.

    Ubuntu Studio meldete am 10.11.2026 für die Patch-Ordner einer
    JamMan-Karte "Datei" - os.path.isdir sah also kein Verzeichnis,
    obwohl eines da ist. Deshalb zusätzlich an den Dateien erkennen,
    die in einem Patch liegen müssen.
    """
    try:
        return stat.S_ISDIR(os.stat(pfad).st_mode)
    except OSError:
        pass
    for probe in ("patch.xml", "PhraseA/phrase.xml", "PhraseA/phrase.wav"):
        if os.path.exists(os.path.join(pfad, *probe.split("/"))):
            return True
    return False


def karte_lesefehler(basis):
    """Zählt Einträge, deren Zustand das System nicht lesen kann.

    Am 10.11.2026 auf Ubuntu Studio: die Karte war eingehängt, das
    Verzeichnis ließ sich auflisten, aber jedes stat() auf die
    Patch-Ordner endete mit "Input/output error". Das ist ein Fehler des
    Dateisystems oder der Karte - nicht des Werkzeugs.
    """
    fehler = []
    try:
        namen = os.listdir(basis)
    except OSError as e:
        return ["(Verzeichnis nicht lesbar: %s)" % e.strerror]
    for name in namen:
        if not re.fullmatch(r"Patch\d+", name, re.IGNORECASE):
            continue
        try:
            os.stat(os.path.join(basis, name))
        except OSError as e:
            fehler.append("%s: %s" % (name, e.strerror))
    return fehler


def patches(basis):
    """Alle Patch-Ordner, numerisch sortiert."""
    gefunden = []
    for name in os.listdir(basis):
        if not re.fullmatch(r"Patch\d+", name, re.IGNORECASE):
            continue
        pfad = os.path.join(basis, name)
        if not ist_ordner(pfad):
            continue
        nummer = int(name[5:])
        gefunden.append((nummer, name, pfad))
    return sorted(gefunden)


def patch_name(pfad):
    """<PatchName> aus patch.xml, sonst der Ordnername."""
    q = os.path.join(pfad, "patch.xml")
    if not os.path.exists(q):
        return "(ohne patch.xml)"
    try:
        baum = ET.parse(q)
        for el in baum.iter():
            if el.tag.endswith("PatchName"):
                return (el.text or "").strip() or "(ohne Namen)"
    except ET.ParseError as e:
        return "(patch.xml nicht lesbar: %s)" % e
    return "(ohne Namen)"


def patch_beschreibung(pfad):
    """Die Beschreibungsfelder eines Patches lesen.

    Rückgabe: dict mit name, artist, genre, rating, tags, description, bpm.
    Was fehlt, kommt als leerer Text zurück; bpm als Zahl oder None.
    """
    werte = {"name": "", "artist": "", "genre": "", "rating": "",
             "tags": "", "description": "", "bpm": None}
    text, ptext = "", ""
    try:
        text = open(os.path.join(pfad, "patch.xml"), encoding="utf-8",
                    errors="replace").read()
    except OSError:
        pass
    try:
        ptext = open(os.path.join(pfad, "PhraseA", "phrase.xml"),
                     encoding="utf-8", errors="replace").read()
    except OSError:
        pass
    for feld, tag in (("name", "PatchName"), ("artist", "Artist"),
                      ("genre", "Genre"), ("rating", "Rating"),
                      ("tags", "Tags"), ("description", "Description")):
        m = re.search(r"<%s>([^<]*)</%s>" % (tag, tag), text)
        if m:
            werte[feld] = xml_entpacken(m.group(1).strip())
    m = re.search(r"<BeatsPerMinute>([^<]*)</BeatsPerMinute>", ptext)
    if m:
        try:
            werte["bpm"] = float(m.group(1))
        except ValueError:
            pass
    return werte


def xml_entpacken(text):
    """&amp; → & usw. (die XML-Dateien des Pedals kodieren Sonderzeichen)."""
    return (text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&apos;", "'").replace("&amp;", "&"))


def patch_beschreibung_setzen(pfad, werte):
    """Beschreibungsfelder schreiben; was nicht in werte steht, bleibt.

    Rückgabe: Felder, die es im XML noch nicht gab (dann wurden sie
    eingefügt).
    """
    def kodiert(text):
        return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    px = os.path.join(pfad, "patch.xml")
    ph = os.path.join(pfad, "PhraseA", "phrase.xml")
    if not os.path.exists(px):
        raise RuntimeError(T("Die Datei %s fehlt.") % px)

    fehlend = []
    text = open(px, encoding="utf-8", errors="replace").read()
    for feld, tag in (("name", "PatchName"), ("artist", "Artist"),
                      ("genre", "Genre"), ("rating", "Rating"),
                      ("tags", "Tags"), ("description", "Description")):
        if feld not in werte:
            continue
        wert = kodiert(werte[feld])
        # Leere Felder stehen im Pedal-XML als <Description /> — das wird
        # zuerst ersetzt, sonst bliebe der alte Inhalt stehen.
        leer = r"<%s\s*/>" % tag
        if re.search(leer, text):
            text = re.sub(leer, "<%s>%s</%s>" % (tag, wert, tag), text, count=1)
            continue
        text, da = setze_wert(text, tag, wert)
        if not da:
            fehlend.append(tag)
    open(px, "w", encoding="utf-8").write(text)

    if werte.get("bpm") is not None and os.path.exists(ph):
        ptext = open(ph, encoding="utf-8", errors="replace").read()
        ptext, da = setze_wert(ptext, "BeatsPerMinute",
                               "%.10f" % float(werte["bpm"]))
        if not da:
            fehlend.append("BeatsPerMinute")
        ptext, _ = setze_wert(ptext, "BpmValidated", "1")
        open(ph, "w", encoding="utf-8").write(ptext)
    return fehlend


def patch_einstellungen(pfad):
    """Stoppart, Rhythmus, Wiedergabe, rückwärts und Tempo eines Patches.

    Rückgabe: (stop, rhy, loop, rueck, bpm) — rueck ist True/False, bpm eine
    Zahl oder None. Am 18.11.2026 ergänzt: rückwärts (IsReversed) und Tempo
    wurden gelesen, aber nirgends gezeigt.
    """
    stop, rhy, loop = "?", "?", "?"
    rueck, bpm = False, None
    px = os.path.join(pfad, "patch.xml")
    ph = os.path.join(pfad, "PhraseA", "phrase.xml")
    try:
        t = open(px, encoding="utf-8", errors="replace").read()
        m = re.search(r"<StopMode>([^<]*)", t)
        if m:
            stop = {"FadeOut": "fade", "StopInstantly": "sofort",
                    "StopAtEndOfLoop": "ende"}.get(m.group(1), m.group(1))
        m = re.search(r"<RhythmType>([^<]*)", t)
        if m:
            rhy = {"Silence": "ohne", "StudioKickAndHighHat": "klick"}.get(
                m.group(1), m.group(1))
    except OSError:
        pass
    try:
        t = open(ph, encoding="utf-8", errors="replace").read()
        m = re.search(r"<IsLoop>([^<]*)", t)
        if m:
            loop = "loop" if m.group(1) == "1" else "single"
        m = re.search(r"<IsReversed>([^<]*)", t)
        if m:
            rueck = m.group(1).strip() == "1"
        m = re.search(r"<BeatsPerMinute>([^<]*)", t)
        if m:
            try:
                bpm = float(m.group(1))
            except ValueError:
                bpm = None
    except OSError:
        pass
    return stop, rhy, loop, rueck, bpm


def wav_rueckwaerts(pfad, ziel=None):
    """Eine WAV rückwärts schreiben und den Pfad zurückgeben.

    Das Pedal spielt rückwärts, weil in seiner phrase.xml IsReversed steht.
    Das Fenster spielte die Datei selbst ab — dort hörte man davon nichts
    (Rückmeldung 18.11.2026). Mit ffmpeg (areverse) entsteht eine Hörprobe;
    ohne ffmpeg bleibt es beim Original.
    """
    import tempfile

    if not werkzeug_pfad("ffmpeg"):
        return None
    ziel = ziel or os.path.join(tempfile.mkdtemp(), "rueckwaerts.wav")
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", pfad,
                        "-af", "areverse", ziel],
                       capture_output=True)
    if r.returncode != 0 or not os.path.exists(ziel):
        return None
    return ziel


def wav_info(pfad):
    """Dauer, Rate, Kanäle, Bittiefe.

    Erst mit dem eingebauten wave-Modul (geht ohne ffmpeg, also auch auf
    einem Mac ohne Homebrew). Nur wenn das scheitert, wird ffprobe gefragt.
    """
    try:
        import wave as _wave
        with _wave.open(pfad, "rb") as w:
            rate = w.getframerate()
            kanaele = w.getnchannels()
            bits = w.getsampwidth() * 8
            dauer = w.getnframes() / float(rate) if rate else 0.0
        return (dauer, rate, kanaele, bits)
    except Exception:
        pass
    werte = {}
    if werkzeug_pfad("ffprobe"):         # ohne ffmpeg gibt es ffprobe nicht
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "stream=sample_rate,channels,bits_per_sample:format=duration",
                            "-of", "default=noprint_wrappers=1", pfad],
                           capture_output=True, text=True)
        for zeile in r.stdout.splitlines():
            if "=" in zeile:
                k, v = zeile.split("=", 1)
                werte[k] = v
    try:
        dauer = float(werte.get("duration", 0))
        rate = int(werte.get("sample_rate", 0))
        kanaele = int(werte.get("channels", 0))
        bits = int(werte.get("bits_per_sample", 0) or 0)
        if dauer > 0:
            return (dauer, rate, kanaele, bits)
    except ValueError:
        pass

    # Ohne ffprobe (z. B. Mac ohne Homebrew): afinfo fragen.
    if MAC and shutil.which("afinfo"):
        r = subprocess.run(["afinfo", pfad], capture_output=True, text=True)
        dauer = rate = kanaele = 0
        m = re.search(r"estimated duration: ([0-9.]+) sec", r.stdout)
        if m:
            dauer = float(m.group(1))
        m = re.search(r"Data format:\s*\d+ ch, (\d+) Hz", r.stdout)
        if m:
            kanaele = int(m.group(1)); rate = int(m.group(2))
        m = re.search(r"(\d+)-bit", r.stdout)
        bits = int(m.group(1)) if m else 0
        return (dauer, rate, kanaele, bits)
    return (0.0, 0, 0, 0)


def phrase_wav(patch_pfad):
    q = os.path.join(patch_pfad, "PhraseA", "phrase.wav")
    return q if os.path.exists(q) else None


def sekunden(s):
    return "%d:%04.1f" % (int(s) // 60, s % 60)


# ------------------------------------------------------------------ Befehle


def saeubere_karte(basis):
    """Entfernt ._* und .DS_Store. macOS legt sie beim Kopieren an;
    der JamMan kann daran scheitern."""
    entfernt = 0
    for w, d, ds in os.walk(os.path.dirname(basis)):
        for x in list(ds):
            if x.startswith("._") or x == ".DS_Store":
                try:
                    os.remove(os.path.join(w, x))
                    entfernt += 1
                except OSError:
                    pass
    if entfernt:
        print(T("  %d Apple-Dateien entfernt (._* und .DS_Store).") % entfernt)


def befehl_liste(basis):
    alle = patches(basis)
    if not alle:
        print(T("  Keine Patches auf der Karte."))
        return
    print(T("  %-10s %-34s %8s  %s") % (T("Ordner"), T("Name"), T("Dauer"), T("WAV-Format")))
    for nummer, name, pfad in alle:
        w = phrase_wav(pfad)
        if w:
            dauer, rate, kanaele, bits = wav_info(w)
            format_text = T("%d Hz / %d Bit / %d K") % (rate, bits, kanaele)
            ok = (rate, bits, kanaele) == WOLL
            print(T("  %-10s %-34s %8s  %s%s%s") % (
                name, patch_name(pfad)[:34], sekunden(dauer), format_text,
                "" if ok else T("   ← abweichend"),
                T("   ← über 10 min, Pedal nimmt sie nicht an")
                if dauer > MAX_LOOP_SEKUNDEN else ""))
        else:
            print(T("  %-10s %-34s %8s  %s") % (name, patch_name(pfad)[:34], "—", T("keine WAV")))
    print(T("\n  %d Patches, %s belegt.") % (len(alle),
          sum(1 for _, _, p in alle if phrase_wav(p))))


EINSTELLUNG_DATEI = "~/.jamman-werkzeug.cnf"


def einstellung_lesen(name, vorgabe=""):
    """Kleine Einstellungsdatei im Benutzerordner (geht auf allen Systemen)."""
    pfad = os.path.expanduser(EINSTELLUNG_DATEI)
    try:
        with open(pfad, encoding="utf-8") as f:
            for zeile in f:
                if "=" in zeile:
                    schluessel, wert = zeile.split("=", 1)
                    if schluessel.strip() == name:
                        return wert.strip()
    except OSError:
        pass
    return vorgabe


def einstellung_schreiben(name, wert):
    pfad = os.path.expanduser(EINSTELLUNG_DATEI)
    werte = {}
    try:
        with open(pfad, encoding="utf-8") as f:
            for zeile in f:
                if "=" in zeile:
                    schluessel, alt = zeile.split("=", 1)
                    werte[schluessel.strip()] = alt.strip()
    except OSError:
        pass
    werte[name] = str(wert)
    with open(pfad, "w", encoding="utf-8") as f:
        for schluessel in sorted(werte):
            f.write("%s=%s\n" % (schluessel, werte[schluessel]))


def karten_stand(basis):
    """Kennzahl der Karte: Anzahl Patches, Summe der Größen, jüngste Änderung.

    Damit lässt sich erkennen, ob sich seit der letzten Sicherung etwas
    geändert hat - dann muss nicht bei jedem Start kopiert werden.
    """
    anzahl = 0
    groesse = 0
    juengste = 0.0
    for wurzel, ordner, dateien in os.walk(basis):
        for datei in dateien:
            if datei.startswith("._") or datei == ".DS_Store":
                continue
            pfad = os.path.join(wurzel, datei)
            try:
                st = os.stat(pfad)
            except OSError:
                continue
            anzahl += 1
            groesse += st.st_size
            juengste = max(juengste, st.st_mtime)
    return "%d-%d-%d" % (anzahl, groesse, int(juengste))


def beim_start_sichern(basis, behalten=3, still=False):
    """Sichert die Karte beim Start - aber nur, wenn sie sich geändert hat.

    Anlass 10.11.2026: Die Karte hatte nach einem Schreibvorgang ihre
    Einträge verloren; nur die Sicherung hat sie gerettet. Deshalb auf
    Wunsch eine Sicherung beim Start, ohne dass bei jedem Start 1,2 GB
    kopiert werden.
    """
    if not basis:
        return None
    try:
        basis = karte_pruefen(basis)      # immer der Ordner JamManSoloXT
    except SystemExit:
        return None
    stand = karten_stand(basis)
    vorher = einstellung_lesen("letzter_stand")
    if stand == vorher:
        if not still:
            print(T("  Sicherung beim Start: Karte unverändert, nichts zu tun."))
        return None
    ziel = os.path.expanduser(SICHERUNG_WURZEL)
    stempel = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    ordner = os.path.join(ziel, "Start_" + stempel)
    os.makedirs(ordner, exist_ok=True)
    print(T("  Sicherung beim Start: %s") % ordner)
    shutil.copytree(basis, os.path.join(ordner, UNTERORDNER), dirs_exist_ok=True)
    anzahl = len(patches(basis))
    groesse = sum(os.path.getsize(os.path.join(w, x))
                  for w, d, ds in os.walk(ordner) for x in ds)
    print(T("    gesichert: %d Patches, %.0f MB") % (anzahl, groesse / 1024 / 1024))
    einstellung_schreiben("letzter_stand", stand)
    # Alte Start-Sicherungen aufräumen (nur die eigenen)
    alte = sorted(d for d in os.listdir(ziel) if d.startswith("Start_"))
    for d in alte[:-behalten] if len(alte) > behalten else []:
        try:
            shutil.rmtree(os.path.join(ziel, d))
            print(T("    alte Sicherung entfernt: %s") % d)
        except OSError:
            pass
    return ordner


def befehl_sichern(basis, ziel, schreiben):
    ziel = os.path.abspath(os.path.expanduser(ziel))
    stempel = datetime.now().strftime("%Y-%m-%d_%H-%M")
    zielordner = os.path.join(ziel, "JamManSicherung_" + stempel)
    print(T("  Quelle: %s") % basis)
    print(T("  Ziel:   %s") % zielordner)
    if not schreiben:
        print(T("  Trockenlauf — nichts kopiert. Mit --schreiben ausführen."))
        return
    os.makedirs(zielordner, exist_ok=True)
    shutil.copytree(basis, os.path.join(zielordner, UNTERORDNER), dirs_exist_ok=True)
    anzahl = len(patches(basis))
    groesse = sum(os.path.getsize(os.path.join(w, x))
                  for w, d, ds in os.walk(zielordner) for x in ds)
    print(T("  Gesichert: %d Patches, %.0f MB.") % (anzahl, groesse / 1024 / 1024))


def naechste_nummer(basis):
    """Erste freie Platznummer, im Format der vorhandenen Namen.

    Am 26.10.2026 wurde hier die hoechste Nummer plus eins genommen und die
    Breite aus dem laengsten Namen. Weil ein Patch "Patch200" heisst, bekam
    ein neuer Patch die Nummer 201 statt der freien 25 - und stand damit
    nicht dort, wo der Nutzer ihn erwartet.
    """
    from collections import Counter
    alle = patches(basis)
    benutzt = set(n for n, _, _ in alle)
    breiten = Counter(len(name) - 5 for _, name, _ in alle)
    breite = breiten.most_common(1)[0][0] if breiten else 2
    frei = 1
    while frei in benutzt:
        frei += 1
    return "%0*d" % (breite, frei)


def vorlage_finden(basis):
    """Ein vollständiger Patch dient als Vorlage für patch.xml und phrase.xml."""
    for nummer, name, pfad in patches(basis):
        if os.path.exists(os.path.join(pfad, "patch.xml")) and \
           os.path.exists(os.path.join(pfad, "PhraseA", "phrase.xml")):
            return pfad
    return None


def wiedergabe_rueckwaerts(wiedergabe):
    """Soll rückwärts gespielt werden? (IsReversed im phrase.xml)"""
    return "1" if wiedergabe and ("↺" in wiedergabe
                            or "rückwärts" in wiedergabe) else "0"


def setze_wert(text, tag, wert):
    """Ersetzt <tag>…</tag>. Fehlt das Feld in der Vorlage, wird es
    eingefügt - sonst bliebe die gewünschte Einstellung wirkungslos."""
    muster = r"<%s>.*?</%s>" % (tag, tag)
    if re.search(muster, text, flags=re.S):
        return re.sub(muster, "<%s>%s</%s>" % (tag, wert, tag),
                      text, count=1, flags=re.S), True
    neu = re.sub(r"</JamManPatch>",
                 "  <%s>%s</%s>\n</JamManPatch>" % (tag, wert, tag),
                 text, count=1)
    return neu, False


def wellenform(pfad, breite=900):
    """Hüllkurve für die Anzeige: je Bildspalte der größte Ausschlag.

    Liefert eine Liste von Werten zwischen 0 und 1, dazu die Dauer und
    den Spitzenwert der Datei.

    Wichtig für das Tempo: max() und min() auf einem array-Abschnitt
    laufen in C. Die erste Fassung baute eine Python-Liste aller
    Einzelwerte und brauchte für 75 MB über zwei Sekunden - das Fenster
    stand so lange (am 10.11.2026 als Trägheit mit Beachball bemerkt).
    """
    import array
    with wave.open(pfad, "rb") as w:
        kanaele, breite_daten, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        gesamt = w.getnframes()
        if not gesamt or breite_daten != 2:
            return [], 0.0, 0
        dauer = gesamt / float(rate)
        # Stichproben statt der ganzen Datei: je Bildspalte ein kurzes
        # Fenster lesen. 900 Sprünge in einer unkomprimierten WAV sind
        # billiger als 150 MB am Stück zu lesen und zu durchsuchen.
        if gesamt > 500000:
            fenster = max(kanaele, int(rate * 0.02) * kanaele)   # 20 ms
            werte = []
            spitze = 1
            for i in range(breite):
                pos = int(gesamt * i / float(breite))
                w.setpos(min(pos, max(0, gesamt - 1)))
                roh = w.readframes(min(fenster // kanaele, gesamt - w.tell()))
                if not roh:
                    werte.append(0.0)
                    continue
                a = array.array("h"); a.frombytes(roh)
                m = max(max(a), -min(a))
                spitze = max(spitze, m)
                werte.append(m)
            return [x / float(spitze) for x in werte], dauer, spitze

        roh = w.readframes(gesamt)
    a = array.array("h"); a.frombytes(roh)
    if not len(a):
        return [], 0.0, 0
    pro_spalte = max(1, len(a) // breite)
    spitze = max(max(a), -min(a)) or 1
    werte = []
    for i in range(0, len(a) - pro_spalte, pro_spalte):
        teil = a[i:i + pro_spalte]
        werte.append(max(max(teil), -min(teil)) / float(spitze))
    return werte, dauer, spitze


def verbinde_patches(pfade, ziel):
    """Hängt mehrere Aufnahmen lückenlos aneinander.

    Notwendig für nahtloses Weiterspielen: startet man für jeden Patch
    einen eigenen Spieler, entsteht dazwischen eine Pause (Prozessstart
    und Abfrage-Rhythmus). Eine einzige Datei hat keine Naht.
    Gibt die Dauer jedes Teils zurück.
    """
    import array
    dauern = []
    daten = []
    kanaele = rate = None
    for pfad in pfade:
        with wave.open(pfad, "rb") as w:
            k, b, rt = w.getnchannels(), w.getsampwidth(), w.getframerate()
            if b != 2 or k != 2 or rt != 44100:
                return None, []
            if kanaele is None:
                kanaele, rate = k, rt
            roh = w.readframes(w.getnframes())
        a = array.array("h"); a.frombytes(roh)
        daten.append(a)
        dauern.append(len(a) / float(kanaele * rate))
    gesamt = array.array("h")
    for teil in daten:
        gesamt.extend(teil)
    with open(ziel, "wb") as f:
        inhalt = gesamt.tobytes()
        kopf = b"RIFF" + struct.pack("<I", 36 + len(inhalt)) + b"WAVE"
        kopf += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, rate,
                                     rate * 4, 4, 16)
        kopf += b"data" + struct.pack("<I", len(inhalt))
        f.write(kopf); f.write(inhalt)
    return dauern, daten


def abspielen_ab(pfad, von=0.0, bis=None):
    """Spielt eine Aufnahme ab einem Zeitpunkt.

    afplay kann nicht springen, deshalb wird bei Bedarf ein Ausschnitt in
    eine Zwischendatei geschrieben und die abgespielt. Gibt
    (Prozess, Zwischendatei oder None, Startzeit) zurück.
    """
    von = max(0.0, float(von or 0))
    if von <= 0.01 and bis is None:
        return abspielen_datei(pfad), None, 0.0
    dauer = wav_info(pfad)[0]
    bis = dauer if bis is None else min(bis, dauer)
    zwischer = neue_tempdatei("jamman-ab-", ".wav")
    if not schneide_bereich(pfad, zwischer, von, bis):
        os.unlink(zwischer)
        return abspielen_datei(pfad), None, 0.0
    return abspielen_datei(zwischer), zwischer, von


def finde_schnittpunkte(pfad, anzahl, fenster=90.0, min_pause=0.25):
    """Sucht musikalisch brauchbare Schnittstellen.

    Vorgehen: Hüllkurve in 50-ms-Schritten, mittlerer Pegel bestimmen,
    dann zusammenhängende ruhige Stellen (unter 35 % des Mittels) suchen.
    Geschnitten wird am ENDE einer solchen Pause - dort beginnt der
    nächste musikalische Abschnitt.

    Rückgabe: Liste von Zeitpunkten (Sekunden) und eine Liste der
    Begründungen. Werden nicht genug Pausen gefunden, wird der Zeitpunkt
    rechnerisch gewählt und das gesagt.
    """
    with wave.open(pfad, 'rb') as w:
        kanaele, breite, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        roh = w.readframes(w.getnframes())
    import array
    a = array.array("h"); a.frombytes(roh)
    if breite != 2:
        return [], ["Aufnahme ist kein 16-Bit-WAV"]
    if kanaele == 2:
        mono = [(a[i] + a[i+1]) / 2.0 for i in range(0, len(a) - 1, 2)]
    else:
        mono = [float(x) for x in a]
    dauer = len(mono) / float(rate)

    schritt, laenge = 0.05, 0.10
    h = []
    i = 0
    while i * schritt + laenge < dauer:
        von = int(i * schritt * rate)
        bis = int((i * schritt + laenge) * rate)
        teil = mono[von:bis]
        h.append((i * schritt, math.sqrt(sum(x*x for x in teil) / len(teil))))
        i += 1
    if not h:
        return [], ["Aufnahme zu kurz"]
    peg = sorted(v for _, v in h)
    mittel = peg[len(peg)//2]
    ruhig = [(tm, v) for tm, v in h if v < mittel * 0.35]

    # Gruppen ruhiger Stellen bilden
    gruppen = []
    for tm, v in ruhig:
        if gruppen and tm - gruppen[-1][-1][0] <= schritt * 3:
            gruppen[-1].append((tm, v))
        else:
            gruppen.append([(tm, v)])
    pausen = [(g[0][0], g[-1][0] + laenge) for g in gruppen
              if (g[-1][0] + laenge) - g[0][0] >= min_pause]

    punkte, gruende, paare = [], [], []
    for teil in range(1, anzahl):
        ziel = dauer * teil / float(anzahl)
        nahe = [(abs(((von + bis) / 2.0) - ziel), bis) for von, bis in pausen
                if abs(((von + bis) / 2.0) - ziel) <= fenster]
        if nahe:
            nahe.sort()
            punkte.append(nahe[0][1])
            paare.append(next((p for p in pausen if abs(p[1] - nahe[0][1]) < 1e-6), None))
            gruende.append("Ende einer Pause, %.1f s vom rechnerischen Punkt entfernt"
                           % nahe[0][0])
        else:
            punkte.append(ziel)
            paare.append(None)
            gruende.append("keine Pause in der Nähe - rechnerisch geschnitten")
    return punkte, gruende, paare


def schneide_bereich(quelle, ziel, von_sekunden, bis_sekunden):
    """Kopiert einen Zeitbereich einer WAV-Datei in eine neue Datei.

    Sample-genau: es wird nichts hinzugefügt und nichts weggelassen.
    """
    angaben = wav_kopf(quelle)
    if not angaben or angaben.get("format") is None:
        return False
    with open(quelle, "rb") as f:
        f.seek(12)
        fmt = None
        start = laenge = None
        while True:
            k = f.read(8)
            if len(k) < 8:
                break
            name, groesse = k[0:4], struct.unpack("<I", k[4:8])[0]
            if name == b"fmt ":
                fmt = f.read(groesse)
            elif name == b"data":
                start, laenge = f.tell(), groesse
                break
            else:
                f.seek(groesse + (groesse % 2), 1)
        if fmt is None or start is None:
            return False
        format_, kanaele, rate, _, block, bits = struct.unpack("<HHIIHH", fmt[0:16])
        if format_ not in (1, 0xFFFE) or bits != 16:
            return False
        anfang = int(von_sekunden * rate) * block
        ende = int(bis_sekunden * rate) * block
        anfang -= anfang % block
        ende -= ende % block
        ende = min(ende, laenge)
        if ende <= anfang:
            return False
        f.seek(start + anfang)
        daten = f.read(ende - anfang)
    with open(ziel, "wb") as z:
        kopf = b"RIFF" + struct.pack("<I", 36 + len(daten)) + b"WAVE"
        kopf += b"fmt " + struct.pack("<IHHIIHH", 16, 1, kanaele, rate,
                                     rate * block, block, 16)
        kopf += b"data" + struct.pack("<I", len(daten))
        z.write(kopf); z.write(daten)
    return True


def befehl_teilen(basis, wav, name, schreiben, teile=None, laenge=None,
                  wurzel=None, stopp=None, wiedergabe=None, sinnvoll=True,
                  ohne_pause=True):
    """Eine Datei in mehrere Patches aufteilen.

    Gedacht für Aufnahmen über 10 Minuten, die das Pedal sonst nicht
    annimmt. Die Teile liegen sample-genau aneinander - kein Ton fehlt,
    keiner doppelt.
    """
    dauer, rate, kanaele, bits = wav_info(wav)
    if dauer <= 0:
        raise SystemExit(T("Fehler: '%s' ist keine lesbare Audiodatei.") % wav)
    if laenge:
        anzahl = max(1, int(math.ceil(dauer / float(laenge))))
        stueck = float(laenge)
    elif teile:
        anzahl = max(1, int(teile))
        stueck = dauer / float(anzahl)
    else:
        raise SystemExit(T("Fehler: --teile N oder --laenge SEKUNDEN angeben."))
    if stueck > MAX_LOOP_SEKUNDEN:
        raise SystemExit(T("Fehler: %.1f Minuten pro Teil sind zu lang "
                         "(Grenze %d Minuten).") % (stueck / 60.0, MAX_LOOP_SEKUNDEN //
                                                   60))
    print(T("  Datei:  %s") % os.path.basename(wav))
    print(T("  Dauer:  %s") % sekunden(dauer))
    print(T("  Teile:  %d × etwa %s") % (anzahl, sekunden(stueck)))
    # Zuerst auf 44,1 kHz/16 Bit bringen: die Analyse und das Schneiden
    # arbeiten mit 16-Bit-Daten. Eine 24-Bit-WAV hat hier sonst nichts zu
    # suchen (am 10.11.2026 mit IndexError abgestürzt).
    zwischer = tempfile.mkdtemp(prefix="jamman-teile-")
    ganz = os.path.join(zwischer, "ganz.wav")
    if not wandle_nach_44k(wav, ganz):
        shutil.rmtree(zwischer, ignore_errors=True)
        raise SystemExit(T("Fehler: Datei ließ sich nicht umwandeln."))
    dauer = wav_info(ganz)[0]

    mitteilen = []
    if sinnvoll and anzahl > 1:
        print(T("  Suche musikalisch brauchbare Schnittstellen …"))
        punkte, gruende, paare = finde_schnittpunkte(ganz, anzahl,
                                                     fenster=max(90.0, stueck/4))
        if len(punkte) != anzahl - 1:
            print(T("  Hinweis: keine brauchbaren Pausen gefunden (%s) -")
                  % (gruende[0] if gruende else "kein Ergebnis"))
            print(T("           es wird gleichmäßig geschnitten."))
            punkte = [dauer * k / float(anzahl) for k in range(1, anzahl)]
            gruende = ["rechnerisch"] * len(punkte)
            paare = [None] * len(punkte)
        grenzen = [0.0] + punkte + [dauer]
        if ohne_pause:
            # Jeder Teil endet dort, wo der Ton aufhört, und beginnt dort,
            # wo er nach der Pause wieder einsetzt. Die Stille fällt weg.
            mitteilen = []
            for i in range(anzahl):
                von = 0.0 if i == 0 else (
                    paare[i-1][1] if paare[i-1] else punkte[i-1])
                bis = dauer if i == anzahl - 1 else (
                    paare[i][0] if paare[i] else punkte[i])
                mitteilen.append((von, bis))
            print(T("  Pause an der Naht wird herausgenommen"))
        else:
            for i in range(anzahl):
                mitteilen.append((grenzen[i], min(dauer, grenzen[i+1])))
        for i, (von, bis) in enumerate(mitteilen):
            print(T("    Teil %d: %7.1f – %7.1f s  (%s)") % (
                i + 1, von, bis, sekunden(bis - von)))
        for i, (pu, gr) in enumerate(zip(punkte, gruende), start=1):
            print(T("      Schnitt %d bei %s: %s") % (i, sekunden(pu), T(gr)))
        mitteilen = [(max(0.0, v), min(dauer, b)) for v, b in mitteilen]
        # Sicherheit: kein Teil über der Grenze
        for i, (von, bis) in enumerate(mitteilen, start=1):
            if bis - von > MAX_LOOP_SEKUNDEN:
                print(T("  Hinweis: Teil %d wäre %.1f min - er wird auf 9:59 gekürzt.")
                      % (i, (bis - von) / 60.0))
    else:
        for i in range(anzahl):
            von = i * stueck
            bis = min(dauer, (i + 1) * stueck)
            mitteilen.append((von, bis))
            print(T("    Teil %d: %s – %s") % (i + 1, sekunden(von), sekunden(bis)))
    if not schreiben:
        print(T("  Trockenlauf — nichts angelegt. Mit --schreiben ausführen."))
        return

    try:
        for i, (von, bis) in enumerate(mitteilen, start=1):
            teil = os.path.join(zwischer, "teil%d.wav" % i)
            if not schneide_bereich(ganz, teil, von, bis):
                raise SystemExit(T("Fehler: Teil %d ließ sich nicht schneiden." )% i)
            befehl_aufspielen(basis, teil, "%s %d/%d" % (name, i, anzahl),
                              True, stopp, None, wiedergabe)
    finally:
        shutil.rmtree(zwischer, ignore_errors=True)


def befehl_aufspielen(basis, wav, name, schreiben, stopp=None, rhythmus=None,
                      wiedergabe=None, fade=None, gain=None, normieren=False,
                      begrenzen=True, trotzdem=False, kuerzen=False,
                      laenge=None):
    wav = os.path.abspath(os.path.expanduser(wav))
    if not os.path.exists(wav):
        raise SystemExit(T("Fehler: '%s' gibt es nicht.") % wav)
    dauer, rate, kanaele, bits = wav_info(wav)
    if dauer <= 0:
        raise SystemExit(T("Fehler: '%s' ist keine lesbare Audiodatei.%s")
                         % (wav, unlesbar_hinweis(wav)))
    ziel_laenge = None
    if dauer > MAX_LOOP_SEKUNDEN:
        if laenge:
            ziel_laenge = min(laenge, float(MAX_LOOP_SEKUNDEN))
        elif kuerzen:
            ziel_laenge = float(MAX_LOOP_SEKUNDEN) - 1.0     # 9:59, mit Luft
        elif not trotzdem:
            raise SystemExit(
                T("Fehler: Die Datei ist %.1f Minuten lang.\n"
                "  Der JamMan Solo XT nimmt höchstens %d Minuten pro Loop an\n"
                "  (Handbuch, Seite 60: \"Max. Individual Loop Time: 10 minutes\").\n"
                "  Längere Loops erscheinen auf der Karte, werden vom Pedal aber\n"
                "  nicht erkannt - und können dabei verschwinden.\n\n"
                "  So geht es weiter:\n"
                "    --kuerzen            auf 9:59 kürzen (harter Schnitt am Ende,\n"
                "                         12 ms ausgeblendet, damit es nicht knackt)\n"
                "    --laenge 480         auf 8:00 kürzen\n"
                "    --trotzdem           unverändert schreiben (zum Ausprobieren)")
                % (dauer / 60.0, MAX_LOOP_SEKUNDEN // 60))

    passt = (rate, kanaele, bits) == WOLL
    name = name or os.path.splitext(os.path.basename(wav))[0]
    zielnummer = naechste_nummer(basis)
    zielpfad = os.path.join(basis, "Patch" + zielnummer)
    vorlage = vorlage_finden(basis)

    print(T("  Quelle:       %s") % os.path.basename(wav))
    print(T("  Dauer:        %s") % sekunden(dauer))
    print(T("  Format:       %d Hz / %d Bit / %d Kanal/Kanäle%s") %
          (rate, bits, kanaele, "" if passt else T("   ← wird umgewandelt")))
    print(T("  Stopp:        %s") % (T("wie Vorlage") if not stopp else
          "%s (%s)" % (stopp, STOPP.get(stopp, stopp))))
    print(T("  Rhythmus:     %s") % (T("wie Vorlage") if not rhythmus else
          "%s (%s)" % (rhythmus, RHYTHMUS.get(rhythmus, (rhythmus,))[0])))
    print(T("  Wiedergabe:   %s") % (T("wie Vorlage") if not wiedergabe else
          "%s (IsLoop %s)" % (wiedergabe, WIEDERGABE.get(wiedergabe, "?"))))
    if fade:
        print(T("  Fade:         letzte %.1f s werden ausgeblendet") % fade)
    if normieren:
        print(T("  Pegel:        auf -1 dB Spitze normieren"))
    elif gain:
        print(T("  Pegel:        %+.1f dB") % gain)
    print(T("  Neuer Patch:  Patch%s  (Name: %s)") % (zielnummer, name))
    print(T("  Vorlage:      %s") % (os.path.basename(vorlage) if vorlage else "KEINE — Abbruch"))
    if not vorlage:
        raise SystemExit(T("Fehler: kein vollständiger Patch als Vorlage gefunden."))
    if os.path.exists(zielpfad):
        raise SystemExit(T("Fehler: '%s' liegt schon vor.") % zielpfad)
    if not schreiben:
        print(T("  Trockenlauf — nichts geschrieben. Mit --schreiben ausführen."))
        return

    os.makedirs(os.path.join(zielpfad, "PhraseA"))

    # 1) WAV ablegen — bei Bedarf vorher umwandeln
    ziel_wav = os.path.join(zielpfad, "PhraseA", "phrase.wav")
    if passt:
        shutil.copy2(wav, ziel_wav)
    else:
        if not wandle_nach_44k(wav, ziel_wav):
            raise SystemExit(
                T("Fehler beim Umwandeln. Die Datei hat %d Hz / %d Kanal/Kanäle;\n"
                "  nötig sind 44100 Hz und 2 Kanäle. Bitte vorher umwandeln oder\n"
                "  ffmpeg installieren.") % (rate, kanaele))

    if ziel_laenge:
        if kuerze_wav(ziel_wav, ziel_laenge):
            print(T("  Kürzen:       auf %s geschnitten (harter Schnitt, "
                  "12 ms ausgeblendet)") % sekunden(ziel_laenge))
        else:
            raise SystemExit(T("Fehler: Die Datei ließ sich nicht kürzen."))

    # Kopf auf einfaches PCM umschreiben - das Pedal lehnt manche
    # WAVE_FORMAT_EXTENSIBLE-Dateien ab (aus M4A entstehend).
    if normalisiere_wav(ziel_wav):
        kopf = wav_kopf(ziel_wav)
        print(T("  Kopf:         WAV-Kopf auf einfaches PCM umgeschrieben"))
        if not kopf or kopf.get("format") != 1 or kopf.get("fmt_groesse") != 16:
            print(T("  Achtung: der WAV-Kopf ist weiterhin ungewöhnlich."))

    spitze, _ = pegel_info(ziel_wav)
    if spitze is None or spitze < -90:
        shutil.rmtree(zielpfad, ignore_errors=True)
        raise SystemExit(T("Fehler: Die Umwandlung hat keine hörbare Aufnahme\n"
                         "  ergeben (nur Stille). Datei prüfen: %s" )% os.path.basename(wav))

    if fade:
        if not fade_ausblenden(ziel_wav, fade):
            print(T("  Hinweis: Fade konnte nicht eingerechnet werden."))
    if normieren or gain:
        ergebnis = (pegel_anpassen(ziel_wav, ziel_spitze_db=-1.0) if normieren
                    else pegel_anpassen(ziel_wav, db=gain, begrenzen=begrenzen))
        if ergebnis:
            print_pegel(ergebnis)
            if ergebnis.get("nachher") is not None:
                schreibe_pegelprotokoll(basis, [{
                    "ordner": os.path.basename(zielpfad), "name": patch_name(zielpfad),
                    "faktor": 10 ** ((ergebnis["nachher"] - ergebnis["vorher"]) / 20.0),
                    "vorher": ergebnis["vorher"], "nachher": ergebnis["nachher"]}],
                    "pegel", basis)
        else:
            print(T("  Hinweis: Pegel ließ sich nicht ändern (kein 16-Bit-WAV?)."))

    # 2) phrase.xml aus der Vorlage übernehmen, IsLoop bei Bedarf setzen
    quelle_phrase = os.path.join(vorlage, "PhraseA", "phrase.xml")
    phrase_text = open(quelle_phrase, encoding="utf-8").read()
    if wiedergabe and wiedergabe in WIEDERGABE:
        phrase_text, vorhanden = setze_wert(phrase_text, "IsLoop", WIEDERGABE[wiedergabe])
        if not vorhanden:
            print(T("  Hinweis: Die Vorlage hatte kein Feld IsLoop - es wurde ergänzt."))
    open(os.path.join(zielpfad, "PhraseA", "phrase.xml"), "w", encoding="utf-8").write(phrase_text)

    # 3) patch.xml aus der Vorlage, Name und Kennung neu
    quelle_xml = os.path.join(vorlage, "patch.xml")
    text = open(quelle_xml, encoding="utf-8").read()
    text = re.sub(r"<PatchName>.*?</PatchName>",
                  "<PatchName>%s</PatchName>" % name.replace("&", "&amp;").replace("<", "&lt;"),
                  text, count=1, flags=re.S)
    text = re.sub(r"<ID>.*?</ID>", "<ID>%s</ID>" % uuid.uuid4(), text, count=1, flags=re.S)
    if stopp and stopp in STOPP:
        text, vorhanden = setze_wert(text, "StopMode", STOPP[stopp])
        if not vorhanden:
            print(T("  Hinweis: Die Vorlage hatte kein Feld StopMode - es wurde ergänzt."))
    if rhythmus and rhythmus in RHYTHMUS:
        wert, version = RHYTHMUS[rhythmus]
        text, vorhanden = setze_wert(text, "RhythmType", wert)
        if not vorhanden:
            print(T("  Hinweis: Die Vorlage hatte kein Feld RhythmType - es wurde ergänzt."))
        # SettingsVersion gehoert zum Rhythmus (0 bei Silence, 1 beim Klick)
        text, _ = setze_wert(text, "SettingsVersion", version)
    open(os.path.join(zielpfad, "patch.xml"), "w", encoding="utf-8").write(text)

    saeubere_karte(basis)
    print(T("  Fertig: %s angelegt.") % zielpfad)
    print(T("  Karte auswerfen, in den JamMan stecken — der Patch erscheint als Patch%s.") % zielnummer)


def sicherung_der_einstellungen(basis, pfad, grund, wurzel=None):
    """Sichert nur patch.xml und phrase.xml - kein WAV.

    Beim Ändern von Name oder Einstellungen bleibt die Aufnahme unberührt;
    eine Kopie der ganzen Patch-Mappe (bis zu 90 MB) wäre dafür unnötig.
    """
    ordner = os.path.expanduser(wurzel or SICHERUNG_WURZEL)
    ziel = freier_name(os.path.join(ordner, "Einstellungen_%s_%s" % (
        datetime.now().strftime("%Y-%m-%d_%H-%M-%S"), grund), os.path.basename(pfad)))
    try:
        os.makedirs(os.path.join(ziel, "PhraseA"), exist_ok=True)
        for name in ("patch.xml", os.path.join("PhraseA", "phrase.xml")):
            quelle = os.path.join(pfad, name)
            if os.path.exists(quelle):
                shutil.copy2(quelle, os.path.join(ziel, name))
    except OSError:
        raise SystemExit(T("Fehler: '%s' lässt sich nicht schreiben.\n"
                         "  Nimm einen anderen Ordner:  --sicherung <pfad>") % ordner)
    return os.path.dirname(ziel)


def befehl_aendern(basis, nummer, schreiben, stopp=None, rhythmus=None,
                   wiedergabe=None, name=None, wurzel=None, fade=None,
                   gain=None, normieren=False, begrenzen=True):
    """Einstellungen eines vorhandenen Patches ändern."""
    alle = dict((n, pf) for n, _, pf in patches(basis))
    if nummer not in alle:
        raise SystemExit(T("Fehler: Patch%d gibt es nicht.") % nummer)
    pfad = alle[nummer]
    print(T("  Patch:        %s  (%s)") % (os.path.basename(pfad), patch_name(pfad)))
    if not (stopp or rhythmus or wiedergabe or name or fade or gain or normieren):
        raise SystemExit(T("Fehler: nichts angegeben (--stopp, --rhythmus, "
                         "--wiedergabe, --name oder --fade)."))
    if stopp:      print(T("  Stopp:        %s (%s)") % (stopp, STOPP[stopp]))
    if rhythmus:   print(T("  Rhythmus:     %s (%s)") % (rhythmus, RHYTHMUS[rhythmus][0]))
    if wiedergabe: print(T("  Wiedergabe:   %s (IsLoop %s)") % (wiedergabe, WIEDERGABE[wiedergabe]))
    if name:       print(T("  Name:         %s") % name)
    if fade:
        print(T("  Fade:         letzte %.1f s der Aufnahme ausblenden") % fade)
    if normieren:
        print(T("  Pegel:        auf -1 dB Spitze normieren"))
    elif gain:
        print(T("  Pegel:        %+.1f dB") % gain)
    if fade or gain or normieren:
        print(T("                (verändert die Audiodatei; vorher wird gesichert)"))
    if not schreiben:
        print(T("  Trockenlauf — nichts geändert. Mit --schreiben ausführen."))
        return

    if fade or gain or normieren:
        ziel = sicher_ablegen(basis, [pfad], "vor_pegel", wurzel)
    else:
        ziel = sicherung_der_einstellungen(basis, pfad, "vor_aendern", wurzel)
    px = os.path.join(pfad, "patch.xml")
    ph = os.path.join(pfad, "PhraseA", "phrase.xml")
    text = open(px, encoding="utf-8").read()
    if stopp:
        text, _ = setze_wert(text, "StopMode", STOPP[stopp])
    if rhythmus:
        wert, version = RHYTHMUS[rhythmus]
        text, _ = setze_wert(text, "RhythmType", wert)
        text, _ = setze_wert(text, "SettingsVersion", version)
    if name:
        text, _ = setze_wert(text, "PatchName", name.replace("&", "&amp;").replace("<", "&lt;"))
    open(px, "w", encoding="utf-8").write(text)

    if wiedergabe and os.path.exists(ph):
        ptext = open(ph, encoding="utf-8").read()
        ptext, _ = setze_wert(ptext, "IsLoop", WIEDERGABE[wiedergabe])
        ptext, _ = setze_wert(ptext, "IsReversed",
                               wiedergabe_rueckwaerts(wiedergabe))
        open(ph, "w", encoding="utf-8").write(ptext)

    if fade or gain or normieren:
        wav = os.path.join(pfad, "PhraseA", "phrase.wav")
        if not os.path.exists(wav):
            raise SystemExit(T("Fehler: Patch%d hat keine phrase.wav.") % nummer)
        if normalisiere_wav(wav):
            print(T("  Kopf:         WAV-Kopf auf einfaches PCM umgeschrieben"))
        if fade and not fade_ausblenden(wav, fade):
            raise SystemExit(T("Fehler: Die Aufnahme ließ sich nicht ausblenden "
                             "(kein 16-Bit-WAV?)."))
        if gain or normieren:
            ergebnis = (pegel_anpassen(wav, ziel_spitze_db=-1.0) if normieren
                        else pegel_anpassen(wav, db=gain, begrenzen=begrenzen))
            if not ergebnis:
                raise SystemExit(T("Fehler: Der Pegel ließ sich nicht ändern."))
            print_pegel(ergebnis)

    saeubere_karte(basis)
    print(T("  Geändert. Sicherung: %s") % ziel)


def befehl_auswerfen(karte):
    """Karte auswerfen. Vorher werden die Apple-Dateien entfernt."""
    ziel = volumen_pfad(karte)
    if not os.path.isdir(ziel):
        raise SystemExit(T("Fehler: '%s' ist nicht eingelegt.") % karte)
    try:
        saeubere_karte(os.path.join(ziel, UNTERORDNER))
    except Exception:
        pass
    erfolg, meldung = auswerfen_datentraeger(karte)
    if erfolg:
        print(T("  %s Sie kann jetzt abgezogen werden.") % meldung)
    else:
        raise SystemExit(T("Fehler beim Auswerfen: %s") % meldung)


def namensform_pruefen(basis):
    """Welche Patch-Ordner heißen nicht so, wie das Gerät sie schreibt?

    FAT32 speichert Namen mitunter in Großbuchstaben (PATCH01). Das Pedal
    liest ohne Rücksicht darauf, andere Programme aber nicht. Diese
    Funktion sagt, was zu ändern wäre - sie ändert nichts.
    """
    from collections import Counter
    alle = patches(basis)
    if not alle:
        return [], 2
    breiten = Counter()
    for nummer, name, _ in alle:
        ziffern = re.sub(r"\D", "", name)
        breiten[len(ziffern)] += 1
    breite = breiten.most_common(1)[0][0]
    aenderungen = []
    for nummer, name, pfad in alle:
        soll = "Patch%0*d" % (breite, nummer)
        if name != soll:
            aenderungen.append((name, soll, pfad))
    return aenderungen, breite


def befehl_namensform(basis, schreiben, wurzel=None):
    """Patch-Ordner auf die Schreibweise des Geräts bringen (Patch01)."""
    aenderungen, breite = namensform_pruefen(basis)
    print(T("  Schreibweise des Geräts: Patch%0*d") % (breite, 1))
    if not aenderungen:
        print(T("  Alle Ordner heißen schon so. Nichts zu tun."))
        return
    print(T("  Zu ändern: %d") % len(aenderungen))
    for alt, neu, _ in aenderungen:
        print(T("    %-12s ->  %s") % (alt, neu))
    if not schreiben:
        print(T("  Trockenlauf — nichts geändert. Mit --schreiben ausführen."))
        return
    # Zuordnung sichern, damit sich alles zurücknehmen lässt
    ordner = wurzel or os.path.expanduser(SICHERUNG_WURZEL)
    os.makedirs(ordner, exist_ok=True)
    protokoll = os.path.join(ordner, "Namensform_%s.txt" % time.strftime("%Y-%m-%d_%H-%M-%S"))
    with open(protokoll, "w", encoding="utf-8") as f:
        f.write("# Umbenannte Patch-Ordner auf %s\n" % basis)
        for alt, neu, _ in aenderungen:
            f.write("%s\t%s\n" % (alt, neu))
    fehler = 0
    for alt, neu, pfad in aenderungen:
        ziel = os.path.join(basis, neu)
        zwischen = os.path.join(basis, "tmp_%s_%d" % (neu, os.getpid()))
        try:
            os.rename(pfad, zwischen)      # erst weg vom alten Namen
            os.rename(zwischen, ziel)      # dann auf die gewünschte Form
        except OSError as e:
            fehler += 1
            print(T("    %-12s FEHLER: %s") % (alt, e.strerror))
            if os.path.exists(zwischen) and not os.path.exists(pfad):
                try:
                    os.rename(zwischen, pfad)   # zurück, falls halb umbenannt
                except OSError:
                    pass
    nach = len(patches(basis))
    print(T("  Fertig: %d Ordner umbenannt, %d Fehler, %d Patches lesbar.")
          % (len(aenderungen) - fehler, fehler, nach))
    print(T("  Zuordnung liegt in: %s") % protokoll)


def befehl_saeubern(basis, schreiben):
    if not schreiben:
        anzahl = sum(1 for w, d, ds in os.walk(os.path.dirname(basis))
                     for x in ds if x.startswith("._") or x == ".DS_Store")
        print(T("  %d Apple-Dateien auf der Karte. Mit --schreiben entfernen.") % anzahl)
        return
    saeubere_karte(basis)
    print(T("  Fertig."))


def befehl_abspielen(basis, nummer):
    alle = dict((n, p) for n, _, p in patches(basis))
    if nummer not in alle:
        raise SystemExit(T("Fehler: Patch%d gibt es nicht.") % nummer)
    w = phrase_wav(alle[nummer])
    if not w:
        raise SystemExit(T("Fehler: Patch%d hat keine WAV.") % nummer)
    print(T("  Spielt \"%s\" (%s) … (Strg+C hält an)")
          % (patch_name(alle[nummer]), sekunden(wav_info(w)[0])))
    griff = abspielen_datei(w)
    try:
        if hasattr(griff, "wait"):
            griff.wait()
        else:
            import time
            while os.path.exists(w):     # Windows: warten, bis der Nutzer Ctrl+C drückt
                time.sleep(0.2)
    except KeyboardInterrupt:
        stoppen_ton(griff)
        print(T("  Angehalten."))


# --------------------------------------------------- Löschen und Verschieben

SICHERUNG_WURZEL = "~/JamMan-Sicherung"

# Englische Befehlsnamen -> deutscher Befehl. Die deutschen Namen bleiben
# gültig; die englischen sind nur ein zweiter Weg.
# Spendenlink. Leer lassen, solange es keinen gibt - das Werkzeug zeigt
# dann einen Hinweis statt einen toten Verweis zu öffnen.
SPENDEN_LINK = ("https://www.paypal.com/donate/"
                "?hosted_button_id=PEJEFMSKUPA8C&currency_code=EUR")

BEFEHL_ALIAS = {
    "list": "liste",
    "names": "namensform",
    "backup": "sichern",
    "import": "aufspielen",
    "play": "abspielen",
    "delete": "loeschen",
    "move": "verschieben",
    "swap": "tauschen",
    "place": "platzieren",
    "restore": "zuruecknehmen",
    "normalize": "alle-normieren",
    "undo-level": "pegel-umkehren",
    "split": "teilen",
    "change": "aendern",
    "clean": "saeubern",
    "eject": "auswerfen",
    "selftest": "selbsttest",
}

BEFEHLE = {
    "liste": ("List patches", "Patches auflisten"),
    "namensform": ("Normalise folder names", "Ordnernamen angleichen"),
    "sichern": ("Back up the card", "Karte sichern"),
    "aufspielen": ("Import an audio file as a patch", "Audiodatei als Patch aufspielen"),
    "abspielen": ("Play a patch", "Patch abspielen"),
    "loeschen": ("Delete a patch", "Patch löschen"),
    "verschieben": ("Move a patch", "Patch verschieben"),
    "tauschen": ("Swap two patches", "Zwei Patches tauschen"),
    "platzieren": ("Put a patch on a free slot", "Patch auf freien Platz legen"),
    "zuruecknehmen": ("Restore from a backup", "Aus Sicherung zurückholen"),
    "alle-normieren": ("Normalise all patches", "Alle Patches normieren"),
    "pegel-umkehren": ("Undo a level change", "Pegeländerung zurücknehmen"),
    "teilen": ("Split a file into several patches", "Datei in mehrere Patches teilen"),
    "aendern": ("Change name, stop mode, playback", "Name, Stoppart, Wiedergabe ändern"),
    "saeubern": ("Remove macOS clutter files", "macOS-Begleitdateien entfernen"),
    "auswerfen": ("Eject the card", "Karte auswerfen"),
    "aktualisieren": ("Download the newest version", "Neueste Fassung holen"),
    "speicherabbild": ("Write an inventory of the pedal (read only)",
                       "Speicherabbild des Pedals schreiben (nur lesend)"),
    "vergleich": ("Compare two inventories", "Zwei Speicherabbilder vergleichen"),
    "firmware-holen": ("Fetch the pedal's firmware files (read only)",
                       "Firmware-Dateien des Pedals holen (nur lesend)"),
    "firmware-schreiben": ("Write firmware files into the pedal",
                           "Firmware-Dateien ins Pedal schreiben"),
    "jamlists": ("List the JamLists in a JamManager library",
                 "JamLists einer JamManager-Bibliothek auflisten"),
    "jamlist-holen": ("JamList as text setlist", "JamList als Textdatei holen"),
    "jamlist-schreiben": ("Text setlist as JamList", "Textdatei als JamList ablegen"),
    "gui": ("Open the window", "Fenster öffnen"),
    "menu": ("Text menu", "Textmenü"),
    "selbsttest": ("Report what the tool sees", "Bericht, was das Werkzeug sieht"),
}


def befehlsliste():
    """Hilfetext mit beiden Namen, in der gewählten Sprache."""
    zeilen = []
    for befehl, (englisch, deutsch) in BEFEHLE.items():
        zeilen.append("  %-14s %s" % (befehl, T(englisch if SPRACHE != "de" else deutsch)))
    return "\n".join(zeilen)

# ---------------------------------------------------------------- Sprachen
# Die Schlüssel sind die deutschen Texte selbst. Fehlt eine Übersetzung,
# erscheint der englische Text, sonst der deutsche. So bleibt das Werkzeug
# auch mit unvollständigem Katalog bedienbar.
SPRACHEN = {
    "de": "Deutsch",
    "en": "English",
    "fr": "Français",
    "es": "Español",
    "zh": "中文",
}

TEXTE = {
    "de": {},          # Deutsch ist die Quelle

    "en": {
        'Dem Python fehlen die Zertifikate. Zwei Wege: im Python-Ordner „Install Certificates.command“ ausführen — oder „pip install certifi“ — oder die Datei von der Release-Seite von Hand laden.':
         'Python is missing its certificates. Two ways: run “Install Certificates.command” in the Python folder, or “pip install certifi” — or download the file from the release page by hand.',
        'Achtung: das Zertifikat ließ sich nicht prüfen — die Datei wurde ohne Prüfung geladen.':
         'Careful: the certificate could not be verified — the file was downloaded unchecked.',
        'Die Datei ist unvollständig: %d statt %d Bytes.':
         'The file is incomplete: %d instead of %d bytes.',
        'Neue Fassung %s wird geholt …':
         'Fetching version %s …',
        'Fassung %s ist installiert. Bitte das Fenster schließen und neu starten.':
         'Version %s is installed. Please close the window and start again.',
        'Update beim Start':
         'Update on start',
        'Neue Fassung %s wird geholt …':
         'Fetching version %s …',
        'Die Karte ist eingebunden, aber das Dateisystem liefert\nLesefehler:\n\n%s\n\nDas liegt an der Karte oder am Einhängepunkt, nicht am\nWerkzeug.\n\nErste Schritte:\n  - Karte im JamMan einschalten (das Pedal prüft sie)\n  - dmesg | tail -20\n  - aushängen und prüfen: sudo fsck.vfat -n <gerät>\n    bzw. sudo exfatfsck <gerät>':
         'The card is mounted, but the file system reports read errors:\n\n%s\n\nThat is the card or the mount point, not the tool.\n\nFirst steps:\n  - put the card into the JamMan and switch it on (the pedal checks it)\n  - dmesg | tail -20\n  - unmount and check: sudo fsck.vfat -n <device>\n    or sudo exfatfsck <device>',
        'kein Pedal':
         'no pedal',
        'Beschreibung …':
         'Description …',
        'Rückwärts: ohne ffmpeg keine Hörprobe — das Pedal spielt es trotzdem rückwärts.':
         'Reverse: without ffmpeg there is no preview — the pedal still plays it backwards.',
        'Tempo':
         'Tempo',
        'Suchen …':
         'Search …',
        'Text im Namen (leer lassen = alles zeigen):':
         'Text in the name (leave empty = show all):',
        'Erst eine Karte oder eine geholte Kopie öffnen — dann läßt sich suchen.':
         'Open a card or a fetched copy first — then you can search.',
        'Suchen':
         'Search',
        'Suche fehlgeschlagen: %s':
         'Search failed: %s',
        'Suchen:':
         'Search:',
        'Karte:':
         'Card:',
        'Meldungen stehen im Fenster; Fehler zusätzlich in jamman-fehler.log.':
         'Messages appear in the window; errors also in jamman-fehler.log.',
        'Firmware-Dateien jetzt ins Pedal schreiben? Ein Abbruch mittendrin kann das Pedal unbrauchbar machen. [ja/nein] ':
         'Write the firmware files to the pedal now? An interruption can make the pedal unusable. [yes/no] ',
        'Zielordner für die Firmware':
         'Target folder for the firmware',
        'Ordner mit der geholten Firmware':
         'Folder with the fetched firmware',
        'In diesem Ordner liegt keine Firmware (usr/JamManUI, usr/loopengine …).':
         'This folder contains no firmware (usr/JamManUI, usr/loopengine …).',
        'Firmware übernehmen':
         'Apply firmware',
        'Firmware übernehmen …':
         'Apply firmware …',
        'Firmware dieses Pedals holen …':
         "Fetch this pedal's firmware …",
        '%s kam unvollständig (%d von %d Bytes).':
         '%s came incomplete (%d of %d bytes).',
        '%d Firmware-Dateien werden jetzt in das Pedal geschrieben. Vorher wird der jetzige Stand gesichert.\n\nEin Abbruch mittendrin kann das Pedal unbrauchbar machen — es gibt keinen Weg zurück.\n\nWirklich schreiben?':
         '%d firmware files will now be written to the pedal. The current state is backed up first.\n\nAn interruption can make the pedal unusable — there is no way back.\n\nReally write?',
        'Sichere den jetzigen Stand …':
         'Backing up the current state …',
        'Gesichert: %d Dateien nach %s':
         'Backed up: %d files to %s',
        'Firmware geholt: %d Dateien':
         'Firmware fetched: %d files',
        'Firmware geholt:\n%s\n%d Dateien':
         'Firmware fetched:\n%s\n%d files',
        'Fertig: %d Dateien geschrieben':
         'Done: %d files written',
        'Nicht bestätigt: %d':
         'Not confirmed: %d',
        'Firmware geschrieben: %d Dateien\nSicherung: %s\nJetzt das Pedal aus- und wieder einschalten.':
         'Firmware written: %d files\nBackup: %s\nNow switch the pedal off and on again.',
        'Schreibe':
         'Writing',
        'Hole':
         'Fetching',
        'JamList aus JamManager XT holen …':
         'Fetch JamList from JamManager XT …',
        'Setlist als JamList ablegen …':
         'Store setlist as JamList …',
        'Setlist speichern':
         'Save setlist',
        'Bibliothek von JamManager XT':
         'JamManager XT library',
        'JamList wählen':
         'Choose a JamList',
        'In dieser Bibliothek liegt keine JamList.':
         'This library contains no JamList.',
        'Geholt: %s\n%d Plätze\n%s':
         'Fetched: %s\n%d slots\n%s',
        'JamList ablegen':
         'Store JamList',
        '%s%s%s als neue JamList eintragen? Die Bibliothek wird dabei verändert — vorher eine Kopie anlegen.':
         'Add %s%s%s as a new JamList? The library will be changed — make a copy first.',
        'JamList %s angelegt (id %d, %d Plätze)':
         'JamList %s created (id %d, %d slots)',
        'Keine JamList mit dem Namen %s.':
         'No JamList named %s.',
        'belegte Plätze':
         'occupied slots',
        'Geschrieben: %s (%d Plätze)':
         'Written: %s (%d slots)',
        'Übernehmen':
         'Apply',
        'Speicherabbild':
         'Memory image',
        'Speicherabbild speichern':
         'Save memory image',
        'Speicherabbild aufnehmen …':
         'Take a memory image …',
        'Speicherabbilder vergleichen …':
         'Compare memory images …',
        'Speicherabbilder vergleichen':
         'Compare memory images',
        'Erstes Speicherabbild':
         'First memory image',
        'Zweites Speicherabbild':
         'Second memory image',
        'Nur im ersten Bild: %d':
         'Only in the first image: %d',
        'Nur im zweiten Bild: %d':
         'Only in the second image: %d',
        'Gleicher Pfad, andere Größe: %d':
         'Same path, different size: %d',
        'Die beiden Bilder sind gleich.':
         'The two images are identical.',
        'Erstes:':
         'First:',
        'Zweites:':
         'Second:',
        'Aufruf:':
         'Usage:',
        'Lese den Verzeichnisbaum …':
         'Reading the directory tree …',
        'Speicherabbild: %d Einträge':
         'Memory image: %d entries',
        'Speicherabbild geschrieben:\n%s':
         'Memory image written:\n%s',
        'Speicherabbild geschrieben: %d Einträge nach %s':
         'Memory image written: %d entries to %s',
        'Dateien für „speicherabbild“ und „vergleich“':
         'Files for “speicherabbild” and “vergleich”',
        'Suchen:':
         'Search:',
        'Sortierung':
         'Sorting',
        '%d von %d Patches passen zu „%s“.':
         '%d of %d patches match “%s”.',
        'Beschreibung':
         'Description',
        'Interpret':
         'Artist',
        'Genre':
         'Genre',
        'Stichwörter':
         'Tags',
        'Bewertung':
         'Rating',
        'Tempo (BPM)':
         'Tempo (BPM)',
        'Die Datei %s fehlt.':
         'The file %s is missing.',
        'Setlists anpassen':
         'Adjust setlists',
        '%d Setlist-Dateien in %s an die neuen Plätze anpassen? Jede Datei wird vorher daneben gesichert.':
         'Adjust %d setlist files in %s to the new slots? Each file is backed up next to it first.',
        'Keine Setlist nannte einen bewegten Platz.':
         'No setlist mentioned a moved slot.',
        'Setlists nicht angepaßt: ':
         'Setlists not adjusted: ',
        'Setlists angepaßt: ':
         'Setlists adjusted: ',
        'Plätze aufrücken':
         'Close the gaps',
        'Plätze aufrücken …':
         'Close the gaps …',
        'Es gibt keine Lücken — die Plätze sind schon lückenlos belegt.':
         'There are no gaps — the slots are already contiguous.',
        '%d Patches rücken in %s nach vorn: %s%s\n\nJeder bewegte Platz wird vorher auf den Rechner gesichert. Setlists im Pedal, die auf Platznummern zeigen, stimmen danach nicht mehr. Fortfahren?':
         '%d patches move up in %s: %s%s\n\nEvery moved slot is backed up to the computer first. Setlists in the pedal that point at slot numbers will no longer match. Continue?',
        'Rücke auf':
         'Moving up',
        'Fertig: %d Plätze aufgerückt, %d vorher gesichert':
         'Done: %d slots moved up, %d backed up first',
        'Aufgerückt: %d Plätze':
         'Moved up: %d slots',
        'Kopie auf dem Rechner — Änderungen hier wirken nicht auf das Pedal.':
         'Copy on the computer — changes here do not affect the pedal.',
        'Patch%d wird aus der Kopie auf dem Rechner entfernt. Das Pedal bleibt unverändert. Fortfahren?':
         'Patch%d will be removed from the copy on the computer. The pedal stays unchanged. Continue?',
        'Patch%d wird von der Karte entfernt. Vorher wird er auf dem Rechner gesichert. Fortfahren?':
         'Patch%d will be removed from the card. It is backed up to the computer first. Continue?',
        'Ja':
         'Yes',
        'Nein':
         'No',
        'Dem Python fehlen die Zertifikate. Auf macOS hilft „Install Certificates.command“ im Python-Ordner, oder: pip install certifi':
         'Python has no certificates. On macOS run “Install Certificates.command” in the Python folder, or: pip install certifi',
        'GitHub ist nicht erreichbar: %s':
         'GitHub cannot be reached: %s',
        'unbekannter Grund':
         'unknown reason',
        'Gelöscht: %s':
         'Deleted: %s',
        'Fehler:':
         'Error:',
        'Platz löschen':
         'Delete slot',
        'Welcher Platz soll gelöscht werden? (z. B. 30 oder Patch30)':
         'Which slot should be deleted? (e.g. 30 or Patch30)',
        'Platz löschen …':
         'Delete slot …',
        '%s ist nicht belegt — nichts zu löschen.':
         '%s is empty — nothing to delete.',
        '%s wird aus %s gelöscht. Das läßt sich nicht rückgängig machen. Fortfahren?':
         '%s will be deleted from %s. This cannot be undone. Continue?',
        'Lösche':
         'Deleting',
        'Gelöscht:':
         'Deleted:',
        'Fehler beim Löschen:':
         'Error while deleting:',
        'Die Datei ist %s lang — das Pedal nimmt höchstens 10 Minuten je Loop an. Bitte im Hauptfenster kürzen oder teilen.':
         'The file is %s long — the pedal accepts at most 10 minutes per loop. Please shorten or split it in the main window.',
        'Dieser Ordner ist nicht lesbar: %s':
         'This folder cannot be read: %s',
        'Auswahl:':
         'Selection:',
        'Pegelprotokoll wählen':
         'Choose level log',
        'Audiodatei zum Teilen auswählen':
         'Choose the audio file to split',
        'Dateiname:':
         'File name:',
        'Speichern':
         'Save',
        'Bitte einen Dateinamen angeben.':
         'Please enter a file name.',
        'Überschreiben':
         'Overwrite',
        '%s gibt es schon. Ersetzen?':
         '%s already exists. Replace?',
        'Speicherkarte des JamMan wählen':
         'Choose the JamMan card',
        'Zielordner für die Sicherung':
         'Target folder for the backup',
        'Dateien':
         'Files',
        'Nach oben':
         'Up',
        'Öffnen':
         'Open',
        'Bitte eine Datei auswählen.':
         'Please choose a file.',
        'Alle Dateien':
         'All files',
        'Die Datei konnte nicht geschrieben werden: %s':
         'The file could not be written: %s',
        'ohne':
         'none',
        'ende':
         'end',
        'klick':
         'click',
        '%d Patches ohne Ton — nur die Beschreibung ist geholt.':
         '%d patches without audio — only the description has been fetched.',
        'Ganzen Ordner aufs Pedal …':
         'Whole folder to the pedal …',
        'Ordner mit Patch-Ordnern wählen':
         'Choose a folder containing patch folders',
        'Ganzen Ordner aufs Pedal':
         'Whole folder to the pedal',
        '%d Patch-Ordner werden in %s geschrieben. Belegte Plätze werden vorher gesichert, unveränderte übersprungen. Fortfahren?':
         '%d patch folders will be written to %s. Occupied slots are backed up first, unchanged ones are skipped. Continue?',
        'internen Speicher':
         'internal memory',
        'der Karte':
         'the card',
        'Im Ordner liegen keine Patch-Ordner (erwartet: Patch01 … Patch200).':
         'The folder contains no patch folders (expected: Patch01 … Patch200).',
        'Schreibe':
         'Writing',
        'Fertig: %d Patches geschrieben, %d übersprungen, %d vorher gesichert':
         'Done: %d patches written, %d skipped, %d backed up first',
        'Fehler beim Schreiben:':
         'Error while writing:',
        'Übertragen: %d Dateien · unverändert übersprungen: %d':
         'Transferred: %d files · skipped unchanged: %d',
        'Zielplatz auf dem Pedal (z. B. 17 oder Patch17):':
         'Target slot on the pedal (e.g. 17 or Patch17):',
        'Unerwarteter Fehler':
         'Unexpected error',
        'Das Werkzeug wurde unerwartet beendet.':
         'The tool stopped unexpectedly.',
        'Protokoll:':
         'Log:',
        'Auswahl kopieren':
         'Copy selection',
        'Nichts markiert — bitte zuerst Text markieren.':
         'Nothing selected — please select some text first.',
        'In die Zwischenablage kopiert.':
         'Copied to the clipboard.',
        'Anleitung speichern':
         'Save the instructions',
        'Gespeichert.':
         'Saved.',
        'Alles kopieren':
         'Copy all',
        'Als Datei speichern':
         'Save as file',
        'Anleitung':
         'Instructions',
        'Zum Umwandeln fehlt ffmpeg.':
         'ffmpeg is missing for the conversion.',
        'Die Programmdateien lassen sich hier nicht ersetzen: %s':
         'The program files cannot be replaced here: %s',
        'Unerwarteter Fehler: %s':
         'Unexpected error: %s',
        'Aktualisieren':
         'Update',
        'Die neueste Fassung wird von GitHub geladen und ersetzt die Programmdateien. Ihre Einstellungen und Ihre Karte bleiben unberührt. Fortfahren?':
         'The latest version is downloaded from GitHub and replaces the program files. Your settings and your card stay untouched. Continue?',
        'Jetzt aktualisieren?':
         'Update now?',
        'Aktualisiert':
         'Updated',
        'Nicht aktualisiert':
         'Not updated',
        'Fenster jetzt schließen?':
         'Close the window now?',
        'GitHub ist nicht erreichbar.':
         'GitHub cannot be reached.',
        'Sie benutzen bereits die neueste Fassung (%s).':
         'You are already using the latest version (%s).',
        'Zur Fassung %s gibt es keine Datei zum Herunterladen.':
         'Version %s has no file to download.',
        'Lade Fassung %s …':
         'Downloading version %s …',
        'Herunterladen fehlgeschlagen: %s':
         'Download failed: %s',
        'Die heruntergeladene Datei ist kein gültiges ZIP.':
         'The downloaded file is not a valid ZIP.',
        'Die Datei ist beschädigt (%s).':
         'The file is damaged (%s).',
        'In der Datei fehlt jamman.py.':
         'The file does not contain jamman.py.',
        'Die Datei gehört nicht zur Fassung %s.':
         'The file does not belong to version %s.',
        'Fassung %s ist installiert (%d Dateien). Bitte das Fenster schließen und neu starten.':
         'Version %s is installed (%d files). Please close the window and start again.',
        'Die Karte hängt nicht im Rechner — nichts auszuwerfen. Steckt sie im Pedal, führt das Pedal sie nicht als Laufwerk.':
         'The card is not in the computer — nothing to eject. If it is in the pedal, the pedal does not offer it as a drive.',
        'Auswerfen ging nicht. Bitte im Dateimanager auf „Auswerfen" klicken.':
         'Ejecting failed. Please use “Eject” in the file manager.',
        'Keine Karte im Rechner gefunden.':
         'No card found in the computer.',
        'Karte im Rechner:  „Karte wählen“ drücken.':
         'Card in the computer:  press “Choose card”.',
        'Karte im Pedal:    oben rechts „Pedal über USB“ — dort sind Karte und interner Speicher erreichbar.':
         'Card in the pedal:  top right “Pedal over USB” — the card and the internal memory are reachable there.',
        'Speicherkarte': 'Memory card',
        'Kopie vom Pedal': 'copy from the pedal',
        'Speicherkarte des JamMan Stereo': 'Memory card of the JamMan Stereo',
        'Im Pedal steckt keine Karte.': 'There is no card in the pedal.',
        'Karte einlegen und dann „Karte wählen“ oder „Neu laden“ drücken.':
         'Insert the card, then press “Choose card” or “Reload”.',
        'keine Karte':
         'no card',
        'Dies ist eine Kopie vom Pedal, nicht die Karte selbst. Über „Karte wählen“ die Karte öffnen.':
         'This is a copy taken from the pedal, not the card itself. Use “Choose card” to open the card.',
        'JamMan Card Tool': 'JamMan Card Tool',
        'Karte und interner Speicher': 'card and internal memory',
        '(für Rückgängig nötig)':
         '(needed for Undo)',
        'Darunter ist die Sicherung für Rückgängig. Danach läßt sich der letzte Schreibvorgang nicht mehr zurücknehmen. Trotzdem löschen?':
         'This includes the backup for Undo. The last write can then no longer be undone. Delete anyway?',
        'Sicherungen aufräumen …':
         'Clean up backups …',
        'Sicherungen aufräumen':
         'Clean up backups',
        'Sicherungen im Zielordner':
         'Backups in the target folder',
        '%d Sicherungen · %.1f MB':
         '%d backups · %.1f MB',
        'Neueste behalten:':
         'Keep newest:',
        'Markierte löschen':
         'Delete selected',
        'Erst Sicherungen in der Liste markieren.':
         'Select backups in the list first.',
        '%d Sicherungen mit %.1f MB werden endgültig gelöscht. Fortsetzen?':
         '%d backups totalling %.1f MB will be deleted permanently. Continue?',
        '%d Sicherungen konnten nicht gelöscht werden.':
         '%d backups could not be deleted.',
        'Der Zielordner ist noch nicht angelegt.':
         'The target folder does not exist yet.',
        'Im Zielordner liegen keine Sicherungen.':
         'There are no backups in the target folder.',
        'Alle auswählen':
         'Select all',
        'Nichts markiert. Alle %d Patches in die Setlist?':
         'Nothing selected. Put all %d patches into the setlist?',
        'Warte auf das Ende des laufenden Vorgangs …':
         'Waiting for the running operation to finish …',
        'Der Vorgang reagiert nicht — Fenster schließen und neu öffnen.':
         'The operation is not responding — close the window and open it again.',
        'Hole Beschreibungen': 'Fetching descriptions',
        'Es wurde kein Zielplatz eingegeben.':
         'No target slot was entered.',
        'Der Zielplatz muß wie Patch01 … Patch200 heißen, nicht „%s“.':
         'The target slot has to be named like Patch01 … Patch200, not “%s”.',
        'OK': 'OK',
        'Ein Vorgang läuft noch — bitte abwarten.':
         'An operation is still running — please wait.',
        'Verbindung neu aufbauen':
         'Reconnect',
        'Verbindung wird neu aufgebaut …':
         'Reconnecting …',
        'Lesen und Schreiben über USB. Vor jedem Überschreiben wird der alte Platz auf den Rechner gesichert.':
         'Reading and writing over USB. Before anything is overwritten, the old slot is saved to the computer.',
        'Über USB fließen etwa 1,3 MB/s. Für eine ganze Karte ist ein Kartenleser viel schneller. Schreiben verändert die Karte — vorher wird gesichert.':
         'Data flows over USB at about 1.3 MB/s. For a whole card a card reader is much faster. Writing changes the card — it is saved first.',
        'Audio in den Speicher schreiben …': 'Write audio into the memory …',
        'Beim JamMan Solo XT behält der FS3X seine Grundfunktionen. Eine eigene Belegung speichert das Pedal zwar, wertet sie aber nicht aus — deshalb wird sie hier nicht angeboten.':
         'On the JamMan Solo XT the FS3X keeps its basic functions. The pedal does store a custom assignment but does not act on it, so it is not offered here.',
        'Änderungen wirken nach einem Neustart des Pedals.': 'Changes take effect after restarting the pedal.',
        'gewünscht': 'wanted',
        'Pedal meldet': 'pedal reports',
        '(nicht belegt)':
         '(not assigned)',
        'UndoRedo = Rückgängig/Wiederholen · Reverse = Rückwärts · Tempo = Tempo antippen':
         'UndoRedo = undo/redo · Reverse = backwards · Tempo = tap tempo',
        'Sichern': 'Save',
        'Schreiben': 'Write',
        'Setlists': 'Setlists',
        'Pedal': 'Pedal',
        'Ins Hauptfenster holen …': 'Fetch into the main window …',
        'Einzelnen Patch sichern …': 'Save a single patch …',
        'Patch-Ordner aufs Pedal …': 'Patch folder to the pedal …',
        'Übernommen.': 'Applied.',
        'Pedal spielt jetzt:': 'The pedal now plays:',
        'Audio aufs Pedal …':
         'Audio to the pedal …',
        'Audio aufs Pedal':
         'Audio to the pedal',
        'Audiodatei wählen':
         'Choose an audio file',
        'Audio':
         'Audio',
        'Sichere den alten Platz …':
         'Saving the old slot …',
        'Wandle die Datei …':
         'Converting the file …',
        'Umwandeln fehlgeschlagen — die Datei muß 44,1 kHz, 16 Bit, Stereo werden.':
         'Conversion failed — the file has to become 44.1 kHz, 16 bit, stereo.',
        'Leere den Platz …': 'Clearing the slot …',
        'Pedal spielt jetzt Bank/Patch:': 'The pedal now plays bank/patch:',
        'Karte erkannt:': 'Card detected:',
        'Als ZIP sichern …':
         'Save as ZIP …',
        'Als ZIP sichern':
         'Save as ZIP',
        'Sichere als ZIP …':
         'Saving as ZIP …',
        'Sichere:':
         'Saving:',
        'Fehler beim Sichern:':
         'Error while saving:',
        'Die Setlist ist leer.':
         'The setlist is empty.',
        'Erst Patches in der Liste markieren (⌘-Klick).':
         'Select patches in the list first (⌘-click).',
        'Es gibt nichts rückgängig zu machen.':
         'There is nothing to undo.',
        'Fußschalter …':
         'Footswitch …',
        'Fußschalter':
         'Footswitch',
        'Tastenbelegung des Pedals':
         'Footswitch assignment of the pedal',
        'Werte sind Geräteeinstellungen — nur ändern, wenn du weißt, was sie bedeuten.':
         'These are device settings — only change them if you know what they mean.',
        'Übernehmen':
         'Apply',
        'JamMan':
         'JamMan',
        'Karte':
         'card',
        'intern':
         'internal',
        'Liste als Textdatei sichern':
         'Save list as a text file',
        'Liste als Textdatei …':
         'List as a text file …',
        'Mitlaufen':
         'Follow',
        'Mitlaufen an — am Pedal einen Patch wählen.':
         'Follow on — select a patch on the pedal.',
        'Mitlaufen aus.':
         'Follow off.',
        'Rückgängig':
         'Undo',
        'Setlist holen …':
         'Fetch setlist …',
        'Setlist laden':
         'Load setlist',
        'Setlist sichern …':
         'Save setlist …',
        'Setlist sichern':
         'Save setlist',
        'Setlist.txt':
         'Setlist.txt',
        'Stelle wieder her:':
         'Restoring:',
        '%s wird durch die Sicherung ersetzt. Fortsetzen?':
         '%s will be replaced by the backup. Continue?',
        'Ordner des Patches wählen':
         'Choose the patch folder',
        'Aufs Pedal schreiben':
         'Write to the pedal',
        '%s ist schon belegt. Der Patch wird vorher auf den Rechner gesichert und dann überschrieben. Fortsetzen?':
         '%s is already in use. That patch will be saved to the computer first and then overwritten. Continue?',
        'Schreibe aufs Pedal …':
         'Writing to the pedal …',
        'Schreibe:':
         'Writing:',
        'Fehler beim Schreiben:':
         'Error while writing:',
        'Patch aufs Pedal schreiben …':
         'Write patch to the pedal …',
        '(Vorschlag)': '(suggested)',
        'Ziel:': 'Target:',
        'noch nicht gewählt': 'not chosen yet',
        'Ziel ändern …': 'Change target …',
        'Aufräumen':
         'Clean up',
        'Die angefangene Kopie entfernen? Nur die gerade geholten Dateien werden gelöscht, der Ordner bleibt.':
         'Remove the unfinished copy? Only the files fetched just now are deleted; the folder stays.',
        'Aufgeräumt:':
         'Cleaned up:',
        'Hole Beschreibungen …': 'Fetching descriptions …',
        'Wird abgebrochen …': 'Cancelling …',
        'Über USB fließen etwa 1,3 MB/s. Für eine ganze Karte ist ein Kartenleser viel schneller — steckt die Karte im Pedal, bleibt nur dieser Weg.':
         'Data flows over USB at about 1.3 MB/s. For a whole card a card reader is much faster — if the card is in the pedal, this is the only way.',
        'Abbrechen': 'Cancel',
        'Abgebrochen.': 'Cancelled.',
        'Fehler beim Öffnen:': 'Error while opening:',
        'Alle ins Hauptfenster holen …': 'Fetch all into the main window …',
        'Zielordner wählen': 'Choose a target folder',
        'Hole Patch': 'Fetching patch',
        'Fertig:': 'Done:',
        'Im Hauptfenster öffnen?': 'Open in the main window?',
        'Gesichert': 'Saved',
        'Pedal über USB': 'Pedal over USB',
        'Das USB-Modul fehlt: greenglass.py muß neben jamman.py liegen.': 'The USB module is missing: greenglass.py must sit next to jamman.py.',
        'Gesichert nach:': 'Saved to:',
        'Interner Speicher': 'Internal memory',
        'SD-Karte': 'SD card',
        'Kein JamMan gefunden — Pedal einschalten und Kabel prüfen.': 'No JamMan found — switch the pedal on and check the cable.',
        '(ohne Namen)': '(no name)',
        'Fehler beim Lesen:': 'Error while reading:',
        'Erst „Neu laden“ drücken.': 'Press "Reload" first.',
        'Lädt …': 'Loading …',
        'Nur lesen: das Pedal wird nicht verändert.': 'Read-only: the pedal is not modified.',
        'Ordner zum Sichern wählen': 'Choose a folder to save to',
        'Patch sichern …': 'Save patch …',
        'Verbinde mit dem Pedal …': 'Connecting to the pedal …',
        'Spenden': 'Donate',
        'Für dieses Werkzeug ist noch kein Spendenlink eingetragen.': 'No donation link has been entered for this tool yet.',
        'Er steht in jamman.py in der Zeile SPENDEN_LINK.': 'It goes into jamman.py, line SPENDEN_LINK.',
        '      Schnitt %d bei %s: %s': '      Cut %d at %s: %s',
        'keine Pause in der Nähe - rechnerisch geschnitten': 'no pause nearby - cut at the calculated point',
        "Speicherkarte des JamMan Solo XT": "JamMan Solo XT memory card",
        "keine Karte gefunden": "no card found",
        "Ordner": "Folder", "Name": "Name", "Dauer": "Length",
        "Stoppart": "Stop", "Wdg.": "Play", "Rhythmus": "Rhythm",
        "Wiedergabe": "Playback",
        "Sicherung": "Backup",
        "Aufspielen": "Import",
        "Der Name darf nicht leer sein.": "The name must not be empty.",
        "Sicherung zurückholen": "Restore backup",
        "Pegel zurückrechnen": "Undo level change",
        "Bitte 2 bis 60 Teile angeben.": "Please enter 2 to 60 parts.",
        "Die Datei ist nicht lesbar.": "The file cannot be read.",
        "Datei teilen": "Split file",
        "Alle Patches normieren": "Normalise all patches",
        "Alle Patches werden auf gleiche Lautheit gebracht:\n\n"
        "  Ziel -18 dB Lautheit, Spitze höchstens -1 dB\n\n"
        "Vorher wird die ganze Karte gesichert - das dauert bei\n"
        "900 MB ein bis zwei Minuten. Fortfahren?":
            "All patches are brought to the same loudness:\n\n"
            "  target -18 dB loudness, peak at most -1 dB\n\n"
            "The whole card is backed up first - about one to two\n"
            "minutes for 900 MB. Continue?",
        "Zum Tauschen genau ZWEI Patches markieren:\n"
        "ersten anklicken, zweiten mit gedrückter Cmd-Taste (⌘).":
            "Select exactly TWO patches to swap:\n"
            "click the first, then the second with Cmd (⌘) held.",
        "Tauschen": "Swap",
        "Oben steht überall „wie Vorlage - dann gibt es nichts zu ändern.":
            "Everywhere says \u201eas template\u201c - nothing to apply.",
        "Einstellungen anwenden": "Apply settings",
        "Erst einen Patch wählen.": "Select a patch first.",
        "Karte auswerfen? Danach kann sie abgezogen werden.":
            "Eject the card? Then it can be unplugged.",
        "Keine JamMan-Karte": "No JamMan card",
        "Datei zu lang": "File too long",
        "Beim Pegel bitte eine Zahl in dB eintragen (z. B. -3), oder das Feld leer lassen.":
            "Enter a level in dB (e.g. -3), or leave the field empty.",
        "Bei Teile bitte eine ganze Zahl eintragen.":
            "Please enter a whole number of parts.",
        "Beim Pegel bitte eine Zahl in dB eintragen (z. B. -3).":
            "Enter a level in dB (e.g. -3).",
        "Bei Fade am Ende bitte eine Zahl in Sekunden eintragen.":
            "Enter the fade length in seconds.",
        "Anleitung in die Zwischenablage kopiert": "Instructions copied to the clipboard",
        "Ende der Liste erreicht": "end of list reached",
        "Pegel zurückgerechnet": "level change undone",
        "alle Patches normiert": "all patches normalised",
        # --- Kommandozeile ---
        "Fertig.": "Done.",
        "Tauschen:": "Swapping:",
        "Karte auswerfen, in den JamMan stecken — der Patch ist weg.":
            "Eject the card, put it into the JamMan — the patch is gone.",
        "Neue Reihenfolge:": "New order:",
        "JamMan Solo XT — Speicherkarte verwalten": "JamMan Solo XT — manage the memory card",
        "Pythons auf diesem Rechner:": "Pythons on this machine:",
        "Geprüfte Orte:": "Places checked:",
        "Laufwerke mit JamManSoloXT:": "Drives with JamManSoloXT:",
        "Trockenlauf — nichts geändert. Mit --schreiben ausführen.":
            "Dry run — nothing changed. Run with --schreiben to apply.",
        "Trockenlauf — nichts kopiert. Mit --schreiben ausführen.":
            "Dry run — nothing copied. Run with --schreiben to apply.",
        "Trockenlauf — nichts angelegt. Mit --schreiben ausführen.":
            "Dry run — nothing created. Run with --schreiben to apply.",
        "Trockenlauf — nichts geschrieben. Mit --schreiben ausführen.":
            "Dry run — nothing written. Run with --schreiben to apply.",
        "Trockenlauf — nichts zurückgeholt. Mit --schreiben ausführen.":
            "Dry run — nothing restored. Run with --schreiben to apply.",
        "Trockenlauf — nichts umbenannt. Mit --schreiben ausführen.":
            "Dry run — nothing renamed. Run with --schreiben to apply.",
        "Trockenlauf — nichts getauscht. Mit --schreiben ausführen.":
            "Dry run — nothing swapped. Run with --schreiben to apply.",
        "Trockenlauf — nichts gelöscht. Mit --schreiben ausführen.":
            "Dry run — nothing deleted. Run with --schreiben to apply.",
        "Keine Patches auf der Karte.": "No patches on the card.",
        "Suche musikalisch brauchbare Schnittstellen …":
            "Looking for musically usable cut points …",
        "Kopf:         WAV-Kopf auf einfaches PCM umgeschrieben":
            "Header:       WAV header rewritten to plain PCM",
        "Alle Ordner heißen schon so. Nichts zu tun.":
            "All folders are already spelled that way. Nothing to do.",
        "Hinweis: diese gibt es auf der Karte nicht und werden neu angelegt:":
            "Note: these are not on the card and will be created:",
        "Nichts zu tun.": "Nothing to do.",
        "1  Patches auflisten": "1  List patches",
        "2  Patch anhören": "2  Play a patch",
        "3  Datei aufspielen": "3  Import a file",
        "4  Patch ändern (Stopp, Wiedergabe, Name)": "4  Change a patch (stop, playback, name)",
        "5  Patch verschieben": "5  Move a patch",
        "6  Patch löschen": "6  Delete a patch",
        "7  Alles sichern": "7  Back up everything",
        "8  Karte auswerfen": "8  Eject the card",
        "0  Beenden": "0  Quit",
        "Angehalten.": "Stopped.",
        "erste Zeile im Fenster:": "first line in the window:",
        "ACHTUNG: das Dateisystem liefert Lesefehler.":
            "WARNING: the file system reports read errors.",
        "Das ist kein Fehler des Werkzeugs - die Karte oder":
            "This is not a program error - the card or",
        "ihr Einhängepunkt ist beschädigt. Erste Schritte:":
            "its mount point is damaged. First steps:",
        "Ton:           afplay": "Sound:        afplay",
        "Ton:           winsound (Windows-eigen)": "Sound:        winsound (built into Windows)",
        "Hinweis: Pegelprotokoll konnte nicht geschrieben werden.":
            "Note: the level log could not be written.",
        "Sicherung beim Start: Karte unverändert, nichts zu tun.":
            "Backup on start: card unchanged, nothing to do.",
        "Pause an der Naht wird herausgenommen": "The pause at the seam is removed",
        # --- Kommandozeile, zweite Runde ---
        # --- Hilfe der Kommandozeile ---
        "Sprache der Ausgabe (Vorgabe: aus dem System)":
            "language of the output (default: from the system)",
        "Laufwerk der Karte (Vorgabe: automatisch erkannt)":
            "drive of the card (default: detected automatically)",
        "Zielordner für 'sichern'": "target folder for 'backup'",
        "Audiodatei für 'aufspielen'": "audio file for 'import'",
        "Name des Patches (Vorgabe: Dateiname)":
            "name of the patch (default: file name)",
        "Patch-Nummer": "patch number",
        "Zielplatz für 'verschieben'": "target slot for 'move'",
        "Tauschpartner für 'tauschen'": "partner slot for 'swap'",
        "Zielplatz für 'platzieren'": "target slot for 'place'",
        "Sicherungsordner für 'zuruecknehmen'": "backup folder for 'restore'",
        "Pegelprotokoll für 'pegel-umkehren'": "level log for 'undo-level'",
        "bei 'zuruecknehmen': alle Patches aus der Sicherung":
            "with 'restore': all patches from the backup",
        "Ziellautheit für 'alle-normieren' (Vorgabe -18 dB)":
            "target loudness for 'normalize' (default -18 dB)",
        "Spitzengrenze für 'alle-normieren' (Vorgabe -1 dB)":
            "peak limit for 'normalize' (default -1 dB)",
        "zu lange Datei auf 9:59 kürzen": "shorten a too long file to 9:59",
        "beim Teilen die Pause an der Naht stehen lassen":
            "keep the pause at the seam when splitting",
        "beim Teilen gleichmäßig schneiden statt an Pausen":
            "split evenly instead of at pauses",
        "Datei in N gleich lange Patches aufteilen":
            "split the file into N equal patches",
        "zu lange Datei auf diese Länge kürzen": "shorten the file to this length",
        "auch Loops über 10 Minuten schreiben (Pedal nimmt sie nicht an)":
            "also write loops over 10 minutes (the pedal rejects them)",
        "Fade am Ende der Datei einrechnen (z. B. 3)":
            "include a fade at the end of the file (e.g. 3)",
        "Lautstärke um DB ändern (z. B. -3 oder +2)":
            "change the volume by DB (e.g. -3 or +2)",
        "Anhebung NICHT auf 0 dBFS begrenzen (kann übersteuern)":
            "do NOT limit the boost to 0 dBFS (may clip)",
        "auf -1 dB Spitzenpegel normieren": "normalise to -1 dB peak",
        "Stopp-Verhalten des neuen Patches (ohne Angabe: wie Vorlage)":
            "stop behaviour of the new patch (default: as template)",
        "Loop oder Single (IsLoop in phrase.xml; ohne Angabe: wie Vorlage)":
            "loop or single (IsLoop in phrase.xml; default: as template)",
        "Rhythmus des neuen Patches (ohne Angabe: wie Vorlage)":
            "rhythm of the new patch (default: as template)",
        "beim Verschieben nur die Namen sichern (schnell, kein Kopieren)":
            "when moving, only save the names (fast, no copying)",
        "Ordner für die Sicherung vor Löschen/Verschieben":
            "folder for the backup before delete/move",
        "wirklich verändern (sonst Trockenlauf)": "actually change (otherwise dry run)",
        "was zu tun ist (Liste unten; englische Namen gehen auch)":
            "what to do (list below; English names work too)",
        "Quelle:       %s": "Source:       %s",
        "Quelle: %s": "Source: %s",
        "Ziel:   %s": "Target: %s",
        "Ziel: %.1f dB Lautheit (RMS), Spitze höchstens %.1f dB":
            "Target: %.1f dB loudness (RMS), peak at most %.1f dB",
        "Karte: %s": "Card: %s",
        "Name:         %s": "Name:         %s",
        "Dauer:        %s": "Length:       %s",
        "Dauer:  %s": "Length: %s",
        "Datei:  %s": "File:  %s",
        "Format:       %d Hz / %d Bit / %d Kanal/Kanäle%s":
            "Format:       %d Hz / %d bit / %d channel(s)%s",
        "Fassung:       %s": "Version:      %s",
        "Python:        %s": "Python:       %s",
        "System:        %s %s": "System:       %s %s",
        "Tk:            %s": "Tk:           %s",
        "Tk-Modul:      %s": "Tk module:    %s",
        "Schrift:       %s": "Font:         %s",
        "Kartenwurzeln: %s": "Card roots:   %s",
        "Einstellungen: %s": "Settings:      %s",
        "ffmpeg:        %s": "ffmpeg:       %s",
        "gewählt:       %s": "chosen:       %s",
        "Ton:           %s": "Sound:        %s",
        "Patches:       %d": "Patches:      %d",
        "Patches: %d": "Patches: %d",
        "Patches auf der Karte:    %d": "Patches on the card:    %d",
        "Patches in der Sicherung: %d": "Patches in the backup:  %d",
        "Inhalt von %s:": "Contents of %s:",
        "Gesichert: %d Patches, %.0f MB.": "Backed up: %d patches, %.0f MB.",
        "gesichert: %d Patches, %.0f MB": "backed up: %d patches, %.0f MB",
        "Fertig. Sicherung: %s": "Done. Backup: %s",
        "Fertig: %s angelegt.": "Done: %s created.",
        "Fertig: %d Ordner umbenannt, %d Fehler, %d Patches lesbar.":
            "Done: %d folders renamed, %d errors, %d patches readable.",
        "Fertig: %d Patches normiert, %d davon in der Spitze gedeckelt.":
            "Done: %d patches normalised, %d of them peak-limited.",
        "Gelöscht. Sicherung: %s": "Deleted. Backup: %s",
        "Getauscht. Sicherung: %s": "Swapped. Backup: %s",
        "Geändert. Sicherung: %s": "Changed. Backup: %s",
        "Verschoben. Sicherung: %s": "Moved. Backup: %s",
        "Zurückgeholt. Der Zustand davor liegt in: %s":
            "Restored. The previous state is in: %s",
        "Zurückgerechnet: %d Patches. Sicherung davor: %s":
            "Undone: %d patches. Backup before: %s",
        "Vorher wird er gesichert nach: %s": "It is backed up first to: %s",
        "Wird gelöscht: %s  (%s)": "Will be deleted: %s  (%s)",
        "Neuer Patch:  Patch%s  (Name: %s)": "New patch:  Patch%s  (name: %s)",
        "Patch:        %s  (%s)": "Patch:        %s  (%s)",
        "Patch:      %s  (%s)": "Patch:      %s  (%s)",
        "Vorlage:      %s": "Template:     %s",
        "Teile:  %d × etwa %s": "Parts:  %d × about %s",
        "Stopp:        %s": "Stop:         %s",
        "Stopp:        %s (%s)": "Stop:         %s (%s)",
        "Rhythmus:     %s": "Rhythm:       %s",
        "Rhythmus:     %s (%s)": "Rhythm:       %s (%s)",
        "Wiedergabe:   %s": "Playback:     %s",
        "Wiedergabe:   %s (IsLoop %s)": "Playback:     %s (IsLoop %s)",
        "Pegel:        %+.1f dB": "Level:        %+.1f dB",
        "Pegel:        %.1f dB → %.1f dB Spitze":
            "Level:        %.1f dB → %.1f dB peak",
        "Pegel:        auf -1 dB Spitze normieren":
            "Level:        normalise to -1 dB peak",
        "Fade:         letzte %.1f s werden ausgeblendet":
            "Fade:         last %.1f s are faded out",
        "Fade:         letzte %.1f s der Aufnahme ausblenden":
            "Fade:         fade out the last %.1f s",
        "Kürzen:       auf %s geschnitten (harter Schnitt, 12 ms ausgeblendet)":
            "Trim:         cut to %s (hard cut, 12 ms faded out)",
        "Protokoll: %s": "Log: %s",
        "Sicherung: %s": "Backup: %s",
        "Sicherung:  %s": "Backup:  %s",
        "Schreibweise des Geräts: Patch%0*d":
            "Spelling used by the device: Patch%0*d",
        "Zu ändern: %d": "To change: %d",
        "Zuordnung liegt in: %s": "Mapping is in: %s",
        "Betroffene Einträge (erste drei): %s": "Affected entries (first three): %s",
        "Keine Karte gefunden unter %s.": "No card found under %s.",
        "Ordner": "Folder",
        "  %-28s  (gibt es nicht)": "  %-28s  (does not exist)",
        "  %-28s  (nicht lesbar: %s)": "  %-28s  (not readable: %s)",
        "  %-28s  (leer)": "  %-28s  (empty)",
        "  %-28s  %s mit %s Patches": "  %-28s  %s with %s patches",
        "kein nutzbares Tk": "no usable Tk",
        "Name": "Name",
        "Dauer": "Length",
        "WAV-Format": "WAV format",
        "   ← abweichend": "   ← differs",
        "   ← über 10 min, Pedal nimmt sie nicht an":
            "   ← over 10 min, the pedal will not take it",
        "keine WAV": "no WAV",
        "%d Hz / %d Bit / %d K": "%d Hz / %d bit / %d ch",
        "  Hinweis: Pegelprotokoll konnte nicht geschrieben werden.": "    Note: the level protocol could not be written.",
        "  Protokoll: %s": "    Protocol: %s",
        "  %d Einträge": "    %d entries",
        "    %-10s %-24s Faktor %.4f → 1/%.4f": "      %-10s %-24s factor %.4f → 1/%.4f",
        "  Trockenlauf — nichts geändert. Mit --schreiben ausführen.": "    Dry run — nothing changed. Run with --schreiben to write.",
        "    %-10s %+6.1f → %+6.1f dB": "      %-10s %+6.1f → %+6.1f dB",
        "  Zurückgerechnet: %d Patches. Sicherung davor: %s": "    Recalculated: %d patches. Backup before: %s",
        "  Pegel:        %.1f dB → %.1f dB Spitze": "    Level:        %.1f dB → %.1f dB peak",
        "                Die Anhebung wurde begrenzt, damit nichts": "                The boost was limited so that nothing",
        "                über 0 dBFS geht (sonst würde übersteuert).": "                goes above 0 dBFS (that would clip).",
        "                Achtung: %d Werte liegen an der Grenze.": "                Note: %d values sit at the limit.",
        "  Hinweis: unbekanntes WAV-Format 0x%04X - nicht angefasst.": "    Note: unknown WAV format 0x%04X — left untouched.",
        "Fehler: '%s' ist kein Ordner. Ist die Karte eingelegt?": "  Error: '%s' is not a folder. Is the card inserted?",
        "Fehler: auf '%s' liegt kein Ordner '%s' oder '%s'. Das ist keine JamMan-Karte.": "  Error: '%s' contains no folder '%s' or '%s'. That is not a JamMan card.",
        "  %d Apple-Dateien entfernt (._* und .DS_Store).": "    %d Apple files removed (._* and .DS_Store).",
        "  Keine Patches auf der Karte.": "    No patches on the card.",
        "  %-10s %-34s %8s  %s": "    %-10s %-34s %8s  %s",
        "  %-10s %-34s %8s  %s%s%s": "    %-10s %-34s %8s  %s%s%s",
        "\n  %d Patches, %s belegt.": "\n  %d patches, %s used.",
        "  Sicherung beim Start: Karte unverändert, nichts zu tun.": "    Backup at start: card unchanged, nothing to do.",
        "  Sicherung beim Start: %s": "    Backup at start: %s",
        "    gesichert: %d Patches, %.0f MB": "      backed up: %d patches, %.0f MB",
        "    alte Sicherung entfernt: %s": "      old backup removed: %s",
        "  Quelle: %s": "    Source: %s",
        "  Ziel:   %s": "    Target: %s",
        "  Trockenlauf — nichts kopiert. Mit --schreiben ausführen.": "    Dry run — nothing copied. Run with --schreiben to write.",
        "  Gesichert: %d Patches, %.0f MB.": "    Backed up: %d patches, %.0f MB.",
        "  Datei:  %s": "    File:   %s",
        "  Dauer:  %s": "    Length: %s",
        "  Teile:  %d × etwa %s": "    Parts:  %d × about %s",
        "  Suche musikalisch brauchbare Schnittstellen …": "    Looking for musically usable cut points …",
        "  Hinweis: keine brauchbaren Pausen gefunden (%s) -": "    Note: no usable pauses found (%s) —",
        "           es wird gleichmäßig geschnitten.": "             cutting evenly instead.",
        "  Pause an der Naht wird herausgenommen": "    Pause at the join is removed",
        "    Teil %d: %7.1f – %7.1f s  (%s)": "      Part %d: %7.1f – %7.1f s  (%s)",
        "  Hinweis: Teil %d wäre %.1f min - er wird auf 9:59 gekürzt.": "    Note: part %d would be %.1f min — it is shortened to 9:59.",
        "    Teil %d: %s – %s": "      Part %d: %s – %s",
        "  Trockenlauf — nichts angelegt. Mit --schreiben ausführen.": "    Dry run — nothing created. Run with --schreiben to write.",
        "  Quelle:       %s": "    Source:       %s",
        "  Dauer:        %s": "    Length:       %s",
        "  Format:       %d Hz / %d Bit / %d Kanal/Kanäle%s": "    Format:       %d Hz / %d bit / %d channel(s)%s",
        "  Stopp:        %s": "    Stop:         %s",
        "  Rhythmus:     %s": "    Rhythm:       %s",
        "  Wiedergabe:   %s": "    Playback:     %s",
        "  Fade:         letzte %.1f s werden ausgeblendet": "    Fade:         last %.1f s are faded out",
        "  Pegel:        auf -1 dB Spitze normieren": "    Level:        normalise to -1 dB peak",
        "  Pegel:        %+.1f dB": "    Level:        %+.1f dB",
        "  Neuer Patch:  Patch%s  (Name: %s)": "    New patch:    Patch%s  (name: %s)",
        "  Vorlage:      %s": "    Template:     %s",
        "  Trockenlauf — nichts geschrieben. Mit --schreiben ausführen.": "    Dry run — nothing written. Run with --schreiben to write.",
        "  Kürzen:       auf %s geschnitten (harter Schnitt, 12 ms ausgeblendet)": "    Shorten:      cut to %s (hard cut, 12 ms faded)",
        "  Kopf:         WAV-Kopf auf einfaches PCM umgeschrieben": "    Header:       WAV header rewritten to plain PCM",
        "  Fertig: %s angelegt.": "    Done: %s created.",
        "  Karte auswerfen, in den JamMan stecken — der Patch erscheint als Patch%s.": "    Eject the card, put it into the JamMan — the patch appears as Patch%s.",
        "  Patch:        %s  (%s)": "    Patch:        %s  (%s)",
        "  Stopp:        %s (%s)": "    Stop:         %s (%s)",
        "  Rhythmus:     %s (%s)": "    Rhythm:       %s (%s)",
        "  Wiedergabe:   %s (IsLoop %s)": "    Playback:     %s (IsLoop %s)",
        "  Name:         %s": "    Name:         %s",
        "  Fade:         letzte %.1f s der Aufnahme ausblenden": "    Fade:         fade out the last %.1f s of the recording",
        "                (verändert die Audiodatei; vorher wird gesichert)": "                (changes the audio file; a backup is made first)",
        "  Geändert. Sicherung: %s": "    Changed. Backup: %s",
        "  %s Sie kann jetzt abgezogen werden.": "    %s It can be removed now.",
        "  Schreibweise des Geräts: Patch%0*d": "    Device's spelling: Patch%0*d",
        "  Alle Ordner heißen schon so. Nichts zu tun.": "    All folders already look like this. Nothing to do.",
        "  Zu ändern: %d": "    To change: %d",
        "    %-12s ->  %s": "      %-12s ->  %s",
        "    %-12s FEHLER: %s": "      %-12s ERROR: %s",
        "  Fertig: %d Ordner umbenannt, %d Fehler, %d Patches lesbar.": "    Done: %d folders renamed, %d errors, %d patches readable.",
        "  Zuordnung liegt in: %s": "    Mapping is in: %s",
        "  %d Apple-Dateien auf der Karte. Mit --schreiben entfernen.": "    %d Apple files on the card. Remove them with --schreiben.",
        "  Fertig.": "    Done.",
        "  Spielt \"%s\" (%s) … (Strg+C hält an)": "    Playing \"%s\" (%s) … (Ctrl+C stops)",
        "  Ziel: %.1f dB Lautheit (RMS), Spitze höchstens %.1f dB": "    Target: %.1f dB loudness (RMS), peak at most %.1f dB",
        "  Patches: %d": "    Patches: %d",
        "  %-10s %-24s %8s": "    %-10s %-24s %8s",
        "  %-10s %-24s %+7.1f dB": "    %-10s %-24s %+7.1f dB",
        "  Sicherung: %s": "    Backup: %s",
        "  %-10s %-24s %+6.1f → %+6.1f dB  (Spitze %+.1f dB)%s": "    %-10s %-24s %+6.1f → %+6.1f dB  (peak %+.1f dB)%s",
        "  Fertig: %d Patches normiert, %d davon in der Spitze gedeckelt.": "    Done: %d patches normalised, %d of them capped at the peak.",
        "  Patches in der Sicherung: %d": "    Patches in the backup: %d",
        "  Patches auf der Karte:    %d": "    Patches on the card:    %d",
        "  Hinweis: diese gibt es auf der Karte nicht und werden neu angelegt:": "    Note: these do not exist on the card and will be created:",
        "           %s": "             %s",
        "  Trockenlauf — nichts zurückgeholt. Mit --schreiben ausführen.": "    Dry run — nothing restored. Run with --schreiben to write.",
        "  Zurückgeholt. Der Zustand davor liegt in: %s": "    Restored. The previous state is in: %s",
        "  Patch:      %s  (%s)": "    Patch:      %s  (%s)",
        "  Sicherung:  %s": "    Backup:     %s",
        "    %-22s %8.2f MB": "      %-22s %8.2f MB",
        "  %s  (%s)": "    %s  (%s)",
        "  →  %s": "    →  %s",
        "  Trockenlauf — nichts umbenannt. Mit --schreiben ausführen.": "    Dry run — nothing renamed. Run with --schreiben to write.",
        "  Fertig. Sicherung: %s": "    Done. Backup: %s",
        "  Nichts zu tun.": "    Nothing to do.",
        "  Achtung: %s und %s haben unterschiedliche Stellenzahl.": "    Note: %s and %s have a different number of digits.",
        "           Der JamMan sortiert nach Namen - der Patch landet dann": "             The JamMan sorts by name — the patch will land",
        "           auf dem Platz, den sein neuer Name angibt.": "             on the slot its new name says.",
        "  Tauschen:": "    Swapping:",
        "    Platz %-4s %-24s  →  Platz %s": "      slot %-4s %-24s  →  slot %s",
        "  Trockenlauf — nichts getauscht. Mit --schreiben ausführen.": "    Dry run — nothing swapped. Run with --schreiben to write.",
        "  Getauscht. Sicherung: %s": "    Swapped. Backup: %s",
        "  Wird gelöscht: %s  (%s)": "    Will be deleted: %s  (%s)",
        "  Vorher wird er gesichert nach: %s": "    It is backed up first to: %s",
        "  Trockenlauf — nichts gelöscht. Mit --schreiben ausführen.": "    Dry run — nothing deleted. Run with --schreiben to write.",
        "  Gelöscht. Sicherung: %s": "    Deleted. Backup: %s",
        "  Karte auswerfen, in den JamMan stecken — der Patch ist weg.": "    Eject the card, put it into the JamMan — the patch is gone.",
        "  Neue Reihenfolge:": "    New order:",
        "    Platz %d:  %-10s  %s%s": "      slot %d:  %-10s  %s%s",
        "  Verschoben. Sicherung: %s": "    Moved. Backup: %s",
        "  JamMan Solo XT — Speicherkarte verwalten": "    JamMan Solo XT — manage the memory card",
        "  Keine Karte gefunden unter %s.": "    No card found under %s.",
        "  Karte: %s": "    Card: %s",
        "  1  Patches auflisten": "    1  List patches",
        "  2  Patch anhören": "    2  Listen to a patch",
        "  3  Datei aufspielen": "    3  Import a file",
        "  4  Patch ändern (Stopp, Wiedergabe, Name)": "    4  Change a patch (stop, playback, name)",
        "  5  Patch verschieben": "    5  Move a patch",
        "  6  Patch löschen": "    6  Delete a patch",
        "  7  Alles sichern": "    7  Back up everything",
        "  8  Karte auswerfen": "    8  Eject the card",
        "  0  Beenden": "    0  Quit",
        "  %s": "    %s",
        "  System:        %s %s": "    System:        %s %s",
        "  Python:        %s": "    Python:        %s",
        "  Fassung:       %s": "    Version:       %s",
        "  Tk:            %s": "    Tk:            %s",
        "  Tk-Modul:      %s": "    Tk module:     %s",
        "  Tk:            NICHT verfügbar — %s": "    Tk:            NOT available — %s",
        "                 (Fenster geht mit diesem Python nicht;": "                    (the window will not work with this Python;",
        "                  das Textmenü geht trotzdem: jamman.py menu)": "                     the text menu still does: jamman.py menu)",
        "  Ton:           winsound (Windows-eigen)": "    Sound:         winsound (built into Windows)",
        "  Ton:           afplay": "    Sound:         afplay",
        "  Ton:           %s": "    Sound:         %s",
        "  Kartenwurzeln: %s": "    Card roots:    %s",
        "  Einstellungen: %s": "    Settings:      %s",
        "  ffmpeg:        %s": "    ffmpeg:        %s",
        "  Schrift:       %s": "    Font:          %s",
        "  Pythons auf diesem Rechner:": "    Pythons on this machine:",
        "    %-62s %s": "      %-62s %s",
        "  Geprüfte Orte:": "    Places checked:",
        "  Laufwerke mit JamManSoloXT:": "    Drives with JamManSoloXT:",
        "    %-24s %3d Patches  %s": "      %-24s %3d patches  %s",
        "  gewählt:       %s": "    chosen:        %s",
        "  Patches:       %d": "    Patches:       %d",
        "  ACHTUNG: das Dateisystem liefert Lesefehler.": "    WARNING: the file system reports read errors.",
        "  Betroffene Einträge (erste drei): %s": "    Affected entries (first three): %s",
        "  Inhalt von %s:": "    Contents of %s:",
        "    %-24r %s, %s": "      %-24r %s, %s",
        "    nicht lesbar: %s": "      not readable: %s",
        "    %-10s %s": "      %-10s %s",
        "  erste Zeile im Fenster:": "    first line in the window:",
        "    %r": "      %r",
        "JamMan Solo XT": "  JamMan Solo XT",
        "BEFEHL": "COMMAND",
        "  Auswahl: ": "  Choice: ",
        "  Anderer Pfad (leer = beenden): ": "  Another path (empty = quit): ",
        "  Patch-Nummer: ": "  Patch number: ",
        "  Name (leer = Dateiname): ": "  Name (empty = file name): ",
        "  Stopp (fade/instant, leer = wie Vorlage): ": "  Stop (fade/instant, empty = as template): ",
        "  Wiedergabe (loop/single, leer = wie Vorlage): ": "  Playback (loop/single, empty = as template): ",
        "  Pfad zur Audiodatei: ": "  Path to the audio file: ",
        "  Stopp (fade/instant, leer = lassen): ": "  Stop (fade/instant, empty = keep): ",
        "  Wiedergabe (loop/single, leer = lassen): ": "  Playback (loop/single, empty = keep): ",
        "  Neuer Name (leer = lassen): ": "  New name (empty = keep): ",
        "  Wirklich schreiben? (j/n): ": "  Really write? (y/n): ",
        "  Welcher Patch: ": "  Which patch: ",
        "  An welche Stelle: ": "  To which slot: ",
        "  Wirklich verschieben? (j/n): ": "  Really move? (y/n): ",
        "  Zielordner (leer = ~/JamMan-Sicherung): ": "  Target folder (empty = ~/JamMan-Sicherung): ",
        "  Wirklich löschen? (j/n): ": "  Really delete? (y/n): ",
        "Keine Karte gefunden": "No card found",
        "Kein Abspielprogramm gefunden (aplay, paplay, mpv oder ffplay).": "No player found (aplay, paplay, mpv or ffplay).",
        "Fehler: im Protokoll stehen keine Einträge.": "Error: the protocol has no entries.",
        "Fehler: Datei ließ sich nicht umwandeln.": "Error: the file could not be converted.",
        "Fehler: kein vollständiger Patch als Vorlage gefunden.": "Error: no complete patch found to use as a template.",
        "Fehler: nichts angegeben (--stopp, --rhythmus, --wiedergabe, --name oder --fade).": "Error: nothing given (--stopp, --rhythmus, --wiedergabe, --name or --fade).",
        "Fehler: '%s' ist keine lesbare Audiodatei.": "Error: '%s' is not a readable audio file.",
        "Fehler: --teile N oder --laenge SEKUNDEN angeben.": "Error: give --teile N or --laenge SEKUNDEN.",
        "Fehler: %.1f Minuten pro Teil sind zu lang (Grenze %d Minuten).": "Error: %.1f minutes per part is too long (limit %d minutes).",
        "Fehler: '%s' gibt es nicht.": "Error: '%s' does not exist.",
        "Fehler: '%s' ist keine lesbare Audiodatei.%s": "Error: '%s' is not a readable audio file.%s",
        "Fehler: '%s' liegt schon vor.": "Error: '%s' already exists.",
        "Fehler: Die Datei ließ sich nicht kürzen.": "Error: the file could not be shortened.",
        "Fehler: Die Umwandlung hat keine hörbare Aufnahme\n  ergeben (nur Stille). Datei prüfen: %s": "Error: the conversion produced no audible recording\n  (silence only). Check the file: %s",
        "Fehler: Patch%d gibt es nicht.": "Error: patch %d does not exist.",
        "Fehler: Die Aufnahme ließ sich nicht ausblenden (kein 16-Bit-WAV?).": "Error: the recording could not be faded (not a 16-bit WAV?).",
        "Fehler: '%s' ist nicht eingelegt.": "Error: '%s' is not inserted.",
        "Fehler beim Auswerfen: %s": "Error while ejecting: %s",
        "Fehler: Patch%d hat keine WAV.": "Error: patch %d has no WAV.",
        "Fehler: Sicherungsordner '%s' gibt es nicht.": "Error: backup folder '%s' does not exist.",
        "Fehler: in '%s' liegen keine Patch-Ordner.": "Error: '%s' contains no patch folders.",
        "Fehler: keine Sicherung mit einer Aufnahme für Patch%d in '%s'.\n  Fade-Sicherungen heißen Sicherung_<Datum>_vor_fade.": "Error: no backup with a recording for patch %d in '%s'.\n  Fade backups are called Sicherung_<date>_vor_fade.",
        "Fehler: Platz %d ist belegt (%s).\n  Zum Tauschen:  tauschen --patch %d --mit %d": "Error: slot %d is taken (%s).\n  To swap:  tauschen --patch %d --mit %d",
        "Fehler: Nummern müssen zwischen 1 und %d liegen.": "Error: numbers must be between 1 and %d.",
        "Fehler: '%s' ist ein Ordner, keine Datei.": "Error: '%s' is a folder, not a file.",
        "Fehler: '%s' ist kein Pegelprotokoll (keine lesbare Textdatei).": "Error: '%s' is not a level protocol (no readable text file).",
        "Fehler: '%s' darf nicht gelesen werden.": "Error: '%s' may not be read.",
        "Fehler: '%s' lässt sich nicht lesen: %s": "Error: '%s' cannot be read: %s",
        "Fehler beim Umwandeln. Die Datei hat %d Hz / %d Kanal/Kanäle;\n  nötig sind 44100 Hz und 2 Kanäle. Bitte vorher umwandeln oder\n  ffmpeg installieren.": "Error while converting. The file has %d Hz / %d channel(s);\n  44100 Hz and 2 channels are needed. Convert it first or\n  install ffmpeg.",
        "Fehler: '%s' lässt sich nicht schreiben.\n  Nimm einen anderen Ordner:  --sicherung <pfad>": "Error: '%s' cannot be written.\n  Choose another folder:  --sicherung <path>",
        "Fehler: Patch%d hat keine phrase.wav.": "Error: patch %d has no phrase.wav.",
        "Fehler: Der Pegel ließ sich nicht ändern.": "Error: the level could not be changed.",
        "Fehler: in '%s' darf nicht geschrieben werden.\n  Nimm einen anderen Ordner:  --sicherung <pfad>": "Error: '%s' may not be written to.\n  Choose another folder:  --sicherung <path>",
        "Fehler: Teil %d ließ sich nicht schneiden.": "Error: part %d could not be cut.",
        "Fehler: Die Datei ist %.1f Minuten lang.\n  Der JamMan Solo XT nimmt höchstens %d Minuten pro Loop an\n  (Handbuch, Seite 60: \"Max. Individual Loop Time: 10 minutes\").\n  Längere Loops erscheinen auf der Karte, werden vom Pedal aber\n  nicht erkannt - und können dabei verschwinden.\n\n  So geht es weiter:\n    --kuerzen            auf 9:59 kürzen (harter Schnitt am Ende,\n                         12 ms ausgeblendet, damit es nicht knackt)\n    --laenge 480         auf 8:00 kürzen\n    --trotzdem           unverändert schreiben (zum Ausprobieren)": "Error: the file is %.1f minutes long.\n  The JamMan Solo XT takes at most %d minutes per loop\n  (manual, page 60: \"Max. Individual Loop Time: 10 minutes\").\n  Longer loops appear on the card but the pedal does not\n  recognise them - and they can disappear in the process.\n\n  What you can do:\n    --kuerzen            cut to 9:59 (hard cut at the end,\n                         12 ms faded so it does not click)\n    --laenge 480         cut to 8:00\n    --trotzdem           write it unchanged (for trying it out)",
        "Fehler: --wav fehlt.": "Error: --wav missing.",
        "Fehler: --patch fehlt.": "Error: --patch missing.",
        "Fehler: --protokoll fehlt.": "Error: --protokoll missing.",
        "Fehler: --patch fehlt (oder --alle angeben).": "Error: --patch missing (or give --alle).",
        "Fehler: --patch und --auf fehlen.": "Error: --patch and --auf missing.",
        "Fehler: --patch und --mit fehlen.": "Error: --patch and --mit missing.",
        "Fehler: --patch und --nach fehlen.": "Error: --patch and --nach missing.",
        "Neue Fassung verfügbar": "New version available",
        "Es gibt eine neuere Fassung: %s. Sie benutzen %s.":
            "There is a newer version: %s. You are using %s.",
        "Zur Download-Seite wechseln?": "Open the download page?",
        "Es liegt keine JamMan-Karte im Rechner.":
            "There is no JamMan card in this computer.",
        "Nimm die microSD aus dem Pedal und stecke sie in einen Kartenleser "
            "oder Kartenslot. Über das USB-Kabel am Pedal erscheint sie nicht "
            "als Laufwerk.":
            "Take the microSD out of the pedal and put it into a card reader or a "
            "card slot. With the USB cable attached it does not show up as a drive.",
        "Ordner selbst wählen?": "Pick the folder yourself?",
        "Ordner der Karte wählen": "Choose the card's folder",
        "Keine JamMan-Karte": "Not a JamMan card",
        "wie Vorlage": "as template",
        "   ← wird umgewandelt": "   ← will be converted",
        "   ← abweichend": "   ← deviates",
        "   ← über 10 min, Pedal nimmt sie nicht an":
            "   ← over 10 min, the pedal will not accept it",
        "keine WAV": "no WAV",
        "  Das Fenster wäre leer. Es geht im Textmenü weiter.":
            "  The window would be empty. The text menu still works.",
        "  Für ein Fenster: Python von python.org installieren (bringt Tk 8.6).":
            "  For a window: install Python from python.org (ships Tk 8.6).",
        "  Tk %s kann auf diesem macOS keinen Text im Fenster zeichnen.":
            "  Tk %s cannot draw text in a window on this macOS.",
        "  Hinweis: Fade konnte nicht eingerechnet werden.":
            "  Note: the fade could not be applied.",
        "  Hinweis: Pegel ließ sich nicht ändern (kein 16-Bit-WAV?).":
            "  Note: the level could not be changed (not a 16-bit WAV?).",
        "  Achtung: der WAV-Kopf ist weiterhin ungewöhnlich.":
            "  Careful: the WAV header is still unusual.",
        "  Hinweis: Die Vorlage hatte kein Feld IsLoop - es wurde ergänzt.":
            "  Note: the template had no IsLoop field - it was added.",
        "  Hinweis: Die Vorlage hatte kein Feld StopMode - es wurde ergänzt.":
            "  Note: the template had no StopMode field - it was added.",
        "  Hinweis: Die Vorlage hatte kein Feld RhythmType - es wurde ergänzt.":
            "  Note: the template had no RhythmType field - it was added.",
        "  Angehalten.": "  Stopped.",
        "  Das ist kein Fehler des Werkzeugs - die Karte oder":
            "  This is not a program error - the card or",
        "  ihr Einhängepunkt ist beschädigt. Erste Schritte:":
            "  its mount point is damaged. First steps:",
        "    dmesg | tail -20            (Meldungen des Systems)":
            "    dmesg | tail -20            (system messages)",
        "    sudo umount <laufwerk>      (aushängen)":
            "    sudo umount <device>        (unmount)",
        "    sudo fsck.vfat -n <geraet>  (FAT32, nur prüfen)":
            "    sudo fsck.vfat -n <device>  (FAT32, check only)",
        "    sudo exfatfsck <geraet>     (exFAT, nur prüfen)":
            "    sudo exfatfsck <device>     (exFAT, check only)",
        "    oder die Karte im JamMan einschalten - das Pedal":
            "    or put the card into the JamMan and switch it on -",
        "    repariert seine Karte beim Einschalten.":
            "    the pedal repairs its card at start-up.",
        "Patch markieren — dann hier klicken zum Springen":
            "Select a patch — then click here to jump",
        "kein Patch gewählt": "no patch selected",
        "automatisch weiter": "continue automatically",
        "beim Start sichern": "back up on start",
        "Platz": "Slot",
        "nächster": "next",
        "vormerken": "cue",
        "vorgemerkt %d": "cued %d",
        "Einstellungen:": "Settings:",
        "wie Vorlage": "as template",
        "Fade am Ende": "Fade at end",
        "Karte wählen": "Choose card",
        "Datei aufspielen": "Import file",
        "Alles sichern": "Back up all",
        "Pegel zurückrechnen": "Undo level change",
        "Neu laden": "Reload",
        "Anhören": "Play",
        "Stopp": "Stop",
        "Umbenennen": "Rename",
        "Platz ändern": "Change slot",
        "Sicherung zurückholen": "Restore backup",
        "Einstellungen anwenden": "Apply settings",
        "▲ nach oben": "▲ move up",
        "▼ nach unten": "▼ move down",
        "Tauschen": "Swap",
        "Datei teilen": "Split file",
        "Alle normieren": "Normalise all",
        "Löschen": "Delete",
        "Karte auswerfen": "Eject card",
        "Pegel": "Level",
        "auf −1 dB Spitze normieren": "normalise to −1 dB peak",
        "Teile": "Parts",
        "an Pausen schneiden": "cut at pauses",
        "Anhebung auf 0 dB begrenzen": "limit boost to 0 dB",
        "Doppelklick spielt · Ziehen ordnet um · ⌘ für zwei Markierungen, dann Tauschen · Esc hält an.":
            "Double-click plays · drag reorders · ⌘ for two selections, then swap · Esc stops.",
        "vorgemerkt: Platz %d": "cued: slot %d",
        "Ende der Liste erreicht": "end of list reached",
        "Erst einen Patch in der Liste wählen.": "Select a patch in the list first.",
        "Erst eine Karte wählen.": "Choose a card first.",
        "Bitte eine Platznummer eintragen.": "Please enter a slot number.",
        "Platz %d gibt es auf dieser Karte nicht.": "Slot %d does not exist on this card.",
        "MP3 und M4A brauchen ffmpeg -": "MP3 and M4A need ffmpeg -",
        "Anleitung": "Instructions",
        "WAV geht auch ohne": "WAV works without it",
        "Schließen": "Close",
        "Text kopieren": "Copy text",
        "Karte nicht lesbar": "Card not readable",
        "Fehler": "Error",
        "Hinweis": "Note",
        "Anleitung in die Zwischenablage kopiert": "Instructions copied to the clipboard",
        "Sicherung beim Start: %s": "Backup on start: %s",
    },
    "fr": {
        'Dem Python fehlen die Zertifikate. Zwei Wege: im Python-Ordner „Install Certificates.command“ ausführen — oder „pip install certifi“ — oder die Datei von der Release-Seite von Hand laden.':
         'Il manque les certificats à Python. Deux solutions : exécuter « Install Certificates.command » dans le dossier Python, ou « pip install certifi » — ou télécharger le fichier depuis la page de publication.',
        'Achtung: das Zertifikat ließ sich nicht prüfen — die Datei wurde ohne Prüfung geladen.':
         "Attention : le certificat n'a pas pu être vérifié — le fichier a été téléchargé sans contrôle.",
        'Die Datei ist unvollständig: %d statt %d Bytes.':
         'Le fichier est incomplet : %d au lieu de %d octets.',
        'Neue Fassung %s wird geholt …':
         'Récupération de la version %s …',
        'Fassung %s ist installiert. Bitte das Fenster schließen und neu starten.':
         'La version %s est installée. Veuillez fermer la fenêtre et redémarrer.',
        'Update beim Start':
         'Mise à jour au démarrage',
        'Neue Fassung %s wird geholt …':
         'Récupération de la version %s …',
        'Die Karte ist eingebunden, aber das Dateisystem liefert\nLesefehler:\n\n%s\n\nDas liegt an der Karte oder am Einhängepunkt, nicht am\nWerkzeug.\n\nErste Schritte:\n  - Karte im JamMan einschalten (das Pedal prüft sie)\n  - dmesg | tail -20\n  - aushängen und prüfen: sudo fsck.vfat -n <gerät>\n    bzw. sudo exfatfsck <gerät>':
         "La carte est montée, mais le système de fichiers signale des erreurs de lecture :\n\n%s\n\nCela vient de la carte ou du point de montage, pas de l'outil.\n\nPremiers pas :\n  - mettre la carte dans la JamMan et l'allumer (la pédale la vérifie)\n  - dmesg | tail -20\n  - démonter et vérifier : sudo fsck.vfat -n <périphérique>\n    ou sudo exfatfsck <périphérique>",
        'kein Pedal':
         'pas de pédale',
        'Beschreibung …':
         'Description …',
        'Rückwärts: ohne ffmpeg keine Hörprobe — das Pedal spielt es trotzdem rückwärts.':
         "Inversé : sans ffmpeg pas d'écoute — la pédale le joue quand même à l'envers.",
        'Tempo':
         'Tempo',
        'Suchen …':
         'Rechercher …',
        'Text im Namen (leer lassen = alles zeigen):':
         'Texte dans le nom (vide = tout afficher) :',
        'Erst eine Karte oder eine geholte Kopie öffnen — dann läßt sich suchen.':
         "Ouvrez d'abord une carte ou une copie récupérée — ensuite la recherche fonctionne.",
        'Suchen':
         'Rechercher',
        'Suche fehlgeschlagen: %s':
         'Recherche échouée : %s',
        'Suchen:':
         'Rechercher :',
        'Karte:':
         'Carte :',
        'Meldungen stehen im Fenster; Fehler zusätzlich in jamman-fehler.log.':
         "Les messages s'affichent dans la fenêtre ; les erreurs aussi dans jamman-fehler.log.",
        'Firmware-Dateien jetzt ins Pedal schreiben? Ein Abbruch mittendrin kann das Pedal unbrauchbar machen. [ja/nein] ':
         'Écrire maintenant les fichiers de firmware dans la pédale ? Une interruption peut la rendre inutilisable. [oui/non] ',
        'Zielordner für die Firmware':
         'Dossier cible de la firmware',
        'Ordner mit der geholten Firmware':
         'Dossier de la firmware récupérée',
        'In diesem Ordner liegt keine Firmware (usr/JamManUI, usr/loopengine …).':
         'Ce dossier ne contient pas de firmware (usr/JamManUI, usr/loopengine …).',
        'Firmware übernehmen':
         'Appliquer la firmware',
        'Firmware übernehmen …':
         'Appliquer la firmware …',
        'Firmware dieses Pedals holen …':
         'Récupérer la firmware de cette pédale …',
        '%s kam unvollständig (%d von %d Bytes).':
         '%s est arrivé incomplet (%d sur %d octets).',
        '%d Firmware-Dateien werden jetzt in das Pedal geschrieben. Vorher wird der jetzige Stand gesichert.\n\nEin Abbruch mittendrin kann das Pedal unbrauchbar machen — es gibt keinen Weg zurück.\n\nWirklich schreiben?':
         "%d fichiers de firmware vont être écrits dans la pédale. L'état actuel est sauvegardé avant.\n\nUne interruption peut rendre la pédale inutilisable — aucun retour.\n\nÉcrire vraiment ?",
        'Sichere den jetzigen Stand …':
         "Sauvegarde de l'état actuel …",
        'Gesichert: %d Dateien nach %s':
         'Sauvegardé : %d fichiers dans %s',
        'Firmware geholt: %d Dateien':
         'Firmware récupérée : %d fichiers',
        'Firmware geholt:\n%s\n%d Dateien':
         'Firmware récupérée :\n%s\n%d fichiers',
        'Fertig: %d Dateien geschrieben':
         'Terminé : %d fichiers écrits',
        'Nicht bestätigt: %d':
         'Non confirmé : %d',
        'Firmware geschrieben: %d Dateien\nSicherung: %s\nJetzt das Pedal aus- und wieder einschalten.':
         'Firmware écrite : %d fichiers\nSauvegarde : %s\nÉteignez puis rallumez la pédale.',
        'Schreibe':
         'Écriture',
        'Hole':
         'Récupération',
        'JamList aus JamManager XT holen …':
         'Récupérer la JamList de JamManager XT …',
        'Setlist als JamList ablegen …':
         'Enregistrer la setlist comme JamList …',
        'Setlist speichern':
         'Enregistrer la setlist',
        'Bibliothek von JamManager XT':
         'Bibliothèque de JamManager XT',
        'JamList wählen':
         'Choisir une JamList',
        'In dieser Bibliothek liegt keine JamList.':
         'Cette bibliothèque ne contient aucune JamList.',
        'Geholt: %s\n%d Plätze\n%s':
         'Récupéré : %s\n%d emplacements\n%s',
        'JamList ablegen':
         'Enregistrer la JamList',
        '%s%s%s als neue JamList eintragen? Die Bibliothek wird dabei verändert — vorher eine Kopie anlegen.':
         "Ajouter %s%s%s comme nouvelle JamList ? La bibliothèque sera modifiée — faites une copie d'abord.",
        'JamList %s angelegt (id %d, %d Plätze)':
         'JamList %s créée (id %d, %d emplacements)',
        'Keine JamList mit dem Namen %s.':
         'Aucune JamList nommée %s.',
        'belegte Plätze':
         'emplacements occupés',
        'Geschrieben: %s (%d Plätze)':
         'Écrit : %s (%d emplacements)',
        'Übernehmen':
         'Appliquer',
        'Speicherabbild':
         'Image mémoire',
        'Speicherabbild speichern':
         "Enregistrer l'image mémoire",
        'Speicherabbild aufnehmen …':
         'Prendre une image mémoire …',
        'Speicherabbilder vergleichen …':
         'Comparer les images mémoire …',
        'Speicherabbilder vergleichen':
         'Comparer les images mémoire',
        'Erstes Speicherabbild':
         'Première image mémoire',
        'Zweites Speicherabbild':
         'Deuxième image mémoire',
        'Nur im ersten Bild: %d':
         'Seulement dans la première image : %d',
        'Nur im zweiten Bild: %d':
         'Seulement dans la deuxième image : %d',
        'Gleicher Pfad, andere Größe: %d':
         'Même chemin, taille différente : %d',
        'Die beiden Bilder sind gleich.':
         'Les deux images sont identiques.',
        'Erstes:':
         'Première :',
        'Zweites:':
         'Deuxième :',
        'Aufruf:':
         'Appel :',
        'Lese den Verzeichnisbaum …':
         "Lecture de l'arborescence …",
        'Speicherabbild: %d Einträge':
         'Image mémoire : %d entrées',
        'Speicherabbild geschrieben:\n%s':
         'Image mémoire enregistrée :\n%s',
        'Speicherabbild geschrieben: %d Einträge nach %s':
         'Image mémoire enregistrée : %d entrées dans %s',
        'Dateien für „speicherabbild“ und „vergleich“':
         'Fichiers pour « speicherabbild » et « vergleich »',
        'Suchen:':
         'Rechercher :',
        'Sortierung':
         'Tri',
        '%d von %d Patches passen zu „%s“.':
         '%d patches sur %d correspondent à « %s ».',
        'Beschreibung':
         'Description',
        'Interpret':
         'Artiste',
        'Genre':
         'Genre',
        'Stichwörter':
         'Mots-clés',
        'Bewertung':
         'Note',
        'Tempo (BPM)':
         'Tempo (BPM)',
        'Die Datei %s fehlt.':
         'Le fichier %s manque.',
        'Setlists anpassen':
         'Adapter les setlists',
        '%d Setlist-Dateien in %s an die neuen Plätze anpassen? Jede Datei wird vorher daneben gesichert.':
         "Adapter %d fichiers de setlist dans %s aux nouveaux emplacements ? Chaque fichier est d'abord sauvegardé à côté.",
        'Keine Setlist nannte einen bewegten Platz.':
         'Aucune setlist ne mentionnait un emplacement déplacé.',
        'Setlists nicht angepaßt: ':
         'Setlists non adaptées : ',
        'Setlists angepaßt: ':
         'Setlists adaptées : ',
        'Plätze aufrücken':
         'Combler les trous',
        'Plätze aufrücken …':
         'Combler les trous …',
        'Es gibt keine Lücken — die Plätze sind schon lückenlos belegt.':
         "Il n'y a pas de trous — les emplacements sont déjà contigus.",
        '%d Patches rücken in %s nach vorn: %s%s\n\nJeder bewegte Platz wird vorher auf den Rechner gesichert. Setlists im Pedal, die auf Platznummern zeigen, stimmen danach nicht mehr. Fortfahren?':
         '%d patches remontent dans %s : %s%s\n\nChaque emplacement déplacé est sauvegardé avant. Les setlists de la pédale qui pointent sur des numéros ne correspondront plus. Continuer ?',
        'Rücke auf':
         'Remontée',
        'Fertig: %d Plätze aufgerückt, %d vorher gesichert':
         'Terminé : %d emplacements remontés, %d sauvegardés avant',
        'Aufgerückt: %d Plätze':
         'Remontés : %d emplacements',
        'Kopie auf dem Rechner — Änderungen hier wirken nicht auf das Pedal.':
         "Copie sur l'ordinateur — les modifications ici n'affectent pas la pédale.",
        'Patch%d wird aus der Kopie auf dem Rechner entfernt. Das Pedal bleibt unverändert. Fortfahren?':
         "Patch%d sera supprimé de la copie sur l'ordinateur. La pédale reste inchangée. Continuer ?",
        'Patch%d wird von der Karte entfernt. Vorher wird er auf dem Rechner gesichert. Fortfahren?':
         "Patch%d sera supprimé de la carte. Il est d'abord sauvegardé sur l'ordinateur. Continuer ?",
        'Ja':
         'Oui',
        'Nein':
         'Non',
        'Dem Python fehlen die Zertifikate. Auf macOS hilft „Install Certificates.command“ im Python-Ordner, oder: pip install certifi':
         "Python n'a pas de certificats. Sous macOS, lancez « Install Certificates.command » dans le dossier Python, ou : pip install certifi",
        'GitHub ist nicht erreichbar: %s':
         'GitHub est injoignable : %s',
        'unbekannter Grund':
         'raison inconnue',
        'Gelöscht: %s':
         'Supprimé : %s',
        'Fehler:':
         'Erreur :',
        'Platz löschen':
         "Supprimer l'emplacement",
        'Welcher Platz soll gelöscht werden? (z. B. 30 oder Patch30)':
         'Quel emplacement supprimer ? (p. ex. 30 ou Patch30)',
        'Platz löschen …':
         "Supprimer l'emplacement …",
        '%s ist nicht belegt — nichts zu löschen.':
         "%s n'est pas occupé — rien à supprimer.",
        '%s wird aus %s gelöscht. Das läßt sich nicht rückgängig machen. Fortfahren?':
         '%s sera supprimé de %s. Action irréversible. Continuer ?',
        'Lösche':
         'Suppression',
        'Gelöscht:':
         'Supprimé :',
        'Fehler beim Löschen:':
         'Erreur lors de la suppression :',
        'Die Datei ist %s lang — das Pedal nimmt höchstens 10 Minuten je Loop an. Bitte im Hauptfenster kürzen oder teilen.':
         'Le fichier dure %s — la pédale accepte au maximum 10 minutes par boucle. Veuillez le raccourcir ou le diviser dans la fenêtre principale.',
        'Dieser Ordner ist nicht lesbar: %s':
         "Ce dossier n'est pas lisible : %s",
        'Auswahl:':
         'Sélection :',
        'Pegelprotokoll wählen':
         'Choisir le journal de niveau',
        'Audiodatei zum Teilen auswählen':
         'Choisir le fichier audio à diviser',
        'Dateiname:':
         'Nom du fichier :',
        'Speichern':
         'Enregistrer',
        'Bitte einen Dateinamen angeben.':
         'Veuillez saisir un nom de fichier.',
        'Überschreiben':
         'Écraser',
        '%s gibt es schon. Ersetzen?':
         '%s existe déjà. Remplacer ?',
        'Speicherkarte des JamMan wählen':
         'Choisir la carte du JamMan',
        'Zielordner für die Sicherung':
         'Dossier cible de la sauvegarde',
        'Dateien':
         'Fichiers',
        'Nach oben':
         'Dossier parent',
        'Öffnen':
         'Ouvrir',
        'Bitte eine Datei auswählen.':
         'Veuillez choisir un fichier.',
        'Alle Dateien':
         'Tous les fichiers',
        'Die Datei konnte nicht geschrieben werden: %s':
         "Le fichier n'a pas pu être écrit : %s",
        'ohne':
         'aucun',
        'ende':
         'fin',
        'klick':
         'clic',
        '%d Patches ohne Ton — nur die Beschreibung ist geholt.':
         '%d patches sans audio — seule la description a été récupérée.',
        'Ganzen Ordner aufs Pedal …':
         'Tout le dossier vers la pédale …',
        'Ordner mit Patch-Ordnern wählen':
         'Choisir un dossier contenant des dossiers de patch',
        'Ganzen Ordner aufs Pedal':
         'Tout le dossier vers la pédale',
        '%d Patch-Ordner werden in %s geschrieben. Belegte Plätze werden vorher gesichert, unveränderte übersprungen. Fortfahren?':
         '%d dossiers de patch seront écrits dans %s. Les emplacements occupés sont sauvegardés avant, les inchangés ignorés. Continuer ?',
        'internen Speicher':
         'la mémoire interne',
        'der Karte':
         'la carte',
        'Im Ordner liegen keine Patch-Ordner (erwartet: Patch01 … Patch200).':
         'Le dossier ne contient aucun dossier de patch (attendu : Patch01 … Patch200).',
        'Schreibe':
         'Écriture',
        'Fertig: %d Patches geschrieben, %d übersprungen, %d vorher gesichert':
         'Terminé : %d patches écrits, %d ignorés, %d sauvegardés avant',
        'Fehler beim Schreiben:':
         "Erreur lors de l'écriture :",
        'Übertragen: %d Dateien · unverändert übersprungen: %d':
         'Transféré : %d fichiers · ignorés inchangés : %d',
        'Zielplatz auf dem Pedal (z. B. 17 oder Patch17):':
         'Emplacement cible sur la pédale (p. ex. 17 ou Patch17) :',
        'Unerwarteter Fehler':
         'Erreur inattendue',
        'Das Werkzeug wurde unerwartet beendet.':
         "L'outil s'est arrêté de façon inattendue.",
        'Protokoll:':
         'Journal :',
        'Auswahl kopieren':
         'Copier la sélection',
        'Nichts markiert — bitte zuerst Text markieren.':
         "Rien de sélectionné — sélectionnez d'abord du texte.",
        'In die Zwischenablage kopiert.':
         'Copié dans le presse-papiers.',
        'Anleitung speichern':
         'Enregistrer les instructions',
        'Gespeichert.':
         'Enregistré.',
        'Alles kopieren':
         'Tout copier',
        'Als Datei speichern':
         'Enregistrer comme fichier',
        'Anleitung':
         'Instructions',
        'Zum Umwandeln fehlt ffmpeg.':
         'ffmpeg manque pour la conversion.',
        'Die Programmdateien lassen sich hier nicht ersetzen: %s':
         'Les fichiers du programme ne peuvent pas être remplacés ici : %s',
        'Unerwarteter Fehler: %s':
         'Erreur inattendue : %s',
        'Aktualisieren':
         'Mettre à jour',
        'Die neueste Fassung wird von GitHub geladen und ersetzt die Programmdateien. Ihre Einstellungen und Ihre Karte bleiben unberührt. Fortfahren?':
         'La dernière version est téléchargée depuis GitHub et remplace les fichiers du programme. Vos réglages et votre carte ne sont pas touchés. Continuer ?',
        'Jetzt aktualisieren?':
         'Mettre à jour maintenant ?',
        'Aktualisiert':
         'Mis à jour',
        'Nicht aktualisiert':
         'Non mis à jour',
        'Fenster jetzt schließen?':
         'Fermer la fenêtre maintenant ?',
        'GitHub ist nicht erreichbar.':
         'GitHub est injoignable.',
        'Sie benutzen bereits die neueste Fassung (%s).':
         'Vous utilisez déjà la dernière version (%s).',
        'Zur Fassung %s gibt es keine Datei zum Herunterladen.':
         "La version %s n'a pas de fichier à télécharger.",
        'Lade Fassung %s …':
         'Téléchargement de la version %s …',
        'Herunterladen fehlgeschlagen: %s':
         'Téléchargement échoué : %s',
        'Die heruntergeladene Datei ist kein gültiges ZIP.':
         "Le fichier téléchargé n'est pas un ZIP valide.",
        'Die Datei ist beschädigt (%s).':
         'Le fichier est endommagé (%s).',
        'In der Datei fehlt jamman.py.':
         'Le fichier ne contient pas jamman.py.',
        'Die Datei gehört nicht zur Fassung %s.':
         "Le fichier n'appartient pas à la version %s.",
        'Fassung %s ist installiert (%d Dateien). Bitte das Fenster schließen und neu starten.':
         'La version %s est installée (%d fichiers). Fermez la fenêtre et relancez le programme.',
        'Die Karte hängt nicht im Rechner — nichts auszuwerfen. Steckt sie im Pedal, führt das Pedal sie nicht als Laufwerk.':
         "La carte n'est pas dans l'ordinateur — rien à éjecter. Si elle est dans la pédale, celle-ci ne la présente pas comme un lecteur.",
        'Auswerfen ging nicht. Bitte im Dateimanager auf „Auswerfen" klicken.':
         "L'éjection a échoué. Utilisez « Éjecter » dans le gestionnaire de fichiers.",
        'Keine Karte im Rechner gefunden.':
         'Aucune carte trouvée dans l\'ordinateur.',
        'Karte im Rechner:  „Karte wählen“ drücken.':
         'Carte dans l\'ordinateur : appuyez sur « Choisir la carte ».',
        'Karte im Pedal:    oben rechts „Pedal über USB“ — dort sind Karte und interner Speicher erreichbar.':
         'Carte dans la pédale : en haut à droite « Pédale par USB » — la carte et la mémoire interne y sont accessibles.',
        'Speicherkarte': 'Carte mémoire',
        'Kopie vom Pedal': 'copie de la pédale',
        'Speicherkarte des JamMan Stereo': 'Carte mémoire du JamMan Stereo',
        'Im Pedal steckt keine Karte.': 'Aucune carte dans la pédale.',
        'Karte einlegen und dann „Karte wählen“ oder „Neu laden“ drücken.':
         'Insérez la carte, puis appuyez sur « Choisir la carte » ou « Recharger ».',
        'keine Karte':
         'aucune carte',
        'Dies ist eine Kopie vom Pedal, nicht die Karte selbst. Über „Karte wählen“ die Karte öffnen.':
         'Ceci est une copie prise sur la pédale, pas la carte elle-même. Utilisez « Choisir la carte ».',
        'JamMan Card Tool': 'JamMan Card Tool',
        'Karte und interner Speicher': 'carte et mémoire interne',
        '(für Rückgängig nötig)':
         '(nécessaire pour Annuler)',
        'Darunter ist die Sicherung für Rückgängig. Danach läßt sich der letzte Schreibvorgang nicht mehr zurücknehmen. Trotzdem löschen?':
         'Cela inclut la sauvegarde pour Annuler. La dernière écriture ne pourra plus être annulée. Supprimer quand même ?',
        'Sicherungen aufräumen …':
         'Nettoyer les sauvegardes …',
        'Sicherungen aufräumen':
         'Nettoyer les sauvegardes',
        'Sicherungen im Zielordner':
         'Sauvegardes dans le dossier cible',
        '%d Sicherungen · %.1f MB':
         '%d sauvegardes · %.1f Mo',
        'Neueste behalten:':
         'Conserver les plus récentes :',
        'Markierte löschen':
         'Supprimer la sélection',
        'Erst Sicherungen in der Liste markieren.':
         'Sélectionnez d\'abord des sauvegardes.',
        '%d Sicherungen mit %.1f MB werden endgültig gelöscht. Fortsetzen?':
         '%d sauvegardes (%.1f Mo) seront définitivement supprimées. Continuer ?',
        '%d Sicherungen konnten nicht gelöscht werden.':
         '%d sauvegardes n\'ont pas pu être supprimées.',
        'Der Zielordner ist noch nicht angelegt.':
         'Le dossier cible n\'existe pas encore.',
        'Im Zielordner liegen keine Sicherungen.':
         'Il n\'y a aucune sauvegarde dans le dossier cible.',
        'Alle auswählen':
         'Tout sélectionner',
        'Nichts markiert. Alle %d Patches in die Setlist?':
         'Rien de sélectionné. Mettre les %d patches dans la setlist ?',
        'Warte auf das Ende des laufenden Vorgangs …':
         'Attente de la fin de l\'opération en cours …',
        'Der Vorgang reagiert nicht — Fenster schließen und neu öffnen.':
         'L\'opération ne répond pas — fermez la fenêtre et rouvrez-la.',
        'Hole Beschreibungen': 'Lecture des descriptions',
        'Es wurde kein Zielplatz eingegeben.':
         'Aucun emplacement cible n\'a été saisi.',
        'Der Zielplatz muß wie Patch01 … Patch200 heißen, nicht „%s“.':
         'L\'emplacement doit s\'appeler Patch01 … Patch200, et non « %s ».',
        'OK': 'OK',
        'Ein Vorgang läuft noch — bitte abwarten.':
         'Une opération est encore en cours — veuillez patienter.',
        'Verbindung neu aufbauen':
         'Reconnecter',
        'Verbindung wird neu aufgebaut …':
         'Reconnexion …',
        'Lesen und Schreiben über USB. Vor jedem Überschreiben wird der alte Platz auf den Rechner gesichert.':
         'Lecture et écriture par USB. Avant tout écrasement, l\'ancien emplacement est sauvegardé sur l\'ordinateur.',
        'Über USB fließen etwa 1,3 MB/s. Für eine ganze Karte ist ein Kartenleser viel schneller. Schreiben verändert die Karte — vorher wird gesichert.':
         'Par USB, le débit est d\'environ 1,3 Mo/s. Pour une carte entière, un lecteur est bien plus rapide. L\'écriture modifie la carte — sauvegarde au préalable.',
        'Audio in den Speicher schreiben …': 'Écrire l\'audio dans la mémoire …',
        'Beim JamMan Solo XT behält der FS3X seine Grundfunktionen. Eine eigene Belegung speichert das Pedal zwar, wertet sie aber nicht aus — deshalb wird sie hier nicht angeboten.':
         'Sur le JamMan Solo XT, le FS3X garde ses fonctions de base. La pédale enregistre bien une affectation personnalisée mais ne l\'exploite pas ; elle n\'est donc pas proposée ici.',
        'Änderungen wirken nach einem Neustart des Pedals.': 'Les modifications prennent effet après un redémarrage de la pédale.',
        'gewünscht': 'demandé',
        'Pedal meldet': 'la pédale renvoie',
        '(nicht belegt)':
         '(non attribué)',
        'UndoRedo = Rückgängig/Wiederholen · Reverse = Rückwärts · Tempo = Tempo antippen':
         'UndoRedo = annuler/rétablir · Reverse = en arrière · Tempo = taper le tempo',
        'Sichern': 'Enregistrer',
        'Schreiben': 'Écrire',
        'Setlists': 'Setlists',
        'Pedal': 'Pédale',
        'Ins Hauptfenster holen …': 'Charger dans la fenêtre principale …',
        'Einzelnen Patch sichern …': 'Enregistrer un seul patch …',
        'Patch-Ordner aufs Pedal …': 'Dossier de patch sur la pédale …',
        'Übernommen.': 'Appliqué.',
        'Pedal spielt jetzt:': 'La pédale joue maintenant :',
        'Audio aufs Pedal …':
         'Audio sur la pédale …',
        'Audio aufs Pedal':
         'Audio sur la pédale',
        'Audiodatei wählen':
         'Choisir un fichier audio',
        'Audio':
         'Audio',
        'Sichere den alten Platz …':
         'Sauvegarde de l\'ancien emplacement …',
        'Wandle die Datei …':
         'Conversion du fichier …',
        'Umwandeln fehlgeschlagen — die Datei muß 44,1 kHz, 16 Bit, Stereo werden.':
         'Conversion échouée — le fichier doit devenir 44,1 kHz, 16 bits, stéréo.',
        'Leere den Platz …': 'Vidage de l\'emplacement …',
        'Pedal spielt jetzt Bank/Patch:': 'La pédale joue maintenant banque/patch :',
        'Karte erkannt:': 'Carte détectée :',
        'Als ZIP sichern …':
         'Enregistrer en ZIP …',
        'Als ZIP sichern':
         'Enregistrer en ZIP',
        'Sichere als ZIP …':
         'Enregistrement en ZIP …',
        'Sichere:':
         'Enregistrement :',
        'Fehler beim Sichern:':
         'Erreur d\'enregistrement :',
        'Die Setlist ist leer.':
         'La setlist est vide.',
        'Erst Patches in der Liste markieren (⌘-Klick).':
         'Sélectionnez d\'abord des patches dans la liste (⌘-clic).',
        'Es gibt nichts rückgängig zu machen.':
         'Il n\'y a rien à annuler.',
        'Fußschalter …':
         'Commande au pied …',
        'Fußschalter':
         'Commande au pied',
        'Tastenbelegung des Pedals':
         'Affectation des commandes au pied de la pédale',
        'Werte sind Geräteeinstellungen — nur ändern, wenn du weißt, was sie bedeuten.':
         'Ce sont des réglages de l\'appareil — ne les modifiez que si vous savez ce qu\'ils font.',
        'Übernehmen':
         'Appliquer',
        'JamMan':
         'JamMan',
        'Karte':
         'carte',
        'intern':
         'interne',
        'Liste als Textdatei sichern':
         'Enregistrer la liste en fichier texte',
        'Liste als Textdatei …':
         'Liste en fichier texte …',
        'Mitlaufen':
         'Suivi',
        'Mitlaufen an — am Pedal einen Patch wählen.':
         'Suivi activé — choisissez un patch sur la pédale.',
        'Mitlaufen aus.':
         'Suivi désactivé.',
        'Rückgängig':
         'Annuler',
        'Setlist holen …':
         'Charger la setlist …',
        'Setlist laden':
         'Charger la setlist',
        'Setlist sichern …':
         'Enregistrer la setlist …',
        'Setlist sichern':
         'Enregistrer la setlist',
        'Setlist.txt':
         'Setlist.txt',
        'Stelle wieder her:':
         'Restauration :',
        '%s wird durch die Sicherung ersetzt. Fortsetzen?':
         '%s sera remplacé par la sauvegarde. Continuer ?',
        'Ordner des Patches wählen':
         'Choisir le dossier du patch',
        'Aufs Pedal schreiben':
         'Écrire sur la pédale',
        '%s ist schon belegt. Der Patch wird vorher auf den Rechner gesichert und dann überschrieben. Fortsetzen?':
         '%s est déjà occupé. Ce patch sera d\'abord enregistré sur l\'ordinateur, puis écrasé. Continuer ?',
        'Schreibe aufs Pedal …':
         'Écriture sur la pédale …',
        'Schreibe:':
         'Écriture :',
        'Fehler beim Schreiben:':
         'Erreur d\'écriture :',
        'Patch aufs Pedal schreiben …':
         'Écrire le patch sur la pédale …',
        '(Vorschlag)': '(proposition)',
        'Ziel:': 'Destination :',
        'noch nicht gewählt': 'pas encore choisi',
        'Ziel ändern …': 'Changer la destination …',
        'Aufräumen':
         'Nettoyer',
        'Die angefangene Kopie entfernen? Nur die gerade geholten Dateien werden gelöscht, der Ordner bleibt.':
         'Supprimer la copie inachevée ? Seuls les fichiers venant d\'être récupérés sont supprimés ; le dossier reste.',
        'Aufgeräumt:':
         'Nettoyé :',
        'Hole Beschreibungen …': 'Lecture des descriptions …',
        'Wird abgebrochen …': 'Annulation …',
        'Über USB fließen etwa 1,3 MB/s. Für eine ganze Karte ist ein Kartenleser viel schneller — steckt die Karte im Pedal, bleibt nur dieser Weg.':
         'Par USB, le débit est d\'environ 1,3 Mo/s. Pour une carte entière, un lecteur de cartes est bien plus rapide — si la carte est dans la pédale, c\'est la seule voie.',
        'Abbrechen': 'Annuler',
        'Abgebrochen.': 'Annulé.',
        'Fehler beim Öffnen:': 'Erreur à l\'ouverture :',
        'Alle ins Hauptfenster holen …': 'Tout charger dans la fenêtre principale …',
        'Zielordner wählen': 'Choisir le dossier de destination',
        'Hole Patch': 'Lecture du patch',
        'Fertig:': 'Terminé :',
        'Im Hauptfenster öffnen?': 'Ouvrir dans la fenêtre principale ?',
        'Gesichert': 'Enregistré',
        'Pedal über USB': 'Pédale par USB',
        'Das USB-Modul fehlt: greenglass.py muß neben jamman.py liegen.': 'Le module USB manque : greenglass.py doit se trouver à côté de jamman.py.',
        'Gesichert nach:': 'Enregistré dans :',
        'Interner Speicher': 'Mémoire interne',
        'SD-Karte': 'Carte SD',
        'Kein JamMan gefunden — Pedal einschalten und Kabel prüfen.': 'Aucun JamMan trouvé — allumez la pédale et vérifiez le câble.',
        '(ohne Namen)': '(sans nom)',
        'Fehler beim Lesen:': 'Erreur de lecture :',
        'Erst „Neu laden“ drücken.': 'Appuyez d\'abord sur « Recharger ».',
        'Lädt …': 'Chargement …',
        'Nur lesen: das Pedal wird nicht verändert.': 'Lecture seule : la pédale n\'est pas modifiée.',
        'Ordner zum Sichern wählen': 'Choisir un dossier de sauvegarde',
        'Patch sichern …': 'Enregistrer le patch …',
        'Verbinde mit dem Pedal …': 'Connexion à la pédale …',
        'Spenden': 'Faire un don',
        'Für dieses Werkzeug ist noch kein Spendenlink eingetragen.': "Aucun lien de don n'est encore renseigné pour cet outil.",
        'Er steht in jamman.py in der Zeile SPENDEN_LINK.': 'Il se place dans jamman.py, ligne SPENDEN_LINK.',
        '      Schnitt %d bei %s: %s': '      Coupe %d à %s : %s',
        'keine Pause in der Nähe - rechnerisch geschnitten': 'aucune pause à proximité — coupe calculée',
        "Tk:            %s": "Tk:            %s",
        'Sicherung': 'Sauvegarde',
        'Aufspielen': 'Importer',
        'Der Name darf nicht leer sein.': 'Le nom ne doit pas être vide.',
        'Bitte 2 bis 60 Teile angeben.': 'Indiquer de 2 à 60 parties.',
        'Die Datei ist nicht lesbar.': 'Le fichier est illisible.',
        'Alle Patches normieren': 'Normaliser tous les patches',
        'Alle Patches werden auf gleiche Lautheit gebracht:\n\n  Ziel -18 dB Lautheit, Spitze höchstens -1 dB\n\nVorher wird die ganze Karte gesichert - das dauert bei\n900 MB ein bis zwei Minuten. Fortfahren?': "Tous les patches sont amenés à la même sonie :\n\n  cible -18 dB de sonie, crête au plus -1 dB\n\nLa carte est d'abord sauvegardée - cela prend\nune à deux minutes pour 900 Mo. Continuer ?",
        'Zum Tauschen genau ZWEI Patches markieren:\nersten anklicken, zweiten mit gedrückter Cmd-Taste (⌘).': 'Pour échanger, sélectionner exactement DEUX patches :\ncliquer le premier, puis le second avec Cmd (⌘) enfoncé.',
        'Oben steht überall „wie Vorlage - dann gibt es nichts zu ändern.': "Partout « comme le modèle » - il n'y a rien à appliquer.",
        'Erst einen Patch wählen.': "Choisir d'abord un patch.",
        'Karte auswerfen? Danach kann sie abgezogen werden.': 'Éjecter la carte ? Elle pourra ensuite être retirée.',
        'Keine JamMan-Karte': 'Pas de carte JamMan',
        'Datei zu lang': 'Fichier trop long',
        'Beim Pegel bitte eine Zahl in dB eintragen (z. B. -3), oder das Feld leer lassen.': 'Entrer un niveau en dB (par ex. -3), ou laisser le champ vide.',
        'Bei Teile bitte eine ganze Zahl eintragen.': 'Indiquer un nombre entier de parties.',
        'Beim Pegel bitte eine Zahl in dB eintragen (z. B. -3).': 'Entrer un niveau en dB (par ex. -3).',
        'Bei Fade am Ende bitte eine Zahl in Sekunden eintragen.': 'Indiquer la durée du fondu en secondes.',
        'Pegel zurückgerechnet': 'niveau annulé',
        'alle Patches normiert': 'tous les patches normalisés',
        'Fertig.': 'Terminé.',
        'Tauschen:': 'Échange :',
        'Karte auswerfen, in den JamMan stecken — der Patch ist weg.': 'Éjecter la carte, la mettre dans le JamMan — le patch a disparu.',
        'Neue Reihenfolge:': 'Nouvel ordre :',
        'JamMan Solo XT — Speicherkarte verwalten': 'JamMan Solo XT — gérer la carte mémoire',
        'Pythons auf diesem Rechner:': 'Pythons sur cette machine :',
        'Geprüfte Orte:': 'Emplacements vérifiés :',
        'Laufwerke mit JamManSoloXT:': 'Lecteurs avec JamManSoloXT :',
        'Trockenlauf — nichts geändert. Mit --schreiben ausführen.': 'Simulation — rien modifié. Exécuter avec --schreiben.',
        'Trockenlauf — nichts kopiert. Mit --schreiben ausführen.': 'Simulation — rien copié. Exécuter avec --schreiben.',
        'Trockenlauf — nichts angelegt. Mit --schreiben ausführen.': 'Simulation — rien créé. Exécuter avec --schreiben.',
        'Trockenlauf — nichts geschrieben. Mit --schreiben ausführen.': 'Simulation — rien écrit. Exécuter avec --schreiben.',
        'Trockenlauf — nichts zurückgeholt. Mit --schreiben ausführen.': 'Simulation — rien restauré. Exécuter avec --schreiben.',
        'Trockenlauf — nichts umbenannt. Mit --schreiben ausführen.': 'Simulation — rien renommé. Exécuter avec --schreiben.',
        'Trockenlauf — nichts getauscht. Mit --schreiben ausführen.': 'Simulation — rien échangé. Exécuter avec --schreiben.',
        'Trockenlauf — nichts gelöscht. Mit --schreiben ausführen.': 'Simulation — rien supprimé. Exécuter avec --schreiben.',
        'Keine Patches auf der Karte.': 'Aucun patch sur la carte.',
        'Suche musikalisch brauchbare Schnittstellen …': 'Recherche de points de coupe musicaux …',
        'Kopf:         WAV-Kopf auf einfaches PCM umgeschrieben': 'En-tête : en-tête WAV réécrit en PCM simple',
        'Alle Ordner heißen schon so. Nichts zu tun.': 'Tous les dossiers portent déjà ce nom. Rien à faire.',
        'Hinweis: diese gibt es auf der Karte nicht und werden neu angelegt:': 'Remarque : ceux-ci ne sont pas sur la carte et seront créés :',
        'Nichts zu tun.': 'Rien à faire.',
        '1  Patches auflisten': '1  Lister les patches',
        '2  Patch anhören': '2  Écouter un patch',
        '3  Datei aufspielen': '3  Importer un fichier',
        '4  Patch ändern (Stopp, Wiedergabe, Name)': '4  Modifier un patch (arrêt, lecture, nom)',
        '5  Patch verschieben': '5  Déplacer un patch',
        '6  Patch löschen': '6  Supprimer un patch',
        '7  Alles sichern': '7  Tout sauvegarder',
        '8  Karte auswerfen': '8  Éjecter la carte',
        '0  Beenden': '0  Quitter',
        'Angehalten.': 'Arrêté.',
        'erste Zeile im Fenster:': 'première ligne dans la fenêtre :',
        'ACHTUNG: das Dateisystem liefert Lesefehler.': 'ATTENTION : le système de fichiers signale des erreurs de lecture.',
        'Das ist kein Fehler des Werkzeugs - die Karte oder': "Ce n'est pas une erreur du programme — la carte ou",
        'ihr Einhängepunkt ist beschädigt. Erste Schritte:': 'son point de montage est endommagé. Premiers pas :',
        'Ton:           afplay': 'Son :         afplay',
        'Ton:           winsound (Windows-eigen)': 'Son :         winsound (intégré à Windows)',
        'Hinweis: Pegelprotokoll konnte nicht geschrieben werden.': "Remarque : le journal des niveaux n'a pas pu être écrit.",
        'Sicherung beim Start: Karte unverändert, nichts zu tun.': 'Sauvegarde au démarrage : carte inchangée, rien à faire.',
        'Pause an der Naht wird herausgenommen': 'La pause à la jonction est supprimée',
        'Quelle:       %s': 'Source :       %s',
        'Quelle: %s': 'Source : %s',
        'Ziel:   %s': 'Cible :   %s',
        'Ziel: %.1f dB Lautheit (RMS), Spitze höchstens %.1f dB': 'Cible : %.1f dB de sonie (RMS), crête au plus %.1f dB',
        'Karte: %s': 'Carte : %s',
        'Name:         %s': 'Nom :         %s',
        'Dauer:        %s': 'Durée :       %s',
        'Dauer:  %s': 'Durée : %s',
        'Datei:  %s': 'Fichier :  %s',
        'Format:       %d Hz / %d Bit / %d Kanal/Kanäle%s': 'Format :       %d Hz / %d bits / %d canal(aux)%s',
        'Fassung:       %s': 'Version :     %s',
        'Python:        %s': 'Python :      %s',
        'System:        %s %s': 'Système :     %s %s',
        'Tk-Modul:      %s': 'Module Tk :   %s',
        'Schrift:       %s': 'Police :      %s',
        'Kartenwurzeln: %s': 'Racines des cartes : %s',
        'ffmpeg:        %s': 'ffmpeg :      %s',
        'gewählt:       %s': 'choisi :      %s',
        'Ton:           %s': 'Son :         %s',
        'Patches:       %d': 'Patches :     %d',
        'Patches: %d': 'Patches : %d',
        'Patches auf der Karte:    %d': 'Patches sur la carte :    %d',
        'Patches in der Sicherung: %d': 'Patches dans la sauvegarde : %d',
        'Inhalt von %s:': 'Contenu de %s :',
        'Gesichert: %d Patches, %.0f MB.': 'Sauvegardé : %d patches, %.0f Mo.',
        'gesichert: %d Patches, %.0f MB': 'sauvegardé : %d patches, %.0f Mo',
        'Fertig. Sicherung: %s': 'Terminé. Sauvegarde : %s',
        'Fertig: %s angelegt.': 'Terminé : %s créé.',
        'Fertig: %d Ordner umbenannt, %d Fehler, %d Patches lesbar.': 'Terminé : %d dossiers renommés, %d erreurs, %d patches lisibles.',
        'Fertig: %d Patches normiert, %d davon in der Spitze gedeckelt.': 'Terminé : %d patches normalisés, %d limités en crête.',
        'Gelöscht. Sicherung: %s': 'Supprimé. Sauvegarde : %s',
        'Getauscht. Sicherung: %s': 'Échangé. Sauvegarde : %s',
        'Geändert. Sicherung: %s': 'Modifié. Sauvegarde : %s',
        'Verschoben. Sicherung: %s': 'Déplacé. Sauvegarde : %s',
        'Zurückgeholt. Der Zustand davor liegt in: %s': "Restauré. L'état précédent se trouve dans : %s",
        'Zurückgerechnet: %d Patches. Sicherung davor: %s': 'Annulé : %d patches. Sauvegarde avant : %s',
        'Vorher wird er gesichert nach: %s': "Il est d'abord sauvegardé dans : %s",
        'Wird gelöscht: %s  (%s)': 'Sera supprimé : %s  (%s)',
        'Neuer Patch:  Patch%s  (Name: %s)': 'Nouveau patch : Patch%s  (nom : %s)',
        'Patch:        %s  (%s)': 'Patch :        %s  (%s)',
        'Patch:      %s  (%s)': 'Patch :      %s  (%s)',
        'Vorlage:      %s': 'Modèle :      %s',
        'Teile:  %d × etwa %s': 'Parties :  %d × environ %s',
        'Stopp:        %s': 'Arrêt :        %s',
        'Stopp:        %s (%s)': 'Arrêt :        %s (%s)',
        'Rhythmus:     %s': 'Rythme :       %s',
        'Rhythmus:     %s (%s)': 'Rythme :       %s (%s)',
        'Wiedergabe:   %s': 'Lecture :      %s',
        'Wiedergabe:   %s (IsLoop %s)': 'Lecture :      %s (IsLoop %s)',
        'Pegel:        %+.1f dB': 'Niveau :      %+.1f dB',
        'Pegel:        %.1f dB → %.1f dB Spitze': 'Niveau :      %.1f dB → %.1f dB crête',
        'Pegel:        auf -1 dB Spitze normieren': 'Niveau :      normaliser à -1 dB crête',
        'Fade:         letzte %.1f s werden ausgeblendet': 'Fondu :       les %.1f dernières s sont atténuées',
        'Fade:         letzte %.1f s der Aufnahme ausblenden': 'Fondu :       atténuer les %.1f dernières s',
        'Kürzen:       auf %s geschnitten (harter Schnitt, 12 ms ausgeblendet)': 'Raccourci :   coupé à %s (coupe franche, 12 ms atténuées)',
        'Protokoll: %s': 'Journal : %s',
        'Sicherung: %s': 'Sauvegarde : %s',
        'Sicherung:  %s': 'Sauvegarde :  %s',
        'Schreibweise des Geräts: Patch%0*d': "Orthographe de l'appareil : Patch%0*d",
        'Zu ändern: %d': 'À modifier : %d',
        'Zuordnung liegt in: %s': 'Correspondance dans : %s',
        'Betroffene Einträge (erste drei): %s': 'Entrées concernées (trois premières) : %s',
        'Keine Karte gefunden unter %s.': 'Aucune carte trouvée sous %s.',
        "BEFEHL": "COMMANDE",
        "  Auswahl: ": "  Choix : ",
        "  Anderer Pfad (leer = beenden): ": "  Autre chemin (vide = quitter) : ",
        "  Patch-Nummer: ": "  Numéro du patch : ",
        "  Name (leer = Dateiname): ": "  Nom (vide = nom du fichier) : ",
        "  Stopp (fade/instant, leer = wie Vorlage): ": "  Arrêt (fade/instant, vide = comme le modèle) : ",
        "  Wiedergabe (loop/single, leer = wie Vorlage): ": "  Lecture (loop/single, vide = comme le modèle) : ",
        "  Pfad zur Audiodatei: ": "  Chemin du fichier audio : ",
        "  Stopp (fade/instant, leer = lassen): ": "  Arrêt (fade/instant, vide = garder) : ",
        "  Wiedergabe (loop/single, leer = lassen): ": "  Lecture (loop/single, vide = garder) : ",
        "  Neuer Name (leer = lassen): ": "  Nouveau nom (vide = garder) : ",
        "  Wirklich schreiben? (j/n): ": "  Vraiment écrire ? (o/n) : ",
        "  Welcher Patch: ": "  Quel patch : ",
        "  An welche Stelle: ": "  À quel emplacement : ",
        "  Wirklich verschieben? (j/n): ": "  Vraiment déplacer ? (o/n) : ",
        "  Zielordner (leer = ~/JamMan-Sicherung): ": "  Dossier cible (vide = ~/JamMan-Sicherung) : ",
        "  Wirklich löschen? (j/n): ": "  Vraiment supprimer ? (o/n) : ",
        "Kein Abspielprogramm gefunden (aplay, paplay, mpv oder ffplay).": "Aucun lecteur trouvé (aplay, paplay, mpv ou ffplay).",
        "Fehler: im Protokoll stehen keine Einträge.": "Erreur : le protocole ne contient aucune entrée.",
        "Fehler: Datei ließ sich nicht umwandeln.": "Erreur : le fichier n'a pas pu être converti.",
        "Fehler: kein vollständiger Patch als Vorlage gefunden.": "Erreur : aucun patch complet trouvé comme modèle.",
        "Fehler: nichts angegeben (--stopp, --rhythmus, --wiedergabe, --name oder --fade).": "Erreur : rien d'indiqué (--stopp, --rhythmus, --wiedergabe, --name ou --fade).",
        "Fehler: '%s' ist keine lesbare Audiodatei.": "Erreur : '%s' n'est pas un fichier audio lisible.",
        "Fehler: --teile N oder --laenge SEKUNDEN angeben.": "Erreur : indiquer --teile N ou --laenge SECONDES.",
        "Fehler: %.1f Minuten pro Teil sind zu lang (Grenze %d Minuten).": "Erreur : %.1f minutes par partie, c'est trop long (limite %d minutes).",
        "Fehler: '%s' gibt es nicht.": "Erreur : '%s' n'existe pas.",
        "Fehler: '%s' ist keine lesbare Audiodatei.%s": "Erreur : '%s' n'est pas un fichier audio lisible.%s",
        "Fehler: '%s' liegt schon vor.": "Erreur : '%s' existe déjà.",
        "Fehler: Die Datei ließ sich nicht kürzen.": "Erreur : le fichier n'a pas pu être raccourci.",
        "Fehler: Die Umwandlung hat keine hörbare Aufnahme\n  ergeben (nur Stille). Datei prüfen: %s": "Erreur : la conversion n'a produit aucun son audible\n  (silence seulement). Vérifier le fichier : %s",
        "Fehler: Patch%d gibt es nicht.": "Erreur : le patch %d n'existe pas.",
        "Fehler: Die Aufnahme ließ sich nicht ausblenden (kein 16-Bit-WAV?).": "Erreur : l'enregistrement n'a pas pu être fondu (pas un WAV 16 bits ?).",
        "Fehler: '%s' ist nicht eingelegt.": "Erreur : '%s' n'est pas insérée.",
        "Fehler beim Auswerfen: %s": "Erreur lors de l'éjection : %s",
        "Fehler: Patch%d hat keine WAV.": "Erreur : le patch %d n'a pas de WAV.",
        "Fehler: Sicherungsordner '%s' gibt es nicht.": "Erreur : le dossier de sauvegarde '%s' n'existe pas.",
        "Fehler: in '%s' liegen keine Patch-Ordner.": "Erreur : '%s' ne contient aucun dossier de patch.",
        "Fehler: keine Sicherung mit einer Aufnahme für Patch%d in '%s'.\n  Fade-Sicherungen heißen Sicherung_<Datum>_vor_fade.": "Erreur : aucune sauvegarde avec un enregistrement pour le patch %d dans '%s'.\n  Les sauvegardes de fondu s'appellent Sicherung_<date>_vor_fade.",
        "Fehler: Platz %d ist belegt (%s).\n  Zum Tauschen:  tauschen --patch %d --mit %d": "Erreur : l'emplacement %d est occupé (%s).\n  Pour échanger :  tauschen --patch %d --mit %d",
        "Fehler: Nummern müssen zwischen 1 und %d liegen.": "Erreur : les numéros doivent être entre 1 et %d.",
        "Fehler: '%s' ist ein Ordner, keine Datei.": "Erreur : '%s' est un dossier, pas un fichier.",
        "Fehler: '%s' ist kein Pegelprotokoll (keine lesbare Textdatei).": "Erreur : '%s' n'est pas un protocole de niveau (pas de fichier texte lisible).",
        "Fehler: '%s' darf nicht gelesen werden.": "Erreur : '%s' ne doit pas être lu.",
        "Fehler: '%s' lässt sich nicht lesen: %s": "Erreur : '%s' est illisible : %s",
        "Fehler beim Umwandeln. Die Datei hat %d Hz / %d Kanal/Kanäle;\n  nötig sind 44100 Hz und 2 Kanäle. Bitte vorher umwandeln oder\n  ffmpeg installieren.": "Erreur lors de la conversion. Le fichier a %d Hz / %d canal(aux) ;\n  44100 Hz et 2 canaux sont nécessaires. Le convertir d'abord ou\n  installer ffmpeg.",
        "Fehler: '%s' lässt sich nicht schreiben.\n  Nimm einen anderen Ordner:  --sicherung <pfad>": "Erreur : '%s' ne peut pas être écrit.\n  Choisir un autre dossier :  --sicherung <chemin>",
        "Fehler: Patch%d hat keine phrase.wav.": "Erreur : le patch %d n'a pas de phrase.wav.",
        "Fehler: Der Pegel ließ sich nicht ändern.": "Erreur : le niveau n'a pas pu être modifié.",
        "Fehler: in '%s' darf nicht geschrieben werden.\n  Nimm einen anderen Ordner:  --sicherung <pfad>": "Erreur : '%s' ne doit pas être écrit.\n  Choisir un autre dossier :  --sicherung <chemin>",
        "Fehler: Teil %d ließ sich nicht schneiden.": "Erreur : la partie %d n'a pas pu être coupée.",
        "Fehler: Die Datei ist %.1f Minuten lang.\n  Der JamMan Solo XT nimmt höchstens %d Minuten pro Loop an\n  (Handbuch, Seite 60: \"Max. Individual Loop Time: 10 minutes\").\n  Längere Loops erscheinen auf der Karte, werden vom Pedal aber\n  nicht erkannt - und können dabei verschwinden.\n\n  So geht es weiter:\n    --kuerzen            auf 9:59 kürzen (harter Schnitt am Ende,\n                         12 ms ausgeblendet, damit es nicht knackt)\n    --laenge 480         auf 8:00 kürzen\n    --trotzdem           unverändert schreiben (zum Ausprobieren)": "Erreur : le fichier dure %.1f minutes.\n  Le JamMan Solo XT accepte au plus %d minutes par boucle\n  (manuel, page 60 : « Max. Individual Loop Time: 10 minutes »).\n  Les boucles plus longues apparaissent sur la carte mais la pédale\n  ne les reconnaît pas - et elles peuvent disparaître.\n\n  Solutions :\n    --kuerzen            réduire à 9:59 (coupe nette à la fin,\n                         12 ms fondues pour éviter le clic)\n    --laenge 480         réduire à 8:00\n    --trotzdem           écrire inchangé (pour essayer)",
        "Fehler: --wav fehlt.": "Erreur : --wav manque.",
        "Fehler: --patch fehlt.": "Erreur : --patch manque.",
        "Fehler: --protokoll fehlt.": "Erreur : --protokoll manque.",
        "Fehler: --patch fehlt (oder --alle angeben).": "Erreur : --patch manque (ou donner --alle).",
        "Fehler: --patch und --auf fehlen.": "Erreur : --patch et --auf manquent.",
        "Fehler: --patch und --mit fehlen.": "Erreur : --patch et --mit manquent.",
        "Fehler: --patch und --nach fehlen.": "Erreur : --patch et --nach manquent.",
        "  Hinweis: Pegelprotokoll konnte nicht geschrieben werden.": "    Remarque : le protocole de niveau n'a pas pu être écrit.",
        "  Protokoll: %s": "    Protocole : %s",
        "  %d Einträge": "    %d entrées",
        "    %-10s %-24s Faktor %.4f → 1/%.4f": "      %-10s %-24s facteur %.4f → 1/%.4f",
        "  Trockenlauf — nichts geändert. Mit --schreiben ausführen.": "    Simulation — rien modifié. Lancer avec --schreiben pour écrire.",
        "    %-10s %+6.1f → %+6.1f dB": "      %-10s %+6.1f → %+6.1f dB",
        "  Zurückgerechnet: %d Patches. Sicherung davor: %s": "    Recalculé : %d patches. Sauvegarde avant : %s",
        "  Pegel:        %.1f dB → %.1f dB Spitze": "    Niveau :      %.1f dB → %.1f dB de crête",
        "                Die Anhebung wurde begrenzt, damit nichts": "                L'augmentation a été limitée pour que rien",
        "                über 0 dBFS geht (sonst würde übersteuert).": "                ne dépasse 0 dBFS (sinon saturation).",
        "                Achtung: %d Werte liegen an der Grenze.": "                Attention : %d valeurs sont à la limite.",
        "  Hinweis: unbekanntes WAV-Format 0x%04X - nicht angefasst.": "    Remarque : format WAV inconnu 0x%04X — non modifié.",
        "Fehler: '%s' ist kein Ordner. Ist die Karte eingelegt?": "  Erreur : '%s' n'est pas un dossier. La carte est-elle insérée ?",
        "Fehler: auf '%s' liegt kein Ordner '%s' oder '%s'. Das ist keine JamMan-Karte.": "  Erreur : '%s' ne contient pas de dossier '%s' ni '%s'. Ce n'est pas une carte JamMan.",
        "  %d Apple-Dateien entfernt (._* und .DS_Store).": "    %d fichiers Apple supprimés (._* et .DS_Store).",
        "  Keine Patches auf der Karte.": "    Aucun patch sur la carte.",
        "  %-10s %-34s %8s  %s": "    %-10s %-34s %8s  %s",
        "  %-10s %-34s %8s  %s%s%s": "    %-10s %-34s %8s  %s%s%s",
        "\n  %d Patches, %s belegt.": "\n  %d patches, %s occupés.",
        "  Sicherung beim Start: Karte unverändert, nichts zu tun.": "    Sauvegarde au démarrage : carte inchangée, rien à faire.",
        "  Sicherung beim Start: %s": "    Sauvegarde au démarrage : %s",
        "    gesichert: %d Patches, %.0f MB": "      sauvegardés : %d patches, %.0f Mo",
        "    alte Sicherung entfernt: %s": "      ancienne sauvegarde supprimée : %s",
        "  Quelle: %s": "    Source : %s",
        "  Ziel:   %s": "    Cible : %s",
        "  Trockenlauf — nichts kopiert. Mit --schreiben ausführen.": "    Simulation — rien copié. Lancer avec --schreiben pour écrire.",
        "  Gesichert: %d Patches, %.0f MB.": "    Sauvegardé : %d patches, %.0f Mo.",
        "  Datei:  %s": "    Fichier : %s",
        "  Dauer:  %s": "    Durée : %s",
        "  Teile:  %d × etwa %s": "    Parties : %d × environ %s",
        "  Suche musikalisch brauchbare Schnittstellen …": "    Recherche de points de coupe utilisables …",
        "  Hinweis: keine brauchbaren Pausen gefunden (%s) -": "    Remarque : aucune pause utilisable (%s) —",
        "           es wird gleichmäßig geschnitten.": "             coupe régulière à la place.",
        "  Pause an der Naht wird herausgenommen": "    La pause à la jonction est retirée",
        "    Teil %d: %7.1f – %7.1f s  (%s)": "      Partie %d : %7.1f – %7.1f s  (%s)",
        "  Hinweis: Teil %d wäre %.1f min - er wird auf 9:59 gekürzt.": "    Remarque : la partie %d ferait %.1f min — elle est réduite à 9:59.",
        "    Teil %d: %s – %s": "      Partie %d : %s – %s",
        "  Trockenlauf — nichts angelegt. Mit --schreiben ausführen.": "    Simulation — rien créé. Lancer avec --schreiben pour écrire.",
        "  Quelle:       %s": "    Source :      %s",
        "  Dauer:        %s": "    Durée :       %s",
        "  Format:       %d Hz / %d Bit / %d Kanal/Kanäle%s": "    Format :      %d Hz / %d bit / %d canal(aux)%s",
        "  Stopp:        %s": "    Arrêt :       %s",
        "  Rhythmus:     %s": "    Rythme :      %s",
        "  Wiedergabe:   %s": "    Lecture :     %s",
        "  Fade:         letzte %.1f s werden ausgeblendet": "    Fondu :       les %.1f dernières s sont fondues",
        "  Pegel:        auf -1 dB Spitze normieren": "    Niveau :      normaliser à -1 dB de crête",
        "  Pegel:        %+.1f dB": "    Niveau :      %+.1f dB",
        "  Neuer Patch:  Patch%s  (Name: %s)": "    Nouveau patch : Patch%s  (nom : %s)",
        "  Vorlage:      %s": "    Modèle :      %s",
        "  Trockenlauf — nichts geschrieben. Mit --schreiben ausführen.": "    Simulation — rien écrit. Lancer avec --schreiben pour écrire.",
        "  Kürzen:       auf %s geschnitten (harter Schnitt, 12 ms ausgeblendet)": "    Raccourcir :  coupé à %s (coupe nette, 12 ms fondues)",
        "  Kopf:         WAV-Kopf auf einfaches PCM umgeschrieben": "    En-tête :     en-tête WAV réécrit en PCM simple",
        "  Fertig: %s angelegt.": "    Terminé : %s créé.",
        "  Karte auswerfen, in den JamMan stecken — der Patch erscheint als Patch%s.": "    Éjectez la carte, mettez-la dans le JamMan — le patch apparaît comme Patch%s.",
        "  Patch:        %s  (%s)": "    Patch :       %s  (%s)",
        "  Stopp:        %s (%s)": "    Arrêt :       %s (%s)",
        "  Rhythmus:     %s (%s)": "    Rythme :      %s (%s)",
        "  Wiedergabe:   %s (IsLoop %s)": "    Lecture :     %s (IsLoop %s)",
        "  Name:         %s": "    Nom :         %s",
        "  Fade:         letzte %.1f s der Aufnahme ausblenden": "    Fondu :       les %.1f dernières s de l'enregistrement sont fondues",
        "                (verändert die Audiodatei; vorher wird gesichert)": "                (modifie le fichier audio ; une sauvegarde est faite avant)",
        "  Geändert. Sicherung: %s": "    Modifié. Sauvegarde : %s",
        "  %s Sie kann jetzt abgezogen werden.": "    %s Elle peut être retirée.",
        "  Schreibweise des Geräts: Patch%0*d": "    Orthographe de l'appareil : Patch%0*d",
        "  Alle Ordner heißen schon so. Nichts zu tun.": "    Tous les dossiers sont déjà ainsi. Rien à faire.",
        "  Zu ändern: %d": "    À modifier : %d",
        "    %-12s ->  %s": "      %-12s ->  %s",
        "    %-12s FEHLER: %s": "      %-12s ERREUR : %s",
        "  Fertig: %d Ordner umbenannt, %d Fehler, %d Patches lesbar.": "    Terminé : %d dossiers renommés, %d erreurs, %d patches lisibles.",
        "  Zuordnung liegt in: %s": "    La correspondance est dans : %s",
        "  %d Apple-Dateien auf der Karte. Mit --schreiben entfernen.": "    %d fichiers Apple sur la carte. Les retirer avec --schreiben.",
        "  Fertig.": "    Terminé.",
        "  Spielt \"%s\" (%s) … (Strg+C hält an)": "    Lecture de « %s » (%s) … (Ctrl+C arrête)",
        "  Ziel: %.1f dB Lautheit (RMS), Spitze höchstens %.1f dB": "    Cible : %.1f dB de sonie (RMS), crête au plus %.1f dB",
        "  Patches: %d": "    Patches : %d",
        "  %-10s %-24s %8s": "    %-10s %-24s %8s",
        "  %-10s %-24s %+7.1f dB": "    %-10s %-24s %+7.1f dB",
        "  Sicherung: %s": "    Sauvegarde : %s",
        "  %-10s %-24s %+6.1f → %+6.1f dB  (Spitze %+.1f dB)%s": "    %-10s %-24s %+6.1f → %+6.1f dB  (crête %+.1f dB)%s",
        "  Fertig: %d Patches normiert, %d davon in der Spitze gedeckelt.": "    Terminé : %d patches normalisés, dont %d écrêtés en crête.",
        "  Patches in der Sicherung: %d": "    Patches dans la sauvegarde : %d",
        "  Patches auf der Karte:    %d": "    Patches sur la carte :    %d",
        "  Hinweis: diese gibt es auf der Karte nicht und werden neu angelegt:": "    Remarque : ceux-ci n'existent pas sur la carte et seront créés :",
        "           %s": "             %s",
        "  Trockenlauf — nichts zurückgeholt. Mit --schreiben ausführen.": "    Simulation — rien restauré. Lancer avec --schreiben pour écrire.",
        "  Zurückgeholt. Der Zustand davor liegt in: %s": "    Restauré. L'état précédent est dans : %s",
        "  Patch:      %s  (%s)": "    Patch :     %s  (%s)",
        "  Sicherung:  %s": "    Sauvegarde : %s",
        "    %-22s %8.2f MB": "      %-22s %8.2f Mo",
        "  %s  (%s)": "    %s  (%s)",
        "  →  %s": "    →  %s",
        "  Trockenlauf — nichts umbenannt. Mit --schreiben ausführen.": "    Simulation — rien renommé. Lancer avec --schreiben pour écrire.",
        "  Fertig. Sicherung: %s": "    Terminé. Sauvegarde : %s",
        "  Nichts zu tun.": "    Rien à faire.",
        "  Achtung: %s und %s haben unterschiedliche Stellenzahl.": "    Attention : %s et %s n'ont pas le même nombre de chiffres.",
        "           Der JamMan sortiert nach Namen - der Patch landet dann": "             Le JamMan trie par nom — le patch atterrira",
        "           auf dem Platz, den sein neuer Name angibt.": "             sur l'emplacement qu'indique son nouveau nom.",
        "  Tauschen:": "    Échange :",
        "    Platz %-4s %-24s  →  Platz %s": "      place %-4s %-24s  →  place %s",
        "  Trockenlauf — nichts getauscht. Mit --schreiben ausführen.": "    Simulation — rien échangé. Lancer avec --schreiben pour écrire.",
        "  Getauscht. Sicherung: %s": "    Échangé. Sauvegarde : %s",
        "  Wird gelöscht: %s  (%s)": "    Sera supprimé : %s  (%s)",
        "  Vorher wird er gesichert nach: %s": "    Il est d'abord sauvegardé dans : %s",
        "  Trockenlauf — nichts gelöscht. Mit --schreiben ausführen.": "    Simulation — rien supprimé. Lancer avec --schreiben pour écrire.",
        "  Gelöscht. Sicherung: %s": "    Supprimé. Sauvegarde : %s",
        "  Karte auswerfen, in den JamMan stecken — der Patch ist weg.": "    Éjectez la carte, mettez-la dans le JamMan — le patch a disparu.",
        "  Neue Reihenfolge:": "    Nouvel ordre :",
        "    Platz %d:  %-10s  %s%s": "      place %d :  %-10s  %s%s",
        "  Verschoben. Sicherung: %s": "    Déplacé. Sauvegarde : %s",
        "  JamMan Solo XT — Speicherkarte verwalten": "    JamMan Solo XT — gérer la carte mémoire",
        "  Keine Karte gefunden unter %s.": "    Aucune carte trouvée sous %s.",
        "  Karte: %s": "    Carte : %s",
        "  1  Patches auflisten": "    1  Lister les patches",
        "  2  Patch anhören": "    2  Écouter un patch",
        "  3  Datei aufspielen": "    3  Importer un fichier",
        "  4  Patch ändern (Stopp, Wiedergabe, Name)": "    4  Modifier un patch (arrêt, lecture, nom)",
        "  5  Patch verschieben": "    5  Déplacer un patch",
        "  6  Patch löschen": "    6  Supprimer un patch",
        "  7  Alles sichern": "    7  Tout sauvegarder",
        "  8  Karte auswerfen": "    8  Éjecter la carte",
        "  0  Beenden": "    0  Quitter",
        "  %s": "    %s",
        "  System:        %s %s": "    Système :      %s %s",
        "  Python:        %s": "    Python :       %s",
        "  Fassung:       %s": "    Version :      %s",
        "  Tk:            %s": "    Tk :           %s",
        "  Tk-Modul:      %s": "    Module Tk :    %s",
        "  Tk:            NICHT verfügbar — %s": "    Tk :           NON disponible — %s",
        "                 (Fenster geht mit diesem Python nicht;": "                    (la fenêtre ne marche pas avec ce Python ;",
        "                  das Textmenü geht trotzdem: jamman.py menu)": "                     le menu texte fonctionne : jamman.py menu)",
        "  Ton:           winsound (Windows-eigen)": "    Son :          winsound (intégré à Windows)",
        "  Ton:           afplay": "    Son :          afplay",
        "  Ton:           %s": "    Son :          %s",
        "  Kartenwurzeln: %s": "    Racines des cartes : %s",
        "  Einstellungen: %s": "    Réglages :     %s",
        "  ffmpeg:        %s": "    ffmpeg :       %s",
        "  Schrift:       %s": "    Police :       %s",
        "  Pythons auf diesem Rechner:": "    Pythons sur cette machine :",
        "    %-62s %s": "      %-62s %s",
        "  Geprüfte Orte:": "    Endroits vérifiés :",
        "  Laufwerke mit JamManSoloXT:": "    Lecteurs avec JamManSoloXT :",
        "    %-24s %3d Patches  %s": "      %-24s %3d patches  %s",
        "  gewählt:       %s": "    choisi :       %s",
        "  Patches:       %d": "    Patches :      %d",
        "  ACHTUNG: das Dateisystem liefert Lesefehler.": "    ATTENTION : le système de fichiers signale des erreurs de lecture.",
        "  Betroffene Einträge (erste drei): %s": "    Entrées concernées (trois premières) : %s",
        "  Inhalt von %s:": "    Contenu de %s :",
        "    %-24r %s, %s": "      %-24r %s, %s",
        "    nicht lesbar: %s": "      illisible : %s",
        "    %-10s %s": "      %-10s %s",
        "  erste Zeile im Fenster:": "    première ligne dans la fenêtre :",
        "    %r": "      %r",
        "JamMan Solo XT": "  JamMan Solo XT",
        "WAV-Format": "Format WAV",
        "%d Hz / %d Bit / %d K": "%d Hz / %d bit / %d can",
        "  %-28s  (gibt es nicht)": "  %-28s  (n'existe pas)",
        "  %-28s  (nicht lesbar: %s)": "  %-28s  (illisible : %s)",
        "  %-28s  (leer)": "  %-28s  (vide)",
        "  %-28s  %s mit %s Patches": "  %-28s  %s avec %s patches",
        "kein nutzbares Tk": "aucun Tk utilisable",
        "Sprache der Ausgabe (Vorgabe: aus dem System)": "Langue de sortie (par défaut : celle du système)",
        "Laufwerk der Karte (Vorgabe: automatisch erkannt)": "Lecteur de la carte (par défaut : détecté automatiquement)",
        "Zielordner für 'sichern'": "Dossier de destination pour « sauvegarder »",
        "Audiodatei für 'aufspielen'": "Fichier audio pour « importer »",
        "Name des Patches (Vorgabe: Dateiname)": "Nom du patch (par défaut : nom du fichier)",
        "Patch-Nummer": "Numéro du patch",
        "Zielplatz für 'verschieben'": "Emplacement cible pour « déplacer »",
        "Tauschpartner für 'tauschen'": "Partenaire d'échange pour « échanger »",
        "Zielplatz für 'platzieren'": "Emplacement cible pour « placer »",
        "Sicherungsordner für 'zuruecknehmen'": "Dossier de sauvegarde pour « restaurer »",
        "Pegelprotokoll für 'pegel-umkehren'": "Protocole de niveau pour « annuler le niveau »",
        "bei 'zuruecknehmen': alle Patches aus der Sicherung": "pour « restaurer » : tous les patches de la sauvegarde",
        "Ziellautheit für 'alle-normieren' (Vorgabe -18 dB)": "Volume cible pour « tout normaliser » (par défaut -18 dB)",
        "Spitzengrenze für 'alle-normieren' (Vorgabe -1 dB)": "Limite de crête pour « tout normaliser » (par défaut -1 dB)",
        "zu lange Datei auf 9:59 kürzen": "raccourcir un fichier trop long à 9:59",
        "beim Teilen die Pause an der Naht stehen lassen": "lors du découpage, garder la pause à la jonction",
        "beim Teilen gleichmäßig schneiden statt an Pausen": "lors du découpage, couper régulièrement au lieu des pauses",
        "Datei in N gleich lange Patches aufteilen": "diviser le fichier en N patches de même longueur",
        "zu lange Datei auf diese Länge kürzen": "raccourcir le fichier trop long à cette longueur",
        "auch Loops über 10 Minuten schreiben (Pedal nimmt sie nicht an)": "écrire aussi des boucles de plus de 10 minutes (la pédale les refuse)",
        "Fade am Ende der Datei einrechnen (z. B. 3)": "tenir compte du fondu en fin de fichier (p. ex. 3)",
        "Lautstärke um DB ändern (z. B. -3 oder +2)": "modifier le volume de DB (p. ex. -3 ou +2)",
        "Anhebung NICHT auf 0 dBFS begrenzen (kann übersteuern)": "ne PAS limiter l'augmentation à 0 dBFS (risque de saturation)",
        "auf -1 dB Spitzenpegel normieren": "normaliser à -1 dB de crête",
        "Stopp-Verhalten des neuen Patches (ohne Angabe: wie Vorlage)": "comportement d'arrêt du nouveau patch (sans indication : comme le modèle)",
        "Loop oder Single (IsLoop in phrase.xml; ohne Angabe: wie Vorlage)": "boucle ou single (IsLoop dans phrase.xml ; sans indication : comme le modèle)",
        "Rhythmus des neuen Patches (ohne Angabe: wie Vorlage)": "rythme du nouveau patch (sans indication : comme le modèle)",
        "beim Verschieben nur die Namen sichern (schnell, kein Kopieren)": "lors du déplacement, ne sauvegarder que les noms (rapide, sans copie)",
        "Ordner für die Sicherung vor Löschen/Verschieben": "dossier de sauvegarde avant suppression/déplacement",
        "wirklich verändern (sonst Trockenlauf)": "modifier réellement (sinon simulation)",
        "was zu tun ist (Liste unten; englische Namen gehen auch)": "ce qu'il faut faire (liste ci-dessous ; les noms anglais marchent aussi)",
        "Einstellungen: %s": "Réglages : %s",
        "Keine Karte gefunden": "Aucune carte trouvée",
        "Neue Fassung verfügbar": "Nouvelle version disponible",
        "Es gibt eine neuere Fassung: %s. Sie benutzen %s.": "Il existe une version plus récente : %s. Vous utilisez %s.",
        "Zur Download-Seite wechseln?": "Ouvrir la page de téléchargement ?",
        "Es liegt keine JamMan-Karte im Rechner.": "Il n'y a pas de carte JamMan dans cet ordinateur.",
        "Nimm die microSD aus dem Pedal und stecke sie in einen Kartenleser oder Kartenslot. Über das USB-Kabel am Pedal erscheint sie nicht als Laufwerk.": "Retirez la microSD de la pédale et insérez-la dans un lecteur de cartes ou une fente. Avec le câble USB branché sur la pédale, elle n'apparaît pas comme lecteur.",
        "Ordner selbst wählen?": "Choisir le dossier vous-même ?",
        "Ordner der Karte wählen": "Choisir le dossier de la carte",
        '   ← wird umgewandelt': '← sera converti',
        '   ← abweichend': '← différent',
        '   ← über 10 min, Pedal nimmt sie nicht an': "← plus de 10 min, la pédale ne l'accepte pas",
        'keine WAV': 'pas de WAV',
        '  Das Fenster wäre leer. Es geht im Textmenü weiter.': 'La fenêtre serait vide. Le menu texte fonctionne encore.',
        '  Für ein Fenster: Python von python.org installieren (bringt Tk 8.6).': 'Pour une fenêtre : installer Python depuis python.org (fournit Tk 8.6).',
        '  Tk %s kann auf diesem macOS keinen Text im Fenster zeichnen.': 'Tk %s ne peut pas dessiner de texte dans une fenêtre sous ce macOS.',
        '  Hinweis: Fade konnte nicht eingerechnet werden.': "Remarque : le fondu n'a pas pu être appliqué.",
        '  Hinweis: Pegel ließ sich nicht ändern (kein 16-Bit-WAV?).': "Remarque : le niveau n'a pas pu être modifié (WAV 16 bits ?).",
        '  Achtung: der WAV-Kopf ist weiterhin ungewöhnlich.': "Attention : l'en-tête WAV reste inhabituel.",
        '  Hinweis: Die Vorlage hatte kein Feld IsLoop - es wurde ergänzt.': "Remarque : le modèle n'avait pas de champ IsLoop — il a été ajouté.",
        '  Hinweis: Die Vorlage hatte kein Feld StopMode - es wurde ergänzt.': "Remarque : le modèle n'avait pas de champ StopMode — il a été ajouté.",
        '  Hinweis: Die Vorlage hatte kein Feld RhythmType - es wurde ergänzt.': "Remarque : le modèle n'avait pas de champ RhythmType — il a été ajouté.",
        '  Angehalten.': 'Arrêté.',
        '  Das ist kein Fehler des Werkzeugs - die Karte oder': "Ce n'est pas une erreur du programme — la carte ou",
        '  ihr Einhängepunkt ist beschädigt. Erste Schritte:': 'son point de montage est endommagé. Premiers pas :',
        '    dmesg | tail -20            (Meldungen des Systems)': 'dmesg | tail -20            (messages du système)',
        '    sudo umount <laufwerk>      (aushängen)': 'sudo umount <périphérique>  (démonter)',
        '    sudo fsck.vfat -n <geraet>  (FAT32, nur prüfen)': 'sudo fsck.vfat -n <périph>  (FAT32, vérifier seulement)',
        '    sudo exfatfsck <geraet>     (exFAT, nur prüfen)': 'sudo exfatfsck <périph>     (exFAT, vérifier seulement)',
        '    oder die Karte im JamMan einschalten - das Pedal': "ou mettre la carte dans le JamMan et l'allumer —",
        '    repariert seine Karte beim Einschalten.': 'la pédale répare sa carte au démarrage.',
        'Erst eine Karte wählen.': "Choisir d'abord une carte.",
        "Speicherkarte des JamMan Solo XT": "Carte mémoire du JamMan Solo XT",
        "keine Karte gefunden": "aucune carte trouvée",
        "Ordner": "Dossier", "Name": "Nom", "Dauer": "Durée",
        "Stoppart": "Arrêt", "Wdg.": "Lecture", "Rhythmus": "Rythme",
        "Wiedergabe": "Lecture",
        "Patch markieren — dann hier klicken zum Springen":
            "Choisir un patch — puis cliquer ici pour y aller",
        "kein Patch gewählt": "aucun patch sélectionné",
        "automatisch weiter": "enchaîner automatiquement",
        "beim Start sichern": "sauvegarder au démarrage",
        "Platz": "Emplacement",
        "nächster": "suivant",
        "vormerken": "préparer",
        "vorgemerkt %d": "préparé %d",
        "Einstellungen:": "Réglages :",
        "wie Vorlage": "comme le modèle",
        "Fade am Ende": "Fondu à la fin",
        "Karte wählen": "Choisir la carte",
        "Datei aufspielen": "Importer un fichier",
        "Alles sichern": "Tout sauvegarder",
        "Pegel zurückrechnen": "Annuler le niveau",
        "Neu laden": "Recharger",
        "Anhören": "Écouter",
        "Stopp": "Arrêter",
        "Umbenennen": "Renommer",
        "Platz ändern": "Changer l'emplacement",
        "Sicherung zurückholen": "Restaurer la sauvegarde",
        "Einstellungen anwenden": "Appliquer les réglages",
        "▲ nach oben": "▲ monter",
        "▼ nach unten": "▼ descendre",
        "Tauschen": "Échanger",
        "Datei teilen": "Diviser le fichier",
        "Alle normieren": "Tout normaliser",
        "Löschen": "Supprimer",
        "Karte auswerfen": "Éjecter la carte",
        "Pegel": "Niveau",
        "auf −1 dB Spitze normieren": "normaliser à −1 dB crête",
        "Teile": "Parties",
        "an Pausen schneiden": "couper aux silences",
        "Anhebung auf 0 dB begrenzen": "limiter la hausse à 0 dB",
        "Doppelklick spielt · Ziehen ordnet um · ⌘ für zwei Markierungen, dann Tauschen · Esc hält an.":
            "Double-clic lecture · glisser réordonne · ⌘ pour deux sélections, puis échanger · Échap arrête.",
        "vorgemerkt: Platz %d": "préparé : emplacement %d",
        "Ende der Liste erreicht": "fin de la liste",
        "Erst einen Patch in der Liste wählen.": "Choisir d'abord un patch dans la liste.",
        "Bitte eine Platznummer eintragen.": "Entrer un numéro d'emplacement.",
        "Platz %d gibt es auf dieser Karte nicht.": "L'emplacement %d n'existe pas sur cette carte.",
        "MP3 und M4A brauchen ffmpeg -": "MP3 et M4A nécessitent ffmpeg -",
        "Anleitung": "Instructions",
        "WAV geht auch ohne": "WAV fonctionne sans",
        "Schließen": "Fermer",
        "Text kopieren": "Copier le texte",
        "Karte nicht lesbar": "Carte illisible",
        "Fehler": "Erreur",
        "Hinweis": "Remarque",
        "Anleitung in die Zwischenablage kopiert": "Instructions copiées",
        "Sicherung beim Start: %s": "Sauvegarde au démarrage : %s",
    },
    "es": {
        'Dem Python fehlen die Zertifikate. Zwei Wege: im Python-Ordner „Install Certificates.command“ ausführen — oder „pip install certifi“ — oder die Datei von der Release-Seite von Hand laden.':
         'A Python le faltan los certificados. Dos caminos: ejecutar «Install Certificates.command» en la carpeta de Python, o «pip install certifi» — o descargar el archivo desde la página de la versión.',
        'Achtung: das Zertifikat ließ sich nicht prüfen — die Datei wurde ohne Prüfung geladen.':
         'Atención: no se pudo verificar el certificado — el archivo se descargó sin comprobación.',
        'Die Datei ist unvollständig: %d statt %d Bytes.':
         'El archivo está incompleto: %d en lugar de %d bytes.',
        'Neue Fassung %s wird geholt …':
         'Obteniendo la versión %s …',
        'Fassung %s ist installiert. Bitte das Fenster schließen und neu starten.':
         'La versión %s está instalada. Cierra la ventana y vuelve a iniciar.',
        'Update beim Start':
         'Actualizar al inicio',
        'Neue Fassung %s wird geholt …':
         'Obteniendo la versión %s …',
        'Die Karte ist eingebunden, aber das Dateisystem liefert\nLesefehler:\n\n%s\n\nDas liegt an der Karte oder am Einhängepunkt, nicht am\nWerkzeug.\n\nErste Schritte:\n  - Karte im JamMan einschalten (das Pedal prüft sie)\n  - dmesg | tail -20\n  - aushängen und prüfen: sudo fsck.vfat -n <gerät>\n    bzw. sudo exfatfsck <gerät>':
         'La tarjeta está montada, pero el sistema de archivos informa errores de lectura:\n\n%s\n\nEs la tarjeta o el punto de montaje, no la herramienta.\n\nPrimeros pasos:\n  - poner la tarjeta en el JamMan y encenderlo (el pedal la comprueba)\n  - dmesg | tail -20\n  - desmontar y comprobar: sudo fsck.vfat -n <dispositivo>\n    o sudo exfatfsck <dispositivo>',
        'kein Pedal':
         'ningún pedal',
        'Beschreibung …':
         'Descripción …',
        'Rückwärts: ohne ffmpeg keine Hörprobe — das Pedal spielt es trotzdem rückwärts.':
         'Inverso: sin ffmpeg no hay escucha previa; el pedal lo reproduce al revés igualmente.',
        'Tempo':
         'Tempo',
        'Suchen …':
         'Buscar …',
        'Text im Namen (leer lassen = alles zeigen):':
         'Texto en el nombre (vacío = mostrar todo):',
        'Erst eine Karte oder eine geholte Kopie öffnen — dann läßt sich suchen.':
         'Abre primero una tarjeta o una copia obtenida — entonces podrás buscar.',
        'Suchen':
         'Buscar',
        'Suche fehlgeschlagen: %s':
         'Búsqueda fallida: %s',
        'Suchen:':
         'Buscar:',
        'Karte:':
         'Tarjeta:',
        'Meldungen stehen im Fenster; Fehler zusätzlich in jamman-fehler.log.':
         'Los mensajes aparecen en la ventana; los errores también en jamman-fehler.log.',
        'Firmware-Dateien jetzt ins Pedal schreiben? Ein Abbruch mittendrin kann das Pedal unbrauchbar machen. [ja/nein] ':
         '¿Escribir ahora los archivos de firmware en el pedal? Una interrupción puede dejarlo inservible. [sí/no] ',
        'Zielordner für die Firmware':
         'Carpeta de destino del firmware',
        'Ordner mit der geholten Firmware':
         'Carpeta del firmware obtenido',
        'In diesem Ordner liegt keine Firmware (usr/JamManUI, usr/loopengine …).':
         'Esta carpeta no contiene firmware (usr/JamManUI, usr/loopengine …).',
        'Firmware übernehmen':
         'Aplicar el firmware',
        'Firmware übernehmen …':
         'Aplicar el firmware …',
        'Firmware dieses Pedals holen …':
         'Traer el firmware de este pedal …',
        '%s kam unvollständig (%d von %d Bytes).':
         '%s llegó incompleto (%d de %d bytes).',
        '%d Firmware-Dateien werden jetzt in das Pedal geschrieben. Vorher wird der jetzige Stand gesichert.\n\nEin Abbruch mittendrin kann das Pedal unbrauchbar machen — es gibt keinen Weg zurück.\n\nWirklich schreiben?':
         '%d archivos de firmware se escribirán ahora en el pedal. Antes se guarda el estado actual.\n\nUna interrupción puede dejar el pedal inservible: no hay vuelta atrás.\n\n¿Escribir de verdad?',
        'Sichere den jetzigen Stand …':
         'Guardando el estado actual …',
        'Gesichert: %d Dateien nach %s':
         'Guardado: %d archivos en %s',
        'Firmware geholt: %d Dateien':
         'Firmware obtenido: %d archivos',
        'Firmware geholt:\n%s\n%d Dateien':
         'Firmware obtenido:\n%s\n%d archivos',
        'Fertig: %d Dateien geschrieben':
         'Listo: %d archivos escritos',
        'Nicht bestätigt: %d':
         'Sin confirmar: %d',
        'Firmware geschrieben: %d Dateien\nSicherung: %s\nJetzt das Pedal aus- und wieder einschalten.':
         'Firmware escrito: %d archivos\nCopia: %s\nApaga y vuelve a encender el pedal.',
        'Schreibe':
         'Escribiendo',
        'Hole':
         'Obteniendo',
        'JamList aus JamManager XT holen …':
         'Traer JamList de JamManager XT …',
        'Setlist als JamList ablegen …':
         'Guardar setlist como JamList …',
        'Setlist speichern':
         'Guardar setlist',
        'Bibliothek von JamManager XT':
         'Biblioteca de JamManager XT',
        'JamList wählen':
         'Elegir una JamList',
        'In dieser Bibliothek liegt keine JamList.':
         'Esta biblioteca no contiene ninguna JamList.',
        'Geholt: %s\n%d Plätze\n%s':
         'Obtenido: %s\n%d plazas\n%s',
        'JamList ablegen':
         'Guardar JamList',
        '%s%s%s als neue JamList eintragen? Die Bibliothek wird dabei verändert — vorher eine Kopie anlegen.':
         '¿Añadir %s%s%s como nueva JamList? La biblioteca cambiará: haz antes una copia.',
        'JamList %s angelegt (id %d, %d Plätze)':
         'JamList %s creada (id %d, %d plazas)',
        'Keine JamList mit dem Namen %s.':
         'Ninguna JamList llamada %s.',
        'belegte Plätze':
         'plazas ocupadas',
        'Geschrieben: %s (%d Plätze)':
         'Escrito: %s (%d plazas)',
        'Übernehmen':
         'Aplicar',
        'Speicherabbild':
         'Imagen de memoria',
        'Speicherabbild speichern':
         'Guardar imagen de memoria',
        'Speicherabbild aufnehmen …':
         'Tomar una imagen de memoria …',
        'Speicherabbilder vergleichen …':
         'Comparar imágenes de memoria …',
        'Speicherabbilder vergleichen':
         'Comparar imágenes de memoria',
        'Erstes Speicherabbild':
         'Primera imagen de memoria',
        'Zweites Speicherabbild':
         'Segunda imagen de memoria',
        'Nur im ersten Bild: %d':
         'Solo en la primera imagen: %d',
        'Nur im zweiten Bild: %d':
         'Solo en la segunda imagen: %d',
        'Gleicher Pfad, andere Größe: %d':
         'Misma ruta, otro tamaño: %d',
        'Die beiden Bilder sind gleich.':
         'Las dos imágenes son iguales.',
        'Erstes:':
         'Primera:',
        'Zweites:':
         'Segunda:',
        'Aufruf:':
         'Uso:',
        'Lese den Verzeichnisbaum …':
         'Leyendo el árbol de carpetas …',
        'Speicherabbild: %d Einträge':
         'Imagen de memoria: %d entradas',
        'Speicherabbild geschrieben:\n%s':
         'Imagen de memoria guardada:\n%s',
        'Speicherabbild geschrieben: %d Einträge nach %s':
         'Imagen de memoria guardada: %d entradas en %s',
        'Dateien für „speicherabbild“ und „vergleich“':
         'Archivos para «speicherabbild» y «vergleich»',
        'Suchen:':
         'Buscar:',
        'Sortierung':
         'Orden',
        '%d von %d Patches passen zu „%s“.':
         '%d de %d parches coinciden con «%s».',
        'Beschreibung':
         'Descripción',
        'Interpret':
         'Intérprete',
        'Genre':
         'Género',
        'Stichwörter':
         'Palabras clave',
        'Bewertung':
         'Valoración',
        'Tempo (BPM)':
         'Tempo (BPM)',
        'Die Datei %s fehlt.':
         'Falta el archivo %s.',
        'Setlists anpassen':
         'Ajustar las setlists',
        '%d Setlist-Dateien in %s an die neuen Plätze anpassen? Jede Datei wird vorher daneben gesichert.':
         '¿Ajustar %d archivos de setlist en %s a las nuevas plazas? Cada archivo se guarda antes al lado.',
        'Keine Setlist nannte einen bewegten Platz.':
         'Ninguna setlist mencionaba una plaza movida.',
        'Setlists nicht angepaßt: ':
         'Setlists no ajustadas: ',
        'Setlists angepaßt: ':
         'Setlists ajustadas: ',
        'Plätze aufrücken':
         'Cerrar los huecos',
        'Plätze aufrücken …':
         'Cerrar los huecos …',
        'Es gibt keine Lücken — die Plätze sind schon lückenlos belegt.':
         'No hay huecos: las plazas ya están contiguas.',
        '%d Patches rücken in %s nach vorn: %s%s\n\nJeder bewegte Platz wird vorher auf den Rechner gesichert. Setlists im Pedal, die auf Platznummern zeigen, stimmen danach nicht mehr. Fortfahren?':
         '%d parches suben en %s: %s%s\n\nCada plaza movida se guarda antes en el ordenador. Las setlists del pedal que apuntan a números ya no coincidirán. ¿Continuar?',
        'Rücke auf':
         'Subiendo',
        'Fertig: %d Plätze aufgerückt, %d vorher gesichert':
         'Listo: %d plazas subidas, %d guardadas antes',
        'Aufgerückt: %d Plätze':
         'Subidas: %d plazas',
        'Kopie auf dem Rechner — Änderungen hier wirken nicht auf das Pedal.':
         'Copia en el ordenador: los cambios aquí no afectan al pedal.',
        'Patch%d wird aus der Kopie auf dem Rechner entfernt. Das Pedal bleibt unverändert. Fortfahren?':
         'Patch%d se eliminará de la copia en el ordenador. El pedal no cambia. ¿Continuar?',
        'Patch%d wird von der Karte entfernt. Vorher wird er auf dem Rechner gesichert. Fortfahren?':
         'Patch%d se eliminará de la tarjeta. Antes se guarda en el ordenador. ¿Continuar?',
        'Ja':
         'Sí',
        'Nein':
         'No',
        'Dem Python fehlen die Zertifikate. Auf macOS hilft „Install Certificates.command“ im Python-Ordner, oder: pip install certifi':
         'A Python le faltan los certificados. En macOS ejecuta «Install Certificates.command» en la carpeta de Python, o: pip install certifi',
        'GitHub ist nicht erreichbar: %s':
         'No se puede acceder a GitHub: %s',
        'unbekannter Grund':
         'motivo desconocido',
        'Gelöscht: %s':
         'Borrado: %s',
        'Fehler:':
         'Error:',
        'Platz löschen':
         'Borrar plaza',
        'Welcher Platz soll gelöscht werden? (z. B. 30 oder Patch30)':
         '¿Qué plaza borrar? (p. ej. 30 o Patch30)',
        'Platz löschen …':
         'Borrar plaza …',
        '%s ist nicht belegt — nichts zu löschen.':
         '%s está vacío: nada que borrar.',
        '%s wird aus %s gelöscht. Das läßt sich nicht rückgängig machen. Fortfahren?':
         '%s se borrará de %s. No se puede deshacer. ¿Continuar?',
        'Lösche':
         'Borrando',
        'Gelöscht:':
         'Borrado:',
        'Fehler beim Löschen:':
         'Error al borrar:',
        'Die Datei ist %s lang — das Pedal nimmt höchstens 10 Minuten je Loop an. Bitte im Hauptfenster kürzen oder teilen.':
         'El archivo dura %s: el pedal admite como máximo 10 minutos por bucle. Acórtalo o divídelo en la ventana principal.',
        'Dieser Ordner ist nicht lesbar: %s':
         'No se puede leer esta carpeta: %s',
        'Auswahl:':
         'Selección:',
        'Pegelprotokoll wählen':
         'Elegir el registro de nivel',
        'Audiodatei zum Teilen auswählen':
         'Elegir el archivo de audio a dividir',
        'Dateiname:':
         'Nombre del archivo:',
        'Speichern':
         'Guardar',
        'Bitte einen Dateinamen angeben.':
         'Introduce un nombre de archivo.',
        'Überschreiben':
         'Sobrescribir',
        '%s gibt es schon. Ersetzen?':
         '%s ya existe. ¿Reemplazar?',
        'Speicherkarte des JamMan wählen':
         'Elegir la tarjeta del JamMan',
        'Zielordner für die Sicherung':
         'Carpeta de destino de la copia',
        'Dateien':
         'Archivos',
        'Nach oben':
         'Subir',
        'Öffnen':
         'Abrir',
        'Bitte eine Datei auswählen.':
         'Elige un archivo.',
        'Alle Dateien':
         'Todos los archivos',
        'Die Datei konnte nicht geschrieben werden: %s':
         'No se pudo escribir el archivo: %s',
        'ohne':
         'ninguno',
        'ende':
         'final',
        'klick':
         'clic',
        '%d Patches ohne Ton — nur die Beschreibung ist geholt.':
         '%d parches sin audio: solo se ha obtenido la descripción.',
        'Ganzen Ordner aufs Pedal …':
         'Toda la carpeta al pedal …',
        'Ordner mit Patch-Ordnern wählen':
         'Elegir una carpeta con carpetas de patch',
        'Ganzen Ordner aufs Pedal':
         'Toda la carpeta al pedal',
        '%d Patch-Ordner werden in %s geschrieben. Belegte Plätze werden vorher gesichert, unveränderte übersprungen. Fortfahren?':
         'Se escribirán %d carpetas de patch en %s. Las plazas ocupadas se guardan antes, las sin cambios se omiten. ¿Continuar?',
        'internen Speicher':
         'la memoria interna',
        'der Karte':
         'la tarjeta',
        'Im Ordner liegen keine Patch-Ordner (erwartet: Patch01 … Patch200).':
         'La carpeta no contiene carpetas de patch (se espera: Patch01 … Patch200).',
        'Schreibe':
         'Escribiendo',
        'Fertig: %d Patches geschrieben, %d übersprungen, %d vorher gesichert':
         'Listo: %d parches escritos, %d omitidos, %d guardados antes',
        'Fehler beim Schreiben:':
         'Error al escribir:',
        'Übertragen: %d Dateien · unverändert übersprungen: %d':
         'Transferidos: %d archivos · omitidos sin cambios: %d',
        'Zielplatz auf dem Pedal (z. B. 17 oder Patch17):':
         'Plaza de destino en el pedal (p. ej. 17 o Patch17):',
        'Unerwarteter Fehler':
         'Error inesperado',
        'Das Werkzeug wurde unerwartet beendet.':
         'La herramienta se detuvo de forma inesperada.',
        'Protokoll:':
         'Registro:',
        'Auswahl kopieren':
         'Copiar la selección',
        'Nichts markiert — bitte zuerst Text markieren.':
         'Nada seleccionado: selecciona primero el texto.',
        'In die Zwischenablage kopiert.':
         'Copiado al portapapeles.',
        'Anleitung speichern':
         'Guardar las instrucciones',
        'Gespeichert.':
         'Guardado.',
        'Alles kopieren':
         'Copiar todo',
        'Als Datei speichern':
         'Guardar como archivo',
        'Anleitung':
         'Instrucciones',
        'Zum Umwandeln fehlt ffmpeg.':
         'Falta ffmpeg para la conversión.',
        'Die Programmdateien lassen sich hier nicht ersetzen: %s':
         'Los archivos del programa no se pueden sustituir aquí: %s',
        'Unerwarteter Fehler: %s':
         'Error inesperado: %s',
        'Aktualisieren':
         'Actualizar',
        'Die neueste Fassung wird von GitHub geladen und ersetzt die Programmdateien. Ihre Einstellungen und Ihre Karte bleiben unberührt. Fortfahren?':
         'La última versión se descarga de GitHub y sustituye los archivos del programa. Tus ajustes y tu tarjeta no se tocan. ¿Continuar?',
        'Jetzt aktualisieren?':
         '¿Actualizar ahora?',
        'Aktualisiert':
         'Actualizado',
        'Nicht aktualisiert':
         'No actualizado',
        'Fenster jetzt schließen?':
         '¿Cerrar la ventana ahora?',
        'GitHub ist nicht erreichbar.':
         'No se puede acceder a GitHub.',
        'Sie benutzen bereits die neueste Fassung (%s).':
         'Ya está usando la última versión (%s).',
        'Zur Fassung %s gibt es keine Datei zum Herunterladen.':
         'La versión %s no tiene archivo para descargar.',
        'Lade Fassung %s …':
         'Descargando la versión %s …',
        'Herunterladen fehlgeschlagen: %s':
         'Descarga fallida: %s',
        'Die heruntergeladene Datei ist kein gültiges ZIP.':
         'El archivo descargado no es un ZIP válido.',
        'Die Datei ist beschädigt (%s).':
         'El archivo está dañado (%s).',
        'In der Datei fehlt jamman.py.':
         'Falta jamman.py en el archivo.',
        'Die Datei gehört nicht zur Fassung %s.':
         'El archivo no corresponde a la versión %s.',
        'Fassung %s ist installiert (%d Dateien). Bitte das Fenster schließen und neu starten.':
         'La versión %s está instalada (%d archivos). Cierre la ventana y vuelva a iniciar.',
        'Die Karte hängt nicht im Rechner — nichts auszuwerfen. Steckt sie im Pedal, führt das Pedal sie nicht als Laufwerk.':
         'La tarjeta no está en el ordenador: no hay nada que expulsar. Si está en el pedal, este no la ofrece como unidad.',
        'Auswerfen ging nicht. Bitte im Dateimanager auf „Auswerfen" klicken.':
         'No se pudo expulsar. Usa «Expulsar» en el gestor de archivos.',
        'Keine Karte im Rechner gefunden.':
         'No se ha encontrado ninguna tarjeta en el ordenador.',
        'Karte im Rechner:  „Karte wählen“ drücken.':
         'Tarjeta en el ordenador: pulsa «Elegir tarjeta».',
        'Karte im Pedal:    oben rechts „Pedal über USB“ — dort sind Karte und interner Speicher erreichbar.':
         'Tarjeta en el pedal: arriba a la derecha «Pedal por USB» — allí se accede a la tarjeta y a la memoria interna.',
        'Speicherkarte': 'Tarjeta de memoria',
        'Kopie vom Pedal': 'copia del pedal',
        'Speicherkarte des JamMan Stereo': 'Tarjeta de memoria del JamMan Stereo',
        'Im Pedal steckt keine Karte.': 'No hay tarjeta en el pedal.',
        'Karte einlegen und dann „Karte wählen“ oder „Neu laden“ drücken.':
         'Inserta la tarjeta y pulsa «Elegir tarjeta» o «Recargar».',
        'keine Karte':
         'sin tarjeta',
        'Dies ist eine Kopie vom Pedal, nicht die Karte selbst. Über „Karte wählen“ die Karte öffnen.':
         'Esta es una copia tomada del pedal, no la tarjeta. Usa «Elegir tarjeta».',
        'JamMan Card Tool': 'JamMan Card Tool',
        'Karte und interner Speicher': 'tarjeta y memoria interna',
        '(für Rückgängig nötig)':
         '(necesaria para Deshacer)',
        'Darunter ist die Sicherung für Rückgängig. Danach läßt sich der letzte Schreibvorgang nicht mehr zurücknehmen. Trotzdem löschen?':
         'Incluye la copia para Deshacer. Después ya no se podrá deshacer la última escritura. ¿Borrar igualmente?',
        'Sicherungen aufräumen …':
         'Limpiar copias …',
        'Sicherungen aufräumen':
         'Limpiar copias',
        'Sicherungen im Zielordner':
         'Copias en la carpeta de destino',
        '%d Sicherungen · %.1f MB':
         '%d copias · %.1f MB',
        'Neueste behalten:':
         'Conservar las más recientes:',
        'Markierte löschen':
         'Borrar seleccionadas',
        'Erst Sicherungen in der Liste markieren.':
         'Primero marca copias en la lista.',
        '%d Sicherungen mit %.1f MB werden endgültig gelöscht. Fortsetzen?':
         'Se borrarán definitivamente %d copias (%.1f MB). ¿Continuar?',
        '%d Sicherungen konnten nicht gelöscht werden.':
         'No se pudieron borrar %d copias.',
        'Der Zielordner ist noch nicht angelegt.':
         'La carpeta de destino aún no existe.',
        'Im Zielordner liegen keine Sicherungen.':
         'No hay copias en la carpeta de destino.',
        'Alle auswählen':
         'Seleccionar todo',
        'Nichts markiert. Alle %d Patches in die Setlist?':
         'Nada seleccionado. ¿Poner los %d patches en la setlist?',
        'Warte auf das Ende des laufenden Vorgangs …':
         'Esperando a que termine la operación en curso …',
        'Der Vorgang reagiert nicht — Fenster schließen und neu öffnen.':
         'La operación no responde: cierra la ventana y ábrela de nuevo.',
        'Hole Beschreibungen': 'Leyendo descripciones',
        'Es wurde kein Zielplatz eingegeben.':
         'No se ha introducido ninguna ranura de destino.',
        'Der Zielplatz muß wie Patch01 … Patch200 heißen, nicht „%s“.':
         'La ranura debe llamarse como Patch01 … Patch200, no «%s».',
        'OK': 'OK',
        'Ein Vorgang läuft noch — bitte abwarten.':
         'Hay una operación en curso: espera por favor.',
        'Verbindung neu aufbauen':
         'Reconectar',
        'Verbindung wird neu aufgebaut …':
         'Reconectando …',
        'Lesen und Schreiben über USB. Vor jedem Überschreiben wird der alte Platz auf den Rechner gesichert.':
         'Lectura y escritura por USB. Antes de sobrescribir, la ranura anterior se guarda en el ordenador.',
        'Über USB fließen etwa 1,3 MB/s. Für eine ganze Karte ist ein Kartenleser viel schneller. Schreiben verändert die Karte — vorher wird gesichert.':
         'Por USB circulan unos 1,3 MB/s. Para una tarjeta completa, un lector es mucho más rápido. Escribir modifica la tarjeta: se guarda antes.',
        'Audio in den Speicher schreiben …': 'Escribir audio en la memoria …',
        'Beim JamMan Solo XT behält der FS3X seine Grundfunktionen. Eine eigene Belegung speichert das Pedal zwar, wertet sie aber nicht aus — deshalb wird sie hier nicht angeboten.':
         'En el JamMan Solo XT, el FS3X conserva sus funciones básicas. El pedal guarda una asignación propia pero no la aplica, por eso no se ofrece aquí.',
        'Änderungen wirken nach einem Neustart des Pedals.': 'Los cambios surten efecto tras reiniciar el pedal.',
        'gewünscht': 'pedido',
        'Pedal meldet': 'el pedal informa',
        '(nicht belegt)':
         '(sin asignar)',
        'UndoRedo = Rückgängig/Wiederholen · Reverse = Rückwärts · Tempo = Tempo antippen':
         'UndoRedo = deshacer/rehacer · Reverse = hacia atrás · Tempo = marcar tempo',
        'Sichern': 'Guardar',
        'Schreiben': 'Escribir',
        'Setlists': 'Setlists',
        'Pedal': 'Pedal',
        'Ins Hauptfenster holen …': 'Traer a la ventana principal …',
        'Einzelnen Patch sichern …': 'Guardar un solo patch …',
        'Patch-Ordner aufs Pedal …': 'Carpeta de patch al pedal …',
        'Übernommen.': 'Aplicado.',
        'Pedal spielt jetzt:': 'El pedal reproduce ahora:',
        'Audio aufs Pedal …':
         'Audio al pedal …',
        'Audio aufs Pedal':
         'Audio al pedal',
        'Audiodatei wählen':
         'Elegir un archivo de audio',
        'Audio':
         'Audio',
        'Sichere den alten Platz …':
         'Guardando la ranura anterior …',
        'Wandle die Datei …':
         'Convirtiendo el archivo …',
        'Umwandeln fehlgeschlagen — die Datei muß 44,1 kHz, 16 Bit, Stereo werden.':
         'Conversión fallida: el archivo debe quedar en 44,1 kHz, 16 bits, estéreo.',
        'Leere den Platz …': 'Vaciando la ranura …',
        'Pedal spielt jetzt Bank/Patch:': 'El pedal reproduce ahora banco/patch:',
        'Karte erkannt:': 'Tarjeta detectada:',
        'Als ZIP sichern …':
         'Guardar como ZIP …',
        'Als ZIP sichern':
         'Guardar como ZIP',
        'Sichere als ZIP …':
         'Guardando como ZIP …',
        'Sichere:':
         'Guardando:',
        'Fehler beim Sichern:':
         'Error al guardar:',
        'Die Setlist ist leer.':
         'La setlist está vacía.',
        'Erst Patches in der Liste markieren (⌘-Klick).':
         'Primero marca patches en la lista (⌘-clic).',
        'Es gibt nichts rückgängig zu machen.':
         'No hay nada que deshacer.',
        'Fußschalter …':
         'Pulsador …',
        'Fußschalter':
         'Pulsador',
        'Tastenbelegung des Pedals':
         'Asignación del pulsador del pedal',
        'Werte sind Geräteeinstellungen — nur ändern, wenn du weißt, was sie bedeuten.':
         'Son ajustes del dispositivo: cámbialos solo si sabes qué significan.',
        'Übernehmen':
         'Aplicar',
        'JamMan':
         'JamMan',
        'Karte':
         'tarjeta',
        'intern':
         'interno',
        'Liste als Textdatei sichern':
         'Guardar la lista como archivo de texto',
        'Liste als Textdatei …':
         'Lista como archivo de texto …',
        'Mitlaufen':
         'Seguir',
        'Mitlaufen an — am Pedal einen Patch wählen.':
         'Seguimiento activado: elige un patch en el pedal.',
        'Mitlaufen aus.':
         'Seguimiento desactivado.',
        'Rückgängig':
         'Deshacer',
        'Setlist holen …':
         'Traer setlist …',
        'Setlist laden':
         'Cargar setlist',
        'Setlist sichern …':
         'Guardar setlist …',
        'Setlist sichern':
         'Guardar setlist',
        'Setlist.txt':
         'Setlist.txt',
        'Stelle wieder her:':
         'Restaurando:',
        '%s wird durch die Sicherung ersetzt. Fortsetzen?':
         '%s se sustituirá por la copia de seguridad. ¿Continuar?',
        'Ordner des Patches wählen':
         'Elegir la carpeta del patch',
        'Aufs Pedal schreiben':
         'Escribir en el pedal',
        '%s ist schon belegt. Der Patch wird vorher auf den Rechner gesichert und dann überschrieben. Fortsetzen?':
         '%s ya está ocupado. Ese patch se guardará primero en el ordenador y luego se sobrescribirá. ¿Continuar?',
        'Schreibe aufs Pedal …':
         'Escribiendo en el pedal …',
        'Schreibe:':
         'Escribiendo:',
        'Fehler beim Schreiben:':
         'Error al escribir:',
        'Patch aufs Pedal schreiben …':
         'Escribir patch en el pedal …',
        '(Vorschlag)': '(propuesta)',
        'Ziel:': 'Destino:',
        'noch nicht gewählt': 'aún no elegido',
        'Ziel ändern …': 'Cambiar destino …',
        'Aufräumen':
         'Limpiar',
        'Die angefangene Kopie entfernen? Nur die gerade geholten Dateien werden gelöscht, der Ordner bleibt.':
         '¿Eliminar la copia inacabada? Solo se borran los archivos recién traídos; la carpeta permanece.',
        'Aufgeräumt:':
         'Limpiado:',
        'Hole Beschreibungen …': 'Leyendo descripciones …',
        'Wird abgebrochen …': 'Cancelando …',
        'Über USB fließen etwa 1,3 MB/s. Für eine ganze Karte ist ein Kartenleser viel schneller — steckt die Karte im Pedal, bleibt nur dieser Weg.':
         'Por USB circulan unos 1,3 MB/s. Para una tarjeta completa, un lector es mucho más rápido — si la tarjeta está en el pedal, este es el único camino.',
        'Abbrechen': 'Cancelar',
        'Abgebrochen.': 'Cancelado.',
        'Fehler beim Öffnen:': 'Error al abrir:',
        'Alle ins Hauptfenster holen …': 'Traer todo a la ventana principal …',
        'Zielordner wählen': 'Elegir la carpeta de destino',
        'Hole Patch': 'Leyendo patch',
        'Fertig:': 'Listo:',
        'Im Hauptfenster öffnen?': '¿Abrir en la ventana principal?',
        'Gesichert': 'Guardado',
        'Pedal über USB': 'Pedal por USB',
        'Das USB-Modul fehlt: greenglass.py muß neben jamman.py liegen.': 'Falta el módulo USB: greenglass.py debe estar junto a jamman.py.',
        'Gesichert nach:': 'Guardado en:',
        'Interner Speicher': 'Memoria interna',
        'SD-Karte': 'Tarjeta SD',
        'Kein JamMan gefunden — Pedal einschalten und Kabel prüfen.': 'No se ha encontrado ningún JamMan — encienda el pedal y compruebe el cable.',
        '(ohne Namen)': '(sin nombre)',
        'Fehler beim Lesen:': 'Error al leer:',
        'Erst „Neu laden“ drücken.': 'Pulse primero «Recargar».',
        'Lädt …': 'Cargando …',
        'Nur lesen: das Pedal wird nicht verändert.': 'Solo lectura: el pedal no se modifica.',
        'Ordner zum Sichern wählen': 'Elegir una carpeta para guardar',
        'Patch sichern …': 'Guardar patch …',
        'Verbinde mit dem Pedal …': 'Conectando con el pedal …',
        'Spenden': 'Donar',
        'Für dieses Werkzeug ist noch kein Spendenlink eingetragen.': 'Todavía no hay un enlace de donación para esta herramienta.',
        'Er steht in jamman.py in der Zeile SPENDEN_LINK.': 'Se introduce en jamman.py, línea SPENDEN_LINK.',
        '      Schnitt %d bei %s: %s': '      Corte %d en %s: %s',
        'keine Pause in der Nähe - rechnerisch geschnitten': 'no hay pausa cerca — corte calculado',
        "Tk:            %s": "Tk:            %s",
        'Sicherung': 'copia de seguridad',
        'Aufspielen': 'Importar',
        'Der Name darf nicht leer sein.': 'El nombre no puede estar vacío.',
        'Bitte 2 bis 60 Teile angeben.': 'Indica de 2 a 60 partes.',
        'Die Datei ist nicht lesbar.': 'El archivo no se puede leer.',
        'Alle Patches normieren': 'Normalizar todos los patches',
        'Alle Patches werden auf gleiche Lautheit gebracht:\n\n  Ziel -18 dB Lautheit, Spitze höchstens -1 dB\n\nVorher wird die ganze Karte gesichert - das dauert bei\n900 MB ein bis zwei Minuten. Fortfahren?': 'Todos los patches se igualan en sonoridad:\n\n  objetivo -18 dB de sonoridad, pico como máximo -1 dB\n\nAntes se copia toda la tarjeta - tarda uno o dos\nminutos para 900 MB. ¿Continuar?',
        'Zum Tauschen genau ZWEI Patches markieren:\nersten anklicken, zweiten mit gedrückter Cmd-Taste (⌘).': 'Para intercambiar, marca exactamente DOS patches:\nhaz clic en el primero y luego en el segundo con Cmd (⌘) pulsada.',
        'Oben steht überall „wie Vorlage - dann gibt es nichts zu ändern.': 'Arriba pone «como plantilla» - no hay nada que aplicar.',
        'Erst einen Patch wählen.': 'Elige primero un patch.',
        'Karte auswerfen? Danach kann sie abgezogen werden.': '¿Expulsar la tarjeta? Después se puede retirar.',
        'Keine JamMan-Karte': 'No es una tarjeta JamMan',
        'Datei zu lang': 'Archivo demasiado largo',
        'Beim Pegel bitte eine Zahl in dB eintragen (z. B. -3), oder das Feld leer lassen.': 'Introduce un nivel en dB (p. ej. -3) o deja el campo vacío.',
        'Bei Teile bitte eine ganze Zahl eintragen.': 'Introduce un número entero de partes.',
        'Beim Pegel bitte eine Zahl in dB eintragen (z. B. -3).': 'Introduce un nivel en dB (p. ej. -3).',
        'Bei Fade am Ende bitte eine Zahl in Sekunden eintragen.': 'Introduce la duración del fundido en segundos.',
        'Pegel zurückgerechnet': 'nivel deshecho',
        'alle Patches normiert': 'todos los patches normalizados',
        'Fertig.': 'Hecho.',
        'Tauschen:': 'Intercambiar:',
        'Karte auswerfen, in den JamMan stecken — der Patch ist weg.': 'Expulsa la tarjeta y pónla en el JamMan — el patch desaparece.',
        'Neue Reihenfolge:': 'Nuevo orden:',
        'JamMan Solo XT — Speicherkarte verwalten': 'JamMan Solo XT — gestionar la tarjeta de memoria',
        'Pythons auf diesem Rechner:': 'Pythons en este equipo:',
        'Geprüfte Orte:': 'Lugares comprobados:',
        'Laufwerke mit JamManSoloXT:': 'Unidades con JamManSoloXT:',
        'Trockenlauf — nichts geändert. Mit --schreiben ausführen.': 'Simulación — nada cambiado. Ejecuta con --schreiben.',
        'Trockenlauf — nichts kopiert. Mit --schreiben ausführen.': 'Simulación — nada copiado. Ejecuta con --schreiben.',
        'Trockenlauf — nichts angelegt. Mit --schreiben ausführen.': 'Simulación — nada creado. Ejecuta con --schreiben.',
        'Trockenlauf — nichts geschrieben. Mit --schreiben ausführen.': 'Simulación — nada escrito. Ejecuta con --schreiben.',
        'Trockenlauf — nichts zurückgeholt. Mit --schreiben ausführen.': 'Simulación — nada restaurado. Ejecuta con --schreiben.',
        'Trockenlauf — nichts umbenannt. Mit --schreiben ausführen.': 'Simulación — nada renombrado. Ejecuta con --schreiben.',
        'Trockenlauf — nichts getauscht. Mit --schreiben ausführen.': 'Simulación — nada intercambiado. Ejecuta con --schreiben.',
        'Trockenlauf — nichts gelöscht. Mit --schreiben ausführen.': 'Simulación — nada borrado. Ejecuta con --schreiben.',
        'Keine Patches auf der Karte.': 'No hay patches en la tarjeta.',
        'Suche musikalisch brauchbare Schnittstellen …': 'Buscando puntos de corte musicales …',
        'Kopf:         WAV-Kopf auf einfaches PCM umgeschrieben': 'Cabecera:     cabecera WAV reescrita a PCM simple',
        'Alle Ordner heißen schon so. Nichts zu tun.': 'Todas las carpetas ya se llaman así. Nada que hacer.',
        'Hinweis: diese gibt es auf der Karte nicht und werden neu angelegt:': 'Nota: estos no están en la tarjeta y se crearán:',
        'Nichts zu tun.': 'Nada que hacer.',
        '1  Patches auflisten': '1  Listar patches',
        '2  Patch anhören': '2  Escuchar un patch',
        '3  Datei aufspielen': '3  Importar un archivo',
        '4  Patch ändern (Stopp, Wiedergabe, Name)': '4  Cambiar un patch (parada, reproducción, nombre)',
        '5  Patch verschieben': '5  Mover un patch',
        '6  Patch löschen': '6  Borrar un patch',
        '7  Alles sichern': '7  Copiar todo',
        '8  Karte auswerfen': '8  Expulsar la tarjeta',
        '0  Beenden': '0  Salir',
        'Angehalten.': 'Detenido.',
        'erste Zeile im Fenster:': 'primera línea en la ventana:',
        'ACHTUNG: das Dateisystem liefert Lesefehler.': 'ATENCIÓN: el sistema de archivos da errores de lectura.',
        'Das ist kein Fehler des Werkzeugs - die Karte oder': 'No es un error del programa — la tarjeta o',
        'ihr Einhängepunkt ist beschädigt. Erste Schritte:': 'su punto de montaje está dañado. Primeros pasos:',
        'Ton:           afplay': 'Sonido:        afplay',
        'Ton:           winsound (Windows-eigen)': 'Sonido:        winsound (incluido en Windows)',
        'Hinweis: Pegelprotokoll konnte nicht geschrieben werden.': 'Nota: no se pudo escribir el registro de niveles.',
        'Sicherung beim Start: Karte unverändert, nichts zu tun.': 'Copia al iniciar: tarjeta sin cambios, nada que hacer.',
        'Pause an der Naht wird herausgenommen': 'La pausa en la unión se elimina',
        'Quelle:       %s': 'Origen:        %s',
        'Quelle: %s': 'Origen: %s',
        'Ziel:   %s': 'Destino:   %s',
        'Ziel: %.1f dB Lautheit (RMS), Spitze höchstens %.1f dB': 'Destino: %.1f dB de sonoridad (RMS), pico como máximo %.1f dB',
        'Karte: %s': 'Tarjeta: %s',
        'Name:         %s': 'Nombre:       %s',
        'Dauer:        %s': 'Duración:     %s',
        'Dauer:  %s': 'Duración: %s',
        'Datei:  %s': 'Archivo:  %s',
        'Format:       %d Hz / %d Bit / %d Kanal/Kanäle%s': 'Formato:      %d Hz / %d bits / %d canal(es)%s',
        'Fassung:       %s': 'Versión:      %s',
        'Python:        %s': 'Python:       %s',
        'System:        %s %s': 'Sistema:      %s %s',
        'Tk-Modul:      %s': 'Módulo Tk:    %s',
        'Schrift:       %s': 'Fuente:       %s',
        'Kartenwurzeln: %s': 'Raíces de tarjetas: %s',
        'ffmpeg:        %s': 'ffmpeg:       %s',
        'gewählt:       %s': 'elegido:      %s',
        'Ton:           %s': 'Sonido:       %s',
        'Patches:       %d': 'Patches:      %d',
        'Patches: %d': 'Patches: %d',
        'Patches auf der Karte:    %d': 'Patches en la tarjeta:    %d',
        'Patches in der Sicherung: %d': 'Patches en la copia:      %d',
        'Inhalt von %s:': 'Contenido de %s:',
        'Gesichert: %d Patches, %.0f MB.': 'Copiado: %d patches, %.0f MB.',
        'gesichert: %d Patches, %.0f MB': 'copiado: %d patches, %.0f MB',
        'Fertig. Sicherung: %s': 'Hecho. Copia: %s',
        'Fertig: %s angelegt.': 'Hecho: %s creado.',
        'Fertig: %d Ordner umbenannt, %d Fehler, %d Patches lesbar.': 'Hecho: %d carpetas renombradas, %d errores, %d patches legibles.',
        'Fertig: %d Patches normiert, %d davon in der Spitze gedeckelt.': 'Hecho: %d patches normalizados, %d limitados en pico.',
        'Gelöscht. Sicherung: %s': 'Borrado. Copia: %s',
        'Getauscht. Sicherung: %s': 'Intercambiado. Copia: %s',
        'Geändert. Sicherung: %s': 'Cambiado. Copia: %s',
        'Verschoben. Sicherung: %s': 'Movido. Copia: %s',
        'Zurückgeholt. Der Zustand davor liegt in: %s': 'Restaurado. El estado anterior está en: %s',
        'Zurückgerechnet: %d Patches. Sicherung davor: %s': 'Deshecho: %d patches. Copia anterior: %s',
        'Vorher wird er gesichert nach: %s': 'Antes se copia a: %s',
        'Wird gelöscht: %s  (%s)': 'Se borrará: %s  (%s)',
        'Neuer Patch:  Patch%s  (Name: %s)': 'Nuevo patch: Patch%s  (nombre: %s)',
        'Patch:        %s  (%s)': 'Patch:        %s  (%s)',
        'Patch:      %s  (%s)': 'Patch:      %s  (%s)',
        'Vorlage:      %s': 'Plantilla:    %s',
        'Teile:  %d × etwa %s': 'Partes:  %d × unos %s',
        'Stopp:        %s': 'Parada:       %s',
        'Stopp:        %s (%s)': 'Parada:       %s (%s)',
        'Rhythmus:     %s': 'Ritmo:        %s',
        'Rhythmus:     %s (%s)': 'Ritmo:        %s (%s)',
        'Wiedergabe:   %s': 'Reproducción: %s',
        'Wiedergabe:   %s (IsLoop %s)': 'Reproducción: %s (IsLoop %s)',
        'Pegel:        %+.1f dB': 'Nivel:        %+.1f dB',
        'Pegel:        %.1f dB → %.1f dB Spitze': 'Nivel:        %.1f dB → %.1f dB pico',
        'Pegel:        auf -1 dB Spitze normieren': 'Nivel:        normalizar a -1 dB pico',
        'Fade:         letzte %.1f s werden ausgeblendet': 'Fundido:      los últimos %.1f s se atenúan',
        'Fade:         letzte %.1f s der Aufnahme ausblenden': 'Fundido:      atenuar los últimos %.1f s',
        'Kürzen:       auf %s geschnitten (harter Schnitt, 12 ms ausgeblendet)': 'Recorte:      cortado a %s (corte duro, 12 ms atenuados)',
        'Protokoll: %s': 'Registro: %s',
        'Sicherung: %s': 'Copia: %s',
        'Sicherung:  %s': 'Copia:  %s',
        'Schreibweise des Geräts: Patch%0*d': 'Ortografía del dispositivo: Patch%0*d',
        'Zu ändern: %d': 'A cambiar: %d',
        'Zuordnung liegt in: %s': 'La correspondencia está en: %s',
        'Betroffene Einträge (erste drei): %s': 'Entradas afectadas (las tres primeras): %s',
        'Keine Karte gefunden unter %s.': 'No se encontró tarjeta en %s.',
        "BEFEHL": "ORDEN",
        "  Auswahl: ": "  Opción: ",
        "  Anderer Pfad (leer = beenden): ": "  Otra ruta (vacío = salir): ",
        "  Patch-Nummer: ": "  Número del patch: ",
        "  Name (leer = Dateiname): ": "  Nombre (vacío = nombre del archivo): ",
        "  Stopp (fade/instant, leer = wie Vorlage): ": "  Parada (fade/instant, vacío = como plantilla): ",
        "  Wiedergabe (loop/single, leer = wie Vorlage): ": "  Reproducción (loop/single, vacío = como plantilla): ",
        "  Pfad zur Audiodatei: ": "  Ruta del archivo de audio: ",
        "  Stopp (fade/instant, leer = lassen): ": "  Parada (fade/instant, vacío = dejar): ",
        "  Wiedergabe (loop/single, leer = lassen): ": "  Reproducción (loop/single, vacío = dejar): ",
        "  Neuer Name (leer = lassen): ": "  Nombre nuevo (vacío = dejar): ",
        "  Wirklich schreiben? (j/n): ": "  ¿Escribir de verdad? (s/n): ",
        "  Welcher Patch: ": "  ¿Qué patch?: ",
        "  An welche Stelle: ": "  ¿A qué plaza?: ",
        "  Wirklich verschieben? (j/n): ": "  ¿Mover de verdad? (s/n): ",
        "  Zielordner (leer = ~/JamMan-Sicherung): ": "  Carpeta de destino (vacío = ~/JamMan-Sicherung): ",
        "  Wirklich löschen? (j/n): ": "  ¿Eliminar de verdad? (s/n): ",
        "Kein Abspielprogramm gefunden (aplay, paplay, mpv oder ffplay).": "No se encontró ningún reproductor (aplay, paplay, mpv o ffplay).",
        "Fehler: im Protokoll stehen keine Einträge.": "Error: el protocolo no tiene entradas.",
        "Fehler: Datei ließ sich nicht umwandeln.": "Error: el archivo no se pudo convertir.",
        "Fehler: kein vollständiger Patch als Vorlage gefunden.": "Error: no se encontró ningún patch completo como plantilla.",
        "Fehler: nichts angegeben (--stopp, --rhythmus, --wiedergabe, --name oder --fade).": "Error: no se indicó nada (--stopp, --rhythmus, --wiedergabe, --name o --fade).",
        "Fehler: '%s' ist keine lesbare Audiodatei.": "Error: '%s' no es un archivo de audio legible.",
        "Fehler: --teile N oder --laenge SEKUNDEN angeben.": "Error: indique --teile N o --laenge SEGUNDOS.",
        "Fehler: %.1f Minuten pro Teil sind zu lang (Grenze %d Minuten).": "Error: %.1f minutos por parte es demasiado (límite %d minutos).",
        "Fehler: '%s' gibt es nicht.": "Error: '%s' no existe.",
        "Fehler: '%s' ist keine lesbare Audiodatei.%s": "Error: '%s' no es un archivo de audio legible.%s",
        "Fehler: '%s' liegt schon vor.": "Error: '%s' ya existe.",
        "Fehler: Die Datei ließ sich nicht kürzen.": "Error: el archivo no se pudo acortar.",
        "Fehler: Die Umwandlung hat keine hörbare Aufnahme\n  ergeben (nur Stille). Datei prüfen: %s": "Error: la conversión no produjo ninguna grabación audible\n  (solo silencio). Revisar el archivo: %s",
        "Fehler: Patch%d gibt es nicht.": "Error: el patch %d no existe.",
        "Fehler: Die Aufnahme ließ sich nicht ausblenden (kein 16-Bit-WAV?).": "Error: la grabación no se pudo atenuar (¿no es un WAV de 16 bits?).",
        "Fehler: '%s' ist nicht eingelegt.": "Error: '%s' no está insertada.",
        "Fehler beim Auswerfen: %s": "Error al expulsar: %s",
        "Fehler: Patch%d hat keine WAV.": "Error: el patch %d no tiene WAV.",
        "Fehler: Sicherungsordner '%s' gibt es nicht.": "Error: la carpeta de copia '%s' no existe.",
        "Fehler: in '%s' liegen keine Patch-Ordner.": "Error: '%s' no contiene carpetas de patch.",
        "Fehler: keine Sicherung mit einer Aufnahme für Patch%d in '%s'.\n  Fade-Sicherungen heißen Sicherung_<Datum>_vor_fade.": "Error: no hay copia con una grabación para el patch %d en '%s'.\n  Las copias de fundido se llaman Sicherung_<fecha>_vor_fade.",
        "Fehler: Platz %d ist belegt (%s).\n  Zum Tauschen:  tauschen --patch %d --mit %d": "Error: la plaza %d está ocupada (%s).\n  Para intercambiar:  tauschen --patch %d --mit %d",
        "Fehler: Nummern müssen zwischen 1 und %d liegen.": "Error: los números deben estar entre 1 y %d.",
        "Fehler: '%s' ist ein Ordner, keine Datei.": "Error: '%s' es una carpeta, no un archivo.",
        "Fehler: '%s' ist kein Pegelprotokoll (keine lesbare Textdatei).": "Error: '%s' no es un protocolo de nivel (no es un archivo de texto legible).",
        "Fehler: '%s' darf nicht gelesen werden.": "Error: '%s' no se puede leer.",
        "Fehler: '%s' lässt sich nicht lesen: %s": "Error: '%s' no se puede leer: %s",
        "Fehler beim Umwandeln. Die Datei hat %d Hz / %d Kanal/Kanäle;\n  nötig sind 44100 Hz und 2 Kanäle. Bitte vorher umwandeln oder\n  ffmpeg installieren.": "Error al convertir. El archivo tiene %d Hz / %d canal(es);\n  se necesitan 44100 Hz y 2 canales. Convertirlo antes o\n  instalar ffmpeg.",
        "Fehler: '%s' lässt sich nicht schreiben.\n  Nimm einen anderen Ordner:  --sicherung <pfad>": "Error: '%s' no se puede escribir.\n  Elija otra carpeta:  --sicherung <ruta>",
        "Fehler: Patch%d hat keine phrase.wav.": "Error: el patch %d no tiene phrase.wav.",
        "Fehler: Der Pegel ließ sich nicht ändern.": "Error: el nivel no se pudo cambiar.",
        "Fehler: in '%s' darf nicht geschrieben werden.\n  Nimm einen anderen Ordner:  --sicherung <pfad>": "Error: en '%s' no se puede escribir.\n  Elija otra carpeta:  --sicherung <ruta>",
        "Fehler: Teil %d ließ sich nicht schneiden.": "Error: la parte %d no se pudo cortar.",
        "Fehler: Die Datei ist %.1f Minuten lang.\n  Der JamMan Solo XT nimmt höchstens %d Minuten pro Loop an\n  (Handbuch, Seite 60: \"Max. Individual Loop Time: 10 minutes\").\n  Längere Loops erscheinen auf der Karte, werden vom Pedal aber\n  nicht erkannt - und können dabei verschwinden.\n\n  So geht es weiter:\n    --kuerzen            auf 9:59 kürzen (harter Schnitt am Ende,\n                         12 ms ausgeblendet, damit es nicht knackt)\n    --laenge 480         auf 8:00 kürzen\n    --trotzdem           unverändert schreiben (zum Ausprobieren)": "Error: el archivo dura %.1f minutos.\n  El JamMan Solo XT acepta como máximo %d minutos por loop\n  (manual, página 60: \"Max. Individual Loop Time: 10 minutes\").\n  Los loops más largos aparecen en la tarjeta, pero el pedal no\n  los reconoce - y pueden desaparecer.\n\n  Qué se puede hacer:\n    --kuerzen            acortar a 9:59 (corte duro al final,\n                         12 ms atenuados para que no chasquee)\n    --laenge 480         acortar a 8:00\n    --trotzdem           escribir sin cambios (para probar)",
        "Fehler: --wav fehlt.": "Error: falta --wav.",
        "Fehler: --patch fehlt.": "Error: falta --patch.",
        "Fehler: --protokoll fehlt.": "Error: falta --protokoll.",
        "Fehler: --patch fehlt (oder --alle angeben).": "Error: falta --patch (o indicar --alle).",
        "Fehler: --patch und --auf fehlen.": "Error: faltan --patch y --auf.",
        "Fehler: --patch und --mit fehlen.": "Error: faltan --patch y --mit.",
        "Fehler: --patch und --nach fehlen.": "Error: faltan --patch y --nach.",
        "  Hinweis: Pegelprotokoll konnte nicht geschrieben werden.": "    Nota: no se pudo escribir el protocolo de nivel.",
        "  Protokoll: %s": "    Protocolo: %s",
        "  %d Einträge": "    %d entradas",
        "    %-10s %-24s Faktor %.4f → 1/%.4f": "      %-10s %-24s factor %.4f → 1/%.4f",
        "  Trockenlauf — nichts geändert. Mit --schreiben ausführen.": "    Simulación — nada cambiado. Ejecutar con --schreiben para escribir.",
        "    %-10s %+6.1f → %+6.1f dB": "      %-10s %+6.1f → %+6.1f dB",
        "  Zurückgerechnet: %d Patches. Sicherung davor: %s": "    Recalculado: %d patches. Copia previa: %s",
        "  Pegel:        %.1f dB → %.1f dB Spitze": "    Nivel:        %.1f dB → %.1f dB de pico",
        "                Die Anhebung wurde begrenzt, damit nichts": "                La subida se limitó para que nada",
        "                über 0 dBFS geht (sonst würde übersteuert).": "                supere 0 dBFS (si no, saturaría).",
        "                Achtung: %d Werte liegen an der Grenze.": "                Atención: %d valores están en el límite.",
        "  Hinweis: unbekanntes WAV-Format 0x%04X - nicht angefasst.": "    Nota: formato WAV desconocido 0x%04X — sin tocar.",
        "Fehler: '%s' ist kein Ordner. Ist die Karte eingelegt?": "  Error: '%s' no es una carpeta. ¿Está insertada la tarjeta?",
        "Fehler: auf '%s' liegt kein Ordner '%s' oder '%s'. Das ist keine JamMan-Karte.": "  Error: '%s' no contiene la carpeta '%s' ni '%s'. No es una tarjeta JamMan.",
        "  %d Apple-Dateien entfernt (._* und .DS_Store).": "    %d archivos Apple eliminados (._* y .DS_Store).",
        "  Keine Patches auf der Karte.": "    No hay patches en la tarjeta.",
        "  %-10s %-34s %8s  %s": "    %-10s %-34s %8s  %s",
        "  %-10s %-34s %8s  %s%s%s": "    %-10s %-34s %8s  %s%s%s",
        "\n  %d Patches, %s belegt.": "\n  %d patches, %s ocupados.",
        "  Sicherung beim Start: Karte unverändert, nichts zu tun.": "    Copia al inicio: tarjeta sin cambios, nada que hacer.",
        "  Sicherung beim Start: %s": "    Copia al inicio: %s",
        "    gesichert: %d Patches, %.0f MB": "      guardados: %d patches, %.0f MB",
        "    alte Sicherung entfernt: %s": "      copia antigua eliminada: %s",
        "  Quelle: %s": "    Origen: %s",
        "  Ziel:   %s": "    Destino: %s",
        "  Trockenlauf — nichts kopiert. Mit --schreiben ausführen.": "    Simulación — nada copiado. Ejecutar con --schreiben para escribir.",
        "  Gesichert: %d Patches, %.0f MB.": "    Guardado: %d patches, %.0f MB.",
        "  Datei:  %s": "    Archivo: %s",
        "  Dauer:  %s": "    Duración: %s",
        "  Teile:  %d × etwa %s": "    Partes: %d × unos %s",
        "  Suche musikalisch brauchbare Schnittstellen …": "    Buscando puntos de corte utilizables …",
        "  Hinweis: keine brauchbaren Pausen gefunden (%s) -": "    Nota: no se encontraron pausas utilizables (%s) —",
        "           es wird gleichmäßig geschnitten.": "             se corta por igual.",
        "  Pause an der Naht wird herausgenommen": "    Se quita la pausa en la unión",
        "    Teil %d: %7.1f – %7.1f s  (%s)": "      Parte %d: %7.1f – %7.1f s  (%s)",
        "  Hinweis: Teil %d wäre %.1f min - er wird auf 9:59 gekürzt.": "    Nota: la parte %d sería %.1f min — se acorta a 9:59.",
        "    Teil %d: %s – %s": "      Parte %d: %s – %s",
        "  Trockenlauf — nichts angelegt. Mit --schreiben ausführen.": "    Simulación — nada creado. Ejecutar con --schreiben para escribir.",
        "  Quelle:       %s": "    Origen:       %s",
        "  Dauer:        %s": "    Duración:     %s",
        "  Format:       %d Hz / %d Bit / %d Kanal/Kanäle%s": "    Formato:      %d Hz / %d bit / %d canal(es)%s",
        "  Stopp:        %s": "    Parada:       %s",
        "  Rhythmus:     %s": "    Ritmo:        %s",
        "  Wiedergabe:   %s": "    Reproducción: %s",
        "  Fade:         letzte %.1f s werden ausgeblendet": "    Fundido:      los últimos %.1f s se atenúan",
        "  Pegel:        auf -1 dB Spitze normieren": "    Nivel:        normalizar a -1 dB de pico",
        "  Pegel:        %+.1f dB": "    Nivel:        %+.1f dB",
        "  Neuer Patch:  Patch%s  (Name: %s)": "    Nuevo patch:  Patch%s  (nombre: %s)",
        "  Vorlage:      %s": "    Plantilla:    %s",
        "  Trockenlauf — nichts geschrieben. Mit --schreiben ausführen.": "    Simulación — nada escrito. Ejecutar con --schreiben para escribir.",
        "  Kürzen:       auf %s geschnitten (harter Schnitt, 12 ms ausgeblendet)": "    Acortar:      cortado a %s (corte duro, 12 ms atenuados)",
        "  Kopf:         WAV-Kopf auf einfaches PCM umgeschrieben": "    Cabecera:     cabecera WAV reescrita a PCM simple",
        "  Fertig: %s angelegt.": "    Hecho: %s creado.",
        "  Karte auswerfen, in den JamMan stecken — der Patch erscheint als Patch%s.": "    Expulsa la tarjeta, insértela en el JamMan — el patch aparece como Patch%s.",
        "  Patch:        %s  (%s)": "    Patch:        %s  (%s)",
        "  Stopp:        %s (%s)": "    Parada:       %s (%s)",
        "  Rhythmus:     %s (%s)": "    Ritmo:        %s (%s)",
        "  Wiedergabe:   %s (IsLoop %s)": "    Reproducción: %s (IsLoop %s)",
        "  Name:         %s": "    Nombre:       %s",
        "  Fade:         letzte %.1f s der Aufnahme ausblenden": "    Fundido:      se atenúan los últimos %.1f s de la grabación",
        "                (verändert die Audiodatei; vorher wird gesichert)": "                (modifica el archivo de audio; antes se hace una copia)",
        "  Geändert. Sicherung: %s": "    Cambiado. Copia: %s",
        "  %s Sie kann jetzt abgezogen werden.": "    %s Ya se puede retirar.",
        "  Schreibweise des Geräts: Patch%0*d": "    Escritura del aparato: Patch%0*d",
        "  Alle Ordner heißen schon so. Nichts zu tun.": "    Todas las carpetas ya están así. Nada que hacer.",
        "  Zu ändern: %d": "    A cambiar: %d",
        "    %-12s ->  %s": "      %-12s ->  %s",
        "    %-12s FEHLER: %s": "      %-12s ERROR: %s",
        "  Fertig: %d Ordner umbenannt, %d Fehler, %d Patches lesbar.": "    Hecho: %d carpetas renombradas, %d errores, %d patches legibles.",
        "  Zuordnung liegt in: %s": "    La asignación está en: %s",
        "  %d Apple-Dateien auf der Karte. Mit --schreiben entfernen.": "    %d archivos Apple en la tarjeta. Quitarlos con --schreiben.",
        "  Fertig.": "    Hecho.",
        "  Spielt \"%s\" (%s) … (Strg+C hält an)": "    Reproduciendo \"%s\" (%s) … (Ctrl+C detiene)",
        "  Ziel: %.1f dB Lautheit (RMS), Spitze höchstens %.1f dB": "    Objetivo: %.1f dB de sonoridad (RMS), pico como máximo %.1f dB",
        "  Patches: %d": "    Patches: %d",
        "  %-10s %-24s %8s": "    %-10s %-24s %8s",
        "  %-10s %-24s %+7.1f dB": "    %-10s %-24s %+7.1f dB",
        "  Sicherung: %s": "    Copia: %s",
        "  %-10s %-24s %+6.1f → %+6.1f dB  (Spitze %+.1f dB)%s": "    %-10s %-24s %+6.1f → %+6.1f dB  (pico %+.1f dB)%s",
        "  Fertig: %d Patches normiert, %d davon in der Spitze gedeckelt.": "    Hecho: %d patches normalizados, %d de ellos limitados en el pico.",
        "  Patches in der Sicherung: %d": "    Patches en la copia: %d",
        "  Patches auf der Karte:    %d": "    Patches en la tarjeta:    %d",
        "  Hinweis: diese gibt es auf der Karte nicht und werden neu angelegt:": "    Nota: estos no existen en la tarjeta y se crearán:",
        "           %s": "             %s",
        "  Trockenlauf — nichts zurückgeholt. Mit --schreiben ausführen.": "    Simulación — nada restaurado. Ejecutar con --schreiben para escribir.",
        "  Zurückgeholt. Der Zustand davor liegt in: %s": "    Restaurado. El estado anterior está en: %s",
        "  Patch:      %s  (%s)": "    Patch:      %s  (%s)",
        "  Sicherung:  %s": "    Copia:      %s",
        "    %-22s %8.2f MB": "      %-22s %8.2f MB",
        "  %s  (%s)": "    %s  (%s)",
        "  →  %s": "    →  %s",
        "  Trockenlauf — nichts umbenannt. Mit --schreiben ausführen.": "    Simulación — nada renombrado. Ejecutar con --schreiben para escribir.",
        "  Fertig. Sicherung: %s": "    Hecho. Copia: %s",
        "  Nichts zu tun.": "    Nada que hacer.",
        "  Achtung: %s und %s haben unterschiedliche Stellenzahl.": "    Atención: %s y %s tienen distinto número de dígitos.",
        "           Der JamMan sortiert nach Namen - der Patch landet dann": "             El JamMan ordena por nombre — el patch irá",
        "           auf dem Platz, den sein neuer Name angibt.": "             a la plaza que indique su nuevo nombre.",
        "  Tauschen:": "    Intercambio:",
        "    Platz %-4s %-24s  →  Platz %s": "      plaza %-4s %-24s  →  plaza %s",
        "  Trockenlauf — nichts getauscht. Mit --schreiben ausführen.": "    Simulación — nada intercambiado. Ejecutar con --schreiben para escribir.",
        "  Getauscht. Sicherung: %s": "    Intercambiado. Copia: %s",
        "  Wird gelöscht: %s  (%s)": "    Se eliminará: %s  (%s)",
        "  Vorher wird er gesichert nach: %s": "    Antes se guarda en: %s",
        "  Trockenlauf — nichts gelöscht. Mit --schreiben ausführen.": "    Simulación — nada eliminado. Ejecutar con --schreiben para escribir.",
        "  Gelöscht. Sicherung: %s": "    Eliminado. Copia: %s",
        "  Karte auswerfen, in den JamMan stecken — der Patch ist weg.": "    Expulsa la tarjeta, insértela en el JamMan — el patch ya no está.",
        "  Neue Reihenfolge:": "    Nuevo orden:",
        "    Platz %d:  %-10s  %s%s": "      plaza %d:  %-10s  %s%s",
        "  Verschoben. Sicherung: %s": "    Movido. Copia: %s",
        "  JamMan Solo XT — Speicherkarte verwalten": "    JamMan Solo XT — gestionar la tarjeta",
        "  Keine Karte gefunden unter %s.": "    No se encontró tarjeta en %s.",
        "  Karte: %s": "    Tarjeta: %s",
        "  1  Patches auflisten": "    1  Listar patches",
        "  2  Patch anhören": "    2  Escuchar un patch",
        "  3  Datei aufspielen": "    3  Cargar un archivo",
        "  4  Patch ändern (Stopp, Wiedergabe, Name)": "    4  Cambiar un patch (parada, reproducción, nombre)",
        "  5  Patch verschieben": "    5  Mover un patch",
        "  6  Patch löschen": "    6  Eliminar un patch",
        "  7  Alles sichern": "    7  Copiar todo",
        "  8  Karte auswerfen": "    8  Expulsar la tarjeta",
        "  0  Beenden": "    0  Salir",
        "  %s": "    %s",
        "  System:        %s %s": "    Sistema:       %s %s",
        "  Python:        %s": "    Python:        %s",
        "  Fassung:       %s": "    Versión:       %s",
        "  Tk:            %s": "    Tk:            %s",
        "  Tk-Modul:      %s": "    Módulo Tk:     %s",
        "  Tk:            NICHT verfügbar — %s": "    Tk:            NO disponible — %s",
        "                 (Fenster geht mit diesem Python nicht;": "                    (la ventana no funciona con este Python;",
        "                  das Textmenü geht trotzdem: jamman.py menu)": "                     el menú de texto sí: jamman.py menu)",
        "  Ton:           winsound (Windows-eigen)": "    Sonido:        winsound (integrado en Windows)",
        "  Ton:           afplay": "    Sonido:        afplay",
        "  Ton:           %s": "    Sonido:        %s",
        "  Kartenwurzeln: %s": "    Raíces de tarjetas: %s",
        "  Einstellungen: %s": "    Ajustes:       %s",
        "  ffmpeg:        %s": "    ffmpeg:        %s",
        "  Schrift:       %s": "    Fuente:        %s",
        "  Pythons auf diesem Rechner:": "    Pythons en esta máquina:",
        "    %-62s %s": "      %-62s %s",
        "  Geprüfte Orte:": "    Lugares comprobados:",
        "  Laufwerke mit JamManSoloXT:": "    Unidades con JamManSoloXT:",
        "    %-24s %3d Patches  %s": "      %-24s %3d patches  %s",
        "  gewählt:       %s": "    elegido:       %s",
        "  Patches:       %d": "    Patches:       %d",
        "  ACHTUNG: das Dateisystem liefert Lesefehler.": "    ATENCIÓN: el sistema de archivos informa errores de lectura.",
        "  Betroffene Einträge (erste drei): %s": "    Entradas afectadas (tres primeras): %s",
        "  Inhalt von %s:": "    Contenido de %s:",
        "    %-24r %s, %s": "      %-24r %s, %s",
        "    nicht lesbar: %s": "      ilegible: %s",
        "    %-10s %s": "      %-10s %s",
        "  erste Zeile im Fenster:": "    primera línea en la ventana:",
        "    %r": "      %r",
        "JamMan Solo XT": "  JamMan Solo XT",
        "WAV-Format": "Formato WAV",
        "%d Hz / %d Bit / %d K": "%d Hz / %d bit / %d can",
        "  %-28s  (gibt es nicht)": "  %-28s  (no existe)",
        "  %-28s  (nicht lesbar: %s)": "  %-28s  (ilegible: %s)",
        "  %-28s  (leer)": "  %-28s  (vacío)",
        "  %-28s  %s mit %s Patches": "  %-28s  %s con %s patches",
        "kein nutzbares Tk": "ningún Tk utilizable",
        "Sprache der Ausgabe (Vorgabe: aus dem System)": "Idioma de salida (por defecto: el del sistema)",
        "Laufwerk der Karte (Vorgabe: automatisch erkannt)": "Unidad de la tarjeta (por defecto: detectada automáticamente)",
        "Zielordner für 'sichern'": "Carpeta de destino para «copiar»",
        "Audiodatei für 'aufspielen'": "Archivo de audio para «cargar»",
        "Name des Patches (Vorgabe: Dateiname)": "Nombre del patch (por defecto: nombre del archivo)",
        "Patch-Nummer": "Número del patch",
        "Zielplatz für 'verschieben'": "Plaza de destino para «mover»",
        "Tauschpartner für 'tauschen'": "Compañero de intercambio para «intercambiar»",
        "Zielplatz für 'platzieren'": "Plaza de destino para «colocar»",
        "Sicherungsordner für 'zuruecknehmen'": "Carpeta de copia para «restaurar»",
        "Pegelprotokoll für 'pegel-umkehren'": "Protocolo de nivel para «deshacer el nivel»",
        "bei 'zuruecknehmen': alle Patches aus der Sicherung": "en «restaurar»: todos los patches de la copia",
        "Ziellautheit für 'alle-normieren' (Vorgabe -18 dB)": "Volumen objetivo para «normalizar todo» (por defecto -18 dB)",
        "Spitzengrenze für 'alle-normieren' (Vorgabe -1 dB)": "Límite de pico para «normalizar todo» (por defecto -1 dB)",
        "zu lange Datei auf 9:59 kürzen": "acortar un archivo demasiado largo a 9:59",
        "beim Teilen die Pause an der Naht stehen lassen": "al dividir, dejar la pausa en la unión",
        "beim Teilen gleichmäßig schneiden statt an Pausen": "al dividir, cortar por igual en vez de en las pausas",
        "Datei in N gleich lange Patches aufteilen": "dividir el archivo en N patches de igual duración",
        "zu lange Datei auf diese Länge kürzen": "acortar el archivo demasiado largo a esta duración",
        "auch Loops über 10 Minuten schreiben (Pedal nimmt sie nicht an)": "escribir también loops de más de 10 minutos (el pedal no los acepta)",
        "Fade am Ende der Datei einrechnen (z. B. 3)": "tener en cuenta el fundido al final del archivo (p. ej. 3)",
        "Lautstärke um DB ändern (z. B. -3 oder +2)": "cambiar el volumen en DB (p. ej. -3 o +2)",
        "Anhebung NICHT auf 0 dBFS begrenzen (kann übersteuern)": "NO limitar la subida a 0 dBFS (puede saturar)",
        "auf -1 dB Spitzenpegel normieren": "normalizar a -1 dB de pico",
        "Stopp-Verhalten des neuen Patches (ohne Angabe: wie Vorlage)": "comportamiento de parada del nuevo patch (sin indicación: como la plantilla)",
        "Loop oder Single (IsLoop in phrase.xml; ohne Angabe: wie Vorlage)": "loop o single (IsLoop en phrase.xml; sin indicación: como la plantilla)",
        "Rhythmus des neuen Patches (ohne Angabe: wie Vorlage)": "ritmo del nuevo patch (sin indicación: como la plantilla)",
        "beim Verschieben nur die Namen sichern (schnell, kein Kopieren)": "al mover, guardar solo los nombres (rápido, sin copiar)",
        "Ordner für die Sicherung vor Löschen/Verschieben": "carpeta para la copia antes de borrar/mover",
        "wirklich verändern (sonst Trockenlauf)": "cambiar de verdad (si no, simulación)",
        "was zu tun ist (Liste unten; englische Namen gehen auch)": "qué hay que hacer (lista abajo; los nombres en inglés también valen)",
        "Einstellungen: %s": "Ajustes: %s",
        "Keine Karte gefunden": "No se encontró ninguna tarjeta",
        "Neue Fassung verfügbar": "Nueva versión disponible",
        "Es gibt eine neuere Fassung: %s. Sie benutzen %s.": "Hay una versión más reciente: %s. Usted usa %s.",
        "Zur Download-Seite wechseln?": "¿Abrir la página de descarga?",
        "Es liegt keine JamMan-Karte im Rechner.": "No hay ninguna tarjeta JamMan en este ordenador.",
        "Nimm die microSD aus dem Pedal und stecke sie in einen Kartenleser oder Kartenslot. Über das USB-Kabel am Pedal erscheint sie nicht als Laufwerk.": "Saque la microSD del pedal e insértela en un lector de tarjetas o en una ranura. Con el cable USB puesto en el pedal no aparece como unidad.",
        "Ordner selbst wählen?": "¿Elegir la carpeta usted mismo?",
        "Ordner der Karte wählen": "Elegir la carpeta de la tarjeta",
        '   ← wird umgewandelt': '← se convertirá',
        '   ← abweichend': '← diferente',
        '   ← über 10 min, Pedal nimmt sie nicht an': '← más de 10 min, la pedalera no lo acepta',
        'keine WAV': 'sin WAV',
        '  Das Fenster wäre leer. Es geht im Textmenü weiter.': 'La ventana quedaría vacía. El menú de texto sigue funcionando.',
        '  Für ein Fenster: Python von python.org installieren (bringt Tk 8.6).': 'Para una ventana: instala Python desde python.org (incluye Tk 8.6).',
        '  Tk %s kann auf diesem macOS keinen Text im Fenster zeichnen.': 'Tk %s no puede dibujar texto en una ventana en este macOS.',
        '  Hinweis: Fade konnte nicht eingerechnet werden.': 'Nota: el fundido no se pudo aplicar.',
        '  Hinweis: Pegel ließ sich nicht ändern (kein 16-Bit-WAV?).': 'Nota: el nivel no se pudo cambiar (¿WAV de 16 bits?).',
        '  Achtung: der WAV-Kopf ist weiterhin ungewöhnlich.': 'Atención: la cabecera WAV sigue siendo inusual.',
        '  Hinweis: Die Vorlage hatte kein Feld IsLoop - es wurde ergänzt.': 'Nota: la plantilla no tenía el campo IsLoop - se añadió.',
        '  Hinweis: Die Vorlage hatte kein Feld StopMode - es wurde ergänzt.': 'Nota: la plantilla no tenía el campo StopMode - se añadió.',
        '  Hinweis: Die Vorlage hatte kein Feld RhythmType - es wurde ergänzt.': 'Nota: la plantilla no tenía el campo RhythmType - se añadió.',
        '  Angehalten.': 'Detenido.',
        '  Das ist kein Fehler des Werkzeugs - die Karte oder': 'No es un error del programa — la tarjeta o',
        '  ihr Einhängepunkt ist beschädigt. Erste Schritte:': 'su punto de montaje está dañado. Primeros pasos:',
        '    dmesg | tail -20            (Meldungen des Systems)': 'dmesg | tail -20            (mensajes del sistema)',
        '    sudo umount <laufwerk>      (aushängen)': 'sudo umount <dispositivo>   (desmontar)',
        '    sudo fsck.vfat -n <geraet>  (FAT32, nur prüfen)': 'sudo fsck.vfat -n <dispos>  (FAT32, solo comprobar)',
        '    sudo exfatfsck <geraet>     (exFAT, nur prüfen)': 'sudo exfatfsck <dispos>     (exFAT, solo comprobar)',
        '    oder die Karte im JamMan einschalten - das Pedal': 'o pon la tarjeta en el JamMan y enciéndelo —',
        '    repariert seine Karte beim Einschalten.': 'la pedalera repara su tarjeta al arrancar.',
        'Erst eine Karte wählen.': 'Elige primero una tarjeta.',
        "Speicherkarte des JamMan Solo XT": "Tarjeta de memoria del JamMan Solo XT",
        "keine Karte gefunden": "no se encontró tarjeta",
        "Ordner": "Carpeta", "Name": "Nombre", "Dauer": "Duración",
        "Stoppart": "Parada", "Wdg.": "Reproducción", "Rhythmus": "Ritmo",
        "Wiedergabe": "Reproducción",
        "Patch markieren — dann hier klicken zum Springen":
            "Elige un patch — luego haz clic aquí para ir",
        "kein Patch gewählt": "ningún patch elegido",
        "automatisch weiter": "continuar automáticamente",
        "beim Start sichern": "copiar al iniciar",
        "Platz": "Posición",
        "nächster": "siguiente",
        "vormerken": "preparar",
        "vorgemerkt %d": "preparado %d",
        "Einstellungen:": "Ajustes:",
        "wie Vorlage": "como plantilla",
        "Fade am Ende": "Fundido al final",
        "Karte wählen": "Elegir tarjeta",
        "Datei aufspielen": "Importar archivo",
        "Alles sichern": "Copiar todo",
        "Pegel zurückrechnen": "Deshacer nivel",
        "Neu laden": "Recargar",
        "Anhören": "Escuchar",
        "Stopp": "Parar",
        "Umbenennen": "Renombrar",
        "Platz ändern": "Cambiar posición",
        "Sicherung zurückholen": "Restaurar copia",
        "Einstellungen anwenden": "Aplicar ajustes",
        "▲ nach oben": "▲ subir",
        "▼ nach unten": "▼ bajar",
        "Tauschen": "Intercambiar",
        "Datei teilen": "Dividir archivo",
        "Alle normieren": "Normalizar todo",
        "Löschen": "Borrar",
        "Karte auswerfen": "Expulsar tarjeta",
        "Pegel": "Nivel",
        "auf −1 dB Spitze normieren": "normalizar a −1 dB pico",
        "Teile": "Partes",
        "an Pausen schneiden": "cortar en pausas",
        "Anhebung auf 0 dB begrenzen": "limitar subida a 0 dB",
        "Doppelklick spielt · Ziehen ordnet um · ⌘ für zwei Markierungen, dann Tauschen · Esc hält an.":
            "Doble clic reproduce · arrastrar reordena · ⌘ para dos selecciones, luego intercambiar · Esc detiene.",
        "vorgemerkt: Platz %d": "preparado: posición %d",
        "Ende der Liste erreicht": "fin de la lista",
        "Erst einen Patch in der Liste wählen.": "Elige primero un patch de la lista.",
        "Bitte eine Platznummer eintragen.": "Introduce un número de posición.",
        "Platz %d gibt es auf dieser Karte nicht.": "La posición %d no existe en esta tarjeta.",
        "MP3 und M4A brauchen ffmpeg -": "MP3 y M4A necesitan ffmpeg -",
        "Anleitung": "Instrucciones",
        "WAV geht auch ohne": "WAV funciona sin él",
        "Schließen": "Cerrar",
        "Text kopieren": "Copiar texto",
        "Karte nicht lesbar": "Tarjeta ilegible",
        "Fehler": "Error",
        "Hinweis": "Nota",
        "Anleitung in die Zwischenablage kopiert": "Instrucciones copiadas",
        "Sicherung beim Start: %s": "Copia al iniciar: %s",
    },
    "zh": {
        'Dem Python fehlen die Zertifikate. Zwei Wege: im Python-Ordner „Install Certificates.command“ ausführen — oder „pip install certifi“ — oder die Datei von der Release-Seite von Hand laden.':
         'Python 缺少证书。两个办法：在 Python 文件夹里运行 “Install Certificates.command”，或执行 “pip install certifi” —— 也可以从发布页手动下载文件。',
        'Achtung: das Zertifikat ließ sich nicht prüfen — die Datei wurde ohne Prüfung geladen.':
         '注意：无法验证证书 —— 文件是在未校验的情况下下载的。',
        'Die Datei ist unvollständig: %d statt %d Bytes.':
         '文件不完整：%d 字节，而不是 %d 字节。',
        'Neue Fassung %s wird geholt …':
         '正在获取 %s …',
        'Fassung %s ist installiert. Bitte das Fenster schließen und neu starten.':
         '已安装 %s。请关闭窗口并重新启动。',
        'Update beim Start':
         '启动时更新',
        'Neue Fassung %s wird geholt …':
         '正在获取 %s …',
        'Die Karte ist eingebunden, aber das Dateisystem liefert\nLesefehler:\n\n%s\n\nDas liegt an der Karte oder am Einhängepunkt, nicht am\nWerkzeug.\n\nErste Schritte:\n  - Karte im JamMan einschalten (das Pedal prüft sie)\n  - dmesg | tail -20\n  - aushängen und prüfen: sudo fsck.vfat -n <gerät>\n    bzw. sudo exfatfsck <gerät>':
         '卡片已挂载，但文件系统报告读取错误：\n\n%s\n\n问题在卡片或挂载点，不在工具。\n\n第一步：\n  - 把卡片插入 JamMan 并开机（踏板会检查卡片）\n  - dmesg | tail -20\n  - 卸载并检查：sudo fsck.vfat -n <设备>\n    或 sudo exfatfsck <设备>',
        'kein Pedal':
         '没有踏板',
        'Beschreibung …':
         '描述…',
        'Rückwärts: ohne ffmpeg keine Hörprobe — das Pedal spielt es trotzdem rückwärts.':
         '倒放：没有 ffmpeg 无法试听——踏板仍会倒放。',
        'Tempo':
         '速度',
        'Suchen …':
         '搜索…',
        'Text im Namen (leer lassen = alles zeigen):':
         '名称中的文字（留空 = 全部显示）：',
        'Erst eine Karte oder eine geholte Kopie öffnen — dann läßt sich suchen.':
         '请先打开卡片或已获取的副本，然后才能搜索。',
        'Suchen':
         '搜索',
        'Suche fehlgeschlagen: %s':
         '搜索失败：%s',
        'Suchen:':
         '搜索：',
        'Karte:':
         '卡片：',
        'Meldungen stehen im Fenster; Fehler zusätzlich in jamman-fehler.log.':
         '消息显示在窗口中；错误也会写入 jamman-fehler.log。',
        'Firmware-Dateien jetzt ins Pedal schreiben? Ein Abbruch mittendrin kann das Pedal unbrauchbar machen. [ja/nein] ':
         '现在把固件文件写入踏板？中途中断可能使踏板无法使用。[是/否] ',
        'Zielordner für die Firmware':
         '固件目标文件夹',
        'Ordner mit der geholten Firmware':
         '已获取固件的文件夹',
        'In diesem Ordner liegt keine Firmware (usr/JamManUI, usr/loopengine …).':
         '此文件夹中没有固件（usr/JamManUI、usr/loopengine…）。',
        'Firmware übernehmen':
         '应用固件',
        'Firmware übernehmen …':
         '应用固件…',
        'Firmware dieses Pedals holen …':
         '获取此踏板的固件…',
        '%s kam unvollständig (%d von %d Bytes).':
         '%s 不完整（%d/%d 字节）。',
        '%d Firmware-Dateien werden jetzt in das Pedal geschrieben. Vorher wird der jetzige Stand gesichert.\n\nEin Abbruch mittendrin kann das Pedal unbrauchbar machen — es gibt keinen Weg zurück.\n\nWirklich schreiben?':
         '现在将把 %d 个固件文件写入踏板。会先备份当前状态。\n\n中途中断可能使踏板无法使用——无法恢复。\n\n确定写入？',
        'Sichere den jetzigen Stand …':
         '正在备份当前状态…',
        'Gesichert: %d Dateien nach %s':
         '已备份：%d 个文件到 %s',
        'Firmware geholt: %d Dateien':
         '已获取固件：%d 个文件',
        'Firmware geholt:\n%s\n%d Dateien':
         '已获取固件：\n%s\n%d 个文件',
        'Fertig: %d Dateien geschrieben':
         '完成：已写入 %d 个文件',
        'Nicht bestätigt: %d':
         '未确认：%d',
        'Firmware geschrieben: %d Dateien\nSicherung: %s\nJetzt das Pedal aus- und wieder einschalten.':
         '固件已写入：%d 个文件\n备份：%s\n现在请关闭踏板再打开。',
        'Schreibe':
         '正在写入',
        'Hole':
         '正在获取',
        'JamList aus JamManager XT holen …':
         '从 JamManager XT 获取 JamList…',
        'Setlist als JamList ablegen …':
         '将 setlist 保存为 JamList…',
        'Setlist speichern':
         '保存 setlist',
        'Bibliothek von JamManager XT':
         'JamManager XT 库',
        'JamList wählen':
         '选择 JamList',
        'In dieser Bibliothek liegt keine JamList.':
         '此库中没有 JamList。',
        'Geholt: %s\n%d Plätze\n%s':
         '已获取：%s\n%d 个位置\n%s',
        'JamList ablegen':
         '保存 JamList',
        '%s%s%s als neue JamList eintragen? Die Bibliothek wird dabei verändert — vorher eine Kopie anlegen.':
         '将 %s%s%s 添加为新的 JamList？库将被修改——请先备份。',
        'JamList %s angelegt (id %d, %d Plätze)':
         '已创建 JamList %s（id %d，%d 个位置）',
        'Keine JamList mit dem Namen %s.':
         '没有名为 %s 的 JamList。',
        'belegte Plätze':
         '已用位置',
        'Geschrieben: %s (%d Plätze)':
         '已写入：%s（%d 个位置）',
        'Übernehmen':
         '应用',
        'Speicherabbild':
         '内存映像',
        'Speicherabbild speichern':
         '保存内存映像',
        'Speicherabbild aufnehmen …':
         '生成内存映像…',
        'Speicherabbilder vergleichen …':
         '比较内存映像…',
        'Speicherabbilder vergleichen':
         '比较内存映像',
        'Erstes Speicherabbild':
         '第一个内存映像',
        'Zweites Speicherabbild':
         '第二个内存映像',
        'Nur im ersten Bild: %d':
         '仅在第一个映像中：%d',
        'Nur im zweiten Bild: %d':
         '仅在第二个映像中：%d',
        'Gleicher Pfad, andere Größe: %d':
         '路径相同、大小不同：%d',
        'Die beiden Bilder sind gleich.':
         '两个映像相同。',
        'Erstes:':
         '第一个：',
        'Zweites:':
         '第二个：',
        'Aufruf:':
         '用法：',
        'Lese den Verzeichnisbaum …':
         '正在读取目录树…',
        'Speicherabbild: %d Einträge':
         '内存映像：%d 项',
        'Speicherabbild geschrieben:\n%s':
         '内存映像已保存：\n%s',
        'Speicherabbild geschrieben: %d Einträge nach %s':
         '内存映像已保存：%d 项到 %s',
        'Dateien für „speicherabbild“ und „vergleich“':
         '用于“speicherabbild”和“vergleich”的文件',
        'Suchen:':
         '搜索：',
        'Sortierung':
         '排序',
        '%d von %d Patches passen zu „%s“.':
         '%d/%d 个音色匹配“%s”。',
        'Beschreibung':
         '描述',
        'Interpret':
         '艺人',
        'Genre':
         '风格',
        'Stichwörter':
         '标签',
        'Bewertung':
         '评级',
        'Tempo (BPM)':
         '速度（BPM）',
        'Die Datei %s fehlt.':
         '文件 %s 不存在。',
        'Setlists anpassen':
         '调整 setlist',
        '%d Setlist-Dateien in %s an die neuen Plätze anpassen? Jede Datei wird vorher daneben gesichert.':
         '将 %s 中的 %d 个 setlist 文件调整为新的位置？每个文件都会先备份在旁边。',
        'Keine Setlist nannte einen bewegten Platz.':
         '没有 setlist 提到被移动的位置。',
        'Setlists nicht angepaßt: ':
         'setlist 未调整：',
        'Setlists angepaßt: ':
         'setlist 已调整：',
        'Plätze aufrücken':
         '填补空缺',
        'Plätze aufrücken …':
         '填补空缺…',
        'Es gibt keine Lücken — die Plätze sind schon lückenlos belegt.':
         '没有空缺——位置已经是连续的。',
        '%d Patches rücken in %s nach vorn: %s%s\n\nJeder bewegte Platz wird vorher auf den Rechner gesichert. Setlists im Pedal, die auf Platznummern zeigen, stimmen danach nicht mehr. Fortfahren?':
         '%d 个音色在%s中前移：%s%s\n\n每个移动的位置都会先备份到电脑。踏板中按位置编号的 setlist 将不再匹配。继续吗？',
        'Rücke auf':
         '正在前移',
        'Fertig: %d Plätze aufgerückt, %d vorher gesichert':
         '完成：前移 %d 个位置，先备份 %d 个',
        'Aufgerückt: %d Plätze':
         '已前移：%d 个位置',
        'Kopie auf dem Rechner — Änderungen hier wirken nicht auf das Pedal.':
         '电脑上的副本——此处的更改不会影响踏板。',
        'Patch%d wird aus der Kopie auf dem Rechner entfernt. Das Pedal bleibt unverändert. Fortfahren?':
         '将从电脑上的副本中删除 Patch%d。踏板保持不变。继续吗？',
        'Patch%d wird von der Karte entfernt. Vorher wird er auf dem Rechner gesichert. Fortfahren?':
         '将从卡片中删除 Patch%d。会先备份到电脑。继续吗？',
        'Ja':
         '是',
        'Nein':
         '否',
        'Dem Python fehlen die Zertifikate. Auf macOS hilft „Install Certificates.command“ im Python-Ordner, oder: pip install certifi':
         'Python 缺少证书。在 macOS 上请运行 Python 文件夹中的“Install Certificates.command”，或：pip install certifi',
        'GitHub ist nicht erreichbar: %s':
         '无法访问 GitHub：%s',
        'unbekannter Grund':
         '原因未知',
        'Gelöscht: %s':
         '已删除：%s',
        'Fehler:':
         '错误：',
        'Platz löschen':
         '删除位置',
        'Welcher Platz soll gelöscht werden? (z. B. 30 oder Patch30)':
         '要删除哪个位置？（例如 30 或 Patch30）',
        'Platz löschen …':
         '删除位置…',
        '%s ist nicht belegt — nichts zu löschen.':
         '%s 未被占用——无可删除。',
        '%s wird aus %s gelöscht. Das läßt sich nicht rückgängig machen. Fortfahren?':
         '将从%s删除%s。此操作无法撤销。继续吗？',
        'Lösche':
         '正在删除',
        'Gelöscht:':
         '已删除：',
        'Fehler beim Löschen:':
         '删除时出错：',
        'Die Datei ist %s lang — das Pedal nimmt höchstens 10 Minuten je Loop an. Bitte im Hauptfenster kürzen oder teilen.':
         '文件长度为 %s——踏板每个循环最多接受 10 分钟。请在主窗口中缩短或分割。',
        'Dieser Ordner ist nicht lesbar: %s':
         '无法读取此文件夹：%s',
        'Auswahl:':
         '所选：',
        'Pegelprotokoll wählen':
         '选择电平日志',
        'Audiodatei zum Teilen auswählen':
         '选择要分割的音频文件',
        'Dateiname:':
         '文件名：',
        'Speichern':
         '保存',
        'Bitte einen Dateinamen angeben.':
         '请输入文件名。',
        'Überschreiben':
         '覆盖',
        '%s gibt es schon. Ersetzen?':
         '%s 已存在。要替换吗？',
        'Speicherkarte des JamMan wählen':
         '选择 JamMan 卡片',
        'Zielordner für die Sicherung':
         '备份的目标文件夹',
        'Dateien':
         '文件',
        'Nach oben':
         '上一级',
        'Öffnen':
         '打开',
        'Bitte eine Datei auswählen.':
         '请选择一个文件。',
        'Alle Dateien':
         '所有文件',
        'Die Datei konnte nicht geschrieben werden: %s':
         '无法写入文件：%s',
        'ohne':
         '无',
        'ende':
         '结尾',
        'klick':
         '节拍',
        '%d Patches ohne Ton — nur die Beschreibung ist geholt.':
         '%d 个音色没有音频——只获取了描述。',
        'Ganzen Ordner aufs Pedal …':
         '整个文件夹写到踏板…',
        'Ordner mit Patch-Ordnern wählen':
         '选择包含音色文件夹的目录',
        'Ganzen Ordner aufs Pedal':
         '整个文件夹写到踏板',
        '%d Patch-Ordner werden in %s geschrieben. Belegte Plätze werden vorher gesichert, unveränderte übersprungen. Fortfahren?':
         '将把 %d 个音色文件夹写入%s。已占用的位置会先备份，未更改的会跳过。继续吗？',
        'internen Speicher':
         '内部存储',
        'der Karte':
         '卡片',
        'Im Ordner liegen keine Patch-Ordner (erwartet: Patch01 … Patch200).':
         '该目录中没有音色文件夹（应为 Patch01 … Patch200）。',
        'Schreibe':
         '正在写入',
        'Fertig: %d Patches geschrieben, %d übersprungen, %d vorher gesichert':
         '完成：写入 %d 个音色，跳过 %d 个，先备份 %d 个',
        'Fehler beim Schreiben:':
         '写入时出错：',
        'Übertragen: %d Dateien · unverändert übersprungen: %d':
         '已传输 %d 个文件 · 跳过未变更的 %d 个',
        'Zielplatz auf dem Pedal (z. B. 17 oder Patch17):':
         '踏板上的目标位置（例如 17 或 Patch17）：',
        'Unerwarteter Fehler':
         '意外错误',
        'Das Werkzeug wurde unerwartet beendet.':
         '工具意外停止。',
        'Protokoll:':
         '日志：',
        'Auswahl kopieren':
         '复制所选',
        'Nichts markiert — bitte zuerst Text markieren.':
         '未选中任何内容——请先选中文本。',
        'In die Zwischenablage kopiert.':
         '已复制到剪贴板。',
        'Anleitung speichern':
         '保存说明',
        'Gespeichert.':
         '已保存。',
        'Alles kopieren':
         '全部复制',
        'Als Datei speichern':
         '另存为文件',
        'Anleitung':
         '说明',
        'Zum Umwandeln fehlt ffmpeg.':
         '转换需要 ffmpeg，但未安装。',
        'Die Programmdateien lassen sich hier nicht ersetzen: %s':
         '无法在此替换程序文件：%s',
        'Unerwarteter Fehler: %s':
         '意外错误：%s',
        'Aktualisieren':
         '更新',
        'Die neueste Fassung wird von GitHub geladen und ersetzt die Programmdateien. Ihre Einstellungen und Ihre Karte bleiben unberührt. Fortfahren?':
         '将从 GitHub 下载最新版本并替换程序文件。您的设置和卡片不受影响。继续吗？',
        'Jetzt aktualisieren?':
         '现在更新？',
        'Aktualisiert':
         '已更新',
        'Nicht aktualisiert':
         '未更新',
        'Fenster jetzt schließen?':
         '现在关闭窗口？',
        'GitHub ist nicht erreichbar.':
         '无法访问 GitHub。',
        'Sie benutzen bereits die neueste Fassung (%s).':
         '您已在使用最新版本（%s）。',
        'Zur Fassung %s gibt es keine Datei zum Herunterladen.':
         '版本 %s 没有可下载的文件。',
        'Lade Fassung %s …':
         '正在下载版本 %s …',
        'Herunterladen fehlgeschlagen: %s':
         '下载失败：%s',
        'Die heruntergeladene Datei ist kein gültiges ZIP.':
         '下载的文件不是有效的 ZIP。',
        'Die Datei ist beschädigt (%s).':
         '文件已损坏（%s）。',
        'In der Datei fehlt jamman.py.':
         '文件中缺少 jamman.py。',
        'Die Datei gehört nicht zur Fassung %s.':
         '该文件不属于版本 %s。',
        'Fassung %s ist installiert (%d Dateien). Bitte das Fenster schließen und neu starten.':
         '版本 %s 已安装（%d 个文件）。请关闭窗口并重新启动。',
        'Die Karte hängt nicht im Rechner — nichts auszuwerfen. Steckt sie im Pedal, führt das Pedal sie nicht als Laufwerk.':
         '卡片不在电脑中——没有可弹出的内容。若卡片在踏板里，踏板不会把它当作驱动器。',
        'Auswerfen ging nicht. Bitte im Dateimanager auf „Auswerfen" klicken.':
         '弹出失败。请在文件管理器中使用“弹出”。',
        'Keine Karte im Rechner gefunden.':
         '在电脑中未找到卡片。',
        'Karte im Rechner:  „Karte wählen“ drücken.':
         '卡片在电脑里：按“选择卡片”。',
        'Karte im Pedal:    oben rechts „Pedal über USB“ — dort sind Karte und interner Speicher erreichbar.':
         '卡片在踏板里：右上角“通过 USB 连接踏板”——在那里可以访问卡片和内部存储。',
        'Speicherkarte': '存储卡',
        'Kopie vom Pedal': '来自踏板的副本',
        'Speicherkarte des JamMan Stereo': 'JamMan Stereo 的存储卡',
        'Im Pedal steckt keine Karte.': '踏板中没有卡片。',
        'Karte einlegen und dann „Karte wählen“ oder „Neu laden“ drücken.':
         '请插入卡片，然后按“选择卡片”或“重新载入”。',
        'keine Karte':
         '无卡片',
        'Dies ist eine Kopie vom Pedal, nicht die Karte selbst. Über „Karte wählen“ die Karte öffnen.':
         '这是从踏板取回的副本，不是卡片本身。请用“选择卡片”打开卡片。',
        'JamMan Card Tool': 'JamMan Card Tool',
        'Karte und interner Speicher': '卡与内部存储',
        '(für Rückgängig nötig)':
         '（撤销需要）',
        'Darunter ist die Sicherung für Rückgängig. Danach läßt sich der letzte Schreibvorgang nicht mehr zurücknehmen. Trotzdem löschen?':
         '其中包括撤销所需的备份。之后将无法撤销上一次写入。仍要删除吗？',
        'Sicherungen aufräumen …':
         '清理备份……',
        'Sicherungen aufräumen':
         '清理备份',
        'Sicherungen im Zielordner':
         '目标文件夹中的备份',
        '%d Sicherungen · %.1f MB':
         '%d 个备份 · %.1f MB',
        'Neueste behalten:':
         '保留最新：',
        'Markierte löschen':
         '删除所选',
        'Erst Sicherungen in der Liste markieren.':
         '请先在列表中标记备份。',
        '%d Sicherungen mit %.1f MB werden endgültig gelöscht. Fortsetzen?':
         '将永久删除 %d 个备份（%.1f MB）。继续？',
        '%d Sicherungen konnten nicht gelöscht werden.':
         '有 %d 个备份无法删除。',
        'Der Zielordner ist noch nicht angelegt.':
         '目标文件夹尚未创建。',
        'Im Zielordner liegen keine Sicherungen.':
         '目标文件夹中没有备份。',
        'Alle auswählen':
         '全选',
        'Nichts markiert. Alle %d Patches in die Setlist?':
         '未选择任何项目。把所有 %d 个 Patch 放入 Setlist？',
        'Warte auf das Ende des laufenden Vorgangs …':
         '正在等待当前操作结束……',
        'Der Vorgang reagiert nicht — Fenster schließen und neu öffnen.':
         '操作无响应——请关闭窗口后重新打开。',
        'Hole Beschreibungen': '正在读取描述',
        'Es wurde kein Zielplatz eingegeben.':
         '未输入目标位置。',
        'Der Zielplatz muß wie Patch01 … Patch200 heißen, nicht „%s“.':
         '目标位置必须命名为 Patch01 … Patch200，而不是“%s”。',
        'OK': '确定',
        'Ein Vorgang läuft noch — bitte abwarten.':
         '仍有操作在进行——请稍候。',
        'Verbindung neu aufbauen':
         '重新连接',
        'Verbindung wird neu aufgebaut …':
         '正在重新连接……',
        'Lesen und Schreiben über USB. Vor jedem Überschreiben wird der alte Platz auf den Rechner gesichert.':
         '通过 USB 读写。覆盖前会先把原位置保存到电脑。',
        'Über USB fließen etwa 1,3 MB/s. Für eine ganze Karte ist ein Kartenleser viel schneller. Schreiben verändert die Karte — vorher wird gesichert.':
         '通过 USB 的速率约为 1,3 MB/s。整张卡用读卡器快得多。写入会修改卡片——之前会先备份。',
        'Audio in den Speicher schreiben …': '把音频写入存储器……',
        'Beim JamMan Solo XT behält der FS3X seine Grundfunktionen. Eine eigene Belegung speichert das Pedal zwar, wertet sie aber nicht aus — deshalb wird sie hier nicht angeboten.':
         '在 JamMan Solo XT 上，FS3X 保留其基本功能。踏板虽会保存自定义分配，但不会执行，因此这里不提供。',
        'Änderungen wirken nach einem Neustart des Pedals.': '更改在踏板重启后生效。',
        'gewünscht': '请求值',
        'Pedal meldet': '踏板返回',
        '(nicht belegt)':
         '（未分配）',
        'UndoRedo = Rückgängig/Wiederholen · Reverse = Rückwärts · Tempo = Tempo antippen':
         'UndoRedo = 撤销/重做 · Reverse = 倒放 · Tempo = 打拍子',
        'Sichern': '保存',
        'Schreiben': '写入',
        'Setlists': 'Setlist',
        'Pedal': '踏板',
        'Ins Hauptfenster holen …': '取到主窗口……',
        'Einzelnen Patch sichern …': '保存单个 Patch……',
        'Patch-Ordner aufs Pedal …': 'Patch 文件夹写入踏板……',
        'Übernommen.': '已应用。',
        'Pedal spielt jetzt:': '踏板现在播放：',
        'Audio aufs Pedal …':
         '音频写入踏板……',
        'Audio aufs Pedal':
         '音频写入踏板',
        'Audiodatei wählen':
         '选择音频文件',
        'Audio':
         '音频',
        'Sichere den alten Platz …':
         '正在备份原位置……',
        'Wandle die Datei …':
         '正在转换文件……',
        'Umwandeln fehlgeschlagen — die Datei muß 44,1 kHz, 16 Bit, Stereo werden.':
         '转换失败——文件必须是 44,1 kHz、16 位、立体声。',
        'Leere den Platz …': '正在清空该位置……',
        'Pedal spielt jetzt Bank/Patch:': '踏板现在播放 Bank/Patch：',
        'Karte erkannt:': '检测到卡：',
        'Als ZIP sichern …':
         '保存为 ZIP……',
        'Als ZIP sichern':
         '保存为 ZIP',
        'Sichere als ZIP …':
         '正在保存为 ZIP……',
        'Sichere:':
         '正在保存：',
        'Fehler beim Sichern:':
         '保存时出错：',
        'Die Setlist ist leer.':
         'Setlist 为空。',
        'Erst Patches in der Liste markieren (⌘-Klick).':
         '请先在列表中标记 Patch（⌘ 单击）。',
        'Es gibt nichts rückgängig zu machen.':
         '没有可撤销的操作。',
        'Fußschalter …':
         '脚踏开关……',
        'Fußschalter':
         '脚踏开关',
        'Tastenbelegung des Pedals':
         '踏板的脚踏开关分配',
        'Werte sind Geräteeinstellungen — nur ändern, wenn du weißt, was sie bedeuten.':
         '这是设备设置——只在你知道含义时才修改。',
        'Übernehmen':
         '应用',
        'JamMan':
         'JamMan',
        'Karte':
         '卡',
        'intern':
         '内部',
        'Liste als Textdatei sichern':
         '将列表保存为文本文件',
        'Liste als Textdatei …':
         '列表保存为文本文件……',
        'Mitlaufen':
         '跟随',
        'Mitlaufen an — am Pedal einen Patch wählen.':
         '跟随已开启——在踏板上选择一个 Patch。',
        'Mitlaufen aus.':
         '跟随已关闭。',
        'Rückgängig':
         '撤销',
        'Setlist holen …':
         '获取 Setlist……',
        'Setlist laden':
         '加载 Setlist',
        'Setlist sichern …':
         '保存 Setlist……',
        'Setlist sichern':
         '保存 Setlist',
        'Setlist.txt':
         'Setlist.txt',
        'Stelle wieder her:':
         '正在恢复：',
        '%s wird durch die Sicherung ersetzt. Fortsetzen?':
         '%s 将被备份替换。继续？',
        'Ordner des Patches wählen':
         '选择 Patch 文件夹',
        'Aufs Pedal schreiben':
         '写入踏板',
        '%s ist schon belegt. Der Patch wird vorher auf den Rechner gesichert und dann überschrieben. Fortsetzen?':
         '%s 已被占用。该 Patch 会先保存到电脑，然后被覆盖。继续？',
        'Schreibe aufs Pedal …':
         '正在写入踏板……',
        'Schreibe:':
         '正在写入：',
        'Fehler beim Schreiben:':
         '写入错误：',
        'Patch aufs Pedal schreiben …':
         '把 Patch 写入踏板……',
        '(Vorschlag)': '（建议）',
        'Ziel:': '目标：',
        'noch nicht gewählt': '尚未选择',
        'Ziel ändern …': '更改目标……',
        'Aufräumen':
         '清理',
        'Die angefangene Kopie entfernen? Nur die gerade geholten Dateien werden gelöscht, der Ordner bleibt.':
         '删除未完成的副本？只删除刚刚取来的文件，文件夹保留。',
        'Aufgeräumt:':
         '已清理：',
        'Hole Beschreibungen …': '正在读取描述……',
        'Wird abgebrochen …': '正在取消……',
        'Über USB fließen etwa 1,3 MB/s. Für eine ganze Karte ist ein Kartenleser viel schneller — steckt die Karte im Pedal, bleibt nur dieser Weg.':
         '通过 USB 的速率约为 1,3 MB/s。整张卡用读卡器快得多——如果卡在踏板里，只能走这条路。',
        'Abbrechen': '取消',
        'Abgebrochen.': '已取消。',
        'Fehler beim Öffnen:': '打开时出错：',
        'Alle ins Hauptfenster holen …': '全部取到主窗口……',
        'Zielordner wählen': '选择目标文件夹',
        'Hole Patch': '正在读取 Patch',
        'Fertig:': '完成：',
        'Im Hauptfenster öffnen?': '在主窗口中打开？',
        'Gesichert': '已保存',
        'Pedal über USB': '通过 USB 连接踏板',
        'Das USB-Modul fehlt: greenglass.py muß neben jamman.py liegen.': '缺少 USB 模块：greenglass.py 必须与 jamman.py 放在同一目录。',
        'Gesichert nach:': '已保存到：',
        'Interner Speicher': '内部存储',
        'SD-Karte': 'SD 卡',
        'Kein JamMan gefunden — Pedal einschalten und Kabel prüfen.': '未找到 JamMan——请打开踏板并检查数据线。',
        '(ohne Namen)': '（无名称）',
        'Fehler beim Lesen:': '读取错误：',
        'Erst „Neu laden“ drücken.': '请先按「重新加载」。',
        'Lädt …': '正在加载……',
        'Nur lesen: das Pedal wird nicht verändert.': '只读：不会修改踏板。',
        'Ordner zum Sichern wählen': '选择保存文件夹',
        'Patch sichern …': '保存 Patch……',
        'Verbinde mit dem Pedal …': '正在连接踏板……',
        'Spenden': '赞助',
        'Für dieses Werkzeug ist noch kein Spendenlink eingetragen.': '本工具尚未填写赞助链接。',
        'Er steht in jamman.py in der Zeile SPENDEN_LINK.': '请在 jamman.py 的 SPENDEN_LINK 一行填写。',
        '      Schnitt %d bei %s: %s': '      切分点 %d 位于 %s：%s',
        'keine Pause in der Nähe - rechnerisch geschnitten': '附近没有停顿 — 按计算位置切分',
        "Tk:            %s": "Tk:            %s",
        'Sicherung': '备份',
        'Aufspielen': '导入',
        'Der Name darf nicht leer sein.': '名称不能为空。',
        'Bitte 2 bis 60 Teile angeben.': '请输入 2 到 60 段。',
        'Die Datei ist nicht lesbar.': '该文件无法读取。',
        'Alle Patches normieren': '全部音色标准化',
        'Alle Patches werden auf gleiche Lautheit gebracht:\n\n  Ziel -18 dB Lautheit, Spitze höchstens -1 dB\n\nVorher wird die ganze Karte gesichert - das dauert bei\n900 MB ein bis zwei Minuten. Fortfahren?': '所有音色将统一响度：\n\n  目标 -18 dB 响度，峰值最高 -1 dB\n\n开始前会先备份整张卡 - 900 MB 大约需要\n一到两分钟。继续吗？',
        'Zum Tauschen genau ZWEI Patches markieren:\nersten anklicken, zweiten mit gedrückter Cmd-Taste (⌘).': '交换需要正好选中两个音色：\n先点第一个，按住 Cmd (⌘) 再点第二个。',
        'Oben steht überall „wie Vorlage - dann gibt es nichts zu ändern.': '上面全部是「同模板」- 没有可应用的设置。',
        'Erst einen Patch wählen.': '请先选择一个音色。',
        'Karte auswerfen? Danach kann sie abgezogen werden.': '弹出存储卡？之后即可拔出。',
        'Keine JamMan-Karte': '不是 JamMan 存储卡',
        'Datei zu lang': '文件太长',
        'Beim Pegel bitte eine Zahl in dB eintragen (z. B. -3), oder das Feld leer lassen.': '请输入音量（dB，例如 -3），或留空。',
        'Bei Teile bitte eine ganze Zahl eintragen.': '请输入整数段数。',
        'Beim Pegel bitte eine Zahl in dB eintragen (z. B. -3).': '请输入音量（dB，例如 -3）。',
        'Bei Fade am Ende bitte eine Zahl in Sekunden eintragen.': '请输入结尾淡出的秒数。',
        'Pegel zurückgerechnet': '音量已撤销',
        'alle Patches normiert': '全部音色已标准化',
        'Fertig.': '完成。',
        'Tauschen:': '交换：',
        'Karte auswerfen, in den JamMan stecken — der Patch ist weg.': '弹出存储卡并插入 JamMan — 该音色已消失。',
        'Neue Reihenfolge:': '新的顺序：',
        'JamMan Solo XT — Speicherkarte verwalten': 'JamMan Solo XT — 管理存储卡',
        'Pythons auf diesem Rechner:': '本机上的 Python：',
        'Geprüfte Orte:': '已检查的位置：',
        'Laufwerke mit JamManSoloXT:': '含 JamManSoloXT 的驱动器：',
        'Trockenlauf — nichts geändert. Mit --schreiben ausführen.': '试运行 — 未做任何修改。请加 --schreiben 执行。',
        'Trockenlauf — nichts kopiert. Mit --schreiben ausführen.': '试运行 — 未复制任何内容。请加 --schreiben 执行。',
        'Trockenlauf — nichts angelegt. Mit --schreiben ausführen.': '试运行 — 未创建任何内容。请加 --schreiben 执行。',
        'Trockenlauf — nichts geschrieben. Mit --schreiben ausführen.': '试运行 — 未写入任何内容。请加 --schreiben 执行。',
        'Trockenlauf — nichts zurückgeholt. Mit --schreiben ausführen.': '试运行 — 未恢复任何内容。请加 --schreiben 执行。',
        'Trockenlauf — nichts umbenannt. Mit --schreiben ausführen.': '试运行 — 未重命名任何内容。请加 --schreiben 执行。',
        'Trockenlauf — nichts getauscht. Mit --schreiben ausführen.': '试运行 — 未交换任何内容。请加 --schreiben 执行。',
        'Trockenlauf — nichts gelöscht. Mit --schreiben ausführen.': '试运行 — 未删除任何内容。请加 --schreiben 执行。',
        'Keine Patches auf der Karte.': '卡上没有音色。',
        'Suche musikalisch brauchbare Schnittstellen …': '正在寻找合适的切分点 …',
        'Kopf:         WAV-Kopf auf einfaches PCM umgeschrieben': '文件头：      WAV 头已改写为简单 PCM',
        'Alle Ordner heißen schon so. Nichts zu tun.': '所有文件夹已经是这种写法。无需操作。',
        'Hinweis: diese gibt es auf der Karte nicht und werden neu angelegt:': '提示：这些不在卡上，将被新建：',
        'Nichts zu tun.': '无需操作。',
        '1  Patches auflisten': '1  列出音色',
        '2  Patch anhören': '2  试听音色',
        '3  Datei aufspielen': '3  导入文件',
        '4  Patch ändern (Stopp, Wiedergabe, Name)': '4  修改音色（停止、播放、名称）',
        '5  Patch verschieben': '5  移动音色',
        '6  Patch löschen': '6  删除音色',
        '7  Alles sichern': '7  全部备份',
        '8  Karte auswerfen': '8  弹出存储卡',
        '0  Beenden': '0  退出',
        'Angehalten.': '已停止。',
        'erste Zeile im Fenster:': '窗口中第一行：',
        'ACHTUNG: das Dateisystem liefert Lesefehler.': '注意：文件系统报读取错误。',
        'Das ist kein Fehler des Werkzeugs - die Karte oder': '这不是程序错误 — 存储卡或',
        'ihr Einhängepunkt ist beschädigt. Erste Schritte:': '它的挂载点已损坏。可以这样做：',
        'Ton:           afplay': '声音：        afplay',
        'Ton:           winsound (Windows-eigen)': '声音：        winsound（Windows 内置）',
        'Hinweis: Pegelprotokoll konnte nicht geschrieben werden.': '提示：音量记录无法写入。',
        'Sicherung beim Start: Karte unverändert, nichts zu tun.': '启动时备份：卡未变化，无需操作。',
        'Pause an der Naht wird herausgenommen': '接缝处的停顿将被去除',
        'Quelle:       %s': '来源：        %s',
        'Quelle: %s': '来源：%s',
        'Ziel:   %s': '目标：  %s',
        'Ziel: %.1f dB Lautheit (RMS), Spitze höchstens %.1f dB': '目标：%.1f dB 响度（RMS），峰值最高 %.1f dB',
        'Karte: %s': '存储卡：%s',
        'Name:         %s': '名称：        %s',
        'Dauer:        %s': '时长：        %s',
        'Dauer:  %s': '时长：%s',
        'Datei:  %s': '文件：  %s',
        'Format:       %d Hz / %d Bit / %d Kanal/Kanäle%s': '格式：        %d Hz / %d 位 / %d 声道%s',
        'Fassung:       %s': '版本：        %s',
        'Python:        %s': 'Python：      %s',
        'System:        %s %s': '系统：        %s %s',
        'Tk-Modul:      %s': 'Tk 模块：     %s',
        'Schrift:       %s': '字体：        %s',
        'Kartenwurzeln: %s': '卡搜索位置：%s',
        'ffmpeg:        %s': 'ffmpeg：      %s',
        'gewählt:       %s': '已选择：      %s',
        'Ton:           %s': '声音：        %s',
        'Patches:       %d': '音色数：      %d',
        'Patches: %d': '音色数：%d',
        'Patches auf der Karte:    %d': '卡上的音色：     %d',
        'Patches in der Sicherung: %d': '备份中的音色：   %d',
        'Inhalt von %s:': '%s 的内容：',
        'Gesichert: %d Patches, %.0f MB.': '已备份：%d 个音色，%.0f MB。',
        'gesichert: %d Patches, %.0f MB': '已备份：%d 个音色，%.0f MB',
        'Fertig. Sicherung: %s': '完成。备份：%s',
        'Fertig: %s angelegt.': '完成：已创建 %s。',
        'Fertig: %d Ordner umbenannt, %d Fehler, %d Patches lesbar.': '完成：重命名 %d 个文件夹，%d 个错误，%d 个音色可读。',
        'Fertig: %d Patches normiert, %d davon in der Spitze gedeckelt.': '完成：%d 个音色已标准化，其中 %d 个限制了峰值。',
        'Gelöscht. Sicherung: %s': '已删除。备份：%s',
        'Getauscht. Sicherung: %s': '已交换。备份：%s',
        'Geändert. Sicherung: %s': '已修改。备份：%s',
        'Verschoben. Sicherung: %s': '已移动。备份：%s',
        'Zurückgeholt. Der Zustand davor liegt in: %s': '已恢复。之前的状态位于：%s',
        'Zurückgerechnet: %d Patches. Sicherung davor: %s': '已撤销：%d 个音色。之前的备份：%s',
        'Vorher wird er gesichert nach: %s': '此前会先备份到：%s',
        'Wird gelöscht: %s  (%s)': '将被删除：%s  (%s)',
        'Neuer Patch:  Patch%s  (Name: %s)': '新音色：Patch%s  （名称：%s）',
        'Patch:        %s  (%s)': '音色：        %s  (%s)',
        'Patch:      %s  (%s)': '音色：      %s  (%s)',
        'Vorlage:      %s': '模板：        %s',
        'Teile:  %d × etwa %s': '段数：  %d × 约 %s',
        'Stopp:        %s': '停止方式：    %s',
        'Stopp:        %s (%s)': '停止方式：    %s (%s)',
        'Rhythmus:     %s': '节奏：        %s',
        'Rhythmus:     %s (%s)': '节奏：        %s (%s)',
        'Wiedergabe:   %s': '播放：        %s',
        'Wiedergabe:   %s (IsLoop %s)': '播放：        %s (IsLoop %s)',
        'Pegel:        %+.1f dB': '音量：        %+.1f dB',
        'Pegel:        %.1f dB → %.1f dB Spitze': '音量：        %.1f dB → %.1f dB 峰值',
        'Pegel:        auf -1 dB Spitze normieren': '音量：        标准化到 -1 dB 峰值',
        'Fade:         letzte %.1f s werden ausgeblendet': '淡出：        最后 %.1f 秒淡出',
        'Fade:         letzte %.1f s der Aufnahme ausblenden': '淡出：        淡出最后 %.1f 秒',
        'Kürzen:       auf %s geschnitten (harter Schnitt, 12 ms ausgeblendet)': '截短：        切到 %s（硬切，12 毫秒淡出）',
        'Protokoll: %s': '记录：%s',
        'Sicherung: %s': '备份：%s',
        'Sicherung:  %s': '备份：  %s',
        'Schreibweise des Geräts: Patch%0*d': '设备的写法：Patch%0*d',
        'Zu ändern: %d': '需修改：%d',
        'Zuordnung liegt in: %s': '对应关系位于：%s',
        'Betroffene Einträge (erste drei): %s': '受影响的条目（前三个）：%s',
        'Keine Karte gefunden unter %s.': '在 %s 下未找到存储卡。',
        "BEFEHL": "命令",
        "  Auswahl: ": "  选择：",
        "  Anderer Pfad (leer = beenden): ": "  其他路径（留空 = 退出）：",
        "  Patch-Nummer: ": "  音色编号：",
        "  Name (leer = Dateiname): ": "  名称（留空 = 文件名）：",
        "  Stopp (fade/instant, leer = wie Vorlage): ": "  停止方式（fade/instant，留空 = 同模板）：",
        "  Wiedergabe (loop/single, leer = wie Vorlage): ": "  播放方式（loop/single，留空 = 同模板）：",
        "  Pfad zur Audiodatei: ": "  音频文件路径：",
        "  Stopp (fade/instant, leer = lassen): ": "  停止方式（fade/instant，留空 = 保持）：",
        "  Wiedergabe (loop/single, leer = lassen): ": "  播放方式（loop/single，留空 = 保持）：",
        "  Neuer Name (leer = lassen): ": "  新名称（留空 = 保持）：",
        "  Wirklich schreiben? (j/n): ": "  真的要写入吗？（y/n）：",
        "  Welcher Patch: ": "  哪个音色：",
        "  An welche Stelle: ": "  放到哪个位置：",
        "  Wirklich verschieben? (j/n): ": "  真的要移动吗？（y/n）：",
        "  Zielordner (leer = ~/JamMan-Sicherung): ": "  目标文件夹（留空 = ~/JamMan-Sicherung）：",
        "  Wirklich löschen? (j/n): ": "  真的要删除吗？（y/n）：",
        "Kein Abspielprogramm gefunden (aplay, paplay, mpv oder ffplay).": "未找到播放程序（aplay、paplay、mpv 或 ffplay）。",
        "Fehler: im Protokoll stehen keine Einträge.": "错误：记录中没有条目。",
        "Fehler: Datei ließ sich nicht umwandeln.": "错误：文件无法转换。",
        "Fehler: kein vollständiger Patch als Vorlage gefunden.": "错误：没有找到可用作模板的完整音色。",
        "Fehler: nichts angegeben (--stopp, --rhythmus, --wiedergabe, --name oder --fade).": "错误：没有给出任何参数（--stopp、--rhythmus、--wiedergabe、--name 或 --fade）。",
        "Fehler: '%s' ist keine lesbare Audiodatei.": "错误：'%s' 不是可读的音频文件。",
        "Fehler: --teile N oder --laenge SEKUNDEN angeben.": "错误：请给出 --teile N 或 --laenge 秒数。",
        "Fehler: %.1f Minuten pro Teil sind zu lang (Grenze %d Minuten).": "错误：每段 %.1f 分钟太长（上限 %d 分钟）。",
        "Fehler: '%s' gibt es nicht.": "错误：'%s' 不存在。",
        "Fehler: '%s' ist keine lesbare Audiodatei.%s": "错误：'%s' 不是可读的音频文件。%s",
        "Fehler: '%s' liegt schon vor.": "错误：'%s' 已存在。",
        "Fehler: Die Datei ließ sich nicht kürzen.": "错误：文件无法截短。",
        "Fehler: Die Umwandlung hat keine hörbare Aufnahme\n  ergeben (nur Stille). Datei prüfen: %s": "错误：转换后没有可听的内容\n  （只有静音）。请检查文件：%s",
        "Fehler: Patch%d gibt es nicht.": "错误：音色 %d 不存在。",
        "Fehler: Die Aufnahme ließ sich nicht ausblenden (kein 16-Bit-WAV?).": "错误：录音无法淡出（不是 16 位 WAV？）。",
        "Fehler: '%s' ist nicht eingelegt.": "错误：'%s' 未插入。",
        "Fehler beim Auswerfen: %s": "弹出时出错：%s",
        "Fehler: Patch%d hat keine WAV.": "错误：音色 %d 没有 WAV。",
        "Fehler: Sicherungsordner '%s' gibt es nicht.": "错误：备份文件夹 '%s' 不存在。",
        "Fehler: in '%s' liegen keine Patch-Ordner.": "错误：'%s' 中没有音色文件夹。",
        "Fehler: keine Sicherung mit einer Aufnahme für Patch%d in '%s'.\n  Fade-Sicherungen heißen Sicherung_<Datum>_vor_fade.": "错误：'%s' 中没有音色 %d 的录音备份。\n  淡出备份名为 Sicherung_<日期>_vor_fade。",
        "Fehler: Platz %d ist belegt (%s).\n  Zum Tauschen:  tauschen --patch %d --mit %d": "错误：位置 %d 已占用（%s）。\n  要交换：  tauschen --patch %d --mit %d",
        "Fehler: Nummern müssen zwischen 1 und %d liegen.": "错误：编号必须在 1 到 %d 之间。",
        "Fehler: '%s' ist ein Ordner, keine Datei.": "错误：'%s' 是文件夹，不是文件。",
        "Fehler: '%s' ist kein Pegelprotokoll (keine lesbare Textdatei).": "错误：'%s' 不是电平记录（不是可读的文本文件）。",
        "Fehler: '%s' darf nicht gelesen werden.": "错误：'%s' 不允许读取。",
        "Fehler: '%s' lässt sich nicht lesen: %s": "错误：'%s' 无法读取：%s",
        "Fehler beim Umwandeln. Die Datei hat %d Hz / %d Kanal/Kanäle;\n  nötig sind 44100 Hz und 2 Kanäle. Bitte vorher umwandeln oder\n  ffmpeg installieren.": "转换时出错。文件是 %d Hz / %d 声道；\n  需要 44100 Hz 和 2 声道。请先转换，或\n  安装 ffmpeg。",
        "Fehler: '%s' lässt sich nicht schreiben.\n  Nimm einen anderen Ordner:  --sicherung <pfad>": "错误：'%s' 无法写入。\n  请换一个文件夹：  --sicherung <路径>",
        "Fehler: Patch%d hat keine phrase.wav.": "错误：音色 %d 没有 phrase.wav。",
        "Fehler: Der Pegel ließ sich nicht ändern.": "错误：电平无法更改。",
        "Fehler: in '%s' darf nicht geschrieben werden.\n  Nimm einen anderen Ordner:  --sicherung <pfad>": "错误：'%s' 不允许写入。\n  请换一个文件夹：  --sicherung <路径>",
        "Fehler: Teil %d ließ sich nicht schneiden.": "错误：第 %d 段无法剪切。",
        "Fehler: Die Datei ist %.1f Minuten lang.\n  Der JamMan Solo XT nimmt höchstens %d Minuten pro Loop an\n  (Handbuch, Seite 60: \"Max. Individual Loop Time: 10 minutes\").\n  Längere Loops erscheinen auf der Karte, werden vom Pedal aber\n  nicht erkannt - und können dabei verschwinden.\n\n  So geht es weiter:\n    --kuerzen            auf 9:59 kürzen (harter Schnitt am Ende,\n                         12 ms ausgeblendet, damit es nicht knackt)\n    --laenge 480         auf 8:00 kürzen\n    --trotzdem           unverändert schreiben (zum Ausprobieren)": "错误：文件长 %.1f 分钟。\n  JamMan Solo XT 每个循环最多接受 %d 分钟\n  （手册第 60 页：\"Max. Individual Loop Time: 10 minutes\"）。\n  更长的循环会出现在卡上，但踏板不识别 -\n  并可能在过程中消失。\n\n  可以这样做：\n    --kuerzen            截到 9:59（结尾硬切，\n                         12 毫秒淡出以免爆音）\n    --laenge 480         截到 8:00\n    --trotzdem           原样写入（供试用）",
        "Fehler: --wav fehlt.": "错误：缺少 --wav。",
        "Fehler: --patch fehlt.": "错误：缺少 --patch。",
        "Fehler: --protokoll fehlt.": "错误：缺少 --protokoll。",
        "Fehler: --patch fehlt (oder --alle angeben).": "错误：缺少 --patch（或给出 --alle）。",
        "Fehler: --patch und --auf fehlen.": "错误：缺少 --patch 和 --auf。",
        "Fehler: --patch und --mit fehlen.": "错误：缺少 --patch 和 --mit。",
        "Fehler: --patch und --nach fehlen.": "错误：缺少 --patch 和 --nach。",
        "  Hinweis: Pegelprotokoll konnte nicht geschrieben werden.": "    提示：电平记录未能写入。",
        "  Protokoll: %s": "    记录：%s",
        "  %d Einträge": "    %d 条",
        "    %-10s %-24s Faktor %.4f → 1/%.4f": "      %-10s %-24s 系数 %.4f → 1/%.4f",
        "  Trockenlauf — nichts geändert. Mit --schreiben ausführen.": "    试运行 — 未做修改。用 --schreiben 执行写入。",
        "    %-10s %+6.1f → %+6.1f dB": "      %-10s %+6.1f → %+6.1f dB",
        "  Zurückgerechnet: %d Patches. Sicherung davor: %s": "    已还原：%d 个音色。之前的备份：%s",
        "  Pegel:        %.1f dB → %.1f dB Spitze": "    电平：        %.1f dB → %.1f dB 峰值",
        "                Die Anhebung wurde begrenzt, damit nichts": "                提升已受限，以免任何",
        "                über 0 dBFS geht (sonst würde übersteuert).": "                超过 0 dBFS（否则会削波）。",
        "                Achtung: %d Werte liegen an der Grenze.": "                注意：%d 个值达到上限。",
        "  Hinweis: unbekanntes WAV-Format 0x%04X - nicht angefasst.": "    提示：未知的 WAV 格式 0x%04X — 未处理。",
        "Fehler: '%s' ist kein Ordner. Ist die Karte eingelegt?": "  错误：'%s' 不是文件夹。卡片插好了吗？",
        "Fehler: auf '%s' liegt kein Ordner '%s' oder '%s'. Das ist keine JamMan-Karte.": "  错误：'%s' 中没有文件夹 '%s' 或 '%s'。这不是 JamMan 存储卡。",
        "  %d Apple-Dateien entfernt (._* und .DS_Store).": "    已删除 %d 个 Apple 文件（._* 和 .DS_Store）。",
        "  Keine Patches auf der Karte.": "    卡上没有音色。",
        "  %-10s %-34s %8s  %s": "    %-10s %-34s %8s  %s",
        "  %-10s %-34s %8s  %s%s%s": "    %-10s %-34s %8s  %s%s%s",
        "\n  %d Patches, %s belegt.": "\n  %d 个音色，%s 个已占用。",
        "  Sicherung beim Start: Karte unverändert, nichts zu tun.": "    启动备份：卡片未变，无需处理。",
        "  Sicherung beim Start: %s": "    启动备份：%s",
        "    gesichert: %d Patches, %.0f MB": "      已备份：%d 个音色，%.0f MB",
        "    alte Sicherung entfernt: %s": "      已删除旧备份：%s",
        "  Quelle: %s": "    来源：%s",
        "  Ziel:   %s": "    目标：%s",
        "  Trockenlauf — nichts kopiert. Mit --schreiben ausführen.": "    试运行 — 未复制。用 --schreiben 执行写入。",
        "  Gesichert: %d Patches, %.0f MB.": "    已备份：%d 个音色，%.0f MB。",
        "  Datei:  %s": "    文件：%s",
        "  Dauer:  %s": "    时长：%s",
        "  Teile:  %d × etwa %s": "    分段：%d × 约 %s",
        "  Suche musikalisch brauchbare Schnittstellen …": "    正在寻找可用的剪切点 …",
        "  Hinweis: keine brauchbaren Pausen gefunden (%s) -": "    提示：未找到可用的停顿（%s）—",
        "           es wird gleichmäßig geschnitten.": "             改为均匀切分。",
        "  Pause an der Naht wird herausgenommen": "    接缝处的停顿已去除",
        "    Teil %d: %7.1f – %7.1f s  (%s)": "      第 %d 段：%7.1f – %7.1f s（%s）",
        "  Hinweis: Teil %d wäre %.1f min - er wird auf 9:59 gekürzt.": "    提示：第 %d 段会是 %.1f 分钟 — 将截到 9:59。",
        "    Teil %d: %s – %s": "      第 %d 段：%s – %s",
        "  Trockenlauf — nichts angelegt. Mit --schreiben ausführen.": "    试运行 — 未创建。用 --schreiben 执行写入。",
        "  Quelle:       %s": "    来源：        %s",
        "  Dauer:        %s": "    时长：        %s",
        "  Format:       %d Hz / %d Bit / %d Kanal/Kanäle%s": "    格式：        %d Hz / %d 位 / %d 声道%s",
        "  Stopp:        %s": "    停止：        %s",
        "  Rhythmus:     %s": "    节奏：        %s",
        "  Wiedergabe:   %s": "    播放：        %s",
        "  Fade:         letzte %.1f s werden ausgeblendet": "    淡出：        最后 %.1f 秒淡出",
        "  Pegel:        auf -1 dB Spitze normieren": "    电平：        标准化到 -1 dB 峰值",
        "  Pegel:        %+.1f dB": "    电平：        %+.1f dB",
        "  Neuer Patch:  Patch%s  (Name: %s)": "    新音色：      Patch%s（名称：%s）",
        "  Vorlage:      %s": "    模板：        %s",
        "  Trockenlauf — nichts geschrieben. Mit --schreiben ausführen.": "    试运行 — 未写入。用 --schreiben 执行写入。",
        "  Kürzen:       auf %s geschnitten (harter Schnitt, 12 ms ausgeblendet)": "    截短：        截到 %s（硬切，12 毫秒淡出）",
        "  Kopf:         WAV-Kopf auf einfaches PCM umgeschrieben": "    文件头：      WAV 头已改写为普通 PCM",
        "  Fertig: %s angelegt.": "    完成：已创建 %s。",
        "  Karte auswerfen, in den JamMan stecken — der Patch erscheint als Patch%s.": "    弹出卡片，插入 JamMan — 音色显示为 Patch%s。",
        "  Patch:        %s  (%s)": "    音色：        %s（%s）",
        "  Stopp:        %s (%s)": "    停止：        %s（%s）",
        "  Rhythmus:     %s (%s)": "    节奏：        %s（%s）",
        "  Wiedergabe:   %s (IsLoop %s)": "    播放：        %s（IsLoop %s）",
        "  Name:         %s": "    名称：        %s",
        "  Fade:         letzte %.1f s der Aufnahme ausblenden": "    淡出：        录音最后 %.1f 秒淡出",
        "                (verändert die Audiodatei; vorher wird gesichert)": "                （会改变音频文件；之前会自动备份）",
        "  Geändert. Sicherung: %s": "    已修改。备份：%s",
        "  %s Sie kann jetzt abgezogen werden.": "    %s 现在可以取出了。",
        "  Schreibweise des Geräts: Patch%0*d": "    设备的写法：Patch%0*d",
        "  Alle Ordner heißen schon so. Nichts zu tun.": "    所有文件夹已是此写法。无需处理。",
        "  Zu ändern: %d": "    待修改：%d",
        "    %-12s ->  %s": "      %-12s ->  %s",
        "    %-12s FEHLER: %s": "      %-12s 错误：%s",
        "  Fertig: %d Ordner umbenannt, %d Fehler, %d Patches lesbar.": "    完成：重命名 %d 个文件夹，%d 个错误，%d 个音色可读。",
        "  Zuordnung liegt in: %s": "    对应关系在：%s",
        "  %d Apple-Dateien auf der Karte. Mit --schreiben entfernen.": "    卡上有 %d 个 Apple 文件。用 --schreiben 删除。",
        "  Fertig.": "    完成。",
        "  Spielt \"%s\" (%s) … (Strg+C hält an)": "    正在播放“%s”（%s）…（Ctrl+C 停止）",
        "  Ziel: %.1f dB Lautheit (RMS), Spitze höchstens %.1f dB": "    目标：%.1f dB 响度（RMS），峰值最高 %.1f dB",
        "  Patches: %d": "    音色：%d",
        "  %-10s %-24s %8s": "    %-10s %-24s %8s",
        "  %-10s %-24s %+7.1f dB": "    %-10s %-24s %+7.1f dB",
        "  Sicherung: %s": "    备份：%s",
        "  %-10s %-24s %+6.1f → %+6.1f dB  (Spitze %+.1f dB)%s": "    %-10s %-24s %+6.1f → %+6.1f dB（峰值 %+.1f dB）%s",
        "  Fertig: %d Patches normiert, %d davon in der Spitze gedeckelt.": "    完成：%d 个音色已标准化，其中 %d 个在峰值处受限。",
        "  Patches in der Sicherung: %d": "    备份中的音色：%d",
        "  Patches auf der Karte:    %d": "    卡上的音色：    %d",
        "  Hinweis: diese gibt es auf der Karte nicht und werden neu angelegt:": "    提示：这些在卡上不存在，将被新建：",
        "           %s": "             %s",
        "  Trockenlauf — nichts zurückgeholt. Mit --schreiben ausführen.": "    试运行 — 未恢复。用 --schreiben 执行写入。",
        "  Zurückgeholt. Der Zustand davor liegt in: %s": "    已恢复。之前的状态在：%s",
        "  Patch:      %s  (%s)": "    音色：      %s（%s）",
        "  Sicherung:  %s": "    备份：      %s",
        "    %-22s %8.2f MB": "      %-22s %8.2f MB",
        "  %s  (%s)": "    %s（%s）",
        "  →  %s": "    →  %s",
        "  Trockenlauf — nichts umbenannt. Mit --schreiben ausführen.": "    试运行 — 未重命名。用 --schreiben 执行写入。",
        "  Fertig. Sicherung: %s": "    完成。备份：%s",
        "  Nichts zu tun.": "    无需处理。",
        "  Achtung: %s und %s haben unterschiedliche Stellenzahl.": "    注意：%s 与 %s 的位数不同。",
        "           Der JamMan sortiert nach Namen - der Patch landet dann": "             JamMan 按名称排序 — 音色将落在",
        "           auf dem Platz, den sein neuer Name angibt.": "             其新名称所指的位置。",
        "  Tauschen:": "    交换：",
        "    Platz %-4s %-24s  →  Platz %s": "      位置 %-4s %-24s  →  位置 %s",
        "  Trockenlauf — nichts getauscht. Mit --schreiben ausführen.": "    试运行 — 未交换。用 --schreiben 执行写入。",
        "  Getauscht. Sicherung: %s": "    已交换。备份：%s",
        "  Wird gelöscht: %s  (%s)": "    将被删除：%s（%s）",
        "  Vorher wird er gesichert nach: %s": "    先备份到：%s",
        "  Trockenlauf — nichts gelöscht. Mit --schreiben ausführen.": "    试运行 — 未删除。用 --schreiben 执行写入。",
        "  Gelöscht. Sicherung: %s": "    已删除。备份：%s",
        "  Karte auswerfen, in den JamMan stecken — der Patch ist weg.": "    弹出卡片，插入 JamMan — 音色已删除。",
        "  Neue Reihenfolge:": "    新顺序：",
        "    Platz %d:  %-10s  %s%s": "      位置 %d：  %-10s  %s%s",
        "  Verschoben. Sicherung: %s": "    已移动。备份：%s",
        "  JamMan Solo XT — Speicherkarte verwalten": "    JamMan Solo XT — 管理存储卡",
        "  Keine Karte gefunden unter %s.": "    在 %s 下未找到存储卡。",
        "  Karte: %s": "    存储卡：%s",
        "  1  Patches auflisten": "    1  列出音色",
        "  2  Patch anhören": "    2  试听音色",
        "  3  Datei aufspielen": "    3  导入文件",
        "  4  Patch ändern (Stopp, Wiedergabe, Name)": "    4  修改音色（停止、播放、名称）",
        "  5  Patch verschieben": "    5  移动音色",
        "  6  Patch löschen": "    6  删除音色",
        "  7  Alles sichern": "    7  全部备份",
        "  8  Karte auswerfen": "    8  弹出卡片",
        "  0  Beenden": "    0  退出",
        "  %s": "    %s",
        "  System:        %s %s": "    系统：        %s %s",
        "  Python:        %s": "    Python：      %s",
        "  Fassung:       %s": "    版本：        %s",
        "  Tk:            %s": "    Tk：          %s",
        "  Tk-Modul:      %s": "    Tk 模块：     %s",
        "  Tk:            NICHT verfügbar — %s": "    Tk：          不可用 — %s",
        "                 (Fenster geht mit diesem Python nicht;": "                    （此 Python 无法显示窗口；",
        "                  das Textmenü geht trotzdem: jamman.py menu)": "                     文本菜单仍可用：jamman.py menu）",
        "  Ton:           winsound (Windows-eigen)": "    声音：        winsound（Windows 自带）",
        "  Ton:           afplay": "    声音：        afplay",
        "  Ton:           %s": "    声音：        %s",
        "  Kartenwurzeln: %s": "    卡片搜索位置：%s",
        "  Einstellungen: %s": "    设置：        %s",
        "  ffmpeg:        %s": "    ffmpeg：      %s",
        "  Schrift:       %s": "    字体：        %s",
        "  Pythons auf diesem Rechner:": "    本机上的 Python：",
        "    %-62s %s": "      %-62s %s",
        "  Geprüfte Orte:": "    已检查的位置：",
        "  Laufwerke mit JamManSoloXT:": "    含 JamManSoloXT 的驱动器：",
        "    %-24s %3d Patches  %s": "      %-24s %3d 个音色  %s",
        "  gewählt:       %s": "    已选择：      %s",
        "  Patches:       %d": "    音色：        %d",
        "  ACHTUNG: das Dateisystem liefert Lesefehler.": "    警告：文件系统报告读取错误。",
        "  Betroffene Einträge (erste drei): %s": "    受影响的条目（前三个）：%s",
        "  Inhalt von %s:": "    %s 的内容：",
        "    %-24r %s, %s": "      %-24r %s, %s",
        "    nicht lesbar: %s": "      无法读取：%s",
        "    %-10s %s": "      %-10s %s",
        "  erste Zeile im Fenster:": "    窗口中的第一行：",
        "    %r": "      %r",
        "JamMan Solo XT": "  JamMan Solo XT",
        "WAV-Format": "WAV 格式",
        "%d Hz / %d Bit / %d K": "%d Hz / %d 位 / %d 声道",
        "  %-28s  (gibt es nicht)": "  %-28s  （不存在）",
        "  %-28s  (nicht lesbar: %s)": "  %-28s  （无法读取：%s）",
        "  %-28s  (leer)": "  %-28s  （空）",
        "  %-28s  %s mit %s Patches": "  %-28s  %s，共 %s 个音色",
        "kein nutzbares Tk": "没有可用的 Tk",
        "Sprache der Ausgabe (Vorgabe: aus dem System)": "输出语言（默认：跟随系统）",
        "Laufwerk der Karte (Vorgabe: automatisch erkannt)": "存储卡驱动器（默认：自动识别）",
        "Zielordner für 'sichern'": "「备份」的目标文件夹",
        "Audiodatei für 'aufspielen'": "「导入」的音频文件",
        "Name des Patches (Vorgabe: Dateiname)": "音色名称（默认：文件名）",
        "Patch-Nummer": "音色编号",
        "Zielplatz für 'verschieben'": "「移动」的目标位置",
        "Tauschpartner für 'tauschen'": "「交换」的对方槽位",
        "Zielplatz für 'platzieren'": "「放置」的目标位置",
        "Sicherungsordner für 'zuruecknehmen'": "「恢复」的备份文件夹",
        "Pegelprotokoll für 'pegel-umkehren'": "「还原电平」的电平记录",
        "bei 'zuruecknehmen': alle Patches aus der Sicherung": "「恢复」时：备份中的全部音色",
        "Ziellautheit für 'alle-normieren' (Vorgabe -18 dB)": "「全部标准化」的目标响度（默认 -18 dB）",
        "Spitzengrenze für 'alle-normieren' (Vorgabe -1 dB)": "「全部标准化」的峰值上限（默认 -1 dB）",
        "zu lange Datei auf 9:59 kürzen": "把过长的文件截到 9:59",
        "beim Teilen die Pause an der Naht stehen lassen": "分割时保留接缝处的停顿",
        "beim Teilen gleichmäßig schneiden statt an Pausen": "分割时均匀切分，而不是在停顿处",
        "Datei in N gleich lange Patches aufteilen": "把文件分成 N 个等长音色",
        "zu lange Datei auf diese Länge kürzen": "把过长的文件截到这个长度",
        "auch Loops über 10 Minuten schreiben (Pedal nimmt sie nicht an)": "也写入超过 10 分钟的循环（踏板不接受）",
        "Fade am Ende der Datei einrechnen (z. B. 3)": "计入文件末尾的淡出（例如 3）",
        "Lautstärke um DB ändern (z. B. -3 oder +2)": "按 DB 改变音量（例如 -3 或 +2）",
        "Anhebung NICHT auf 0 dBFS begrenzen (kann übersteuern)": "提升时不要限制在 0 dBFS（可能削波）",
        "auf -1 dB Spitzenpegel normieren": "标准化到 -1 dB 峰值",
        "Stopp-Verhalten des neuen Patches (ohne Angabe: wie Vorlage)": "新音色的停止方式（不填：同模板）",
        "Loop oder Single (IsLoop in phrase.xml; ohne Angabe: wie Vorlage)": "循环或单次（phrase.xml 中的 IsLoop；不填：同模板）",
        "Rhythmus des neuen Patches (ohne Angabe: wie Vorlage)": "新音色的节奏（不填：同模板）",
        "beim Verschieben nur die Namen sichern (schnell, kein Kopieren)": "移动时只备份名称（快，不复制）",
        "Ordner für die Sicherung vor Löschen/Verschieben": "删除/移动前备份的文件夹",
        "wirklich verändern (sonst Trockenlauf)": "真正修改（否则只是试运行）",
        "was zu tun ist (Liste unten; englische Namen gehen auch)": "要做什么（见下方列表；英文名称也可以）",
        "Einstellungen: %s": "设置：%s",
        "Keine Karte gefunden": "未找到存储卡",
        "Neue Fassung verfügbar": "有新的版本",
        "Es gibt eine neuere Fassung: %s. Sie benutzen %s.": "有更新的版本：%s。您使用的是 %s。",
        "Zur Download-Seite wechseln?": "要打开下载页面吗？",
        "Es liegt keine JamMan-Karte im Rechner.": "电脑里没有 JamMan 存储卡。",
        "Nimm die microSD aus dem Pedal und stecke sie in einen Kartenleser oder Kartenslot. Über das USB-Kabel am Pedal erscheint sie nicht als Laufwerk.": "请把 microSD 从踏板中取出，插入读卡器或卡槽。通过踏板的 USB 线连接时，它不会显示为驱动器。",
        "Ordner selbst wählen?": "自己选择文件夹吗？",
        "Ordner der Karte wählen": "选择存储卡文件夹",
        '   ← wird umgewandelt': '← 将被转换',
        '   ← abweichend': '← 格式不同',
        '   ← über 10 min, Pedal nimmt sie nicht an': '← 超过 10 分钟，踏板不接受',
        'keine WAV': '没有 WAV',
        '  Das Fenster wäre leer. Es geht im Textmenü weiter.': '窗口将是空白的。文本菜单仍可使用。',
        '  Für ein Fenster: Python von python.org installieren (bringt Tk 8.6).': '要使用窗口：请安装 python.org 的 Python（自带 Tk 8.6）。',
        '  Tk %s kann auf diesem macOS keinen Text im Fenster zeichnen.': '此 macOS 上 Tk %s 无法在窗口中绘制文字。',
        '  Hinweis: Fade konnte nicht eingerechnet werden.': '提示：淡出无法应用。',
        '  Hinweis: Pegel ließ sich nicht ändern (kein 16-Bit-WAV?).': '提示：音量无法更改（不是 16 位 WAV？）。',
        '  Achtung: der WAV-Kopf ist weiterhin ungewöhnlich.': '注意：WAV 头仍然异常。',
        '  Hinweis: Die Vorlage hatte kein Feld IsLoop - es wurde ergänzt.': '提示：模板缺少 IsLoop 字段 - 已补上。',
        '  Hinweis: Die Vorlage hatte kein Feld StopMode - es wurde ergänzt.': '提示：模板缺少 StopMode 字段 - 已补上。',
        '  Hinweis: Die Vorlage hatte kein Feld RhythmType - es wurde ergänzt.': '提示：模板缺少 RhythmType 字段 - 已补上。',
        '  Angehalten.': '已停止。',
        '  Das ist kein Fehler des Werkzeugs - die Karte oder': '这不是程序错误 — 存储卡或',
        '  ihr Einhängepunkt ist beschädigt. Erste Schritte:': '它的挂载点已损坏。可以这样做：',
        '    dmesg | tail -20            (Meldungen des Systems)': 'dmesg | tail -20            （系统消息）',
        '    sudo umount <laufwerk>      (aushängen)': 'sudo umount <设备>          （卸载）',
        '    sudo fsck.vfat -n <geraet>  (FAT32, nur prüfen)': 'sudo fsck.vfat -n <设备>    （FAT32，仅检查）',
        '    sudo exfatfsck <geraet>     (exFAT, nur prüfen)': 'sudo exfatfsck <设备>       （exFAT，仅检查）',
        '    oder die Karte im JamMan einschalten - das Pedal': '或者把卡插入 JamMan 并开机 —',
        '    repariert seine Karte beim Einschalten.': '踏板会在开机时修复它的卡。',
        'Erst eine Karte wählen.': '请先选择存储卡。',
        "Speicherkarte des JamMan Solo XT": "JamMan Solo XT 存储卡",
        "keine Karte gefunden": "未找到存储卡",
        "Ordner": "文件夹", "Name": "名称", "Dauer": "时长",
        "Stoppart": "停止方式", "Wdg.": "播放", "Rhythmus": "节奏",
        "Wiedergabe": "播放",
        "Patch markieren — dann hier klicken zum Springen":
            "先选择音色，再点击此处跳转",
        "kein Patch gewählt": "未选择音色",
        "automatisch weiter": "自动继续",
        "beim Start sichern": "启动时备份",
        "Platz": "位置",
        "nächster": "下一个",
        "vormerken": "预定",
        "vorgemerkt %d": "已预定 %d",
        "Einstellungen:": "设置：",
        "wie Vorlage": "同模板",
        "Fade am Ende": "结尾淡出",
        "Karte wählen": "选择存储卡",
        "Datei aufspielen": "导入文件",
        "Alles sichern": "全部备份",
        "Pegel zurückrechnen": "撤销音量",
        "Neu laden": "重新载入",
        "Anhören": "试听",
        "Stopp": "停止",
        "Umbenennen": "重命名",
        "Platz ändern": "更改位置",
        "Sicherung zurückholen": "恢复备份",
        "Einstellungen anwenden": "应用设置",
        "▲ nach oben": "▲ 上移",
        "▼ nach unten": "▼ 下移",
        "Tauschen": "交换",
        "Datei teilen": "分割文件",
        "Alle normieren": "全部标准化",
        "Löschen": "删除",
        "Karte auswerfen": "弹出存储卡",
        "Pegel": "音量",
        "auf −1 dB Spitze normieren": "标准化到 −1 dB 峰值",
        "Teile": "段数",
        "an Pausen schneiden": "在停顿处切分",
        "Anhebung auf 0 dB begrenzen": "提升限制在 0 dB",
        "Doppelklick spielt · Ziehen ordnet um · ⌘ für zwei Markierungen, dann Tauschen · Esc hält an.":
            "双击播放 · 拖动排序 · ⌘ 选两个后交换 · Esc 停止。",
        "vorgemerkt: Platz %d": "已预定：位置 %d",
        "Ende der Liste erreicht": "已到列表末尾",
        "Erst einen Patch in der Liste wählen.": "请先在列表中选择一个音色。",
        "Bitte eine Platznummer eintragen.": "请输入位置编号。",
        "Platz %d gibt es auf dieser Karte nicht.": "此卡上没有位置 %d。",
        "MP3 und M4A brauchen ffmpeg -": "MP3 和 M4A 需要 ffmpeg -",
        "Anleitung": "说明",
        "WAV geht auch ohne": "WAV 无需 ffmpeg",
        "Schließen": "关闭",
        "Text kopieren": "复制文本",
        "Karte nicht lesbar": "存储卡无法读取",
        "Fehler": "错误",
        "Hinweis": "提示",
        "Anleitung in die Zwischenablage kopiert": "说明已复制到剪贴板",
        "Sicherung beim Start: %s": "启动时备份：%s",
    },
}

SPRACHE = "de"

# Hinweis zur Übersetzung - erscheint im Fenster, wenn die Sprache nicht
# Deutsch oder Englisch ist. Diese Fassungen sind vorläufig; Korrekturen
# von Muttersprachlern sind ausdrücklich willkommen.
UEBERSETZUNGSHINWEIS = {
    "fr": "Traduction provisoire — corrections bienvenues.",
    "es": "Traducción provisional — se agradecen correcciones.",
    "zh": "翻译为初步版本，欢迎指正。",
}


def sprache_setzen(code):
    """Sprache wählen; unbekannte Angaben fallen auf Deutsch zurück."""
    global SPRACHE
    code = (code or "").strip().lower()[:2]
    SPRACHE = code if code in TEXTE else "de"


def sprache_aus_system():
    """Sprache des Systems erraten.

    Reihenfolge:
      1. JAMMAN_SPRACHE (zum Ausprobieren gesetzt)
      2. macOS: die eingestellte Systemsprache (`AppleLanguages`)
      3. LANG / LC_ALL / LC_MESSAGES

    Anlaß (18.11.2026): Auf einem Intel-Mac startete das Werkzeug englisch,
    obwohl der Nutzer deutsch erwartet hatte — das Terminal meldete `en`,
    die Systemsprache war nicht gefragt worden. `C`/`POSIX` werden
    übersprungen, weil sie keine Sprache nennen.
    """
    eigene = os.environ.get("JAMMAN_SPRACHE")
    if eigene:
        return eigene[:2]

    if sys.platform == "darwin":
        try:
            roh = subprocess.run(["defaults", "read", "-g", "AppleLanguages"],
                                 capture_output=True, text=True, timeout=5).stdout
            treffer = re.findall(r'"([a-zA-Z]{2})', roh)
            if treffer:
                return treffer[0].lower()
        except Exception:
            pass

    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        wert = os.environ.get(name)
        if wert and wert[:2].lower() not in ("c.", "c ", "po"):
            return wert[:2]
    return "de"


def uebersetzt_mb(mb):
    """Hülle um tkinter.messagebox: übersetzt Titel und Text.

    So müssen die über 50 Dialogaufrufe im Fenster nicht einzeln angefasst
    werden - übersetzt wird beim Anzeigen.
    """
    class Huelle:
        def __init__(self, echt):
            self._echt = echt

        def __getattr__(self, name):
            funktion = getattr(self._echt, name)

            def aufruf(*args, **kwargs):
                neue = tuple(T(a) if isinstance(a, str) else a for a in args)
                return funktion(*neue, **kwargs)

            return aufruf

    return Huelle(mb)


def T(text):
    """Übersetzung holen: gewählte Sprache, sonst Englisch, sonst Deutsch.

    Einrückungen bleiben erhalten: die Katalogeinträge stehen ohne
    führende Leerzeichen, die Ausgabe behält ihre Spalten.
    """
    if SPRACHE == "de" or not isinstance(text, str):
        return text
    katalog = TEXTE.get(SPRACHE) or {}
    englisch = TEXTE.get("en") or {}
    if text in katalog:
        return katalog[text]
    if text in englisch:
        return englisch[text]
    einrueckung = text[:len(text) - len(text.lstrip())]
    rest = text.lstrip()
    if einrueckung and rest:
        if rest in katalog:
            return einrueckung + katalog[rest]
        if rest in englisch:
            return einrueckung + englisch[rest]
    return text


def freier_name(pfad):
    """Hängt -2, -3 … an, wenn der Name schon existiert.

    Zwei Änderungen in derselben Sekunde erzeugten sonst denselben
    Sicherungsordner - am 10.11.2026 geschehen, copytree brach ab.
    """
    if not os.path.exists(pfad):
        return pfad
    i = 2
    while os.path.exists("%s-%d" % (pfad, i)):
        i += 1
    return "%s-%d" % (pfad, i)


def sicher_ablegen(basis, patch_pfade, grund, wurzel=None):
    """Kopiert Patches vorher weg. Gibt den Zielordner zurück."""
    ordner = os.path.expanduser(wurzel or SICHERUNG_WURZEL)
    ziel = freier_name(os.path.join(
        ordner, "Sicherung_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        + "_" + grund))
    try:
        os.makedirs(ziel, exist_ok=True)
    except PermissionError:
        raise SystemExit(T("Fehler: in '%s' darf nicht geschrieben werden.\n"
                         "  Nimm einen anderen Ordner:  --sicherung <pfad>") % ordner)
    for pfad in patch_pfade:
        shutil.copytree(pfad, os.path.join(ziel, os.path.basename(pfad)),
                        dirs_exist_ok=True)
    return ziel


def zuruecknehmen_kandidaten(ordner, nummer):
    """Sicherungen, die für diesen Platz eine Aufnahme enthalten,
    die jüngste zuerst. Namen wie Sicherung_2026-11-10_11-46-24_vor_fade."""
    treffer = []
    try:
        namen = sorted(os.listdir(ordner), reverse=True)
    except OSError:
        return treffer
    for name in namen:
        # "vor_zuruecknehmen" ist die Kopie des Zustands, der gerade ersetzt
        # wurde - kein Ziel zum Wiederherstellen.
        if name.endswith("vor_zuruecknehmen"):
            continue
        for form in ("Patch%02d" % nummer, "Patch%d" % nummer):
            k = os.path.join(ordner, name, form)
            if os.path.isdir(k) and os.path.exists(os.path.join(k, "PhraseA", "phrase.wav")):
                treffer.append(k)
                break
    return treffer


def befehl_alle_normieren(basis, schreiben, ziel_rms_db=-18.0, grenze_db=-1.0,
                          wurzel=None, fortschritt=None):
    """Alle Patches auf gleiche Lautheit bringen - für die Bühne.

    Normiert wird auf den Effektivwert (RMS), nicht auf die Spitze:
    gleich laute Spitzen bedeuten nicht gleich laute Loop. Die Spitze
    wird zusätzlich bei grenze_db gedeckelt, damit nichts übersteuert.
    """
    alle = patches(basis)
    if not alle:
        raise SystemExit(T("Keine Patches auf der Karte."))
    print(T("  Ziel: %.1f dB Lautheit (RMS), Spitze höchstens %.1f dB")
          % (ziel_rms_db, grenze_db))
    print(T("  Patches: %d") % len(alle))
    if not schreiben:
        print()
        print(T("  %-10s %-24s %8s") % ("Ordner", "Name", "Lautheit jetzt"))
        for nummer, name, pfad in alle:
            w = phrase_wav(pfad)
            if not w:
                continue
            _, rms = pegel_info(w)
            print(T("  %-10s %-24s %+7.1f dB") % (name, patch_name(pfad)[:24], rms))
        print()
        print(T("  Trockenlauf — nichts geändert. Mit --schreiben ausführen."))
        return

    ziel = sicher_ablegen(basis, [pf for _, _, pf in alle], "vor_normieren", wurzel)
    print(T("  Sicherung: %s") % ziel)
    print()
    geaendert = 0
    gedeckelt = 0
    protokoll_eintraege = []
    for nummer, name, pfad in alle:
        w = phrase_wav(pfad)
        if not w:
            continue
        vorher = pegel_info(w)
        if vorher[0] is None:
            continue
        ergebnis = pegel_anpassen(w, ziel_rms_db=ziel_rms_db,
                                  begrenzen=True, grenze_db=grenze_db)
        if not ergebnis:
            continue
        nachher = pegel_info(w)
        if nachher[1] and vorher[1]:
            protokoll_eintraege.append({
                "ordner": name, "name": patch_name(pfad),
                "faktor": 10 ** ((nachher[1] - vorher[1]) / 20.0),
                "vorher": vorher[1], "nachher": nachher[1]})
        geaendert += 1
        if ergebnis["begrenzt"]:
            gedeckelt += 1
        if fortschritt:
            fortschritt("normiert: %s (%d/%d)" % (patch_name(pfad)[:24],
                                                 geaendert, len(alle)))
        print(T("  %-10s %-24s %+6.1f → %+6.1f dB  (Spitze %+.1f dB)%s")
              % (name, patch_name(pfad)[:24], vorher[1], nachher[1],
                 nachher[0], "  [gedeckelt]" if ergebnis["begrenzt"] else ""))
    saeubere_karte(basis)
    schreibe_pegelprotokoll(basis, protokoll_eintraege, "alle_normieren", wurzel)
    print()
    print(T("  Fertig: %d Patches normiert, %d davon in der Spitze gedeckelt.")
          % (geaendert, gedeckelt))


def befehl_zuruecknehmen_alle(basis, aus, schreiben, wurzel=None):
    """Alle Patches aus einer Sicherung zurückholen.

    Für den Fall, dass eine Sammelaktion (z. B. alle normieren) rückgängig
    gemacht werden soll.
    """
    quelle = os.path.expanduser(aus or "")
    if not os.path.isdir(quelle):
        raise SystemExit(T("Fehler: Sicherungsordner '%s' gibt es nicht.") % quelle)
    vorhanden = sorted(d for d in os.listdir(quelle)
                       if re.fullmatch(r"Patch\d+", d))
    if not vorhanden:
        raise SystemExit(T("Fehler: in '%s' liegen keine Patch-Ordner.") % quelle)
    auf_der_karte = [name for _, name, _ in patches(basis)]
    print(T("  Sicherung: %s") % quelle)
    print(T("  Patches in der Sicherung: %d") % len(vorhanden))
    print(T("  Patches auf der Karte:    %d") % len(auf_der_karte))
    fehlend = [x for x in vorhanden if x not in auf_der_karte]
    if fehlend:
        print(T("  Hinweis: diese gibt es auf der Karte nicht und werden neu angelegt:"))
        print(T("           %s") % ", ".join(fehlend))
    if not schreiben:
        print(T("  Trockenlauf — nichts zurückgeholt. Mit --schreiben ausführen."))
        return
    ziel = sicher_ablegen(basis, [pf for _, _, pf in patches(basis)],
                          "vor_zuruecknehmen_alle", wurzel)
    for name in vorhanden:
        shutil.copytree(os.path.join(quelle, name), os.path.join(basis, name),
                        dirs_exist_ok=True)
    saeubere_karte(basis)
    print(T("  Zurückgeholt. Der Zustand davor liegt in: %s") % ziel)


def befehl_zuruecknehmen(basis, nummer, schreiben, wurzel=None, aus=None):
    """Holt eine frühere Fassung eines Patches aus der Sicherung zurück.

    Gedacht für den Fade: der verändert die Aufnahme, und rückgängig geht
    das nur über die Sicherung, die dabei angelegt wurde.
    """
    alle = dict((n, pf) for n, _, pf in patches(basis))
    if nummer not in alle:
        raise SystemExit(T("Fehler: Patch%d gibt es nicht.") % nummer)
    ordner = os.path.expanduser(aus or wurzel or SICHERUNG_WURZEL)
    if not os.path.isdir(ordner):
        raise SystemExit(T("Fehler: Sicherungsordner '%s' gibt es nicht.") % ordner)

    kandidaten = zuruecknehmen_kandidaten(ordner, nummer)
    if not kandidaten:
        raise SystemExit(T("Fehler: keine Sicherung mit einer Aufnahme für "
                         "Patch%d in '%s'.\n"
                         "  Fade-Sicherungen heißen Sicherung_<Datum>_vor_fade.")
                         % (nummer, ordner))
    quelle = kandidaten[0]
    print(T("  Patch:      %s  (%s)") % (os.path.basename(alle[nummer]), patch_name(alle[nummer])))
    print(T("  Sicherung:  %s") % quelle)
    for datei in ("PhraseA/phrase.wav", "patch.xml", "PhraseA/phrase.xml"):
        q = os.path.join(quelle, datei)
        if os.path.exists(q):
            print(T("    %-22s %8.2f MB") % (datei, os.path.getsize(q) / 1024 / 1024))
    if not schreiben:
        print(T("  Trockenlauf — nichts zurückgeholt. Mit --schreiben ausführen."))
        return
    ziel = sicher_ablegen(basis, [alle[nummer]], "vor_zuruecknehmen", wurzel)
    for datei in ("PhraseA/phrase.wav", "patch.xml", "PhraseA/phrase.xml"):
        q = os.path.join(quelle, datei)
        z = os.path.join(alle[nummer], datei)
        if os.path.exists(q):
            os.makedirs(os.path.dirname(z), exist_ok=True)
            shutil.copy2(q, z)
    saeubere_karte(basis)
    print(T("  Zurückgeholt. Der Zustand davor liegt in: %s") % ziel)


def befehl_platzieren(basis, von, auf, schreiben, wurzel=None):
    """Einen Patch auf eine bestimmte Platznummer legen.

    Beispiel: Patch200 soll Patch28 werden. Der Platz muss frei sein;
    ist er belegt, hilft "tauschen" weiter.
    """
    alle = dict((n, pf) for n, _, pf in patches(basis))
    if von not in alle:
        raise SystemExit(T("Fehler: Patch%d gibt es nicht.") % von)
    if auf in alle and auf != von:
        raise SystemExit(T("Fehler: Platz %d ist belegt (%s).\n"
                         "  Zum Tauschen:  tauschen --patch %d --mit %d")
                         % (auf, patch_name(alle[auf]), von, auf))
    pfad = alle[von]
    alter_name = os.path.basename(pfad)
    # Stellenzahl: zweistellig bis 99, dreistellig darunter nicht auffüllen
    neuer_name = "Patch%02d" % auf if auf < 100 else "Patch%d" % auf
    print(T("  %s  (%s)") % (alter_name, patch_name(pfad)))
    print(T("  →  %s") % neuer_name)
    if not schreiben:
        print(T("  Trockenlauf — nichts umbenannt. Mit --schreiben ausführen."))
        return
    ziel = sicherung_der_namen(basis, "vor_platzieren", wurzel)
    os.rename(pfad, os.path.join(basis, neuer_name))
    saeubere_karte(basis)
    print(T("  Fertig. Sicherung: %s") % ziel)


def befehl_tauschen(basis, a, b, schreiben, wurzel=None, kopie=False):
    """Zwei Patches tauschen die Plätze. Der Inhalt bleibt, wo er ist -
    nur die Ordnernamen wechseln. Alle anderen bleiben unberührt."""
    alle = dict((n, pf) for n, _, pf in patches(basis))
    if a not in alle or b not in alle:
        fehlend = a if a not in alle else b
        raise SystemExit(T("Fehler: Patch%d gibt es nicht.") % fehlend)
    if a == b:
        print(T("  Nichts zu tun."))
        return
    pfad_a, pfad_b = alle[a], alle[b]
    name_a, name_b = os.path.basename(pfad_a), os.path.basename(pfad_b)
    if len(name_a) != len(name_b):
        print(T("  Achtung: %s und %s haben unterschiedliche Stellenzahl.") % (name_a, name_b))
        print(T("           Der JamMan sortiert nach Namen - der Patch landet dann"))
        print(T("           auf dem Platz, den sein neuer Name angibt."))
    print(T("  Tauschen:"))
    print(T("    Platz %-4s %-24s  →  Platz %s") % (a, patch_name(pfad_a), b))
    print(T("    Platz %-4s %-24s  →  Platz %s") % (b, patch_name(pfad_b), a))
    if not schreiben:
        print(T("  Trockenlauf — nichts getauscht. Mit --schreiben ausführen."))
        return

    ziel = sicherung_der_namen(basis, "vor_tauschen", wurzel)
    zwischen = os.path.join(basis, "tmp_tausch_%d" % a)
    os.rename(pfad_a, zwischen)
    os.rename(pfad_b, os.path.join(basis, name_a))
    os.rename(zwischen, os.path.join(basis, name_b))
    saeubere_karte(basis)
    print(T("  Getauscht. Sicherung: %s") % ziel)


def befehl_loeschen(basis, nummer, schreiben, wurzel=None):
    alle = dict((n, pf) for n, _, pf in patches(basis))
    if nummer not in alle:
        raise SystemExit(T("Fehler: Patch%d gibt es nicht.") % nummer)
    pfad = alle[nummer]
    print(T("  Wird gelöscht: %s  (%s)") % (os.path.basename(pfad), patch_name(pfad)))
    print(T("  Vorher wird er gesichert nach: %s") % os.path.expanduser(wurzel or SICHERUNG_WURZEL))
    if not schreiben:
        print(T("  Trockenlauf — nichts gelöscht. Mit --schreiben ausführen."))
        return
    ziel = sicher_ablegen(basis, [pfad], "vor_loeschen", wurzel)
    shutil.rmtree(pfad)
    saeubere_karte(basis)
    print(T("  Gelöscht. Sicherung: %s") % ziel)
    print(T("  Karte auswerfen, in den JamMan stecken — der Patch ist weg."))


def sicherung_der_namen(basis, grund, wurzel=None):
    """Schreibt eine Liste Ordner → Patchname → Kennung weg.

    Beim reinen Umsortieren wird kein Inhalt verändert, nur umbenannt.
    Eine Kopie aller WAV-Dateien (867 MB) wäre dafür unverhältnismäßig;
    die Zuordnung genügt, um jeden Schritt rückgängig zu machen.
    """
    ordner = os.path.expanduser(wurzel or SICHERUNG_WURZEL)
    try:
        os.makedirs(ordner, exist_ok=True)
    except PermissionError:
        raise SystemExit(T("Fehler: in '%s' darf nicht geschrieben werden.\n"
                         "  Nimm einen anderen Ordner:  --sicherung <pfad>") % ordner)
    name = "Umbenennung_%s_%s.txt" % (datetime.now().strftime("%Y-%m-%d_%H-%M-%S"), grund)
    ziel = freier_name(os.path.join(ordner, name))
    try:
        f = open(ziel, "w", encoding="utf-8")
    except OSError:
        raise SystemExit(T("Fehler: '%s' lässt sich nicht schreiben.\n"
                         "  Nimm einen anderen Ordner:  --sicherung <pfad>") % ziel)
    with f:
        f.write("Reihenfolge vor der Änderung (%s)\n" % datetime.now().isoformat(timespec="seconds"))
        f.write("Ordner\tName\tKennung\n")
        for nummer, ordnername, pfad in patches(basis):
            kennung = ""
            try:
                txt = open(os.path.join(pfad, "patch.xml"), encoding="utf-8",
                           errors="replace").read()
                m = re.search(r"<ID>([^<]*)", txt)
                kennung = m.group(1) if m else ""
            except OSError:
                pass
            f.write("%s\t%s\t%s\n" % (ordnername, patch_name(pfad), kennung))
    return ziel


def befehl_verschieben(basis, von, nach, schreiben, wurzel=None, kopie=True):
    """Patch 'von' an die Stelle 'nach' schieben; die anderen rücken nach."""
    reihe = [pf for _, _, pf in patches(basis)]
    namen = [os.path.basename(p) for p in reihe]
    if not (1 <= von <= len(reihe)) or not (1 <= nach <= len(reihe)):
        raise SystemExit(T("Fehler: Nummern müssen zwischen 1 und %d liegen." )% len(reihe))
    if von == nach:
        print(T("  Nichts zu tun."))
        return
    bewegt = reihe.pop(von - 1)
    reihe.insert(nach - 1, bewegt)
    print(T("  Neue Reihenfolge:"))
    for i, pf in enumerate(reihe, start=1):
        alt = os.path.basename(pf)
        print(T("    Platz %d:  %-10s  %s%s") % (
            i, alt, patch_name(pf), "   ← wird verschoben" if pf == bewegt else ""))
    if not schreiben:
        print(T("  Trockenlauf — nichts umbenannt. Mit --schreiben ausführen."))
        return

    # Die vorhandenen Ordnernamen bleiben erhalten - nur ihre Zuordnung
    # aendert sich. Am 26.10.2026 hat eine berechnete Breite aus einem
    # einzelnen "Patch200" alle Namen auf drei Stellen gebracht
    # (Patch001 ...). Der JamMan erkannte danach keinen Patch mehr.
    namen = sorted(os.path.basename(p) for p in reihe)
    if kopie:
        ziel = sicher_ablegen(basis, reihe, "vor_verschieben", wurzel)
    else:
        ziel = sicherung_der_namen(basis, "vor_verschieben", wurzel)

    for i, pf in enumerate(reihe, start=1):
        os.rename(pf, os.path.join(basis, "tmp_%03d" % i))
    for i, name in enumerate(namen, start=1):
        os.rename(os.path.join(basis, "tmp_%03d" % i), os.path.join(basis, name))
    saeubere_karte(basis)
    print(T("  Verschoben. Sicherung: %s") % ziel)



# ------------------------------------------------------- Menü ohne Fenster

def menu(karte):
    """Textmenü im Terminal. Braucht KEIN tkinter.

    Anlass 26.10.2026: Auf einem MacBook Pro von 2012 (macOS 15.8) bringt
    das mit Xcode gelieferte Python 3.9 das alte Tk 8.5.9 mit und stirbt
    beim Öffnen eines Fensters (Tcl_Panic in TkpInit, SIGABRT). Dieses Menü
    läuft dort ohne Einschränkung.
    """
    print()
    print(T("  JamMan Solo XT — Speicherkarte verwalten"))
    while True:
        basis = None
        try:
            basis = karte_pruefen(karte)
        except SystemExit:
            print(T("  Keine Karte gefunden unter %s.") % karte)
            neu = input(T("  Anderer Pfad (leer = beenden): ")).strip()
            if not neu:
                return
            karte = neu
            continue

        print()
        print(T("  Karte: %s") % karte)
        print(T("  1  Patches auflisten"))
        print(T("  2  Patch anhören"))
        print(T("  3  Datei aufspielen"))
        print(T("  4  Patch ändern (Stopp, Wiedergabe, Name)"))
        print(T("  5  Patch verschieben"))
        print(T("  6  Patch löschen"))
        print(T("  7  Alles sichern"))
        print(T("  8  Karte auswerfen"))
        print(T("  0  Beenden"))
        wahl = input(T("  Auswahl: ")).strip()

        try:
            if wahl == "0":
                return
            elif wahl == "1":
                befehl_liste(basis)
            elif wahl == "2":
                befehl_abspielen(basis, int(input(T("  Patch-Nummer: ")).strip()))
            elif wahl == "3":
                datei = input(T("  Pfad zur Audiodatei: ")).strip().strip("'")
                name = input(T("  Name (leer = Dateiname): ")).strip() or None
                st = input(T("  Stopp (fade/instant, leer = wie Vorlage): ")).strip() or None
                wg = input(T("  Wiedergabe (loop/single, leer = wie Vorlage): ")).strip() or None
                if input(T("  Wirklich schreiben? (j/n): ")).strip().lower() in ("j", "y", "ja", "yes"):
                    befehl_aufspielen(basis, datei, name, True, st, None, wg)
            elif wahl == "4":
                nr = int(input(T("  Patch-Nummer: ")).strip())
                st = input(T("  Stopp (fade/instant, leer = lassen): ")).strip() or None
                wg = input(T("  Wiedergabe (loop/single, leer = lassen): ")).strip() or None
                nm = input(T("  Neuer Name (leer = lassen): ")).strip() or None
                if input(T("  Wirklich schreiben? (j/n): ")).strip().lower() in ("j", "y", "ja", "yes"):
                    befehl_aendern(basis, nr, True, st, None, wg, nm)
            elif wahl == "5":
                von = int(input(T("  Welcher Patch: ")).strip())
                nach = int(input(T("  An welche Stelle: ")).strip())
                if input(T("  Wirklich verschieben? (j/n): ")).strip().lower() in ("j", "y", "ja", "yes"):
                    befehl_verschieben(basis, von, nach, True)
            elif wahl == "6":
                nr = int(input(T("  Welcher Patch: ")).strip())
                if input(T("  Wirklich löschen? (j/n): ")).strip().lower() in ("j", "y", "ja", "yes"):
                    befehl_loeschen(basis, nr, True)
            elif wahl == "7":
                ziel = input(T("  Zielordner (leer = ~/JamMan-Sicherung): ")).strip() or "~/JamMan-Sicherung"
                befehl_sichern(basis, ziel, True)
            elif wahl == "8":
                befehl_auswerfen(karte)
                return
        except (ValueError, SystemExit) as e:
            print(T("  %s") % (str(e) or "Eingabe nicht verstanden."))
        except KeyboardInterrupt:
            print()
            return


# --------------------------------------------------------------------- GUI

# Farben ausdrücklich setzen. Ohne sie ist im dunklen Erscheinungsbild des
# Systems die Schrift der Liste unsichtbar (dunkel auf dunkel) - genau das
# war am 10.11.2026 zu sehen: Die Patches waren da, man konnte sie nur
# nicht lesen.
FARBE_HINTERGRUND = "#1e1e1e"
FARBE_SCHRIFT = "#f2f2f2"
FARBE_LEISE = "#a0a0a0"
FARBE_LISTE = "#161616"
FARBE_AUSWAHL = "#2f6fb3"
FARBE_TON = "#5fd08a"


def selbsttest(pfad=None):
    """Sagt, was das Fenster sehen müsste. Für Fehlersuche am Telefon.

    Wichtig: tkinter darf hier NICHT zwingend gebraucht werden - sonst
    bricht der Test genau auf dem Rechner ab, auf dem man ihn braucht
    (so geschehen am 10.11.2026 mit Homebrew-Python 3.15 ohne Tk).
    """
    print(T("  System:        %s %s") % (sys.platform,
          os.uname().release if hasattr(os, "uname") else ""))
    print(T("  Python:        %s") % sys.executable)
    print(T("  Fassung:       %s") % sys.version.split()[0])
    try:
        import tkinter as tk
        print(T("  Tk:            %s") % tk.TkVersion)
        try:
            import tkinter
            print(T("  Tk-Modul:      %s") % tkinter.__file__)
        except Exception:
            pass
    except Exception as e:
        print(T("  Tk:            NICHT verfügbar — %s") % e)
        print(T("                 (Fenster geht mit diesem Python nicht;"))
        print(T("                  das Textmenü geht trotzdem: jamman.py menu)"))
    if WINDOWS:
        print(T("  Ton:           winsound (Windows-eigen)"))
    elif MAC:
        print(T("  Ton:           afplay"))
    else:
        programme = [x for x in ("aplay", "paplay", "mpv", "ffplay", "cvlc")
                     if shutil.which(x)]
        print(T("  Ton:           %s") % (", ".join(programme) or "KEINES gefunden"))
    print(T("  Kartenwurzeln: %s") % ", ".join(karten_wurzeln()))
    print(T("  Einstellungen: %s") % os.path.expanduser(EINSTELLUNG_DATEI))
    ff = werkzeug_pfad("ffmpeg")
    print(T("  ffmpeg:        %s") % (ff if ff else
          "fehlt - MP3/M4A lassen sich nicht umwandeln (nur WAV)"))
    print(T("  Schrift:       %s") % (schrift(12)[0],))
    print(T("  Pythons auf diesem Rechner:"))
    for kandidat in ("/usr/bin/python3", "/usr/local/bin/python3",
                     "/opt/homebrew/bin/python3",
                     "/Library/Frameworks/Python.framework/Versions/Current/bin/python3"):
        if not os.path.exists(kandidat):
            continue
        r = subprocess.run([kandidat, "-c",
                            "import tkinter;print(tkinter.TkVersion)"],
                           capture_output=True, text=True)
        stand = ("Tk %s" % r.stdout.strip()) if r.returncode == 0 else T("kein nutzbares Tk")
        print(T("    %-62s %s") % (kandidat, stand))

    kandidaten = karten_kandidaten()
    print(T("  Geprüfte Orte:"))
    for zeile in karten_diagnose():
        print(zeile)
    print(T("  Laufwerke mit JamManSoloXT:"))
    for p, anz, name in kandidaten:
        print(T("    %-24s %3d Patches  %s") % (name, anz, p))
    basis = karte_finden(pfad)
    print(T("  gewählt:       %s") % (basis or "keine"))
    if basis:
        alle = patches(basis)
        print(T("  Patches:       %d") % len(alle))
        if not alle:
            fehler = karte_lesefehler(basis)
            if fehler:
                print(T("  ACHTUNG: das Dateisystem liefert Lesefehler."))
                print(T("  Das ist kein Fehler des Werkzeugs - die Karte oder"))
                print(T("  ihr Einhängepunkt ist beschädigt. Erste Schritte:"))
                if not WINDOWS and not MAC:
                    print(T("    dmesg | tail -20            (Meldungen des Systems)"))
                    print(T("    sudo umount <laufwerk>      (aushängen)"))
                    print(T("    sudo fsck.vfat -n <geraet>  (FAT32, nur prüfen)"))
                    print(T("    sudo exfatfsck <geraet>     (exFAT, nur prüfen)"))
                print(T("    oder die Karte im JamMan einschalten - das Pedal"))
                print(T("    repariert seine Karte beim Einschalten."))
                print(T("  Betroffene Einträge (erste drei): %s") % ", ".join(fehler[:3]))
            print(T("  Inhalt von %s:") % basis)
            try:
                for name in sorted(os.listdir(basis))[:12]:
                    pp = os.path.join(basis, name)
                    try:
                        st = os.stat(pp)
                        art = ("Ordner" if stat.S_ISDIR(st.st_mode) else "Datei")
                        art += " %04o" % (st.st_mode & 0o7777)
                    except OSError as e2:
                        art = "stat-Fehler: %s" % e2.strerror
                    innen = "patch.xml" if os.path.exists(
                        os.path.join(pp, "patch.xml")) else "kein patch.xml"
                    print(T("    %-24r %s, %s") % (name, art, innen))
            except OSError as e:
                print(T("    nicht lesbar: %s") % e)
        else:
            for nummer, name, pfad in alle[:3]:
                print(T("    %-10s %s") % (name, patch_name(pfad)))
            print(T("  erste Zeile im Fenster:"))
            print(T("    %r") % ("  %-10s %-36s %8s" % (
                alle[0][1], patch_name(alle[0][2])[:36], "—")))



def karte_finden(pfad=None, streng=False):
    """Karte suchen: erst der genannte Pfad, dann das wahrscheinlichste
    Laufwerk (Bewertung siehe karten_kandidaten).

    streng=True: Ist ein Pfad genannt, wird NUR dieser benutzt. Weicht die
    Suche nicht aus, kann ein Tippfehler keine fremde Karte verändern.
    """
    kandidaten = []
    if pfad:
        try:
            return karte_pruefen(os.path.expanduser(pfad))
        except SystemExit:
            if streng:
                raise
        kandidaten.append(os.path.expanduser(pfad))
    kandidaten += [p for p, _, _ in karten_kandidaten()]
    if not MAC:
        kandidaten.append(standard_karte())
    for k in kandidaten:
        try:
            return karte_pruefen(k)
        except SystemExit:
            continue
    return None


def fassung_pruefen(zeitlimit=4):
    """Fragt bei GitHub nach der neuesten Fassung. Gibt "3.104" zurück oder None.

    Läuft im Hintergrund; ohne Netz oder bei Zeitüberschreitung kommt None
    zurück und niemand merkt etwas davon. Scheitert die Zertifikatsprüfung,
    wird es einmal ohne Prüfung versucht — hier wird nur eine Versionsnummer
    gelesen, kein Programm geladen.
    """
    import json
    import urllib.request
    adresse = ("https://api.github.com/repos/Suilui1/"
               "jamman-card-tool/releases/latest")
    anfrage = urllib.request.Request(adresse, headers={
        "User-Agent": "jamman-card-tool",
        "Accept": "application/vnd.github+json"})
    for unsicher in (False, True):
        try:
            with _url_oeffnen(anfrage, zeitlimit, unsicher=unsicher) as antwort:
                daten = json.load(antwort)
            tag = (daten.get("tag_name") or "").strip().lstrip("vV")
            return tag or None
        except Exception as fehler:
            if unsicher or "CERTIFICATE_VERIFY_FAILED" not in str(fehler):
                return None
    return None


def eigene_fassung():
    """Nur die Nummer aus FASSUNG: "3.103 (13.11.2026)" -> "3.103"."""
    return FASSUNG.split()[0]


def _numerisch(nummer):
    """„3.110" → (3, 110). Zahlenvergleich, kein Textvergleich.

    Anlaß (17.11.2026): Beim Zählen der Releases war „3.2" textlich größer
    als „3.100" — bei Fassungsnummern wäre derselbe Fehler teuer.
    """
    zahlen = re.findall(r"\d+", str(nummer or ""))
    return tuple(int(z) for z in zahlen[:3]) or (0,)


def _zertifikat_hilfe():
    """Klartext-Hilfe, wenn die Zertifikatsprüfung scheitert.

    Anlaß (18.11.2026): Auf einem Intel-Mac kam trotz aller Ersatzwege
    „CERTIFICATE_VERIFY_FAILED“. Der Text sagt jetzt, was zu tun ist.
    """
    return T("Dem Python fehlen die Zertifikate. Zwei Wege: "
             "im Python-Ordner „Install Certificates.command“ ausführen — "
             "oder „pip install certifi“ — oder die Datei von der "
             "Release-Seite von Hand laden.")


def _ssl_kontext(unsicher=False):
    """SSL-Kontext, der auch ohne „Install Certificates“ funktioniert.

    Anlaß (17.11.2026): Auf einem Mac meldete das Aktualisieren
    „CERTIFICATE_VERIFY_FAILED — unable to get local issuer certificate“.
    Das Python von python.org bringt ohne den Schritt „Install Certificates“
    keine Zertifikate mit. Deshalb: certifi, wenn vorhanden, sonst die
    Zertifikatsdatei des Systems (macOS: /etc/ssl/cert.pem), sonst die
    Voreinstellung.

    ``unsicher=True`` schaltet die Prüfung ab. Das geschieht nur nach
    ausdrücklicher Bestätigung des Nutzers — am 18.11.2026 schlug die Prüfung
    auf einem Intel-Mac fehl, obwohl alle bekannten Dateien durchprobiert
    wurden.
    """
    import ssl

    if unsicher:
        return ssl._create_unverified_context()

    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    for pfad in ("/etc/ssl/cert.pem",
                 "/usr/local/etc/openssl/cert.pem",
                 "/opt/homebrew/etc/openssl/cert.pem",
                 "/etc/pki/tls/certs/ca-bundle.crt",
                 "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.exists(pfad):
            try:
                return ssl.create_default_context(cafile=pfad)
            except Exception:
                continue
    return ssl.create_default_context()


def _url_oeffnen(anfrage, zeitlimit, unsicher=False):
    """urlopen mit dem Zertifikats-Kontext von oben."""
    import urllib.request
    return urllib.request.urlopen(anfrage, timeout=zeitlimit,
                                  context=_ssl_kontext(unsicher))


def neueste_fassung(zeitlimit=15):
    """Neueste Veröffentlichung bei GitHub.

    Rückgabe: {"tag", "url", "groesse", "fehler"}. Ist "tag" leer, steht in
    "fehler", warum es nicht ging — vorher meldete das Werkzeug nur „GitHub ist
    nicht erreichbar", ohne den Grund zu nennen (17.11.2026).
    """
    import json
    import urllib.request
    adresse = ("https://api.github.com/repos/Suilui1/"
               "jamman-card-tool/releases/latest")
    anfrage = urllib.request.Request(adresse, headers={
        "User-Agent": "jamman-card-tool",
        "Accept": "application/vnd.github+json"})
    geprueft = True
    try:
        try:
            with _url_oeffnen(anfrage, zeitlimit) as antwort:
                daten = json.load(antwort)
        except Exception as erster:
            if "CERTIFICATE_VERIFY_FAILED" not in str(erster):
                raise
            with _url_oeffnen(anfrage, zeitlimit, unsicher=True) as antwort:
                daten = json.load(antwort)
            geprueft = False
        tag = (daten.get("tag_name") or "").strip().lstrip("vV")
        dateien = [a for a in daten.get("assets") or []
                   if str(a.get("name", "")).lower().endswith(".zip")]
        if tag:
            return {"tag": tag,
                    "url": dateien[0]["browser_download_url"] if dateien else "",
                    "groesse": dateien[0].get("size") if dateien else 0,
                    "zertifikat": geprueft,
                    "fehler": ""}
        fehler = "kein Tag in der Antwort"
    except Exception as e:
        fehler = str(e)

    # Ersatzweg ohne API: die Seite /releases/latest leitet auf den Tag um
    try:
        anfrage = urllib.request.Request(
            "https://github.com/Suilui1/jamman-card-tool/releases/latest",
            headers={"User-Agent": "jamman-card-tool"})
        with _url_oeffnen(anfrage, zeitlimit, unsicher=not geprueft) as antwort:
            ziel = antwort.geturl() or ""
        tag = ziel.rstrip("/").split("/")[-1].lstrip("vV")
        if tag and tag[:1].isdigit():
            return {"tag": tag,
                    "url": ("https://github.com/Suilui1/jamman-card-tool/"
                            "releases/download/v%s/jamman-card-tool-%s.zip"
                            % (tag, tag)),
                    "groesse": 0,
                    "zertifikat": geprueft,
                    "fehler": ""}
    except Exception as e:
        fehler = "%s · %s" % (fehler, e)

    return {"tag": "", "url": "", "groesse": 0, "fehler": fehler}


def aktualisieren(melden=None):
    """Holt die neueste Fassung und ersetzt die Programmdateien.

    Gibt (erfolg, text) zurück. Die Einstellungen des Nutzers liegen in
    ~/.jamman-werkzeug.cnf und werden nicht angefaßt; die ersetzten Dateien
    kommen vorher in einen Ordner „sicherung-vor-<alte Fassung>".
    """
    import shutil
    import tempfile
    import urllib.request
    import zipfile

    sag = melden or (lambda text: None)
    stand = neueste_fassung()
    if not stand.get("tag"):
        grund = stand.get("fehler") or T("unbekannter Grund")
        if "CERTIFICATE_VERIFY_FAILED" in grund:
            grund += "\n\n" + T("Dem Python fehlen die Zertifikate. Auf macOS "
                                 "hilft „Install Certificates.command“ im "
                                 "Python-Ordner, oder: pip install certifi")
        return False, T("GitHub ist nicht erreichbar: %s") % grund
    if _numerisch(stand["tag"]) <= _numerisch(eigene_fassung()):
        return False, (T("Sie benutzen bereits die neueste Fassung (%s).")
                       % eigene_fassung())
    if not stand["url"]:
        return False, (T("Zur Fassung %s gibt es keine Datei zum Herunterladen.")
                       % stand["tag"])

    sag(T("Lade Fassung %s …") % stand["tag"])
    ziel_ordner = os.path.dirname(os.path.abspath(__file__))
    anzahl = 0
    with tempfile.TemporaryDirectory() as temp:
        paket = os.path.join(temp, "neu.zip")
        anfrage = urllib.request.Request(stand["url"], headers={
            "User-Agent": "jamman-card-tool"})
        ohne_pruefung = not stand.get("zertifikat", True)
        if ohne_pruefung:
            sag(T("Achtung: das Zertifikat ließ sich nicht prüfen — die "
                  "Datei wurde ohne Prüfung geladen."))
        try:
            with _url_oeffnen(anfrage, 120, unsicher=ohne_pruefung) as antwort:
                with open(paket, "wb") as datei:
                    shutil.copyfileobj(antwort, datei)
        except Exception as fehler:
            if "CERTIFICATE_VERIFY_FAILED" in str(fehler) and not ohne_pruefung:
                # Zweiter Versuch ohne Prüfung: der Nutzer hat das Aktualisieren
                # ausdrücklich verlangt, und die Datei wird anschließend auf
                # Größe und Lesbarkeit geprüft (18.11.2026, Intel-Mac).
                try:
                    with _url_oeffnen(anfrage, 120, unsicher=True) as antwort:
                        with open(paket, "wb") as datei:
                            shutil.copyfileobj(antwort, datei)
                except Exception as zweiter:
                    return False, (T("Herunterladen fehlgeschlagen: %s") % zweiter
                                   + "\n\n" + _zertifikat_hilfe())
                sag(T("Achtung: das Zertifikat ließ sich nicht prüfen — die "
                      "Datei wurde ohne Prüfung geladen."))
            else:
                return False, (T("Herunterladen fehlgeschlagen: %s") % fehler
                               + "\n\n" + _zertifikat_hilfe())

        # Größe gegen die Angabe von GitHub vergleichen
        erwartet = int(stand.get("groesse") or 0)
        if erwartet:
            geladen = os.path.getsize(paket)
            if geladen != erwartet:
                return False, (T("Die Datei ist unvollständig: %d statt %d Bytes.")
                               % (geladen, erwartet))

        try:
            archiv = zipfile.ZipFile(paket)
        except zipfile.BadZipFile:
            return False, T("Die heruntergeladene Datei ist kein gültiges ZIP.")

        with archiv:
            kaputt = archiv.testzip()
            if kaputt:
                return False, T("Die Datei ist beschädigt (%s).") % kaputt
            namen = [n.replace("\\", "/") for n in archiv.namelist()]
            if "jamman.py" not in namen:
                return False, T("In der Datei fehlt jamman.py.")

            # Gehört das Paket wirklich zur angekündigten Fassung?
            quelle = archiv.read("jamman.py").decode("utf-8", "replace")
            treffer = re.search(r'FASSUNG\s*=\s*"([^"]+)"', quelle)
            # Nur die ersten beiden Zahlen vergleichen: in FASSUNG steht
            # „3.109 (17.11.2026)", im Tag nur „3.109".
            if (not treffer
                    or _numerisch(treffer.group(1))[:2] != _numerisch(stand["tag"])[:2]):
                return False, (T("Die Datei gehört nicht zur Fassung %s.")
                               % stand["tag"])

            # Alte Fassung sichern, bevor etwas überschrieben wird
            sicherung = os.path.join(ziel_ordner,
                                     "sicherung-vor-" + eigene_fassung())
            for name in namen:
                if name.endswith("/") or "__pycache__" in name:
                    continue
                if name.startswith("/") or ".." in name.split("/"):
                    continue
                alt = os.path.join(ziel_ordner, name)
                if os.path.exists(alt):
                    unter = os.path.join(sicherung, name)
                    os.makedirs(os.path.dirname(unter), exist_ok=True)
                    try:
                        shutil.copy2(alt, unter)
                    except OSError:
                        pass

            for name in namen:
                if name.endswith("/") or "__pycache__" in name:
                    continue
                if name.startswith("/") or ".." in name.split("/"):
                    continue
                ziel = os.path.join(ziel_ordner, name)
                try:
                    os.makedirs(os.path.dirname(ziel), exist_ok=True)
                    with archiv.open(name) as quelle_datei:
                        with open(ziel, "wb") as ziel_datei:
                            shutil.copyfileobj(quelle_datei, ziel_datei)
                except OSError as fehler:
                    return False, (T("Die Programmdateien lassen sich hier nicht "
                                     "ersetzen: %s") % fehler)
                anzahl += 1

    return True, (T("Fassung %s ist installiert (%d Dateien). "
                    "Bitte das Fenster schließen und neu starten.")
                  % (stand["tag"], anzahl))


# ---------------------------------------------------------------- USB-Zugang
def _greenglass():
    """Das USB-Modul laden (liegt neben dieser Datei)."""
    import importlib.util
    pfad = os.path.join(os.path.dirname(os.path.abspath(__file__)), "greenglass.py")
    spec = importlib.util.spec_from_file_location("greenglass", pfad)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def _pedal_lesen(g, speicher, fortschritt=None):
    """Patches eines Speichers holen: [(platz, name, groesse)] — nur lesend.

    fortschritt(anzahl, gesamt, bisher) wird nach jedem fünften Patch gerufen,
    damit eine lange Liste nicht am Ende auf einmal erscheint.
    """
    eigenschaften = g.eigenschaften()
    if speicher != "intern" and not eigenschaften.get("RemovableMediaIsPresent"):
        raise RuntimeError(T("Im Pedal steckt keine Karte."))
    basis = eigenschaften["InternalPatchFolderPath" if speicher == "intern"
                           else "RemovablePatchFolderPath"]
    _, ordner = g.ordner(basis)
    ordner = sorted(ordner)
    ergebnis = []
    for i, d in enumerate(ordner, 1):
        name = ""
        groesse = 0
        try:
            xml = g.lies_datei(basis + d + "/patch.xml").decode("utf-8", "replace")
            import re as _re
            m = _re.search(r"<PatchName>(.*?)</PatchName>", xml)
            if m:
                name = m.group(1)
        except Exception:
            pass
        ergebnis.append((d, name, groesse))
        if fortschritt and (i % 5 == 0 or i == len(ordner)):
            fortschritt(i, len(ordner), list(ergebnis))
    return basis, ergebnis


def _raeume_auf(wurzel, geschrieben):
    """Nur die in diesem Lauf neu angelegten Dateien entfernen.

    Der gewählte Ordner selbst bleibt immer stehen. Dateien, die schon vorher
    dort lagen, werden nicht angefaßt — sonst löschte ein Abbruch eine ältere
    Sicherung (16.11.2026).
    """
    entfernt = 0
    for pfad in geschrieben:
        try:
            if os.path.isfile(pfad):
                os.remove(pfad)
                entfernt += 1
        except OSError:
            pass
    # leere Ordner von unten nach oben, aber nie den Wurzelordner
    for aktuell, ordner, dateien in os.walk(wurzel, topdown=False):
        if os.path.abspath(aktuell) == os.path.abspath(wurzel):
            continue
        if not ordner and not dateien:
            try:
                os.rmdir(aktuell)
            except OSError:
                pass
        elif not os.listdir(aktuell):
            try:
                os.rmdir(aktuell)
            except OSError:
                pass
    return entfernt


def _ordnergroesse(pfad):
    """Wie groß ist ein Ordner auf dem Rechner? (für die Fortschrittsanzeige)"""
    summe = 0
    for wurzel, _, dateien in os.walk(pfad):
        for f in dateien:
            try:
                summe += os.path.getsize(os.path.join(wurzel, f))
            except OSError:
                pass
    return summe


def _patch_holen(g, pfad, ziel, stopp=None, nur=None, protokoll=None,
                 ueberschreiben=False, zaehler=None):
    """Einen Patch samt Phrasen in einen Ordner auf dem Rechner kopieren.

    stopp: wird vor jeder Datei und in der Leseschleife gefragt — damit
    „Abbrechen“ auch mitten in einer großen WAV greift.
    """
    daten, unter = g.ordner(pfad)
    os.makedirs(ziel, exist_ok=True)
    angefangen = []
    try:
        for f in daten:
            # nur="xml": erst die kleinen Beschreibungen (schnelle Liste)
            # nur="ton": später die WAVs im Hintergrund
            if nur == "xml" and not f.lower().endswith(".xml"):
                continue
            if nur == "ton" and f.lower().endswith(".xml"):
                continue
            if stopp is not None and stopp():
                raise g.Abgebrochen()
            ziel_datei = os.path.join(ziel, f)
            war_schon_da = os.path.exists(ziel_datei)
            if war_schon_da and not ueberschreiben:
                # schon vorhanden: nur ersetzen, wenn es im Pedal anders ist
                # (Größe oder Änderungszeit). Sonst bleibt die Datei stehen —
                # ein zweiter Lauf ist dann sehr schnell (16.11.2026).
                try:
                    fern = g.info(pfad.rstrip("/") + "/" + f)
                    gleich = (os.path.getsize(ziel_datei) == fern["groesse"])
                except Exception:
                    gleich = True
                if gleich and not f.lower().endswith(".xml"):
                    # unverändert und keine Beschreibung: nicht noch einmal
                    # holen. (Die Zuweisung an `ueberschreiben` stand hier
                    # früher fälschlich und ließ danach ALLE Dateien neu
                    # laden — am 16.11.2026 gefunden.)
                    if zaehler is not None:
                        zaehler["uebersprungen"] = zaehler.get("uebersprungen", 0) + 1
                    continue
                war_schon_da = False
            angefangen.append((ziel_datei, war_schon_da))
            # erst als .part schreiben, dann umbenennen: bricht der Vorgang ab,
            # bleibt keine halbe WAV zurück, die später wie eine echte aussieht
            inhalt = g.lies_datei(pfad.rstrip("/") + "/" + f, stopp=stopp)
            # Einmal wiederholen, wenn Ordner oder .part zwischenzeitlich
            # verschwunden sind (17.11.2026: „No such file … .part -> …wav“).
            for versuch in (1, 2):
                try:
                    os.makedirs(ziel, exist_ok=True)
                    with open(ziel_datei + ".part", "wb") as fp:
                        fp.write(inhalt)
                    os.replace(ziel_datei + ".part", ziel_datei)
                    break
                except FileNotFoundError:
                    if versuch == 2:
                        raise RuntimeError(
                            T("Die Datei konnte nicht geschrieben werden: %s")
                            % ziel_datei)
            if zaehler is not None:
                zaehler["gelesen"] = zaehler.get("gelesen", 0) + 1
            if protokoll is not None and not war_schon_da:
                protokoll.append(ziel_datei)
        for u in unter:
            if stopp is not None and stopp():
                raise g.Abgebrochen()
            _patch_holen(g, pfad.rstrip("/") + "/" + u, os.path.join(ziel, u),
                         stopp=stopp, nur=nur, protokoll=protokoll,
                         ueberschreiben=ueberschreiben, zaehler=zaehler)
    except g.Abgebrochen:
        for d, war_schon_da in angefangen:
            # nur entfernen, was dieser Lauf NEU angelegt hat — eine frühere
            # Sicherung im selben Ordner darf ein Abbruch nicht löschen
            for rest in (d, d + ".part"):
                try:
                    if rest.endswith(".part") or not war_schon_da:
                        if os.path.exists(rest):
                            os.remove(rest)
                except OSError:
                    pass
        raise
    return True


def _patch_schreiben(g, quell_ordner, ziel_pfad, meldung=None, stopp=None):
    """Einen Ordner vom Rechner ins Pedal schreiben (rekursiv).

    Schreibt Datei für Datei und meldet den Fortschritt. Vorher muß der
    Aufrufer sichern, was am Ziel schon liegt — diese Funktion überschreibt.
    """
    for eintrag in sorted(os.listdir(quell_ordner)):
        quelle = os.path.join(quell_ordner, eintrag)
        ziel = ziel_pfad.rstrip("/") + "/" + eintrag
        if stopp is not None and stopp():
            raise g.Abgebrochen()
        if os.path.isdir(quelle):
            g.erstelle_ordner(ziel)
            _patch_schreiben(g, quelle, ziel, meldung=meldung, stopp=stopp)
        else:
            with open(quelle, "rb") as fp:
                daten = fp.read()
            if meldung:
                meldung(eintrag, len(daten))
            g.schreibe_datei(ziel, daten, stopp=stopp)
    return True


def sicherung_begrenzen(platz_ordner, behalten=5):
    """Je Platz nur die letzten fünf Sicherungen behalten.

    Anlaß (17.11.2026, Wunsch des Nutzers): „vor-ueberschreiben" wuchs mit
    jedem Schreibvorgang. Jede Sicherung liegt jetzt in einem eigenen Ordner
    mit Zeitstempel; ältere werden hier entfernt — nur innerhalb dieses
    Ordners und nur die eigenen Einträge (Namen beginnen mit einer Ziffer).
    """
    import shutil

    try:
        eintraege = [d for d in os.listdir(platz_ordner)
                     if d[:1].isdigit()
                     and os.path.isdir(os.path.join(platz_ordner, d))]
    except OSError:
        return 0
    eintraege.sort()
    entfernt = 0
    for alt_name in (eintraege[:-behalten] if len(eintraege) > behalten else []):
        try:
            shutil.rmtree(os.path.join(platz_ordner, alt_name))
            entfernt += 1
        except OSError:
            pass
    return entfernt



def setlists_anpassen(ordner, zuege, stempel=None):
    """Setlist-Dateien im Ordner auf neue Platznummern umschreiben.

    Eine Setlist ist eine Textdatei mit „PatchNN<TAB>Name“ je Zeile (siehe
    _setlist_speichern). Nach dem Aufrücken zeigen die Nummern auf andere
    Patches; hier werden sie mitgezogen. Jede geänderte Datei wird vorher
    daneben gesichert. Rückgabe: [(Datei, Anzahl geänderter Zeilen)].
    """
    import re as _re

    zuordnung = {"Patch%02d" % alt: "Patch%02d" % neu for alt, neu in zuege}
    if not zuordnung:
        return []
    stempel = stempel or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    muster = _re.compile(r"^(Patch\d{1,3})(\s*\t.*)?$", _re.IGNORECASE)
    geaendert = []
    for name in sorted(os.listdir(ordner)):
        if not name.lower().endswith(".txt"):
            continue
        pfad = os.path.join(ordner, name)
        if not os.path.isfile(pfad):
            continue
        try:
            with open(pfad, encoding="utf-8") as fp:
                zeilen = fp.read().splitlines()
        except Exception:
            continue
        neu_zeilen, anzahl = [], 0
        for zeile in zeilen:
            treffer = muster.match(zeile)
            if treffer:
                alt = treffer.group(1)
                schluessel = ("Patch%02d" % int(alt[5:]))
                ziel = zuordnung.get(schluessel)
                if ziel and ziel != schluessel:
                    zeile = ziel + (treffer.group(2) or "")
                    anzahl += 1
            neu_zeilen.append(zeile)
        if not anzahl:
            continue
        sicherung = os.path.join(ordner, "%s.vor-aufruecken-%s.txt"
                                 % (os.path.splitext(name)[0], stempel))
        try:
            shutil.copy2(pfad, sicherung)
        except Exception:
            pass
        with open(pfad, "w", encoding="utf-8") as fp:
            fp.write("\n".join(neu_zeilen) + "\n")
        geaendert.append((name, anzahl))
    return geaendert


FIRMWARE_DATEIEN = [
    # Was die Firmware ausmacht (aus dem Speicherabbild vom 17.11.2026).
    # .filetable fehlt absichtlich: das ist der Verzeichnisindex des
    # Dateisystems und wird vom Pedal selbst gepflegt.
    "/mnt/nandfs/usr/IOMgr",
    "/mnt/nandfs/usr/JamManUI",
    "/mnt/nandfs/usr/loopengine",
    "/mnt/nandfs/usr/usbserial",
    "/mnt/nandfs/lib/devb-sd-at91sam9g35",
    "/mnt/nandfs/lib/devc-serusart",
    "/mnt/nandfs/lib/devc-serusb_g",
    "/mnt/nandfs/lib/devu-g15-at91sam_g.so",
    # JamManSoloXT.xml bleibt draußen: darin stehen Einstellungen des Nutzers
    # (CurrentPatch, PedalTempo, AutoRecord), keine Firmware. Am 18.11.2026
    # geklärt, nachdem die Datei gelesen wurde.
]

FIRMWARE_ORDNER = ["/mnt/nandfs/usr/attributes"]

# Einzelstücke je Gerät: unit_guid.attr ist die Kennung DIESES Pedals.
# Sie darf nicht auf ein anderes Gerät wandern (18.11.2026 in der geholten
# attributes-Liste gefunden).
FIRMWARE_NICHT_KOPIEREN = {"unit_guid.attr"}

FIRMWARE_REIHENFOLGE = [
    # Zuerst die Bibliotheken, dann die Programme, zuletzt der USB-Treiber:
    # bricht es ab, ist die Verbindung bis zum Schluß erhalten.
    "/mnt/nandfs/lib/devb-sd-at91sam9g35",
    "/mnt/nandfs/lib/devc-serusart",
    "/mnt/nandfs/lib/devu-g15-at91sam_g.so",
    "/mnt/nandfs/usr/IOMgr",
    "/mnt/nandfs/usr/loopengine",
    "/mnt/nandfs/usr/JamManUI",
    "/mnt/nandfs/lib/devc-serusb_g",
    "/mnt/nandfs/usr/usbserial",
]


def firmware_holen(g, ziel_ordner, melden=None, stopp=None):
    """Die Systemdateien eines Pedals auf den Rechner holen (nur lesend).

    Legt sie unter ziel_ordner mit ihrem Pfad an (usr/IOMgr usw.), damit
    firmware_schreiben() sie zurücklegen kann. Rückgabe: Liste (Pfad, Größe).
    """
    geholt = []
    for pfad in FIRMWARE_DATEIEN:
        if stopp is not None and stopp():
            raise g.Abgebrochen()
        if melden:
            melden(pfad)
        try:
            groesse = g.info(pfad).get("groesse", 0)
        except Exception:
            continue                      # Datei gibt es auf diesem Pedal nicht
        daten = g.lies_datei(pfad, stopp=stopp)
        if len(daten) != groesse:
            raise RuntimeError(T("%s kam unvollständig (%d von %d Bytes).")
                               % (pfad, len(daten), groesse))
        hiesig = os.path.join(ziel_ordner, pfad.lstrip("/").replace("/", os.sep))
        os.makedirs(os.path.dirname(hiesig), exist_ok=True)
        with open(hiesig, "wb") as fp:
            fp.write(daten)
        geholt.append((pfad, groesse))
    for ordner in FIRMWARE_ORDNER:
        try:
            dateien, _ = g.ordner(ordner.rstrip("/") + "/")
        except Exception:
            continue
        for name in dateien:
            pfad = ordner.rstrip("/") + "/" + name
            if stopp is not None and stopp():
                raise g.Abgebrochen()
            daten = g.lies_datei(pfad, stopp=stopp)
            hiesig = os.path.join(ziel_ordner, pfad.lstrip("/").replace("/", os.sep))
            os.makedirs(os.path.dirname(hiesig), exist_ok=True)
            with open(hiesig, "wb") as fp:
                fp.write(daten)
            geholt.append((pfad, len(daten)))
    return geholt


def _datei_ersetzen(g, pfad, daten, stopp=None):
    """Eine laufende Programmdatei ersetzen, ohne sie zu überschreiben.

    Anlaß (18.11.2026): „Das Pedal meldet einen Fehler: Resource busy" — QNX
    läßt eine Datei nicht überschreiben, solange sie läuft. Deshalb erst
    daneben schreiben (…neu), dann umbenennen; klappt das nicht, wird die
    laufende Datei nach …alt weggeschoben und die neue an ihren Platz gelegt.
    """
    neu_pfad = pfad + ".neu"
    try:
        g.loesche(neu_pfad)
    except Exception:
        pass
    g.schreibe_datei(neu_pfad, daten, stopp=stopp)
    try:
        g.benenne_um(neu_pfad, pfad, ueberschreiben=True)
        return "umbenannt"
    except Exception:
        pass
    try:
        g.benenne_um(pfad, pfad + ".alt", ueberschreiben=True)
    except Exception:
        pass
    g.benenne_um(neu_pfad, pfad, ueberschreiben=True)
    return "alt weggeschoben"


def firmware_schreiben(g, quell_ordner, melden=None, stopp=None):
    """Die Systemdateien aus einem geholten Ordner ins Pedal schreiben.

    Reihenfolge siehe FIRMWARE_REIHENFOLGE; nach jeder Datei wird die Größe
    verglichen. Rückgabe: Liste (Pfad, geschrieben, geprüft).
    """
    ergebnis = []
    pfade = list(FIRMWARE_REIHENFOLGE)
    attributes = os.path.join(quell_ordner, "mnt", "nandfs", "usr", "attributes")
    if os.path.isdir(attributes):
        pfade.append("/mnt/nandfs/usr/attributes/")
    for pfad in pfade:
        if stopp is not None and stopp():
            raise g.Abgebrochen()
        ist_ordner = pfad.endswith("/")
        hiesig = os.path.join(quell_ordner, pfad.lstrip("/").replace("/", os.sep))
        if ist_ordner:
            g.erstelle_ordner(pfad.rstrip("/"))
            for name in sorted(os.listdir(hiesig)):
                if name in FIRMWARE_NICHT_KOPIEREN:
                    continue
                ganz = pfad + name
                with open(os.path.join(hiesig, name), "rb") as fp:
                    daten = fp.read()
                if melden:
                    melden(ganz, len(daten))
                g.schreibe_datei(ganz, daten, stopp=stopp)
                ergebnis.append((ganz, len(daten), True))
            continue
        if not os.path.exists(hiesig):
            continue
        with open(hiesig, "rb") as fp:
            daten = fp.read()
        if melden:
            melden(pfad, len(daten))
        _datei_ersetzen(g, pfad, daten, stopp=stopp)
        try:
            danach = g.info(pfad).get("groesse", -1)
        except Exception:
            danach = -1
        ergebnis.append((pfad, len(daten), danach == len(daten)))
    return ergebnis


def jamlists_lesen(db_pfad):
    """JamLists aus der Bibliothek von JamManager XT lesen.

    Aufbau (aus dem Programmtext, 17.11.2026): JamManager.db.sqlite mit den
    Tabellen jamlists(id, color, name) und loops(…, jamlistid, "Loop #", …).
    Eine JamList besteht aus 200 Zeilen — eine je Platz des Pedals; nur die
    belegten tragen einen Namen.

    Rückgabe: Liste von dicts {id, name, platz: [(nummer, name, datei)]}
    """
    import sqlite3

    if not os.path.exists(db_pfad):
        raise RuntimeError(T("Die Datei %s fehlt.") % db_pfad)
    liste = []
    verbindung = sqlite3.connect(db_pfad)
    try:
        for kennung, name in verbindung.execute(
                "SELECT id, name FROM jamlists ORDER BY id"):
            plaetze = []
            for nummer, loop_name, datei in verbindung.execute(
                    'SELECT "Loop #", Name, filepath FROM loops '
                    'WHERE jamlistid=? AND Name IS NOT NULL ORDER BY "Loop #"',
                    (kennung,)):
                plaetze.append((nummer, loop_name or "", datei or ""))
            liste.append({"id": kennung, "name": name or "", "platz": plaetze})
    finally:
        verbindung.close()
    return liste


def jamlist_schreiben(db_pfad, name, eintraege, farbe=0):
    """Eine JamList in eine JamManager-Bibliothek eintragen.

    eintraege: [(platz, name, datei)] — Plätze 1…200. Fehlende Plätze bleiben
    leer, wie in der Vorlage des Herstellers. Rückgabe: Kennung der neuen Liste.
    """
    import sqlite3

    verbindung = sqlite3.connect(db_pfad)
    try:
        kennung = verbindung.execute(
            "INSERT INTO jamlists (color, name) VALUES (?, ?)",
            (int(farbe), name)) .lastrowid
        vorhanden = {int(platz): (loop_name, datei)
                     for platz, loop_name, datei in eintraege}
        for platz in range(1, 201):
            loop_name, datei = vorhanden.get(platz, (None, None))
            verbindung.execute(
                'INSERT INTO loops (jamlistid, isvalid, inlibrary, "Loop #", '
                'Name, Rating, filepath) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (kennung, "false", "false", platz, loop_name, 0, datei))
        verbindung.commit()
    finally:
        verbindung.close()
    return kennung


def speicherabbild(g, ziel_datei, wurzel="/", tiefe=8, hoechstens=5000,
                   melden=None, stopp=None,
                   ohne=("/proc", "/dev", "/mnt/sdfs")):
    """Verzeichnisbaum des Pedals als Textdatei festhalten (nur lesend).

    Zweck (17.11.2026): Zwei Pedale mit unterschiedlicher Firmware sollen
    verglichen werden, ohne etwas zu schreiben. Aufgenommen werden Pfad und
    Größe jeder Datei; das reicht, um Firmware-Dateien zu finden und zu sehen,
    was sich zwischen zwei Fassungen unterscheidet.

    Rückgabe: Anzahl der Einträge.
    """
    zeilen = []
    stapel = [(wurzel.rstrip("/") + "/", 0)]
    while stapel:
        pfad, tiefe_jetzt = stapel.pop(0)
        if stopp and stopp():
            raise RuntimeError(T("Abgebrochen."))
        if tiefe_jetzt > tiefe or len(zeilen) >= hoechstens:
            continue
        # /proc enthält Laufzeitspeicher (z. B. /proc/1/as), /dev die Geräte
        # und /mnt/sdfs die SD-Karte des Nutzers. Nichts davon gehört in einen
        # Firmware-Vergleich (18.11.2026: 222 Unterschiede, fast alle von der
        # eingelegten Karte).
        if any(pfad.startswith(anf) for anf in ohne):
            continue
        try:
            dateien, ordner = g.ordner(pfad)
        except Exception:
            continue
        for name in dateien:
            voll = pfad + name
            try:
                groesse = g.info(voll).get("groesse", 0)
            except Exception:
                groesse = 0
            zeilen.append("%s\t%d\tD" % (voll, groesse))
            if melden and len(zeilen) % 25 == 0:
                melden(len(zeilen), voll)
        for name in ordner:
            voll = pfad + name
            zeilen.append("%s\t0\tO" % voll)
            stapel.append((voll + "/", tiefe_jetzt + 1))
    with open(ziel_datei, "w", encoding="utf-8") as fp:
        fp.write("# JamMan Card Tool — " + T("Speicherabbild") + "\n")
        fp.write("# %s\n" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        fp.write("# Pfad\tGroesse\tArt (D=Datei, O=Ordner)\n")
        fp.write("\n".join(zeilen) + "\n")
    return len(zeilen)


def speicherabbilder_vergleichen(datei_a, datei_b):
    """Zwei Speicherabbilder vergleichen. Rückgabe: Liste von Sätzen."""
    def lesen(pfad):
        werte = {}
        with open(pfad, encoding="utf-8") as fp:
            for zeile in fp:
                if zeile.startswith("#") or not zeile.strip():
                    continue
                teile = zeile.rstrip("\n").split("\t")
                if len(teile) >= 3:
                    werte[teile[0]] = (int(teile[1] or 0), teile[2])
        return werte

    a, b = lesen(datei_a), lesen(datei_b)
    nur_a = sorted(set(a) - set(b))
    nur_b = sorted(set(b) - set(a))
    anders = sorted(p for p in set(a) & set(b) if a[p][0] != b[p][0])
    return nur_a, nur_b, [(p, a[p][0], b[p][0]) for p in anders]


def plaetze_zusammenruecken(g, basis):
    """Belegte Plätze lückenlos nach vorn ziehen.

    Gibt [(alt, neu)] zurück — die Reihenfolge der Patches bleibt, nur die
    Plätze rücken auf. Wird erst nach einer Rückfrage ausgeführt, weil sich
    damit die Platznummern aller späteren Patches ändern.
    """
    import re as _re

    _, ordner = g.ordner(basis)
    muster = _re.compile(r"^Patch(\d{1,3})$", _re.IGNORECASE)
    belegt = sorted(int(muster.match(name).group(1)) for name in ordner
                    if muster.match(name))
    zuege = []
    for ziel, alt in enumerate(belegt, 1):
        if ziel != alt:
            zuege.append((alt, ziel))
    return zuege


def patches_aufs_pedal(g, basis, quell, ziel_ordner, melden=None, stopp=None):
    """Alle Patch-Ordner aus `quell` in den Speicher `basis` schreiben.

    Rückgabe: (geschrieben, übersprungen, gesichert).
    Belegte Plätze werden vorher nach `vor-ueberschreiben/<Platz>/<Stempel>`
    geholt (fünf Stände je Platz bleiben), unveränderte übersprungen.
    """
    import re as _re

    sag = melden or (lambda text: None)
    muster = _re.compile(r"^Patch(\d{1,3})$", _re.IGNORECASE)
    patches = []
    for name in sorted(os.listdir(quell)):
        treffer = muster.match(name)
        if treffer and os.path.isdir(os.path.join(quell, name)):
            patches.append((name, "Patch%02d" % int(treffer.group(1))))

    geschrieben = uebersprungen = gesichert = 0
    for i, (name, platz) in enumerate(patches, 1):
        if stopp is not None and stopp():
            raise g.Abgebrochen()
        sag("%s %d/%d — %s" % (T("Schreibe"), i, len(patches), platz))
        quell_patch = os.path.join(quell, name)
        ziel_pfad = basis + platz

        # unverändert? (gleiche Dateien, gleiche Größen im Pedal)
        try:
            _, vorhandene = g.ordner(ziel_pfad)
            gleich = bool(vorhandene)
            for wurzel, _, dateien in os.walk(quell_patch):
                for datei in dateien:
                    if not gleich:
                        break
                    hiesig = os.path.join(wurzel, datei)
                    fern = ziel_pfad + hiesig[len(quell_patch):].replace(os.sep, "/")
                    try:
                        if g.info(fern)["groesse"] != os.path.getsize(hiesig):
                            gleich = False
                    except Exception:
                        gleich = False
            if gleich:
                uebersprungen += 1
                continue
        except Exception:
            pass

        # belegt? vorher sichern und leeren
        try:
            _, vorhandene = g.ordner(ziel_pfad)
            if vorhandene:
                platz_ordner = os.path.join(ziel_ordner, "vor-ueberschreiben", platz)
                sicherung = os.path.join(
                    platz_ordner, datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
                sag(T("Sichere den alten Platz …") + " " + platz)
                _patch_holen(g, ziel_pfad, sicherung)
                sicherung_begrenzen(platz_ordner)
                gesichert += 1
                g.loesche(ziel_pfad, ist_ordner=True, rekursiv=True)
        except Exception:
            pass

        g.erstelle_ordner(ziel_pfad)
        _patch_schreiben(g, quell_patch, ziel_pfad, stopp=stopp)
        geschrieben += 1
    return geschrieben, uebersprungen, gesichert


def _hol_wurzel(ziel, geraetename, speicher):
    """Zielordner für einen Hole-Lauf — je Lauf ein eigener Unterordner.

    Anlaß (16.11.2026): Alles landete in <Ziel>/<Gerät>. Lag dort schon eine
    frühere Kopie, wurden vorhandene Dateien übersprungen — und im
    Hauptfenster standen danach die alten Namen. Jetzt bekommt jeder Lauf
    seinen eigenen Ordner mit Datum und Speicher.
    """
    # Fester Ordner JE SPEICHER (nicht je Lauf): so werden beim zweiten Mal
    # nur die fehlenden oder geänderten Dateien geholt. Mit einem neuen
    # Ordner je Lauf wurde jedes Mal alles wieder geladen (16.11.2026).
    teil = "intern" if speicher == "intern" else "karte"
    return os.path.join(ziel, teil, geraetename)


def _als_zip(g, basis, platzliste, ziel_datei, meldung=None, stopp=None):
    """Alle Patches eines Speichers als ZIP sichern (ohne Umweg über den Ordner).

    Der Inhalt entspricht dem Ordner, den „Alle holen“ anlegt: <Gerät>/PatchNN/…
    """
    import zipfile
    geraetename = os.path.basename(basis.rstrip("/")) or "JamMan"
    with zipfile.ZipFile(ziel_datei, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        def sammle(pfad, innen):
            dateien, ordner = g.ordner(pfad)
            for f in dateien:
                if stopp is not None and stopp():
                    raise g.Abgebrochen()
                if meldung:
                    meldung(f, 0)
                daten = g.lies_datei(pfad + "/" + f, stopp=stopp)
                # WAV lässt sich nicht sinnvoll komprimieren; das Packen
                # kostete bei großen Loops Minuten und ließ „Abbrechen“
                # wirkungslos erscheinen (16.11.2026).
                art = (zipfile.ZIP_STORED if f.lower().endswith(".wav")
                       else zipfile.ZIP_DEFLATED)
                z.writestr(geraetename + "/" + innen + f, daten, compress_type=art)
            for o in ordner:
                sammle(pfad.rstrip("/") + "/" + o, innen + o + "/")
        for i, platz in enumerate(platzliste, 1):
            if stopp is not None and stopp():
                raise g.Abgebrochen()
            if meldung:
                meldung(platz, i)
            sammle(basis + platz, platz + "/")
    return True


def frage_ja_nein_konsole(text=None):
    """Rückfrage in der Konsole (für das Schreiben von Firmware)."""
    if text is None:
        text = T("Firmware-Dateien jetzt ins Pedal schreiben? Ein Abbruch "
                 "mittendrin kann das Pedal unbrauchbar machen. [ja/nein] ")
    try:
        antwort = input("  " + text).strip().lower()
    except EOFError:
        return False
    return antwort in ("j", "ja", "y", "yes")


def _setlist_speichern(patchliste, indizes, ziel_datei, geraetename=""):
    """Eine Auswahl von Patches als Setlist (Textdatei) sichern."""
    with open(ziel_datei, "w", encoding="utf-8") as fp:
        fp.write("# " + T("JamMan Card Tool") + " — " + T("Karte und interner Speicher") + "\n")
        if geraetename:
            fp.write("# Gerät: %s\n" % geraetename)
        for i in indizes:
            platz, name, _ = patchliste[i]
            fp.write("%s\t%s\n" % (platz, name or ""))
    return True


def _setlist_lesen(pfad):
    """Setlist einlesen: [(Platz, Name)] — Zeilen mit # sind Bemerkungen."""
    eintraege = []
    with open(pfad, encoding="utf-8") as fp:
        for zeile in fp:
            zeile = zeile.rstrip("\n")
            if not zeile.strip() or zeile.lstrip().startswith("#"):
                continue
            teile = zeile.split("\t")
            eintraege.append((teile[0].strip(), teile[1] if len(teile) > 1 else ""))
    return eintraege


_USB_GERAET = {"obj": None}


def _geraet_holen(g_modul, anschuss=None):
    """Genau EIN Zugang zum Pedal je Programm.

    Zwei gleichzeitig geöffnete Anschlüsse (etwa nach „Verbindung neu
    aufbauen", während noch ein Strang las) brachten das Pedal am 16.11.2026
    zum Schweigen: es bekam Rahmen von zwei Sprechern und verlor die
    Ausrichtung. „Hole Beschreibungen …" blieb dann stehen.
    """
    if _USB_GERAET["obj"] is None:
        _USB_GERAET["obj"] = g_modul.JamManUSB(anschuss)
    return _USB_GERAET["obj"]


def _geraet_schliessen():
    try:
        if _USB_GERAET["obj"] is not None:
            _USB_GERAET["obj"].close()
    except Exception:
        pass
    _USB_GERAET["obj"] = None


_USB_FENSTER = {"fenster": None}


def usb_fenster(haupfenster, oeffne_karte=None):
    """Fenster „Pedal über USB": Speicher wählen, Patches ansehen, sichern.

    Regeln, die aus dem ersten Versuch am 16.11.2026 stammen:
      - nur EIN Fenster. Ein zweiter Klick holt das vorhandene nach vorn,
        statt ein weiteres zu öffnen (es gingen mehrere auf, das Programm
        wurde träge).
      - tkinter wird ausschließlich im Hauptstrang benutzt. Der Arbeitsstrang
        legt seine Ergebnisse in eine Warteschlange, die Oberfläche liest sie
        mit after() ab. Ein Tk-Aufruf aus einem anderen Strang kann das
        Fenster blockieren.
      - der gefundene Anschluß wird gemerkt, damit beim nächsten Mal nicht
        wieder alle Anschlüsse durchprobiert werden (das kostete Sekunden).
    """
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog

    offen = _USB_FENSTER.get("fenster")
    if offen is not None:
        try:
            offen.deiconify()
            offen.lift()
            offen.focus_force()
            return
        except Exception:
            _USB_FENSTER["fenster"] = None

    try:
        g_modul = _greenglass()
    except Exception as e:
        messagebox.showerror(T("Pedal über USB"),
                             T("Das USB-Modul fehlt: greenglass.py muß neben jamman.py liegen.")
                             + "\n" + str(e))
        return
    # Wenn beim Aufbau etwas schiefgeht, soll es zu sehen sein und nicht
    # nur im Terminal stehen (das Fenster blieb am 16.11.2026 unsichtbar).
    try:
        return _usb_fenster_bauen(haupfenster, g_modul, oeffne_karte)
    except Exception as e:
        import traceback
        messagebox.showerror(T("Pedal über USB"),
                             T("Fehler beim Öffnen:") + "\n" + str(e) + "\n\n"
                             + traceback.format_exc()[-600:])
        return

def _neuer_patch_xml(geraetename, name):
    """patch.xml für einen neuen Patch bauen (Aufbau wie vom Pedal erzeugt)."""
    import uuid
    name = (name or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return ('<?xml version="1.0" encoding="UTF-8" ?>\n'
            '<JamManPatch xmlns="http://schemas.digitech.com/JamMan/Patch" '
            'device="%s" version="1">\n'
            '    <PatchName>%s</PatchName>\n'
            '    <RhythmType>Silence</RhythmType>\n'
            '    <StopMode>StopInstantly</StopMode>\n'
            '    <SettingsVersion>0</SettingsVersion>\n'
            '    <ID>%s</ID>\n'
            '    <Metadata>\n'
            '    <Description />\n'
            '    <Artist />\n'
            '    <Genre>Unspecified</Genre>\n'
            '    <Rating>0</Rating>\n'
            '    <Tags />\n'
            '</Metadata>\n'
            '</JamManPatch>\n' % (geraetename, name, uuid.uuid4())).encode("utf-8")


def _neue_phrase_xml():
    """phrase.xml für eine neue Phrase (wie vom Pedal erzeugt)."""
    import uuid
    return ('<?xml version="1.0" encoding="UTF-8" ?>\n'
            '<JamManPhrase xmlns="http://schemas.digitech.com/JamMan/Phrase" version="1">\n'
            '    <BeatsPerMinute>90.0000000000</BeatsPerMinute>\n'
            '    <BeatsPerMeasure>4</BeatsPerMeasure>\n'
            '    <BpmValidated>0</BpmValidated>\n'
            '    <IsLoop>1</IsLoop>\n'
            '    <IsReversed>0</IsReversed>\n'
            '    <SettingsVersion>0</SettingsVersion>\n'
            '    <AudioVersion>0</AudioVersion>\n'
            '    <ID>%s</ID>\n'
            '    <Metadata />\n'
            '</JamManPhrase>\n' % (uuid.uuid4())).encode("utf-8")


# Mögliche Werte der Pedal-Einstellungen. Sie stammen aus dem Protokoll-
# dokument von RenanDiaz/jamman-manager (docs/usb-protocol.md) und von dem,
# was das Pedal selbst zurückmeldet. Unbekannte Werte werden zusätzlich
# angeboten, damit nichts verlorengeht.
WERTE_WARTESCHLANGE = ["RevertToSaved", "TransitionIfStopped"]
WERTE_TASTE = ["UndoRedo", "Reverse", "Tempo", ""]


def _frage_ja(fenster, titel, text):
    """Ja/Nein-Frage, die nicht hinter dem Vordergrundfenster verschwindet.

    Anlaß (16.11.2026): Das USB-Fenster steht dauerhaft im Vordergrund. Eine
    Rückfrage, die dahinter aufging, blieb unsichtbar — das Werkzeug wirkte
    eingefroren, obwohl es nur auf ein Ja oder Nein wartete. Vor der Frage
    wird der Vordergrund kurz abgegeben, danach wieder gesetzt.
    """
    from tkinter import messagebox
    vorne = False
    try:
        vorne = bool(fenster.attributes("-topmost"))
        if vorne:
            fenster.attributes("-topmost", False)
    except Exception:
        vorne = False
    try:
        return frage_ja_nein(fenster, titel, text)
    finally:
        try:
            if vorne:
                fenster.attributes("-topmost", True)
                fenster.lift()
        except Exception:
            pass


def _sicherungs_laeufe(ziel):
    """Alle Hole- und Sicherungsläufe unter einem Zielordner.

    Rückgabe: [(Pfad, Name, Änderungszeit, Größe in Bytes)] — nur direkte
    Unterordner, nie der Zielordner selbst.
    """
    import time as _zeit
    laeufe = []
    if not ziel or not os.path.isdir(ziel):
        return laeufe
    for name in sorted(os.listdir(ziel)):
        pfad = os.path.join(ziel, name)
        if not os.path.isdir(pfad):
            continue
        try:
            laeufe.append((pfad, name, os.path.getmtime(pfad), _ordnergroesse(pfad)))
        except OSError:
            pass
    laeufe.sort(key=lambda x: x[2], reverse=True)   # neueste zuerst
    return laeufe


def fenster_aufraeumen(ziel, haupfenster):
    """Sicherungen im Zielordner ansehen und alte löschen.

    Anlaß (16.11.2026): Mit jedem Hole-Lauf wächst der Zielordner; nach
    mehreren Läufen lagen hunderte Megabyte alter Kopien herum.
    Gelöscht wird nur, was ausdrücklich angekreuzt ist — und nur innerhalb
    des Zielordners.
    """
    import shutil
    import tkinter as tk
    from tkinter import messagebox

    if not ziel or not os.path.isdir(ziel):
        messagebox.showinfo(T("Aufräumen"),
                            T("Der Zielordner ist noch nicht angelegt."))
        return

    laeufe = _sicherungs_laeufe(ziel)
    if not laeufe:
        messagebox.showinfo(T("Aufräumen"), T("Im Zielordner liegen keine Sicherungen."))
        return

    f = tk.Toplevel(haupfenster)
    f.title(T("Sicherungen aufräumen"))
    f.configure(bg=FARBE_HINTERGRUND)
    f.geometry("720x520+%d+%d" % (haupfenster.winfo_rootx() + 50,
                                  haupfenster.winfo_rooty() + 50))
    try:
        f.attributes("-topmost", True)
    except Exception:
        pass

    tk.Label(f, text=T("Sicherungen im Zielordner"), bg=FARBE_HINTERGRUND,
             fg=FARBE_SCHRIFT, font=schrift(12, fest=True)).pack(pady=(12, 2))
    tk.Label(f, text=ziel, bg=FARBE_HINTERGRUND, fg=FARBE_LEISE,
             font=schrift(9), wraplength=660).pack(pady=(0, 8))

    rahmen = tk.Frame(f, bg=FARBE_HINTERGRUND)
    rahmen.pack(fill="both", expand=True, padx=16)

    anzeige = tk.Listbox(rahmen, bg=FARBE_LISTE, fg=FARBE_SCHRIFT,
                         selectbackground=FARBE_AUSWAHL, selectmode="extended",
                         exportselection=False, font=schrift(10))
    anzeige.pack(side="left", fill="both", expand=True)
    roll = tk.Scrollbar(rahmen, command=anzeige.yview)
    roll.pack(side="right", fill="y")
    anzeige.config(yscrollcommand=roll.set)

    import time as _zeit
    for pfad, name, wann, groesse in laeufe:
        zusatz = ""
        if name == "vor-ueberschreiben":
            zusatz = "   " + T("(für Rückgängig nötig)")
        anzeige.insert("end", "%s   %s   %6.1f MB%s" %
                       (_zeit.strftime("%d.%m.%Y %H:%M", _zeit.localtime(wann)),
                        name[:28], groesse / 1048576.0, zusatz))

    gesamt = sum(g for _, _, _, g in laeufe)
    kopf = tk.Label(f, text=T("%d Sicherungen · %.1f MB") % (len(laeufe), gesamt / 1048576.0),
                    bg=FARBE_HINTERGRUND, fg=FARBE_LEISE, font=schrift(9))
    kopf.pack(pady=(6, 2))

    def markieren_neueste(n):
        anzeige.selection_clear(0, "end")
        for i in range(min(n, anzeige.size())):
            anzeige.selection_set(i)

    zeile = tk.Frame(f, bg=FARBE_HINTERGRUND)
    zeile.pack(fill="x", padx=16, pady=(4, 0))
    tk.Label(zeile, text=T("Neueste behalten:"), bg=FARBE_HINTERGRUND,
             fg=FARBE_SCHRIFT).pack(side="left")
    for n in (1, 2, 3, 5):
        tk.Button(zeile, text=str(n), width=3,
                  command=lambda n=n: markieren_neueste(n)).pack(side="left", padx=2)

    def loeschen():
        indizes = list(anzeige.curselection())
        if not indizes:
            messagebox.showinfo(T("Hinweis"), T("Erst Sicherungen in der Liste markieren."))
            return
        weg = [laeufe[i] for i in indizes]
        menge = sum(g for _, _, _, g in weg) / 1048576.0
        if any(name == "vor-ueberschreiben" for _, name, _, _ in weg):
            if not _frage_ja(f, 
                    T("Sicherungen aufräumen"),
                    T("Darunter ist die Sicherung für Rückgängig. Danach läßt "
                      "sich der letzte Schreibvorgang nicht mehr zurücknehmen. "
                      "Trotzdem löschen?")):
                return
        if not _frage_ja(f, 
                T("Sicherungen aufräumen"),
                T("%d Sicherungen mit %.1f MB werden endgültig gelöscht. Fortsetzen?")
                % (len(weg), menge)):
            return
        fehler = 0
        for pfad, name, _, _ in weg:
            # Sicherheitsnetz: nur löschen, was direkt im Zielordner liegt
            if os.path.dirname(os.path.abspath(pfad)) != os.path.abspath(ziel):
                fehler += 1
                continue
            try:
                shutil.rmtree(pfad)
            except OSError:
                fehler += 1
        if fehler:
            messagebox.showwarning(T("Aufräumen"),
                                   T("%d Sicherungen konnten nicht gelöscht werden.") % fehler)
        f.destroy()
        fenster_aufraeumen(ziel, haupfenster)

    tk.Button(f, text=T("Markierte löschen"), command=loeschen).pack(pady=(10, 4))
    tk.Button(f, text=T("Schließen"), command=f.destroy).pack(pady=(0, 12))


def datei_auswahl(haupfenster, titel, muster, start=None, nur_ordner=False,
                  vorgabe_name=None):
    """Eigener Dateiauswahl-Dialog: immer im Vordergrund, ohne Systemdialog.

    Anlaß (17.11.2026): zenity erschien hinter dem Werkzeug (das Fenster steht
    auf -topmost), und der Tk-Dialog war unübersichtlich. Dieser Dialog gehört
    zum Werkzeug, steht deshalb davor und blendet versteckte Einträge aus.

    Rückgabe: vollständiger Pfad oder "" (abgebrochen).
    """
    import tkinter as tk
    from tkinter import messagebox

    endungen = [m.lstrip("*").lower() for m in muster.split() if m.startswith("*.")]
    def lesbar(pfad):
        """Kann dieser Ordner überhaupt aufgelistet werden?"""
        try:
            os.listdir(pfad)
            return True
        except OSError:
            return False

    ordner = os.path.abspath(os.path.expanduser(start)) if start else os.path.expanduser("~")
    if not os.path.isdir(ordner) or not lesbar(ordner):
        # Nicht lesbar (Windows: „Zugriff verweigert" bei verknüpften
        # Bibliotheksordnern, 17.11.2026) — dann etwas Nehmbares vorschlagen.
        for ersatz in (os.path.join(os.path.expanduser("~"), "Music"),
                       os.path.join(os.path.expanduser("~"), "Musik"),
                       os.path.expanduser("~")):
            if os.path.isdir(ersatz) and lesbar(ersatz):
                ordner = ersatz
                break
        else:
            ordner = os.path.expanduser("~")

    fenster = tk.Toplevel(haupfenster)
    fenster.title(titel)
    fenster.configure(bg=FARBE_HINTERGRUND)
    fenster.geometry("960x560")
    fenster.minsize(700, 420)
    wahl = {"pfad": ""}

    # Pfad als Eingabefeld: unter Linux liegen Karten unter /media/<Benutzer>/…,
    # das tippt niemand gern von Hand nach.
    pfad_zeile = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    pfad_zeile.pack(fill="x", padx=12, pady=(10, 4))
    kopf_var = tk.StringVar(value="")
    kopf = tk.Entry(pfad_zeile, textvariable=kopf_var, bg=FARBE_LISTE,
                    fg=FARBE_SCHRIFT, insertbackground=FARBE_SCHRIFT,
                    relief="flat", font=schrift(10))
    kopf.pack(side="left", fill="x", expand=True, ipady=3)

    zustand_text = tk.StringVar(value="")
    tk.Label(fenster, textvariable=zustand_text, bg=FARBE_HINTERGRUND, fg="#d98c4a",
             font=schrift(9), anchor="w", justify="left",
             wraplength=880).pack(fill="x", padx=12)

    leiste = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    leiste.pack(fill="x", padx=12, pady=(0, 6))

    inhalt = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    inhalt.pack(fill="both", expand=True, padx=12)

    links = tk.Frame(inhalt, bg=FARBE_HINTERGRUND, width=340)
    links.pack(side="left", fill="both", expand=True)
    links.pack_propagate(False)
    tk.Label(links, text=T("Ordner"), bg=FARBE_HINTERGRUND, fg=FARBE_LEISE,
             font=schrift(9), anchor="w").pack(fill="x")
    ordner_liste = tk.Listbox(links, bg=FARBE_LISTE, fg=FARBE_SCHRIFT,
                              selectbackground=FARBE_AUSWAHL, relief="flat",
                              font=schrift(11), activestyle="none")
    ordner_liste.pack(fill="both", expand=True, pady=(2, 0))

    rechts = tk.Frame(inhalt, bg=FARBE_HINTERGRUND)
    rechts.pack(side="left", fill="both", expand=True, padx=(10, 0))
    tk.Label(rechts, text=T("Dateien"), bg=FARBE_HINTERGRUND, fg=FARBE_LEISE,
             font=schrift(9), anchor="w").pack(fill="x")
    datei_liste = tk.Listbox(rechts, bg=FARBE_LISTE, fg=FARBE_SCHRIFT,
                             selectbackground=FARBE_AUSWAHL, relief="flat",
                             font=schrift(11), activestyle="none")
    datei_liste.pack(fill="both", expand=True, pady=(2, 0))

    unten = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    unten.pack(fill="x", padx=12, pady=(8, 12))
    tk.Label(unten, text=T("Auswahl:"), bg=FARBE_HINTERGRUND, fg=FARBE_LEISE,
             font=schrift(9)).pack(side="left", padx=(0, 8))
    pfad_var = tk.StringVar(value="")
    tk.Entry(unten, textvariable=pfad_var, bg=FARBE_LISTE, fg=FARBE_SCHRIFT,
             insertbackground=FARBE_SCHRIFT, relief="flat",
             font=schrift(11)).pack(side="left", fill="x", expand=True, ipady=4)

    def gehe_zu(ereignis=None):
        ziel = os.path.abspath(os.path.expanduser(kopf_var.get().strip()))
        if os.path.isdir(ziel):
            fuellen(ziel)

    kopf.bind("<Return>", gehe_zu)

    def fuellen(pfad):
        kopf_var.set(pfad)
        ordner_liste.delete(0, "end")
        datei_liste.delete(0, "end")
        try:
            eintraege = sorted(os.listdir(pfad))
        except OSError as fehler:
            # Kein Abbruch: Hinweis zeigen, Listen leeren, „Nach oben" bleibt.
            zustand_text.set(T("Dieser Ordner ist nicht lesbar: %s") % fehler)
            return
        zustand_text.set("")
        if os.path.dirname(pfad) != pfad:
            ordner_liste.insert("end", "..")
        for name in eintraege:
            if name.startswith("."):
                continue
            voll = os.path.join(pfad, name)
            if os.path.isdir(voll):
                ordner_liste.insert("end", name + "/")
            elif not nur_ordner and any(name.lower().endswith(e) for e in endungen):
                datei_liste.insert("end", name)
        fenster.pfad = pfad

    def hinein(ereignis=None):
        gewaehlt = ordner_liste.curselection()
        if not gewaehlt:
            return
        name = ordner_liste.get(gewaehlt[0])
        ziel = (os.path.dirname(fenster.pfad) if name == ".."
                else os.path.join(fenster.pfad, name.rstrip("/")))
        if os.path.isdir(ziel):
            fuellen(ziel)

    def markiere(ereignis=None):
        gewaehlt = datei_liste.curselection()
        if gewaehlt:
            pfad_var.set(os.path.join(fenster.pfad, datei_liste.get(gewaehlt[0])))

    name_eingabe = None
    if vorgabe_name is not None:
        name_zeile = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
        name_zeile.pack(fill="x", padx=12, pady=(0, 6))
        tk.Label(name_zeile, text=T("Dateiname:"), bg=FARBE_HINTERGRUND,
                 fg=FARBE_LEISE).pack(side="left")
        name_var = tk.StringVar(value=vorgabe_name)
        name_eingabe = tk.Entry(name_zeile, textvariable=name_var,
                                bg=FARBE_LISTE, fg=FARBE_SCHRIFT,
                                insertbackground=FARBE_SCHRIFT, relief="flat",
                                font=schrift(11))
        name_eingabe.pack(side="left", fill="x", expand=True, padx=(8, 0), ipady=3)

    def oeffnen():
        if vorgabe_name is not None:
            name = name_var.get().strip()
            if not name:
                messagebox.showinfo(T("Hinweis"), T("Bitte einen Dateinamen angeben."),
                                    parent=fenster)
                return
            ziel = os.path.join(fenster.pfad, name)
            if os.path.exists(ziel) and not frage_ja_nein(
                    fenster, T("Überschreiben"), T("%s gibt es schon. Ersetzen?") % ziel):
                return
            wahl["pfad"] = ziel
            fenster.destroy()
            return
        if nur_ordner:
            wahl["pfad"] = fenster.pfad
            fenster.destroy()
            return
        pfad = pfad_var.get().strip()
        if pfad and os.path.isfile(pfad):
            wahl["pfad"] = pfad
            fenster.destroy()
        else:
            messagebox.showinfo(T("Hinweis"), T("Bitte eine Datei auswählen."),
                                parent=fenster)

    def hoch():
        fuellen(os.path.dirname(fenster.pfad))

    tk.Button(leiste, text=T("Nach oben"), command=hoch).pack(side="left")
    tk.Button(leiste, text=T("Speichern") if vorgabe_name is not None else T("Öffnen"),
              command=oeffnen).pack(side="right")
    tk.Button(leiste, text=T("Abbrechen"),
              command=fenster.destroy).pack(side="right", padx=(0, 8))

    ordner_liste.bind("<Double-Button-1>", hinein)
    ordner_liste.bind("<Return>", hinein)
    datei_liste.bind("<<ListboxSelect>>", markiere)
    datei_liste.bind("<Double-Button-1>", lambda e: oeffnen())
    datei_liste.bind("<Return>", lambda e: oeffnen())

    fuellen(ordner)

    # Das Elternfenster (USB-Fenster) hält sich mit -topmost nach vorn und
    # springt sonst über diesen Dialog (17.11.2026). Also für die Dauer des
    # Dialogs zurücknehmen und danach wiederherstellen.
    eltern_vorne = False
    try:
        if haupfenster is not None:
            eltern_vorne = bool(haupfenster.attributes("-topmost"))
            if eltern_vorne:
                haupfenster.attributes("-topmost", False)
    except Exception:
        eltern_vorne = False

    try:
        fenster.attributes("-topmost", True)
        fenster.lift()
        fenster.focus_force()
        fenster.grab_set()
        # Manche Fenstermanager stellen das Elternfenster erst danach wieder
        # nach vorn — ein zweites Anheben kurz darauf hilft.
        fenster.after(120, lambda: (fenster.lift(), fenster.focus_force()))
    except Exception:
        pass

    haupfenster.wait_window(fenster)
    if eltern_vorne:
        try:
            haupfenster.attributes("-topmost", True)
        except Exception:
            pass
    return wahl["pfad"]


def ordner_waehlen(haupfenster, titel, start=None):
    """Ordner auswählen — mit dem eigenen Dialog (siehe datei_auswahl)."""
    return datei_auswahl(haupfenster, titel, "", start, nur_ordner=True)


def datei_speichern(haupfenster, titel, vorgabe_name, start=None, muster=""):
    """Dateiname und Ordner wählen — eigener Dialog, siehe datei_auswahl."""
    return datei_auswahl(haupfenster, titel, muster, start,
                         vorgabe_name=vorgabe_name)


def datei_waehlen(haupfenster, titel, muster, start=None, endung=".txt"):
    """Datei auswählen — über den eigenen Dialog des Werkzeugs.

    Anlaß (17.11.2026): Der Tk-Dialog war unübersichtlich (enge Spalten, alle
    versteckten Dateien), und zenity erschien hinter dem Fenster, weil das
    Werkzeug auf -topmost steht. Der eigene Dialog gehört zum Werkzeug und
    steht damit zwangsläufig davor.
    """
    return datei_auswahl(haupfenster, titel, muster, start)


def sperrbare_widgets(wurzel, ausnahmen=()):
    """Alle Knöpfe und Menüs unter `wurzel`, außer den genannten Beschriftungen.

    Wird gebraucht, damit während eines Hole- oder Schreibvorgangs nichts
    dazwischenfunkt (17.11.2026).
    """
    treffer = []

    def lauf(widget):
        for kind in widget.winfo_children():
            art = kind.winfo_class()
            if art in ("Button", "Menubutton", "Radiobutton"):
                beschriftung = ""
                if art != "Radiobutton":
                    try:
                        beschriftung = str(kind.cget("text"))
                    except Exception:
                        beschriftung = ""
                if beschriftung not in ausnahmen:
                    treffer.append(kind)
            lauf(kind)

    lauf(wurzel)
    return treffer


def beschreibung_fenster(haupfenster, pfad):
    """Name, Interpret, Genre, Bewertung, Stichwörter, Text und Tempo ändern.

    Diese Felder stehen im patch.xml/phrase.xml des Pedals, hatten aber keine
    Oberfläche (17.11.2026: JamManager XT kann sie, das Werkzeug schrieb immer
    „Unspecified“, Bewertung 0 und 90 BPM).
    """
    import tkinter as tk
    from tkinter import messagebox

    ist = patch_beschreibung(pfad)
    fenster = tk.Toplevel(haupfenster)
    fenster.title(T("Beschreibung"))
    fenster.configure(bg=FARBE_HINTERGRUND)
    fenster.resizable(False, False)

    eltern_vorne = False
    try:
        eltern_vorne = bool(haupfenster.attributes("-topmost"))
        if eltern_vorne:
            haupfenster.attributes("-topmost", False)
    except Exception:
        eltern_vorne = False

    felder = {}
    zeilen = [
        ("name", T("Name"), 40),
        ("artist", T("Interpret"), 40),
        ("genre", T("Genre"), 24),
        ("tags", T("Stichwörter"), 40),
        ("description", T("Beschreibung"), 56),
    ]
    for schluessel, beschriftung, breite in zeilen:
        zeile = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
        zeile.pack(fill="x", padx=16, pady=3)
        tk.Label(zeile, text=beschriftung + ":", width=13, anchor="w",
                 bg=FARBE_HINTERGRUND, fg=FARBE_LEISE).pack(side="left")
        var = tk.StringVar(value=str(ist.get(schluessel) or ""))
        tk.Entry(zeile, textvariable=var, width=breite, bg=FARBE_LISTE,
                 fg=FARBE_SCHRIFT, insertbackground=FARBE_SCHRIFT,
                 relief="flat").pack(side="left", ipady=3)
        felder[schluessel] = var

    unten = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    unten.pack(fill="x", padx=16, pady=(6, 14))

    tk.Label(unten, text=T("Bewertung") + ":", bg=FARBE_HINTERGRUND,
             fg=FARBE_LEISE).pack(side="left")
    bewertung = tk.StringVar(value=str(ist.get("rating") or "0"))
    tk.Spinbox(unten, from_=0, to=5, width=4, textvariable=bewertung,
               bg=FARBE_LISTE, fg=FARBE_SCHRIFT, relief="flat",
               insertbackground=FARBE_SCHRIFT).pack(side="left", padx=(6, 18))

    tk.Label(unten, text=T("Tempo (BPM)") + ":", bg=FARBE_HINTERGRUND,
             fg=FARBE_LEISE).pack(side="left")
    tempo = tk.StringVar(value=("%.1f" % ist["bpm"]) if ist.get("bpm") else "")
    tk.Spinbox(unten, from_=30, to=300, width=6, textvariable=tempo,
               bg=FARBE_LISTE, fg=FARBE_SCHRIFT, relief="flat",
               insertbackground=FARBE_SCHRIFT).pack(side="left", padx=(6, 18))

    def speichern():
        werte = {k: v.get().strip() for k, v in felder.items()}
        werte["rating"] = (bewertung.get().strip() or "0")
        try:
            werte["bpm"] = float(tempo.get().replace(",", "."))                 if tempo.get().strip() else None
        except ValueError:
            werte["bpm"] = None
        try:
            patch_beschreibung_setzen(pfad, werte)
        except Exception as e:
            messagebox.showerror(T("Fehler"), str(e), parent=fenster)
            return
        fenster.destroy()

    tk.Button(unten, text=T("Speichern"), command=speichern).pack(side="right")
    tk.Button(unten, text=T("Abbrechen"),
              command=fenster.destroy).pack(side="right", padx=(0, 8))

    fenster.bind("<Return>", lambda e: speichern())
    fenster.bind("<Escape>", lambda e: fenster.destroy())
    try:
        fenster.attributes("-topmost", True)
        fenster.lift()
        fenster.focus_force()
        fenster.grab_set()
    except Exception:
        pass
    haupfenster.wait_window(fenster)
    if eltern_vorne:
        try:
            haupfenster.attributes("-topmost", True)
        except Exception:
            pass


def _frage_auswahl(haupfenster, titel, moeglichkeiten):
    """Eine von mehreren Möglichkeiten wählen; gibt den Index oder None."""
    import tkinter as tk

    fenster = tk.Toplevel(haupfenster)
    fenster.title(titel)
    fenster.configure(bg=FARBE_HINTERGRUND)
    eltern_vorne = False
    try:
        eltern_vorne = bool(haupfenster.attributes("-topmost"))
        if eltern_vorne:
            haupfenster.attributes("-topmost", False)
    except Exception:
        eltern_vorne = False
    auswahl = {"index": None}
    liste = tk.Listbox(fenster, height=min(12, max(3, len(moeglichkeiten))),
                       width=44, bg=FARBE_LISTE, fg=FARBE_SCHRIFT,
                       selectbackground=FARBE_AUSWAHL, relief="flat",
                       exportselection=False)
    for wert in moeglichkeiten:
        liste.insert("end", wert)
    liste.pack(padx=16, pady=12)
    liste.selection_set(0)
    liste.focus_set()

    def uebernehmen(*_):
        gewaehlt = liste.curselection()
        if gewaehlt:
            auswahl["index"] = gewaehlt[0]
        fenster.destroy()

    leiste = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    leiste.pack(fill="x", padx=16, pady=(0, 12))
    tk.Button(leiste, text=T("Abbrechen"), command=fenster.destroy).pack(side="right")
    tk.Button(leiste, text=T("\u00dcbernehmen"),
              command=uebernehmen).pack(side="right", padx=(0, 8))
    liste.bind("<Double-Button-1>", uebernehmen)
    fenster.bind("<Return>", uebernehmen)
    fenster.bind("<Escape>", lambda e: fenster.destroy())
    try:
        fenster.attributes("-topmost", True)
        fenster.lift()
        fenster.grab_set()
    except Exception:
        pass
    haupfenster.wait_window(fenster)
    if eltern_vorne:
        try:
            haupfenster.attributes("-topmost", True)
        except Exception:
            pass
    return auswahl["index"]


def frage_ja_nein(haupfenster, titel, text):
    """Ja/Nein-Frage mit eigenen Knöpfen.

    Anlaß (17.11.2026): Die Systemdialoge beschriften ihre Knöpfe selbst
    („Yes“/„No“), während der Text deutsch war. Hier stehen T("Ja") und
    T("Nein"), in jeder Sprache.
    """
    import tkinter as tk

    fenster = tk.Toplevel(haupfenster)
    fenster.title(titel)
    fenster.configure(bg=FARBE_HINTERGRUND)
    fenster.resizable(False, False)
    antwort = {"ja": False}

    eltern_vorne = False
    try:
        eltern_vorne = bool(haupfenster.attributes("-topmost"))
        if eltern_vorne:
            haupfenster.attributes("-topmost", False)
    except Exception:
        eltern_vorne = False

    tk.Label(fenster, text=text, bg=FARBE_HINTERGRUND, fg=FARBE_SCHRIFT,
             font=schrift(11), wraplength=520, justify="left").pack(
        padx=18, pady=(18, 12))

    def schliessen(ja):
        antwort["ja"] = ja
        fenster.destroy()

    leiste = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    leiste.pack(fill="x", padx=18, pady=(0, 16))
    tk.Button(leiste, text=T("Nein"), width=10,
              command=lambda: schliessen(False)).pack(side="right")
    tk.Button(leiste, text=T("Ja"), width=10,
              command=lambda: schliessen(True)).pack(side="right", padx=(0, 8))

    fenster.bind("<Return>", lambda e: schliessen(True))
    fenster.bind("<Escape>", lambda e: schliessen(False))
    try:
        fenster.attributes("-topmost", True)
        fenster.lift()
        fenster.focus_force()
        fenster.grab_set()
    except Exception:
        pass

    haupfenster.wait_window(fenster)
    if eltern_vorne:
        try:
            haupfenster.attributes("-topmost", True)
        except Exception:
            pass
    return antwort["ja"]


def _anleitung_fenster(haupfenster, text, titel=None):
    """Langen Hilfetext in einem eigenen Fenster: scrollbar, markierbar, kopierbar.

    Anlaß (17.11.2026): Der ffmpeg-Text landete in der Statuszeile des
    USB-Fensters — abgeschnitten, und Befehle ließen sich nicht kopieren.
    """
    import tkinter as tk
    from tkinter import filedialog

    titel = titel or T("Anleitung")
    fenster = tk.Toplevel(haupfenster)
    fenster.title(titel)
    fenster.configure(bg=FARBE_HINTERGRUND)
    fenster.geometry("780x560")
    fenster.minsize(560, 360)

    # Das Elternfenster hält sich mit -topmost nach vorn. Ohne Gegenmaßnahme
    # läge dieses Fenster dahinter (17.11.2026 die gleiche Ursache wie beim
    # Hinweis-Dialog). Also: Eltern kurz zurücknehmen, dieses Fenster nach
    # vorn, beim Schließen alles zurückgeben.
    eltern_vorne = False
    try:
        eltern_vorne = bool(haupfenster.attributes("-topmost"))
        if eltern_vorne:
            haupfenster.attributes("-topmost", False)
    except Exception:
        eltern_vorne = False
    try:
        fenster.attributes("-topmost", True)
    except Exception:
        pass

    def schliessen():
        try:
            fenster.destroy()
        finally:
            if eltern_vorne:
                try:
                    haupfenster.attributes("-topmost", True)
                except Exception:
                    pass

    fenster.protocol("WM_DELETE_WINDOW", schliessen)

    rahmen = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    rahmen.pack(fill="both", expand=True, padx=14, pady=(14, 8))
    rolle = tk.Scrollbar(rahmen)
    rolle.pack(side="right", fill="y")
    bereich = tk.Text(rahmen, wrap="word", yscrollcommand=rolle.set,
                      bg=FARBE_LISTE, fg=FARBE_SCHRIFT, insertbackground=FARBE_SCHRIFT,
                      relief="flat", padx=12, pady=10,
                      font=("Menlo", 11) if MAC else ("Consolas", 10))
    bereich.pack(side="left", fill="both", expand=True)
    rolle.config(command=bereich.yview)
    bereich.insert("1.0", text)
    # Nur lesen, aber bedienbar: Ein gesperrtes Textfeld ("disabled") nimmt
    # auch keine Kopier-Taste an (17.11.2026 nachgemessen). Deshalb bleibt es
    # bedienbar, und Tippen wird abgewiesen — Kopieren und Blättern nicht.
    def nur_lesen(ereignis):
        if ereignis.state & 0x4 or ereignis.state & 0x8 or ereignis.state & 0x10:
            return None                      # Strg/Cmd: kopieren, alles markieren
        if ereignis.keysym in ("Left", "Right", "Up", "Down", "Home", "End",
                               "Prior", "Next", "Shift_L", "Shift_R",
                               "Control_L", "Control_R", "Command"):
            return None                      # Blättern und Markieren
        return "break"                       # alles andere abweisen

    bereich.bind("<Key>", nur_lesen)

    def kopiere(ereignis=None):
        """Auswahl kopieren — eigene Bindung, unabhängig vom Systemdialog.

        Die Standardbindung des Textfelds greift nicht zuverlässig, wenn das
        Feld den Fokus nicht hat (17.11.2026 nachgemessen).
        """
        try:
            auswahl = bereich.get("sel.first", "sel.last")
        except tk.TclError:
            auswahl = bereich.get("1.0", "end-1c")
        try:
            fenster.clipboard_clear()
            fenster.clipboard_append(auswahl)
            melde_kurz(T("In die Zwischenablage kopiert."))
        except Exception as fehler:
            melde_kurz(str(fehler))
        return "break"

    for folge in ("<Command-c>", "<Command-C>", "<Control-c>", "<Control-C>"):
        bereich.bind(folge, kopiere)
    bereich.focus_set()

    leiste = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    leiste.pack(fill="x", padx=14, pady=(0, 12))
    zustand = tk.Label(leiste, text="", bg=FARBE_HINTERGRUND, fg=FARBE_TON)

    def melde_kurz(text):
        zustand.config(text=text)
        zustand.pack(side="left", padx=(12, 0))

    def kopieren(nur_auswahl=False):
        try:
            if nur_auswahl:
                try:
                    inhalt = bereich.get("sel.first", "sel.last")
                except tk.TclError:
                    melde_kurz(T("Nichts markiert — bitte zuerst Text markieren."))
                    return
            else:
                inhalt = bereich.get("1.0", "end-1c")
            fenster.clipboard_clear()
            fenster.clipboard_append(inhalt)
            melde_kurz(T("In die Zwischenablage kopiert."))
        except Exception as fehler:
            melde_kurz(str(fehler))

    def als_datei():
        ziel = datei_speichern(fenster, T("Anleitung speichern"),
                               "ffmpeg-anleitung.txt", None, "*.txt")
        if not ziel:
            return
        try:
            with open(ziel, "w", encoding="utf-8") as datei:
                datei.write(bereich.get("1.0", "end-1c"))
            melde_kurz(T("Gespeichert."))
        except OSError as fehler:
            melde_kurz(str(fehler))

    tk.Button(leiste, text=T("Auswahl kopieren"),
              command=lambda: kopieren(True)).pack(side="left")
    tk.Button(leiste, text=T("Alles kopieren"),
              command=lambda: kopieren(False)).pack(side="left", padx=(8, 0))
    tk.Button(leiste, text=T("Als Datei speichern"),
              command=als_datei).pack(side="left", padx=(8, 0))
    tk.Button(leiste, text=T("Schließen"), command=schliessen).pack(side="right")

    try:
        fenster.lift()
        fenster.focus_force()
    except Exception:
        pass
    return fenster


def _hinweis(fenster, text, titel=None):
    """Hinweis-Dialog, der nicht hinter dem eigenen Fenster verschwindet.

    Anlaß (17.11.2026): Im USB-Fenster (bleibt im Vordergrund) erschien der
    Hinweis zum Zielplatz dahinter — der Nutzer sah nur den Fehler, nicht die
    Erklärung. Hier wird das „immer im Vordergrund" für die Dauer des Dialogs
    abgeschaltet und der Dialog als Kind des Fensters gezeigt.
    """
    from tkinter import messagebox

    titel = titel or T("Hinweis")
    war_vorne = False
    if fenster is not None:
        try:
            war_vorne = bool(fenster.attributes("-topmost"))
            if war_vorne:
                fenster.attributes("-topmost", False)
        except Exception:
            war_vorne = False
    try:
        if fenster is not None:
            messagebox.showinfo(titel, text, parent=fenster)
        else:
            messagebox.showinfo(titel, text)
    finally:
        if fenster is not None and war_vorne:
            try:
                fenster.attributes("-topmost", True)
            except Exception:
                pass


def _platz_pruefen(fenster, platz):
    """Prüft die Eingabe des Zielplatzes und sagt, was erwartet wird.

    Am 16.11.2026 blieb ein leerer oder falscher Platz ohne jede Meldung —
    es sah aus, als sei der Knopf kaputt.
    """
    import re as _re
    from tkinter import messagebox
    if not platz:
        _hinweis(fenster, T("Es wurde kein Zielplatz eingegeben."))
        return None
    # Tolerant lesen: Leerzeichen, Anführungszeichen, vorangestellte
    # Sonderzeichen (am 17.11.2026 kam „_Patch17" an) und ein bloßes „17".
    platz = str(platz).strip().strip("\"'\u201e\u201c\u2018\u2019").rstrip("/")
    platz = platz.lstrip("_.- ")
    treffer = _re.match(r"^Patch\s*(\d{1,3})$", platz, _re.IGNORECASE)
    if not treffer:
        treffer = _re.match(r"^(\d{1,3})$", platz)
    if not treffer or not (1 <= int(treffer.group(1)) <= 200):
        _hinweis(fenster, T("Der Zielplatz muß wie Patch01 … Patch200 heißen, "
                            "nicht „%s“.") % platz)
        return None
    nummer = int(treffer.group(1))
    return "Patch%02d" % nummer


def _frage_text(haupfenster, titel, frage, vorgabe=""):
    """Eigene Eingabe — ohne simpledialog und ohne angehängten Dialog.

    Anlaß (16.11.2026): Im USB-Fenster ließ sich kein Zielplatz eingeben.
    Der Systemdialog schloß sofort, und das Fenster stand im Vordergrund
    (`-topmost`), was das Zusammenspiel mit einem angehängten Dialog
    („sheet") zerstört. Diese Fassung nimmt dem Elternfenster für die Dauer
    der Eingabe das „immer im Vordergrund" und bringt den Dialog selbst nach
    vorn — ohne transient, ohne Systemdialog.
    """
    import tkinter as tk

    ergebnis = {"wert": None}
    vorne = True
    try:
        vorne = bool(haupfenster.attributes("-topmost"))
        haupfenster.attributes("-topmost", False)
    except Exception:
        vorne = False

    d = tk.Toplevel(haupfenster)
    d.title(titel)
    d.configure(bg=FARBE_HINTERGRUND)
    d.resizable(False, False)
    try:
        d.attributes("-topmost", True)
    except Exception:
        pass
    d.geometry("470x180+%d+%d" % (haupfenster.winfo_rootx() + 80,
                                  haupfenster.winfo_rooty() + 90))

    tk.Label(d, text=frage, bg=FARBE_HINTERGRUND, fg=FARBE_SCHRIFT,
             font=schrift(10), wraplength=430, justify="left").pack(padx=16, pady=(14, 8))
    feld = tk.Entry(d, width=32, font=schrift(13))
    feld.insert(0, vorgabe)
    feld.pack(padx=16, pady=(0, 10))
    feld.select_range(0, "end")
    feld.icursor("end")

    def aufraeumen():
        try:
            haupfenster.attributes("-topmost", vorne)
        except Exception:
            pass

    def uebernehmen(*_a):
        ergebnis["wert"] = feld.get().strip()
        aufraeumen()
        d.destroy()

    def abbrechen(*_a):
        ergebnis["wert"] = None
        aufraeumen()
        d.destroy()

    zeile = tk.Frame(d, bg=FARBE_HINTERGRUND)
    zeile.pack(pady=(0, 12))
    tk.Button(zeile, text=T("OK"), command=uebernehmen, width=10).pack(side="left", padx=6)
    tk.Button(zeile, text=T("Abbrechen"), command=abbrechen, width=10).pack(side="left", padx=6)

    # Nur das Feld hört auf Return/Escape — nicht das ganze Fenster.
    feld.bind("<Return>", uebernehmen)
    feld.bind("<KP_Enter>", uebernehmen)
    feld.bind("<Escape>", abbrechen)
    d.protocol("WM_DELETE_WINDOW", abbrechen)
    d.grab_set()
    d.lift()
    feld.focus_force()
    haupfenster.wait_window(d)
    return ergebnis["wert"]


def _usb_fenster_bauen(haupfenster, g_modul, oeffne_karte=None):
    """Der eigentliche Aufbau des USB-Fensters."""
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox

    f = tk.Toplevel(haupfenster)
    f.title(T("Pedal über USB"))
    f.configure(bg=FARBE_HINTERGRUND)
    # Über dem Hauptfenster und in dessen Mitte: sonst öffnet es sich
    # irgendwo und wirkt, als sei nichts passiert (16.11.2026).
    try:
        # KEIN transient(): auf macOS nimmt das Hauptfenster dann keine Klicks
        # mehr an, solange das USB-Fenster offen ist (16.11.2026).
        haupfenster.update_idletasks()
        bx, by = haupfenster.winfo_rootx(), haupfenster.winfo_rooty()
        bw, bh = haupfenster.winfo_width(), haupfenster.winfo_height()
        f.geometry("760x620+%d+%d" % (bx + max(0, (bw - 760) // 2),
                                      by + max(0, (bh - 620) // 2)))
    except Exception:
        f.geometry("760x620")
    f.lift()
    try:
        f.focus_force()
        # bleibt im Vordergrund: vorher verschwand es nach 1,2 s hinter dem
        # großen Hauptfenster und wirkte wie geschlossen (16.11.2026)
        f.attributes("-topmost", True)
    except Exception:
        pass
    _USB_FENSTER["fenster"] = f

    kopf = tk.Label(f, text=T("Verbinde mit dem Pedal …"), bg=FARBE_HINTERGRUND,
                    fg=FARBE_SCHRIFT, font=schrift(11, fest=True),
                    wraplength=720, justify="center")
    kopf.pack(pady=(10, 4))

    wahl = tk.StringVar(value="intern")
    zeile = tk.Frame(f, bg=FARBE_HINTERGRUND)
    zeile.pack(pady=(0, 6))
    for text, wert in ((T("Interner Speicher"), "intern"), (T("SD-Karte"), "sd")):
        tk.Radiobutton(zeile, text=text, value=wert, variable=wahl,
                       command=lambda: anstossen(), bg=FARBE_HINTERGRUND,
                       fg=FARBE_SCHRIFT, selectcolor=FARBE_LISTE,
                       activebackground=FARBE_HINTERGRUND).pack(side="left", padx=6)

    liste = tk.Listbox(f, bg=FARBE_LISTE, fg=FARBE_SCHRIFT, selectbackground=FARBE_AUSWAHL,
                       height=14, font=schrift(10), selectmode="extended",
                       exportselection=False)
    liste.pack(fill="both", expand=True, padx=14, pady=6)

    for _taste in ("<Command-a>", "<Control-a>"):
        try:
            f.bind_all(_taste, alle_auswaehlen, add="+")
        except Exception:
            pass

    hinweis = tk.Label(f, text="",
                       bg=FARBE_HINTERGRUND, fg=FARBE_LEISE,
                       font=schrift(9), wraplength=560, justify="center")
    hinweis.pack(pady=(0, 4))

    stand = {"geraet": None, "basis": None, "patchliste": [], "laeuft": False,
             "abbruch": False, "t0": 0.0, "bytes": 0,
             "ziel": "", "ziel_eigen": False,
             "zu": False, "nach": None}
    meldungen = queue.Queue()

    # Alles, was während eines Laufs stören würde, wird gesperrt
    # (17.11.2026: Umschalten oder ein zweiter Lauf brachte zwei Stränge
    #  durcheinander). „Abbrechen“ und „Schließen“ bleiben bedienbar.
    sperrbar = []

    def sperren(an):
        for ding in sperrbar:
            try:
                ding.config(state="disabled" if an else "normal")
            except Exception:
                pass


    def melde(text):
        kopf.config(text=text)

    def anzeigen(patchliste):
        liste.delete(0, "end")
        for platz, name, groesse in patchliste:
            groesse_txt = ("%.1f MB" % (groesse / 1048576.0)) if groesse else "—"
            liste.insert("end", "%s  ·  %s  ·  %s" %
                         (platz, name or T("(ohne Namen)"), groesse_txt))
        # Ohne Gerät gibt es keinen Namen — das passiert, wenn die Verbindung
        # scheitert, die Liste aber trotzdem aufgebaut wird (Rückmeldung
        # 18.11.2026: AttributeError 'NoneType' object has no attribute 'kopf').
        geraet = stand.get("geraet")
        name = geraet.kopf.get("name") if geraet is not None else T("kein Pedal")
        melde("%s — %s" % (name,
                           T("Interner Speicher") if wahl.get() == "intern" else T("SD-Karte")))
        try:
            f.lift()
        except Exception:
            pass

    def abholen():
        """Im Hauptstrang: Meldungen des Arbeitsstrangs abarbeiten."""
        if stand["zu"]:
            return
        try:
            while True:
                art, inhalt = meldungen.get_nowait()
                if art == "text":
                    melde(inhalt)
                elif art == "setlists":
                    ort, zuege_fertig = inhalt
                    try:
                        kandidaten = [n for n in sorted(os.listdir(ort))
                                      if n.lower().endswith(".txt")]
                    except Exception:
                        kandidaten = []
                    if kandidaten and _frage_ja(
                            f, T("Setlists anpassen"),
                            T("%d Setlist-Dateien in %s an die neuen Plätze "
                              "anpassen? Jede Datei wird vorher daneben "
                              "gesichert.") % (len(kandidaten), ort)):
                        try:
                            geaendert = setlists_anpassen(ort, zuege_fertig)
                        except Exception as e:
                            meldungen.put(("text", T("Setlists nicht angepaßt: ")
                                           + str(e)))
                        else:
                            if geaendert:
                                meldungen.put(("text", T("Setlists angepaßt: ")
                                               + ", ".join("%s (%d Zeilen)" % g
                                                           for g in geaendert)))
                            else:
                                meldungen.put(("text", T("Keine Setlist nannte "
                                                         "einen bewegten Platz.")))
                elif art == "hinweis":
                    _hinweis(f, inhalt)
                elif art == "anleitung":
                    _anleitung_fenster(f, inhalt, T("Anleitung"))
                elif art == "liste":
                    anzeigen(inhalt)
                elif art == "markieren":
                    try:
                        liste.selection_clear(0, "end")
                        liste.selection_set(inhalt)
                        liste.see(inhalt)
                    except Exception:
                        pass
                elif art == "neu":
                    stand["laeuft"] = False
                    anstossen()
                elif art == "aufraeumen":
                    ordner_pfad, dateien = inhalt
                    # Läuft schon ein neuer Hole-Lauf (z. B. nach „Verbindung
                    # neu aufbauen“), darf das Aufräumen des alten Laufs nicht
                    # dazwischenfahren — sonst verschwindet die .part-Datei,
                    # die der neue Lauf gerade schreibt (17.11.2026).
                    if not stand.get("laeuft") and dateien and _frage_ja(f,
                            T("Aufräumen"),
                            T("Die angefangene Kopie entfernen? Nur die gerade "
                              "geholten Dateien werden gelöscht, der Ordner bleibt.")):
                        weg = _raeume_auf(ordner_pfad, dateien)
                        melde(T("Aufgeräumt:") + " %d Dateien" % weg)
                elif art == "auffrischen":
                    if oeffne_karte:
                        try:
                            oeffne_karte(inhalt)
                        except Exception:
                            pass
                elif art == "oeffnen":
                    if oeffne_karte and _frage_ja(f, 
                            T("Pedal über USB"),
                            T("Im Hauptfenster öffnen?") + "\n" + inhalt):
                        try:
                            stand["geoeffnet"] = inhalt
                            oeffne_karte(inhalt)
                        except SystemExit as e:
                            messagebox.showerror(T("Fehler"), str(e))
                        except Exception as e:
                            messagebox.showerror(T("Fehler"), str(e))
                elif art == "fertig":
                    sperren(False)
                    stand["laeuft"] = False
        except queue.Empty:
            pass
        try:
            if not stand["zu"] and f.winfo_exists():
                stand["nach"] = f.after(150, abholen)
        except Exception:
            pass

    def arbeit(speicher, geraet, modul):
        try:
            if geraet is None:
                meldungen.put(("text", T("Verbinde mit dem Pedal …")))
                geraet = _geraet_holen(modul, einstellung_lesen("usb_geraet") or None)
                stand["geraet"] = geraet
                try:
                    einstellung_schreiben("usb_geraet", geraet.geraet)
                except Exception:
                    pass
            stand["geraet"] = geraet
            def fortschritt(fertig, gesamt, bisher):
                meldungen.put(("liste", list(bisher)))

            basis, patchliste = _pedal_lesen(geraet, speicher, fortschritt)
            stand["basis"], stand["patchliste"] = basis, patchliste
            meldungen.put(("liste", patchliste))
        except Exception as e:
            if not stand.get("patchliste"):
                meldungen.put(("liste", []))
            meldungen.put(("text", str(e) if "Karte" in str(e)
                           else T("Kein JamMan gefunden — Pedal einschalten und Kabel prüfen.")
                           + " " + str(e)))
        finally:
            meldungen.put(("fertig", None))

    def hinweis_setzen():
        # Der alte Satz „Nur lesen: das Pedal wird nicht verändert." stimmt
        # seit dem 16.11.2026 nicht mehr — das Werkzeug schreibt inzwischen
        # (Audio, Patch-Ordner, Rückgängig). Jetzt steht dort, was gilt.
        if wahl.get() == "sd":
            hinweis.config(text=T("Über USB fließen etwa 1,3 MB/s. Für eine ganze "
                                  "Karte ist ein Kartenleser viel schneller. "
                                  "Schreiben verändert die Karte — vorher wird gesichert."))
        else:
            hinweis.config(text=T("Lesen und Schreiben über USB. Vor jedem "
                                  "Überschreiben wird der alte Platz auf den Rechner "
                                  "gesichert."))

    def anstossen():
        hinweis_setzen()
        if stand["laeuft"]:
            return
        stand["laeuft"] = True
        sperren(True)
        melde(T("Lädt …"))
        threading.Thread(target=arbeit, args=(wahl.get(), stand["geraet"], g_modul),
                         daemon=True).start()

    def alles_holen():
        if not stand["patchliste"]:
            _hinweis(f, T("Erst „Neu laden“ drücken."))
            return
        mitlaufen_stoppen()
        ziel = stand.get("ziel") or ""
        if not ziel:
            ziel = ordner_waehlen(f, T("Zielordner wählen"), stand.get("ziel"))
            if not ziel:
                return
            stand["ziel"] = ziel
            stand["ziel_eigen"] = True
            try:
                einstellung_schreiben("usb_ziel", ziel)
            except Exception:
                pass
            ziel_anzeigen()
        geraetename = os.path.basename(stand["basis"].rstrip("/")) or "JamManSoloXT"
        wurzel = _hol_wurzel(ziel, geraetename, wahl.get())
        platzliste = [platz for platz, _, _ in stand["patchliste"]]

        def arbeit():
            import time as _zeit
            stand["abbruch"] = False
            stand["t0"] = _zeit.time()
            stand["bytes"] = 0
            geschrieben = []
            zaehler = {"gelesen": 0, "uebersprungen": 0}
            try:
                # Stufe A: nur die XML-Dateien — damit die Liste sofort erscheint
                for i, platz in enumerate(platzliste, 1):
                    if stand["abbruch"]:
                        meldungen.put(("text", T("Abgebrochen.")))
                        return
                    # Fortschritt auch in Stufe A: ohne Zähler sah es aus,
                    # als hänge der Vorgang (16.11.2026).
                    meldungen.put(("text", "%s %d/%d — %s" %
                                   (T("Hole Beschreibungen"), i, len(platzliste), platz)))
                    _patch_holen(stand["geraet"], stand["basis"] + platz,
                                 os.path.join(wurzel, platz),
                                 stopp=lambda: stand["abbruch"], nur="xml",
                                 protokoll=geschrieben, zaehler=zaehler)
                meldungen.put(("oeffnen", wurzel))
                # Stufe B: die Töne im Hintergrund
                for i, platz in enumerate(platzliste, 1):
                    if stand["abbruch"]:
                        meldungen.put(("text", T("Abgebrochen.")))
                        return
                    vergangen = max(0.1, _zeit.time() - stand["t0"])
                    tempo = (stand["bytes"] / 1048576.0) / vergangen
                    meldungen.put(("text", "%s %d/%d — %s · %.1f MB · %.2f MB/s" %
                                   (T("Hole Patch"), i, len(platzliste), platz,
                                    stand["bytes"] / 1048576.0, tempo)))
                    vorher = stand["bytes"]
                    _patch_holen(stand["geraet"], stand["basis"] + platz,
                                 os.path.join(wurzel, platz),
                                 stopp=lambda: stand["abbruch"], nur="ton",
                                 protokoll=geschrieben, zaehler=zaehler)
                    stand["bytes"] = vorher + _ordnergroesse(os.path.join(wurzel, platz))
                meldungen.put(("text", T("Fertig:") + " %d Patches → %s" % (len(platzliste), wurzel)))
                meldungen.put(("text", T("Übertragen: %d Dateien · unverändert übersprungen: %d")
                               % (zaehler["gelesen"], zaehler["uebersprungen"])))
                if stand.get("geoeffnet") == wurzel:
                    meldungen.put(("auffrischen", wurzel))
            except Exception as e:
                if "Abgebrochen" in type(e).__name__:
                    meldungen.put(("text", T("Abgebrochen.")))
                    meldungen.put(("aufraeumen", (wurzel, geschrieben)))
                else:
                    meldungen.put(("text", T("Fehler beim Lesen:") + " " + str(e)))
            finally:
                stand["abbruch"] = False
                meldungen.put(("fertig", None))

        stand["laeuft"] = True
        sperren(True)
        threading.Thread(target=arbeit, daemon=True).start()

    def laeuft_noch():
        """Sagt, warum ein Knopf gerade nichts tut."""
        if stand.get("laeuft"):
            melde(T("Ein Vorgang läuft noch — bitte abwarten."))
            return True
        return False

    def verbindung_neu():
        """Anschluß schließen und neu aufbauen (hilft nach einem Abbruch)."""
        if stand.get("laeuft"):
            # erst den laufenden Vorgang beenden, sonst sprechen zwei Stränge
            stand["abbruch"] = True
            melde(T("Warte auf das Ende des laufenden Vorgangs …"))
            for _ in range(50):
                if not stand.get("laeuft"):
                    break
                f.update()
                f.after(100)
                import time as _zeit
                _zeit.sleep(0.1)
            if stand.get("laeuft"):
                melde(T("Der Vorgang reagiert nicht — Fenster schließen und neu öffnen."))
                return
        _geraet_schliessen()
        stand["geraet"] = None
        stand["abbruch"] = False
        melde(T("Verbindung wird neu aufgebaut …"))
        anstossen()

    def aufs_pedal():
        if laeuft_noch():
            return
        if stand["geraet"] is None:
            _hinweis(f, T("Erst „Neu laden“ drücken."))
            return
        quell = ordner_waehlen(f, T("Ordner des Patches wählen"))
        if not quell:
            return
        platz = _frage_text(f, T("Aufs Pedal schreiben"),
                            T("Zielplatz auf dem Pedal (z. B. 17 oder Patch17):"),
                            "Patch50")
        platz = _platz_pruefen(f, platz)
        if not platz:
            return
        mitlaufen_stoppen()
        ziel_pfad = stand["basis"] + platz

        # Sicherheit: liegt dort schon ein Patch, wird er vorher geholt
        vorhanden = False
        try:
            _, ordner_da = stand["geraet"].ordner(stand["basis"])
            vorhanden = platz in ordner_da
        except Exception:
            pass
        if vorhanden:
            if not _frage_ja(f, 
                    T("Aufs Pedal schreiben"),
                    T("%s ist schon belegt. Der Patch wird vorher auf den Rechner "
                      "gesichert und dann überschrieben. Fortsetzen?") % platz):
                return
            platz_ordner = os.path.join(stand.get("ziel") or os.path.expanduser("~"),
                                      "vor-ueberschreiben", platz)
            sicherung = os.path.join(
                platz_ordner, datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
            try:
                _patch_holen(stand["geraet"], ziel_pfad, sicherung)
                sicherung_begrenzen(platz_ordner)
            except Exception as e:
                messagebox.showerror(T("Fehler"), str(e))
                return

        def arbeit():
            try:
                # Ein belegter Platz wird geleert, nachdem die Sicherung steht:
                # sonst bleiben Dateien des alten Patches stehen, die der neue
                # nicht hat (am 16.11.2026 im Test gesehen).
                if vorhanden:
                    meldungen.put(("text", T("Leere den Platz …")))
                    try:
                        stand["geraet"].loesche(ziel_pfad, ist_ordner=True, rekursiv=True)
                    except Exception:
                        pass
                stand["geraet"].erstelle_ordner(ziel_pfad)
                meldungen.put(("text", T("Schreibe aufs Pedal …")))
                _patch_schreiben(stand["geraet"], quell, ziel_pfad,
                                 meldung=lambda name, groesse: meldungen.put(
                                     ("text", "%s %s (%.1f MB)" %
                                      (T("Schreibe:"), name, groesse / 1048576.0))),
                                 stopp=lambda: stand["abbruch"])
                meldungen.put(("text", T("Fertig:") + " " + ziel_pfad))
                meldungen.put(("neu", None))
            except Exception as e:
                if "Abgebrochen" in type(e).__name__:
                    meldungen.put(("text", T("Abgebrochen.")))
                else:
                    meldungen.put(("text", T("Fehler beim Schreiben:") + " " + str(e)))
            finally:
                stand["abbruch"] = False
                meldungen.put(("fertig", None))

        stand["laeuft"] = True
        sperren(True)
        threading.Thread(target=arbeit, daemon=True).start()

    def plaetze_aufruecken():
        """Lücken zwischen den belegten Plätzen schließen.

        Anlaß (17.11.2026): Nach dem Löschen von Patch16 klaffte eine Lücke
        zwischen 15 und 17. Hier rücken alle späteren Patches um eins nach
        vorn; ihre Reihenfolge bleibt, nur die Platznummern ändern sich.
        Vorher wird gefragt und jeder bewegte Platz gesichert.
        """
        if laeuft_noch():
            return
        if stand["geraet"] is None:
            _hinweis(f, T("Erst „Neu laden“ drücken."))
            return
        try:
            zuege = plaetze_zusammenruecken(stand["geraet"], stand["basis"])
        except Exception as e:
            _hinweis(f, T("Fehler:") + " " + str(e))
            return
        if not zuege:
            _hinweis(f, T("Es gibt keine Lücken — die Plätze sind schon "
                          "lückenlos belegt."))
            return
        speicher = T("internen Speicher") if wahl.get() == "intern" else T("der Karte")
        beispiele = ", ".join("%s → %s" % ("Patch%02d" % alt, "Patch%02d" % neu)
                              for alt, neu in zuege[:4])
        weitere = "" if len(zuege) <= 4 else " …"
        if not _frage_ja(f, T("Plätze aufrücken"),
                         T("%d Patches rücken in %s nach vorn: %s%s\n\n"
                           "Jeder bewegte Platz wird vorher auf den Rechner "
                           "gesichert. Setlists im Pedal, die auf Platznummern "
                           "zeigen, stimmen danach nicht mehr. Fortfahren?")
                         % (len(zuege), speicher, beispiele, weitere)):
            return
        mitlaufen_stoppen()
        ziel_ordner = stand.get("ziel") or os.path.expanduser("~")

        def arbeit():
            stand["abbruch"] = False
            gesichert = 0
            try:
                for i, (alt, neu) in enumerate(zuege, 1):
                    if stand["abbruch"]:
                        meldungen.put(("text", T("Abgebrochen.")))
                        return
                    alt_pfad = stand["basis"] + "Patch%02d" % alt
                    neu_pfad = stand["basis"] + "Patch%02d" % neu
                    meldungen.put(("text", "%s %d/%d — %s → %s" %
                                   (T("Rücke auf"), i, len(zuege),
                                    "Patch%02d" % alt, "Patch%02d" % neu)))
                    platz_ordner = os.path.join(ziel_ordner, "vor-ueberschreiben",
                                                "Patch%02d" % alt)
                    sicherung = os.path.join(
                        platz_ordner, datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
                    try:
                        _patch_holen(stand["geraet"], alt_pfad, sicherung)
                        sicherung_begrenzen(platz_ordner)
                        gesichert += 1
                    except Exception:
                        pass
                    stand["geraet"].benenne_um(alt_pfad, neu_pfad, ist_ordner=True)
                meldungen.put(("text", T("Fertig: %d Plätze aufgerückt, %d vorher "
                                         "gesichert") % (len(zuege), gesichert)))
                ort = einstellung_lesen("usb_setlist_pfad") or ziel_ordner
                if ort and os.path.isdir(ort):
                    meldungen.put(("setlists", (ort, list(zuege))))
                meldungen.put(("hinweis", T("Aufgerückt: %d Plätze") % len(zuege)))
                meldungen.put(("neu", None))
            except Exception as e:
                meldungen.put(("text", T("Fehler beim Schreiben:") + " " + str(e)))
            finally:
                stand["abbruch"] = False
                meldungen.put(("fertig", None))

        stand["laeuft"] = True
        sperren(True)
        threading.Thread(target=arbeit, daemon=True).start()

    def firmware_holen_gui():
        """Systemdateien des angeschlossenen Pedals auf den Rechner holen."""
        if laeuft_noch():
            return
        if stand["geraet"] is None:
            _hinweis(f, T("Erst \u201eNeu laden\u201c dr\u00fccken."))
            return
        start = stand.get("ziel") or os.path.expanduser("~")
        ordner = ordner_waehlen(f, T("Zielordner f\u00fcr die Firmware"), start)
        if not ordner:
            return
        mitlaufen_stoppen()

        def arbeit():
            stand["abbruch"] = False
            try:
                geholt = firmware_holen(
                    stand["geraet"], ordner,
                    melden=lambda p: meldungen.put(
                        ("text", T("Hole") + " " + str(p).split("nandfs/")[-1])),
                    stopp=lambda: stand["abbruch"])
                meldungen.put(("text", T("Firmware geholt: %d Dateien") % len(geholt)))
                meldungen.put(("hinweis", T("Firmware geholt:\n%s\n%d Dateien")
                               % (ordner, len(geholt))))
            except Exception as e:
                meldungen.put(("text", T("Fehler:") + " " + str(e)))
            finally:
                stand["abbruch"] = False
                meldungen.put(("fertig", None))

        stand["laeuft"] = True
        sperren(True)
        threading.Thread(target=arbeit, daemon=True).start()

    def firmware_uebernehmen_gui():
        """Firmware aus einem geholten Ordner auf das Pedal schreiben."""
        if laeuft_noch():
            return
        if stand["geraet"] is None:
            _hinweis(f, T("Erst \u201eNeu laden\u201c dr\u00fccken."))
            return
        start = stand.get("ziel") or os.path.expanduser("~")
        quell = ordner_waehlen(f, T("Ordner mit der geholten Firmware"), start)
        if not quell:
            return
        vorhanden = [p for p in FIRMWARE_DATEIEN
                     if os.path.exists(os.path.join(
                         quell, p.lstrip("/").replace("/", os.sep)))]
        if not vorhanden:
            _hinweis(f, T("In diesem Ordner liegt keine Firmware "
                          "(usr/JamManUI, usr/loopengine …)."))
            return
        if not _frage_ja(f, T("Firmware \u00fcbernehmen"),
                         T("%d Firmware-Dateien werden jetzt in das Pedal "
                           "geschrieben. Vorher wird der jetzige Stand gesichert.\n\n"
                           "Ein Abbruch mittendrin kann das Pedal unbrauchbar "
                           "machen \u2014 es gibt keinen Weg zur\u00fcck.\n\n"
                           "Wirklich schreiben?") % len(vorhanden)):
            return
        sicherung = os.path.join(
            stand.get("ziel") or os.path.expanduser("~"),
            "sicherung-vor-firmware-" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
        mitlaufen_stoppen()

        def arbeit():
            stand["abbruch"] = False
            try:
                meldungen.put(("text", T("Sichere den jetzigen Stand \u2026")))
                alt = firmware_holen(stand["geraet"], sicherung,
                                     stopp=lambda: stand["abbruch"])
                meldungen.put(("text", T("Gesichert: %d Dateien nach %s")
                               % (len(alt), sicherung)))
                ergebnis = firmware_schreiben(
                    stand["geraet"], quell,
                    melden=lambda p, n: meldungen.put(
                        ("text", T("Schreibe") + " " + str(p).split("nandfs/")[-1]
                         + " (%d Bytes)" % n)),
                    stopp=lambda: stand["abbruch"])
                schlecht = [p for p, _, ok in ergebnis if not ok]
                meldungen.put(("text", T("Fertig: %d Dateien geschrieben")
                               % len(ergebnis)))
                if schlecht:
                    meldungen.put(("text", T("Nicht best\u00e4tigt: %d") % len(schlecht)))
                meldungen.put(("hinweis",
                               T("Firmware geschrieben: %d Dateien\nSicherung: %s\n"
                                 "Jetzt das Pedal aus- und wieder einschalten.")
                               % (len(ergebnis), sicherung)))
            except Exception as e:
                meldungen.put(("text", T("Fehler beim Schreiben:") + " " + str(e)))
            finally:
                stand["abbruch"] = False
                meldungen.put(("fertig", None))

        stand["laeuft"] = True
        sperren(True)
        threading.Thread(target=arbeit, daemon=True).start()

    def speicherabbild_aufnehmen():
        """Verzeichnisbaum des Pedals festhalten — nur lesend.

        Damit lassen sich zwei Pedale mit unterschiedlicher Firmware
        vergleichen, ohne etwas zu schreiben (17.11.2026).
        """
        if laeuft_noch():
            return
        if stand["geraet"] is None:
            _hinweis(f, T("Erst „Neu laden“ drücken."))
            return
        start = stand.get("ziel") or os.path.expanduser("~")
        vorgabe = "speicherabbild-%s.txt" % datetime.now().strftime("%Y-%m-%d_%H-%M")
        ziel = datei_speichern(f, T("Speicherabbild speichern"), vorgabe,
                               start, "*.txt")
        if not ziel:
            return
        mitlaufen_stoppen()

        def arbeit():
            stand["abbruch"] = False
            try:
                meldungen.put(("text", T("Lese den Verzeichnisbaum …")))
                anzahl = speicherabbild(
                    stand["geraet"], ziel,
                    melden=lambda n, p: meldungen.put(
                        ("text", "%d  %s" % (n, str(p)[:70]))),
                    stopp=lambda: stand["abbruch"])
                meldungen.put(("text", T("Speicherabbild: %d Einträge") % anzahl))
                meldungen.put(("hinweis", T("Speicherabbild geschrieben:\n%s")
                               % ziel))
            except Exception as e:
                meldungen.put(("text", T("Fehler:") + " " + str(e)))
            finally:
                stand["abbruch"] = False
                meldungen.put(("fertig", None))

        stand["laeuft"] = True
        sperren(True)
        threading.Thread(target=arbeit, daemon=True).start()

    def speicherabbilder_vergleichen_gui():
        """Zwei aufgenommene Speicherabbilder gegenüberstellen."""
        start = stand.get("ziel") or os.path.expanduser("~")
        erst = datei_waehlen(f, T("Erstes Speicherabbild"), "*.txt", start)
        if not erst:
            return
        zweit = datei_waehlen(f, T("Zweites Speicherabbild"), "*.txt",
                              os.path.dirname(erst))
        if not zweit:
            return
        try:
            nur_a, nur_b, anders = speicherabbilder_vergleichen(erst, zweit)
        except Exception as e:
            _hinweis(f, T("Fehler:") + " " + str(e))
            return
        zeilen = [T("Erstes:") + " " + erst, T("Zweites:") + " " + zweit, "",
                  T("Nur im ersten Bild: %d") % len(nur_a)]
        zeilen += ["  + " + p for p in nur_a[:40]]
        zeilen += ["", T("Nur im zweiten Bild: %d") % len(nur_b)]
        zeilen += ["  - " + p for p in nur_b[:40]]
        zeilen += ["", T("Gleicher Pfad, andere Größe: %d") % len(anders)]
        zeilen += ["  %s  %d → %d" % (p, a_, b_) for p, a_, b_ in anders[:60]]
        if not (nur_a or nur_b or anders):
            zeilen.append(T("Die beiden Bilder sind gleich."))
        _anleitung_fenster(f, "\n".join(zeilen), T("Speicherabbilder vergleichen"))

    def platz_loeschen():
        """Einen Platz im gewählten Speicher löschen.

        Anlaß (17.11.2026): Ein versehentlich geschriebener, viel zu großer
        Patch (1238 MB) ließ sich nur durch Überschreiben loswerden — und
        davor sicherte das Werkzeug ihn erst einmal über USB. Löschen ist der
        gerade Weg.
        """
        if laeuft_noch():
            return
        if stand["geraet"] is None:
            _hinweis(f, T("Erst „Neu laden“ drücken."))
            return
        platz = _frage_text(f, T("Platz löschen"),
                            T("Welcher Platz soll gelöscht werden? "
                              "(z. B. 30 oder Patch30)"), "")
        platz = _platz_pruefen(f, platz)
        if not platz:
            return
        ziel_pfad = stand["basis"] + platz
        try:
            _, vorhandene = stand["geraet"].ordner(stand["basis"])
        except Exception as e:
            melde(T("Fehler:") + " " + str(e))
            return
        if platz not in vorhandene:
            _hinweis(f, T("%s ist nicht belegt — nichts zu löschen.") % platz)
            return
        speicher = T("internen Speicher") if wahl.get() == "intern" else T("der Karte")
        if not _frage_ja(f, T("Platz löschen"),
                         T("%s wird aus %s gelöscht. Das läßt sich nicht "
                           "rückgängig machen. Fortfahren?") % (platz, speicher)):
            return
        mitlaufen_stoppen()

        def arbeit():
            stand["abbruch"] = False
            try:
                meldungen.put(("text", T("Lösche") + " " + platz + " …"))
                stand["geraet"].loesche(ziel_pfad, ist_ordner=True, rekursiv=True)
                meldungen.put(("text", T("Gelöscht:") + " " + platz))
                meldungen.put(("hinweis", T("Gelöscht: %s") % platz))
                meldungen.put(("neu", None))
            except Exception as e:
                meldungen.put(("text", T("Fehler beim Löschen:") + " " + str(e)))
            finally:
                stand["abbruch"] = False
                meldungen.put(("fertig", None))

        stand["laeuft"] = True
        sperren(True)
        threading.Thread(target=arbeit, daemon=True).start()

    def ordner_aufs_pedal():
        """Alle Patch-Ordner eines Ordners in den gewählten Speicher schreiben.

        Anlaß (17.11.2026): Eine geholte Kopie ließ sich nur Patch für Patch
        zurückspielen. Jetzt: Ordner wählen, alle Patch01 … PatchNN werden
        geschrieben — belegte Plätze vorher gesichert (fünf Stände je Platz),
        unveränderte übersprungen.
        """
        if laeuft_noch():
            return
        if stand["geraet"] is None:
            _hinweis(f, T("Erst „Neu laden“ drücken."))
            return
        quell = ordner_waehlen(f, T("Ordner mit Patch-Ordnern wählen"))
        if not quell:
            return
        muster = re.compile(r"^Patch(\d{1,3})$", re.IGNORECASE)
        patches = []
        for name in sorted(os.listdir(quell)):
            treffer = muster.match(name)
            if treffer and os.path.isdir(os.path.join(quell, name)):
                patches.append((name, "Patch%02d" % int(treffer.group(1))))
        if not patches:
            _hinweis(f, T("Im Ordner liegen keine Patch-Ordner "
                          "(erwartet: Patch01 … Patch200)."))
            return
        speicher = T("internen Speicher") if wahl.get() == "intern" else T("der Karte")
        if not _frage_ja(f, T("Ganzen Ordner aufs Pedal"),
                         T("%d Patch-Ordner werden in %s geschrieben. Belegte Plätze "
                           "werden vorher gesichert, unveränderte übersprungen. "
                           "Fortfahren?") % (len(patches), speicher)):
            return
        mitlaufen_stoppen()
        ziel_ordner = stand.get("ziel") or os.path.expanduser("~")

        def arbeit():
            stand["abbruch"] = False
            try:
                geschrieben, uebersprungen, gesichert = patches_aufs_pedal(
                    stand["geraet"], stand["basis"], quell, ziel_ordner,
                    melden=lambda text: meldungen.put(("text", text)),
                    stopp=lambda: stand["abbruch"])
                meldungen.put(("text",
                               T("Fertig: %d Patches geschrieben, %d übersprungen, "
                                 "%d vorher gesichert")
                               % (geschrieben, uebersprungen, gesichert)))
            except Exception as e:
                if "Abgebrochen" in type(e).__name__:
                    meldungen.put(("text", T("Abgebrochen.")))
                else:
                    meldungen.put(("text", T("Fehler beim Schreiben:") + " " + str(e)))
            finally:
                stand["abbruch"] = False
                meldungen.put(("fertig", None))

        stand["laeuft"] = True
        sperren(True)
        threading.Thread(target=arbeit, daemon=True).start()

    def als_zip():
        if laeuft_noch():
            return
        mitlaufen_stoppen()
        if not stand["patchliste"] or not stand.get("ziel"):
            _hinweis(f, T("Erst „Neu laden“ drücken."))
            return
        name = (T("JamMan") + "-" +
                (T("intern") if wahl.get() == "intern" else T("Karte")))
        ziel_datei = datei_speichern(f, T("Als ZIP sichern"),
                                     name + ".zip", stand["ziel"], "*.zip")
        if not ziel_datei:
            return
        platzliste = [p for p, _, _ in stand["patchliste"]]

        def arbeit():
            try:
                meldungen.put(("text", T("Sichere als ZIP …")))
                _als_zip(stand["geraet"], stand["basis"], platzliste, ziel_datei,
                         meldung=lambda name, i: meldungen.put(
                             ("text", "%s %s" % (T("Sichere:"), name))),
                         stopp=lambda: stand["abbruch"])
                meldungen.put(("text", T("Fertig:") + " " + ziel_datei))
            except Exception as e:
                meldungen.put(("text", T("Fehler beim Sichern:") + " " + str(e)))
            finally:
                stand["abbruch"] = False
                meldungen.put(("fertig", None))
        stand["laeuft"] = True
        sperren(True)
        threading.Thread(target=arbeit, daemon=True).start()

    def alle_auswaehlen(*_a):
        liste.selection_set(0, "end")
        return "break"

    def setlist_speichern():
        if not stand["patchliste"]:
            _hinweis(f, T("Erst „Neu laden“ drücken."))
            return
        indizes = [i for i in range(len(stand["patchliste"]))
                   if liste.selection_includes(i)]
        if not indizes:
            # Ohne Auswahl alle Patches nehmen — das ist der häufigste Fall
            if not _frage_ja(f, 
                    T("Setlist sichern"),
                    T("Nichts markiert. Alle %d Patches in die Setlist?"
                      % len(stand["patchliste"]))):
                return
            indizes = list(range(len(stand["patchliste"])))
        start = einstellung_lesen("usb_setlist_pfad") or stand.get("ziel") or None
        ziel_datei = datei_speichern(f, T("Setlist sichern"),
                                     T("Setlist.txt"), start, "*.txt")
        if not ziel_datei:
            return
        einstellung_schreiben("usb_setlist_pfad", os.path.dirname(ziel_datei))
        geraet = stand.get("geraet")
        geraetename = geraet.kopf.get("name", "") if geraet is not None else ""
        _setlist_speichern(stand["patchliste"], indizes, ziel_datei, geraetename)
        melde(T("Fertig:") + " " + ziel_datei)

    def jamlist_holen():
        """JamList aus der Bibliothek von JamManager XT als Textdatei holen."""
        start = einstellung_lesen("usb_setlist_pfad") or os.path.expanduser("~")
        db = datei_waehlen(f, T("Bibliothek von JamManager XT"), "*.sqlite *.db", start)
        if not db:
            return
        try:
            listen = jamlists_lesen(db)
        except Exception as e:
            _hinweis(f, T("Fehler:") + " " + str(e))
            return
        if not listen:
            _hinweis(f, T("In dieser Bibliothek liegt keine JamList."))
            return
        namen = ["%s (%d)" % (l["name"] or "?", len(l["platz"])) for l in listen]
        index = _frage_auswahl(f, T("JamList w\u00e4hlen"), namen)
        if index is None:
            return
        liste = listen[index]
        ziel = datei_speichern(f, T("Setlist speichern"),
                               (liste["name"] or "JamList") + ".txt",
                               os.path.dirname(db), "*.txt")
        if not ziel:
            return
        eintraege = [(("Patch%02d" % nr), n, "") for nr, n, _ in liste["platz"]]
        _setlist_speichern(eintraege, list(range(len(eintraege))), ziel)
        einstellung_schreiben("usb_setlist_pfad", os.path.dirname(ziel))
        _hinweis(f, T("Geholt: %s\n%d Pl\u00e4tze\n%s")
                 % (liste["name"], len(eintraege), ziel))

    def jamlist_ablegen():
        """Eine Textsetlist als JamList in die Bibliothek eintragen."""
        start = einstellung_lesen("usb_setlist_pfad") or os.path.expanduser("~")
        db = datei_waehlen(f, T("Bibliothek von JamManager XT"), "*.sqlite *.db", start)
        if not db:
            return
        quelle = datei_waehlen(f, T("Setlist laden"), "*.txt", os.path.dirname(db))
        if not quelle:
            return
        name = os.path.basename(quelle).rsplit(".", 1)[0]
        if not _frage_ja(f, T("JamList ablegen"),
                         T("%s%s%s als neue JamList eintragen? Die Bibliothek "
                           "wird dabei ver\u00e4ndert \u2014 vorher eine Kopie anlegen.")
                         % ("\u201e", name, "\u201c")):
            return
        try:
            eintraege = []
            for platz, patch_name in _setlist_lesen(quelle):
                zahl = int(str(platz).replace("Patch", "") or 0)
                eintraege.append((zahl, patch_name, ""))
            kennung = jamlist_schreiben(db, name, eintraege)
        except Exception as e:
            _hinweis(f, T("Fehler:") + " " + str(e))
            return
        _hinweis(f, T("JamList %s angelegt (id %d, %d Pl\u00e4tze)")
                 % (name, kennung, len(eintraege)))

    def setlist_holen():
        mitlaufen_stoppen()
        if stand["geraet"] is None or not stand.get("ziel"):
            _hinweis(f, T("Erst „Neu laden“ drücken."))
            return
        start = einstellung_lesen("usb_setlist_pfad") or stand.get("ziel") or None
        quelle = datei_waehlen(f, T("Setlist laden"), "*.txt", start)
        if not quelle:
            return
        einstellung_schreiben("usb_setlist_pfad", os.path.dirname(quelle))
        try:
            eintraege = _setlist_lesen(quelle)
        except Exception as e:
            messagebox.showerror(T("Fehler"), str(e))
            return
        if not eintraege:
            messagebox.showinfo(T("Hinweis"), T("Die Setlist ist leer."))
            return
        platzliste = [p for p, _ in eintraege]
        wurzel = _hol_wurzel(stand["ziel"],
                             os.path.basename(stand["basis"].rstrip("/")),
                             wahl.get())

        def arbeit():
            try:
                for i, platz in enumerate(platzliste, 1):
                    if stand["abbruch"]:
                        meldungen.put(("text", T("Abgebrochen.")))
                        return
                    meldungen.put(("text", "%s %d/%d — %s" %
                                   (T("Hole Patch"), i, len(platzliste), platz)))
                    _patch_holen(stand["geraet"], stand["basis"] + platz,
                                 os.path.join(wurzel, platz),
                                 stopp=lambda: stand["abbruch"])
                meldungen.put(("text", T("Fertig:") + " " + wurzel))
                try:
                    einstellung_schreiben("usb_letzter_holpfad", wurzel)
                except Exception:
                    pass
                meldungen.put(("oeffnen", wurzel))
            except Exception as e:
                meldungen.put(("text", T("Fehler beim Lesen:") + " " + str(e)))
            finally:
                stand["abbruch"] = False
                meldungen.put(("fertig", None))
        stand["laeuft"] = True
        sperren(True)
        threading.Thread(target=arbeit, daemon=True).start()

    def rueckgaengig():
        if laeuft_noch():
            return
        sicherung = einstellung_lesen("usb_letzte_sicherung")
        platz = einstellung_lesen("usb_letzter_platz")
        speicher = einstellung_lesen("usb_letzter_speicher")
        if not (sicherung and platz and os.path.isdir(sicherung)):
            messagebox.showinfo(T("Hinweis"), T("Es gibt nichts rückgängig zu machen."))
            return
        if not _frage_ja(f, 
                T("Rückgängig"),
                T("%s wird durch die Sicherung ersetzt. Fortsetzen?") % platz):
            return

        def arbeit():
            try:
                ziel_pfad = stand["basis"] + platz
                meldungen.put(("text", T("Stelle wieder her:") + " " + platz))
                try:
                    stand["geraet"].loesche(ziel_pfad, ist_ordner=True, rekursiv=True)
                except Exception:
                    pass
                stand["geraet"].erstelle_ordner(ziel_pfad)
                _patch_schreiben(stand["geraet"], sicherung, ziel_pfad,
                                 stopp=lambda: stand["abbruch"])
                meldungen.put(("text", T("Fertig:") + " " + ziel_pfad))
                meldungen.put(("neu", None))
            except Exception as e:
                meldungen.put(("text", T("Fehler beim Schreiben:") + " " + str(e)))
            finally:
                meldungen.put(("fertig", None))
        stand["laeuft"] = True
        sperren(True)
        threading.Thread(target=arbeit, daemon=True).start()

    def audio_aufs_pedal():
        if laeuft_noch():
            return
        if stand["geraet"] is None:
            _hinweis(f, T("Erst „Neu laden“ drücken."))
            return
        start = einstellung_lesen("usb_audio_pfad") or stand.get("ziel") or None
        quelle = datei_waehlen(f, T("Audiodatei wählen"), "*.wav *.aif *.aiff *.aifc *.caf *.mp3 *.m4a *.aac *.flac *.ogg *.opus *.wma", start)
        einstellung_schreiben("usb_audio_pfad", os.path.dirname(quelle))
        if not quelle:
            return
        platz = _frage_text(f, T("Audio aufs Pedal"),
                            T("Zielplatz auf dem Pedal (z. B. 17 oder Patch17):"),
                            "Patch50")
        platz = _platz_pruefen(f, platz)
        if not platz:
            return
        mitlaufen_stoppen()
        ziel_pfad = stand["basis"] + platz
        name = os.path.splitext(os.path.basename(quelle))[0]
        geraetename = os.path.basename(stand["basis"].rstrip("/")) or "JamManSoloXT"

        vorhanden = False
        try:
            _, ordner_da = stand["geraet"].ordner(stand["basis"])
            vorhanden = platz in ordner_da
        except Exception:
            pass
        if vorhanden and not _frage_ja(f, 
                T("Audio aufs Pedal"),
                T("%s ist schon belegt. Der Patch wird vorher auf den Rechner "
                  "gesichert und dann überschrieben. Fortsetzen?") % platz):
            return

        def arbeit():
            import tempfile
            try:
                if vorhanden:
                    platz_ordner = os.path.join(
                        stand.get("ziel") or os.path.expanduser("~"),
                        "vor-ueberschreiben", platz)
                    sicherung = os.path.join(
                        platz_ordner, datetime.now().strftime("%Y-%m-%d_%H-%M-%S"))
                    meldungen.put(("text", T("Sichere den alten Platz …")))
                    _patch_holen(stand["geraet"], ziel_pfad, sicherung)
                    sicherung_begrenzen(platz_ordner)
                    einstellung_schreiben("usb_letzte_sicherung", sicherung)
                    einstellung_schreiben("usb_letzter_platz", platz)
                    einstellung_schreiben("usb_letzter_speicher", wahl.get())
                # Länge prüfen, BEVOR Minuten über USB geschrieben werden:
                # das Pedal nimmt höchstens 10 Minuten je Loop an (Handbuch).
                # Am 17.11.2026 lief eine 1238-MB-Datei (~117 Minuten) los.
                try:
                    dauer_quelle = wav_info(quelle)[0]
                except Exception:
                    dauer_quelle = 0.0
                if dauer_quelle > MAX_LOOP_SEKUNDEN:
                    meldungen.put(("text", T("Die Datei ist %s lang — das Pedal nimmt "
                                             "höchstens 10 Minuten je Loop an. Bitte im "
                                             "Hauptfenster kürzen oder teilen.")
                                   % sekunden(dauer_quelle)))
                    meldungen.put(("fertig", None))
                    return
                meldungen.put(("text", T("Wandle die Datei …")))
                temp = tempfile.mkdtemp()
                wav = os.path.join(temp, "phrase.wav")
                if not wandle_nach_44k(quelle, wav):
                    if ffmpeg_fehlt():
                        # Der eigentliche Grund: ffmpeg fehlt (17.11.2026
                        # meldete das Fenster nur „Umwandeln fehlgeschlagen").
                        meldungen.put(("text", T("Zum Umwandeln fehlt ffmpeg.")))
                        meldungen.put(("anleitung", ffmpeg_anleitung()))
                    else:
                        meldungen.put(("text", T("Umwandeln fehlgeschlagen — die "
                                                 "Datei muß 44,1 kHz, 16 Bit, "
                                                 "Stereo werden.")))
                    return
                try:
                    dauer_fertig = wav_info(wav)[0]
                except Exception:
                    dauer_fertig = 0.0
                if dauer_fertig > MAX_LOOP_SEKUNDEN:
                    meldungen.put(("text", T("Die Datei ist %s lang — das Pedal nimmt "
                                             "höchstens 10 Minuten je Loop an. Bitte im "
                                             "Hauptfenster kürzen oder teilen.")
                                   % sekunden(dauer_fertig)))
                    meldungen.put(("fertig", None))
                    return
                meldungen.put(("text", T("Schreibe aufs Pedal …")))
                stand["geraet"].loesche(ziel_pfad, ist_ordner=True, rekursiv=True) if vorhanden else None
                stand["geraet"].erstelle_ordner(ziel_pfad)
                stand["geraet"].erstelle_ordner(ziel_pfad + "/PhraseA")
                stand["geraet"].schreibe_datei(ziel_pfad + "/patch.xml",
                                               _neuer_patch_xml(geraetename, name),
                                               stopp=lambda: stand["abbruch"])
                stand["geraet"].schreibe_datei(ziel_pfad + "/PhraseA/phrase.xml",
                                               _neue_phrase_xml(),
                                               stopp=lambda: stand["abbruch"])
                with open(wav, "rb") as fp:
                    daten = fp.read()
                stand["geraet"].schreibe_datei(ziel_pfad + "/PhraseA/phrase.wav", daten,
                                               stopp=lambda: stand["abbruch"],
                                               fortschritt=lambda f, g: meldungen.put(
                                                   ("text", "%s %.1f / %.1f MB" %
                                                    (T("Schreibe:"), f / 1048576.0,
                                                     g / 1048576.0))))
                meldungen.put(("text", T("Fertig:") + " " + platz + " — " + name))
                meldungen.put(("neu", None))
            except Exception as e:
                if "Abgebrochen" in type(e).__name__:
                    meldungen.put(("text", T("Abgebrochen.")))
                else:
                    meldungen.put(("text", T("Fehler beim Schreiben:") + " " + str(e)))
            finally:
                stand["abbruch"] = False
                meldungen.put(("fertig", None))

        stand["laeuft"] = True
        sperren(True)
        threading.Thread(target=arbeit, daemon=True).start()

    def mitlaufen_stoppen():
        """Mitlaufen beenden, bevor gelesen oder geschrieben wird.

        Zwei Stränge am selben Anschluß brachten das Pedal am 16.11.2026 zum
        Schweigen: der Mitlauf-Strang nahm Antworten weg, die zum Schreiben
        gehörten.
        """
        stand["mitlaufen"] = False

    # Was während eines Laufs gesperrt wird: alle Knöpfe und Auswahlmenüs
    def schliessen():
        # erst den Zeitgeber abbestellen, sonst meldet Tk nach dem Schließen
        # „invalid command name …abholen" (16.11.2026)
        stand["zu"] = True
        try:
            if stand.get("nach"):
                f.after_cancel(stand["nach"])
        except Exception:
            pass
        _geraet_schliessen()
        stand["geraet"] = None
        _USB_FENSTER["fenster"] = None
        f.destroy()

    def abbrechen():
        if stand["laeuft"]:
            stand["abbruch"] = True
            melde(T("Wird abgebrochen …"))

    # Bedienung: wenige Knöpfe, der Rest in aufklappbaren Menüs. Am 16.11.2026
    # waren es 15 Knöpfe in drei Reihen — technisch in Ordnung, aber niemand
    # findet dort etwas.
    reihe1 = tk.Frame(f, bg=FARBE_HINTERGRUND)
    reihe1.pack(fill="x", padx=14, pady=(0, 4))
    tk.Button(reihe1, text=T("Neu laden"), command=anstossen).pack(side="left")
    tk.Button(reihe1, text=T("Ins Hauptfenster holen …"),
              command=alles_holen).pack(side="left", padx=8)
    tk.Button(reihe1, text=T("Schließen"), command=schliessen).pack(side="right")

    def geschuetzt(befehl):
        """Einen Befehl so aufrufen, daß ein Fehler als Meldung erscheint.

        Am 16.11.2026 blieb „Audio in den Speicher schreiben“ wirkungslos:
        im Menübefehl fehlte ein Import, die Ausnahme landete nur im
        Terminal, im Fenster passierte nichts.
        """
        def huelle(*_a, **_k):
            try:
                return befehl(*_a, **_k)
            except Exception as e:
                messagebox.showerror(T("Fehler"), "%s: %s" % (type(e).__name__, e))
        return huelle

    def menue(eltern, beschriftung, eintraege):
        mb = tk.Menubutton(eltern, text=beschriftung + "  ▾", anchor="w",
                           bg=FARBE_LISTE, fg=FARBE_SCHRIFT,
                           activebackground=FARBE_AUSWAHL, activeforeground="#ffffff",
                           relief="raised", bd=1, padx=8, pady=2)
        m = tk.Menu(mb, tearoff=0, bg=FARBE_LISTE, fg=FARBE_SCHRIFT,
                    activebackground=FARBE_AUSWAHL, activeforeground="#ffffff")
        for text, befehl in eintraege:
            m.add_command(label=text, command=geschuetzt(befehl))
        mb["menu"] = m
        mb.pack(side="left", padx=(0, 6))
        return mb

    reihe2 = tk.Frame(f, bg=FARBE_HINTERGRUND)
    reihe2.pack(fill="x", padx=14, pady=(0, 6))
    menue(reihe2, T("Sichern"), [
        (T("Als ZIP sichern …"), als_zip),
        (T("Firmware dieses Pedals holen …"), firmware_holen_gui),
        (T("Speicherabbild aufnehmen …"), speicherabbild_aufnehmen),
        (T("Speicherabbilder vergleichen …"), speicherabbilder_vergleichen_gui),
        (T("Sicherungen aufräumen …"),
         lambda: fenster_aufraeumen(stand.get("ziel") or "", f)),
    ])
    menue(reihe2, T("Schreiben"), [
        (T("Patch-Ordner aufs Pedal …"), aufs_pedal),
        (T("Ganzen Ordner aufs Pedal …"), ordner_aufs_pedal),
        (T("Audio in den Speicher schreiben …"), audio_aufs_pedal),
        (T("Rückgängig"), rueckgaengig),
        (T("Firmware übernehmen …"), firmware_uebernehmen_gui),
        (T("Platz löschen …"), platz_loeschen),
        (T("Plätze aufrücken …"), plaetze_aufruecken),
    ])
    menue(reihe2, T("Setlists"), [
        (T("Alle auswählen"), alle_auswaehlen),
        (T("JamList aus JamManager XT holen …"), jamlist_holen),
        (T("Setlist als JamList ablegen …"), jamlist_ablegen),
        (T("Setlist sichern …"), setlist_speichern),
        (T("Setlist holen …"), setlist_holen),
    ])
    tk.Button(reihe2, text=T("Verbindung neu aufbauen"),
              command=verbindung_neu).pack(side="left", padx=(0, 6))
    tk.Button(reihe2, text=T("Abbrechen"), command=abbrechen).pack(side="right")

    # Zielordner dauerhaft anzeigen: nach dem ersten Wählen stand er nur
    # kurz in der Kopfzeile und war danach weg (16.11.2026).
    ziel_zeile = tk.Frame(f, bg=FARBE_HINTERGRUND)
    ziel_zeile.pack(fill="x", padx=14, pady=(0, 10))
    ziel_label = tk.Label(ziel_zeile, text="", bg=FARBE_HINTERGRUND, fg=FARBE_LEISE,
                          font=schrift(9), anchor="w", justify="left", wraplength=430)
    ziel_label.pack(side="left", fill="x", expand=True)

    # Vorschlag für den Zielordner — auf allen drei Systemen:
    # macOS und Linux haben ~/Documents, Windows meist auch; fehlt der
    # Ordner (oder ist er verschoben), wird der Benutzerordner genommen.
    _dokumente = os.path.expanduser("~/Documents")
    if not os.path.isdir(_dokumente):
        _dokumente = os.path.expanduser("~")
    VORSCHLAG = os.path.join(_dokumente, "JamMan-Sicherung")

    def ziel_anzeigen():
        if not stand["ziel"]:
            gespeichert = einstellung_lesen("usb_ziel")
            if gespeichert:
                stand["ziel"] = gespeichert
                stand["ziel_eigen"] = True
            else:
                stand["ziel"] = VORSCHLAG      # Vorschlag, noch nicht bestätigt
                stand["ziel_eigen"] = False
        text = T("Ziel:") + " " + stand["ziel"]
        if not stand["ziel_eigen"]:
            text += "   " + T("(Vorschlag)")
        ziel_label.config(text=text)

    def ziel_waehlen():
        p = ordner_waehlen(f, T("Zielordner wählen"),
                               stand["ziel"] or None)
        if p:
            stand["ziel"] = p
            stand["ziel_eigen"] = True
            try:
                einstellung_schreiben("usb_ziel", p)
            except Exception:
                pass
            ziel_anzeigen()

    tk.Button(ziel_zeile, text=T("Ziel ändern …"),
              command=ziel_waehlen).pack(side="right")
    ziel_anzeigen()

    f.protocol("WM_DELETE_WINDOW", schliessen)

    # Die Knopfreihen sind mit der Zeit mehr geworden; bei fester Höhe fiel
    # die unterste Reihe aus dem Fenster (16.11.2026). Deshalb wird die Höhe
    # nach dem Aufbau am tatsächlichen Bedarf ausgerichtet.
    try:
        f.update_idletasks()
        breite = max(760, f.winfo_reqwidth())
        hoehe = f.winfo_reqheight()
        bildschirm = f.winfo_screenheight()
        hoehe = min(max(hoehe, 520), bildschirm - 120)
        f.geometry("%dx%d" % (breite, hoehe))
    except Exception:
        pass

    # Erst hier einsammeln — jetzt sind alle Knöpfe und Menüs angelegt
    # (am 17.11.2026 stand die Stelle zu früh, deshalb blieben „Neu laden“,
    #  „Ins Hauptfenster holen“ und „Verbindung neu aufbauen“ bedienbar).
    sperrbar.extend(sperrbare_widgets(f, ausnahmen=(T("Abbrechen"), T("Schließen"))))

    f.after(150, abholen)
    anstossen()


def ist_kopie(pfad):
    """Ist das ein geholter Ordner vom Pedal (also nur auf dem Rechner)?"""
    pfad = (pfad or "").replace("\\", "/")
    return "/intern/" in pfad or "/karte/" in pfad


def _fenster_titel(basis, unterordner=""):
    """Überschrift des Hauptfensters passend zu dem, was offen ist.

    Anlaß (16.11.2026): „Speicherkarte des JamMan Solo XT" stand auch dann
    noch da, als eine geholte Kopie oder der interne Speicher offen war.
    """
    pfad = (basis or "").replace("\\", "/")
    if not pfad:
        return T("keine Karte")
    if "/intern/" in pfad:
        return T("Interner Speicher") + " — " + T("Kopie vom Pedal")
    if "/karte/" in pfad:
        return T("Speicherkarte") + " — " + T("Kopie vom Pedal")
    if "stereo" in (unterordner or "").lower():
        return T("Speicherkarte des JamMan Stereo")
    return T("Speicherkarte des JamMan Solo XT")


def gui(karte):
    """Oberfläche. Der Ton läuft nebenher: kein Warten, Stopp jederzeit."""
    # Drei Zeilen im Terminal, damit man sieht, daß es läuft und was es sieht
    # (18.11.2026: beim Start aus dem Terminal blieb es sonst völlig still).
    print("  " + T("JamMan Card Tool") + " " + FASSUNG)
    try:
        # Nicht den übergebenen Wert zeigen, sondern das, was wirklich gefunden
        # wurde — am 18.11.2026 stand dort "/media" statt „keine Karte".
        basis = karte_finden(karte, streng=False)
        if basis and os.path.isdir(basis):
            print("  " + T("Karte:") + " " + basis)
        else:
            print("  " + T("Karte:") + " " + T("keine Karte gefunden"))
        print("  " + T("Einstellungen:") + " " + EINSTELLUNG_DATEI)
    except Exception:
        pass
    print("  " + T("Meldungen stehen im Fenster; Fehler zusätzlich in "
                   "jamman-fehler.log."))
    print()
    # Sicherung beim Start - vor dem Fenster, damit man den Fortschritt
    # im Terminal sieht und das Fenster nicht einfriert.
    if einstellung_lesen("start_sichern") == "ja":
        beim_start_sichern(karte or karte_finden())

    import tkinter as tk
    from tkinter import filedialog, messagebox as _mb, simpledialog
    messagebox = uebersetzt_mb(_mb)

    # Apples Python 3.9 bringt Tk 8.5 mit. Auf macOS 27 zeichnet es die
    # Beschriftungen und die Liste nicht - das Fenster ist leer, die
    # Knöpfe sind da. (Am 10.11.2026 nachgestellt.) Deshalb: nur 8.6+.
    if float(tk.TkVersion) < 8.6:
        print(T("  Tk %s kann auf diesem macOS keinen Text im Fenster zeichnen.") % tk.TkVersion)
        print(T("  Das Fenster wäre leer. Es geht im Textmenü weiter."))
        print(T("  Für ein Fenster: Python von python.org installieren (bringt Tk 8.6)."))
        print()
        menu(karte)
        return

    basis = karte_finden(karte, streng=False)

    fenster = tk.Tk()
    fenster.minsize(1000, 720)
    fenster.configure(bg=FARBE_HINTERGRUND)

    # Ohne Karte: kein Dialog mehr. Früher fragte eine Meldung „Ordner selbst
    # wählen?" — auf Linux war das der erste Bildschirm, obwohl das Pedal per
    # USB erreichbar war (Rückmeldung 17.11.2026). Jetzt erscheint das Fenster
    # mit dem Hinweis, und wenn ein Pedal am Kabel hängt, öffnet sich gleich
    # das USB-Fenster mit Karte und internem Speicher.
    if basis is None:
        fenster.deiconify()

    # Einmal am Tag bei GitHub nachsehen, ob es eine neuere Fassung gibt.
    # Läuft im Hintergrund: ohne Netz startet das Fenster trotzdem sofort.
    def jetzt_aktualisieren():
        """Holt die neueste Fassung im Hintergrund und meldet das Ergebnis.

        Der Download läuft in einem Nebenlauf, damit das Fenster nicht
        einfriert; der Hauptlauf fragt alle 300 ms nach (tkinter darf nur
        aus dem Hauptlauf angesprochen werden).
        """
        if not frage_ja_nein(
                fenster, T("Aktualisieren"),
                T("Die neueste Fassung wird von GitHub geladen und ersetzt die "
                  "Programmdateien. Ihre Einstellungen und Ihre Karte bleiben "
                  "unberührt. Fortfahren?")):
            return
        stand = {"fertig": False, "ok": False, "text": ""}

        def lauf():
            try:
                ok, text = aktualisieren()
            except Exception as fehler:      # nie ohne Meldung enden
                ok, text = False, T("Unerwarteter Fehler: %s") % fehler
            stand["ok"] = ok
            stand["text"] = text
            stand["fertig"] = True

        def nachsehen():
            if not stand["fertig"]:
                fenster.after(300, nachsehen)
                return
            if not stand["ok"]:
                messagebox.showinfo(T("Nicht aktualisiert"), stand["text"])
                return
            if frage_ja_nein(fenster, T("Aktualisiert"),
                                   stand["text"] + "\n\n"
                                   + T("Fenster jetzt schließen?")):
                fenster.destroy()

        threading.Thread(target=lauf, daemon=True).start()
        fenster.after(300, nachsehen)

    def _status(text):
        """Statuszeile setzen — oder, falls es sie noch nicht gibt, die Konsole."""
        try:
            status_var.set(text)
        except Exception:
            print("  " + str(text))

    def fassung_melden(tag):
        """Meldet eine neuere Fassung — und installiert sie, wenn gewünscht.

        „Automatisch" heißt: ohne Rückfrage holen und einsetzen, danach einmal
        sagen, daß ein Neustart nötig ist. Abschaltbar über die Einstellung
        update_auto = nein (Häkchen „Update beim Start").
        """
        try:
            if not tag or _numerisch(tag) <= _numerisch(eigene_fassung()):
                return
            automatisch = einstellung_lesen("update_auto") != "nein"
            if not automatisch:
                if frage_ja_nein(
                        fenster, T("Neue Fassung verfügbar"),
                        T("Es gibt eine neuere Fassung: %s. Sie benutzen %s.")
                        % (tag, eigene_fassung()) + "\n\n"
                        + T("Jetzt aktualisieren?")):
                    jetzt_aktualisieren()
                return
            _status(T("Neue Fassung %s wird geholt …") % tag)
            stand = {"fertig": False, "ok": False, "text": ""}

            def lauf():
                try:
                    stand["ok"], stand["text"] = aktualisieren()
                except Exception as f:
                    stand["ok"], stand["text"] = False, str(f)
                stand["fertig"] = True

            def nachsehen():
                if not stand["fertig"]:
                    fenster.after(300, nachsehen)
                    return
                _status(stand["text"])
                if stand["ok"]:
                    messagebox.showinfo(
                        T("Aktualisiert"),
                        T("Fassung %s ist installiert. Bitte das Fenster "
                          "schließen und neu starten.") % tag)
            threading.Thread(target=lauf, daemon=True).start()
            fenster.after(300, nachsehen)
        except Exception:
            pass

    def fassung_im_hintergrund():
        """Fragt im Hintergrund bei GitHub nach; das Fenster wartet nicht.

        Der Fund wird NICHT aus dem Nebenlauf ans Fenster gemeldet -
        tkinter darf nur aus dem Hauptlauf angesprochen werden. Der
        Hauptlauf fragt stattdessen alle 300 ms nach.
        """
        if einstellung_lesen("update_pruefung") == "nein":
            return
        heute = datetime.now().strftime("%Y-%m-%d")
        if einstellung_lesen("update_geprueft") == heute:
            return

        stand = {"fertig": False, "tag": None}

        def lauf():
            tag = fassung_pruefen()
            try:
                einstellung_schreiben("update_geprueft", heute)
            except Exception:
                pass
            stand["tag"] = tag
            stand["fertig"] = True

        def nachsehen():
            if not stand["fertig"]:
                fenster.after(300, nachsehen)
                return
            if stand["tag"]:
                fassung_melden(stand["tag"])

        threading.Thread(target=lauf, daemon=True).start()
        fenster.after(300, nachsehen)

    fassung_im_hintergrund()

    # Sprachwahl oben rechts. Ein Wechsel öffnet das Fenster neu, damit
    # alle Beschriftungen in der neuen Sprache erscheinen.
    leiste = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    leiste.pack(fill="x", padx=14, pady=(8, 0))
    sprach_var = tk.StringVar(value=SPRACHEN.get(SPRACHE, "Deutsch"))

    def sprache_waehlen(gewaehlt):
        code = next((c for c, n in SPRACHEN.items() if n == gewaehlt), SPRACHE)
        einstellung_schreiben("sprache", code)
        sprache_setzen(code)
        fenster.destroy()
        gui(basis_aktuell)

    tk.OptionMenu(leiste, sprach_var, *SPRACHEN.values(),
                  command=sprache_waehlen).pack(side="right")
    tk.Label(leiste, text="Sprache / Language", bg=FARBE_HINTERGRUND,
             fg=FARBE_LEISE).pack(side="right", padx=(0, 8))

    def spenden():
        if not SPENDEN_LINK:
            messagebox.showinfo(
                T("Spenden"),
                T("Für dieses Werkzeug ist noch kein Spendenlink eingetragen.")
                + "\n\n"
                + T("Er steht in jamman.py in der Zeile SPENDEN_LINK."))
            return
        try:
            import webbrowser
            webbrowser.open(SPENDEN_LINK)
        except Exception:
            messagebox.showinfo(T("Spenden"), SPENDEN_LINK)

    tk.Button(leiste, text=T("Spenden"), command=spenden).pack(side="right", padx=(0, 12))
    def usb_oeffnen():
        def oeffne(pfad):
            zustand["basis"] = karte_pruefen(pfad)
            neu_laden()
        usb_fenster(fenster, oeffne_karte=oeffne)

    tk.Button(leiste, text=T("Aktualisieren"),
              command=jetzt_aktualisieren).pack(side="right", padx=(0, 12))

    tk.Button(leiste, text=T("Pedal über USB"),
              command=usb_oeffnen).pack(side="right", padx=(0, 12))


    titel_var = tk.StringVar(value=_fenster_titel(basis, UNTERORDNER if basis else ""))
    tk.Label(fenster, textvariable=titel_var,
             font=schrift(15, fest=True, titel=True),
             bg=FARBE_HINTERGRUND, fg=FARBE_SCHRIFT).pack(pady=(12, 2))

    if SPRACHE in UEBERSETZUNGSHINWEIS:
        tk.Label(fenster, text=UEBERSETZUNGSHINWEIS[SPRACHE],
                 bg=FARBE_HINTERGRUND, fg="#c8a24a",
                 font=schrift(11)).pack(pady=(2, 0))

    pfad_var = tk.StringVar(value=basis or T("keine Karte gefunden"))
    tk.Label(fenster, textvariable=pfad_var, bg=FARBE_HINTERGRUND,
             fg=FARBE_LEISE).pack()

    # Bei einer geholten Kopie ausdrücklich sagen, daß Änderungen hier nur
    # den Rechner betreffen (17.11.2026).
    kopie_hinweis = tk.StringVar(value="")
    if ist_kopie(basis):
        kopie_hinweis.set(T("Kopie auf dem Rechner — Änderungen hier wirken "
                            "nicht auf das Pedal."))
    tk.Label(fenster, textvariable=kopie_hinweis, bg=FARBE_HINTERGRUND,
             fg="#c8a24a", font=schrift(10), wraplength=1000).pack()

    # Achtungshinweis, wenn hier eine geholte Kopie offen ist und nicht die
    # Karte selbst. Am 16.11.2026 zweimal verwechselt: „Neu laden" zeigte
    # dann die halb geholte Kopie, und das sah nach Durcheinander aus.
    try:
        sicherungsziel = einstellung_lesen("usb_ziel") or os.path.join(
            os.path.expanduser("~/Documents"), "JamMan-Sicherung")
        if basis and os.path.abspath(basis).startswith(
                os.path.abspath(sicherungsziel) + os.sep):
            tk.Label(fenster,
                     text=T("Dies ist eine Kopie vom Pedal, nicht die Karte selbst. "
                            "Über „Karte wählen“ die Karte öffnen."),
                     bg=FARBE_HINTERGRUND, fg="#c8a24a",
                     font=schrift(10), wraplength=1000).pack(pady=(2, 0))
    except Exception:
        pass

    def zeige_ffmpeg_anleitung():
        # Dasselbe Fenster wie im USB-Fenster: scrollbar, kopierbar und
        # im Vordergrund (ersetzt die kleinere eigene Fassung vom 16.11.2026).
        _anleitung_fenster(fenster, ffmpeg_anleitung(),
                           "MP3 und M4A: ffmpeg installieren")

    if ffmpeg_fehlt():
        hinweis = tk.Frame(fenster, bg="#3a3320")
        hinweis.pack(fill="x", padx=14, pady=(6, 0))
        tk.Label(hinweis, text=T("MP3 und M4A brauchen ffmpeg -"),
                 bg="#3a3320", fg="#f0d48a").pack(side="left", padx=(10, 4))
        tk.Button(hinweis, text=T("Anleitung"),
                  command=zeige_ffmpeg_anleitung).pack(side="left", pady=4)
        tk.Label(hinweis, text=T("WAV geht auch ohne"),
                 bg="#3a3320", fg="#a0a0a0").pack(side="left", padx=8)

    tk.Label(fenster, text="  %-10s %-28s %8s  %-8s %-9s %-6s %s" % (
          T("Ordner"), T("Name"), T("Dauer"), T("Stoppart"), T("Wdg."),
          T("Tempo"), T("Rhythmus")),
        font=schrift(12), bg=FARBE_HINTERGRUND, fg=FARBE_LEISE,
        anchor="w", justify="left").pack(fill="x", padx=14)

    # Suchen und sortieren (17.11.2026: die Liste ließ sich nur in
    # Ordnerreihenfolge ansehen — bei 200 Plätzen zu wenig).
    such_zeile = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    such_zeile.pack(fill="x", padx=14, pady=(2, 2))
    tk.Label(such_zeile, text=T("Suchen:") + " ", bg=FARBE_HINTERGRUND,
             fg=FARBE_LEISE, font=schrift(11)).pack(side="left")
    such_var = tk.StringVar(value="")
    # Weißes Feld mit Rahmen und größerer Schrift: auf Windows war das Feld
    # (dunkel auf dunkel, ohne Rahmen) so flach, daß man nicht sah, ob etwas
    # darin steht — getippt wurde, aber nichts erschien (18.11.2026).
    such_eingabe = tk.Entry(such_zeile, textvariable=such_var, width=26,
                            bg="#ffffff", fg="#000000",
                            insertbackground="#000000",
                            relief="solid", borderwidth=1, font=schrift(11))
    such_eingabe.pack(side="left", ipady=4, pady=2)
    tk.Label(such_zeile, text="   " + T("Sortierung") + ": ",
             bg=FARBE_HINTERGRUND, fg=FARBE_LEISE,
             font=schrift(11)).pack(side="left")
    SORTIERUNG = [("Ordner", "ordner"), ("Name", "name"),
                  ("Dauer", "dauer"), ("Länge", "laenge")]
    sort_var = tk.StringVar(value="ordner")
    sort_menue = tk.OptionMenu(such_zeile, sort_var,
                               *[b for b, _ in SORTIERUNG])
    sort_menue.config(bg=FARBE_LISTE, fg=FARBE_SCHRIFT, relief="flat",
                      highlightthickness=0)
    sort_menue.pack(side="left")

    # Ein Knopf und die Eingabetaste als zweiter Weg: am 18.11.2026 wirkte die
    # Suche auf Windows, als täte sie nichts — so läßt sich prüfen, ob es am
    # Tippen (trace) oder am Filtern liegt.
    such_knopf = tk.Button(such_zeile, text=T("Suchen"),
                           command=lambda: zeilen_anwenden())
    such_knopf.pack(side="left", padx=(8, 0))
    # Fassung neben dem Feld: damit sich zwei entpackte Fassungen im Betrieb
    # unterscheiden lassen (18.11.2026 — es war unklar, welche läuft).
    tk.Label(such_zeile, text="  " + FASSUNG, bg=FARBE_HINTERGRUND,
             fg=FARBE_LEISE).pack(side="right")
    such_eingabe.bind("<Return>", lambda e: zeilen_anwenden())

    rahmen = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    rahmen.pack(fill="both", expand=True, padx=14, pady=(0, 6))
    # Listbox statt Zeichenfläche: Tk bringt für Listboxen eine eigene
    # Rad-Behandlung mit (tk::MouseWheel …), für Zeichenflächen nicht.
    # Mit der Zeichenfläche kam das Mausrad auf diesem Rechner nicht mehr
    # durch (am 10.11.2026 mehrfach bestätigt). Der Einfügestrich wird
    # stattdessen als markierte Zeile "hier ablegen" eingeblendet.
    ZEILE_H = 24
    such_var.trace_add("write", lambda *a: zeilen_anwenden())
    sort_var.trace_add("write", lambda *a: zeilen_anwenden())

    liste = tk.Listbox(rahmen, font=schrift(13),
                       background="#ffffff", foreground="#000000",
                       selectbackground=FARBE_AUSWAHL, selectforeground="#ffffff",
                       highlightthickness=0, borderwidth=0,
                       activestyle="none", selectmode="extended",
                       exportselection=False)
    liste.pack(side="left", fill="both", expand=True)
    rollbalken = tk.Scrollbar(rahmen, command=lambda *a: liste.yview(*a))
    rollbalken.pack(side="right", fill="y")
    liste.config(yscrollcommand=rollbalken.set)

    # Wellenform zum Scrubben, unter der Liste
    spuren = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    spuren.pack(fill="x", padx=14, pady=(6, 0))
    leinwand = tk.Canvas(spuren, height=84, bg="#111111",
                         highlightthickness=0, borderwidth=0)
    leinwand.pack(fill="x")
    zeit_var = tk.StringVar(value=T("kein Patch gewählt"))
    zeile_unten = tk.Frame(spuren, bg=FARBE_HINTERGRUND)
    zeile_unten.pack(fill="x")
    tk.Label(zeile_unten, textvariable=zeit_var, bg=FARBE_HINTERGRUND,
             fg=FARBE_LEISE, anchor="w").pack(side="left", fill="x", expand=True)
    # Blätterknöpfe: falls das Mausrad vom System nicht durchkommt
    weiter_var = tk.BooleanVar(value=False)
    start_sicher_var = tk.BooleanVar(
        value=einstellung_lesen("start_sichern") == "ja")

    def start_sicherung_umschalten():
        einstellung_schreiben("start_sichern",
                              "ja" if start_sicher_var.get() else "nein")
        status_var.set("Sicherung beim Start: %s"
                       % ("ein" if start_sicher_var.get() else "aus"))

    tk.Checkbutton(zeile_unten, text=T("beim Start sichern"),
                   variable=start_sicher_var, command=start_sicherung_umschalten,
                   bg=FARBE_HINTERGRUND, fg=FARBE_LEISE,
                   activebackground=FARBE_HINTERGRUND,
                   activeforeground=FARBE_SCHRIFT,
                   selectcolor=FARBE_HINTERGRUND).pack(side="left", padx=(0, 14))
    tk.Checkbutton(zeile_unten, text=T("automatisch weiter"),
                   variable=weiter_var, bg=FARBE_HINTERGRUND, fg=FARBE_LEISE,
                   activebackground=FARBE_HINTERGRUND,
                   activeforeground=FARBE_SCHRIFT,
                   selectcolor=FARBE_HINTERGRUND).pack(side="left", padx=(0, 14))
    # Automatisches Aktualisieren: an, solange nichts anderes gesagt wurde.
    update_auto_var = tk.BooleanVar(
        value=einstellung_lesen("update_auto") != "nein")

    def update_auto_umschalten():
        try:
            einstellung_schreiben("update_auto",
                                  "ja" if update_auto_var.get() else "nein")
        except Exception:
            pass

    tk.Checkbutton(zeile_unten, text=T("Update beim Start"),
                   variable=update_auto_var, command=update_auto_umschalten,
                   bg=FARBE_HINTERGRUND, fg=FARBE_LEISE,
                   activebackground=FARBE_HINTERGRUND,
                   activeforeground=FARBE_SCHRIFT,
                   selectcolor=FARBE_HINTERGRUND).pack(side="left", padx=(0, 14))
    tk.Label(zeile_unten, text=T("Platz"), bg=FARBE_HINTERGRUND,
             fg=FARBE_LEISE).pack(side="left", padx=(10, 2))
    platz_eingabe = tk.Entry(zeile_unten, width=4, justify="center",
                             bg="#ffffff", fg="#000000",
                             insertbackground="#000000",
                             relief="solid", borderwidth=1)
    platz_eingabe.pack(side="left")
    platz_eingabe.bind("<Return>", lambda e: gehe_zu())
    platz_eingabe.bind("<Escape>", lambda e: (platz_eingabe.delete(0, "end"),
                                              zustand.__setitem__("tippen", ""),
                                              status_var.set("")))
    tk.Button(zeile_unten, text=T("nächster"), width=9,
              command=lambda: gehe_zu()).pack(side="left", padx=2)
    # Windows zeichnet Knöpfe nativ und ignoriert highlightbackground.
    # Deshalb sitzt der Knopf in einem eigenen Rahmen, dessen Farbe den
    # vorgemerkten Zustand zeigt - das geht auf allen Systemen.
    rahmen_vormerken = tk.Frame(zeile_unten, bg=FARBE_HINTERGRUND)
    rahmen_vormerken.pack(side="left", padx=2)
    knopf_vormerken = tk.Button(rahmen_vormerken, text=T("vormerken"), width=10,
                                command=lambda: vormerken())
    knopf_vormerken.pack(padx=2, pady=2)
    ursprung_rahmen = (knopf_vormerken.cget("highlightthickness"),
                       knopf_vormerken.cget("highlightbackground"))
    tk.Button(zeile_unten, text="▲ 5", width=4,
              command=lambda: seite(-1)).pack(side="left", padx=2)
    tk.Button(zeile_unten, text="▼ 5", width=4,
              command=lambda: seite(1)).pack(side="left", padx=2)

    basis_aktuell = basis
    zustand = {"basis": basis, "nummern": [], "spieler": None,
               "temp": None, "start": 0.0, "dauer": 0.0,
               "welle": [], "marke": None, "abschnitte": [],
               "zeilen": [], "auswahl": [], "oben": 0,
               "ziehung": None, "strich": None, "auto": None}

    # ---------------- Liste: füllen, auswählen, Strich ----------------
    def liste_zeichnen():
        """Nur den Einfügestrich pflegen - die Zeilen macht die Listbox."""
        if zustand.get("marker") is not None:
            liste.itemconfig(zustand["marker"], background="#ffe9a8",
                             foreground="#000000")

    def zeilen_anwenden(*_):
        """Zeilen nach Suchtext filtern und sortieren, dann anzeigen.

        Fehler werden gemeldet statt verschluckt: eine Ausnahme in einem
        trace-Rückruf zeigt Tk nur in der Konsole, das Fenster bliebe stumm.
        """
        try:
            _zeilen_anwenden()
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                status_var.set(T("Suche fehlgeschlagen: %s") % e)
            except Exception:
                pass

    def _zeilen_anwenden(*_):
        alle = zustand.get("alle") or []
        if not any(z.get("nummer") is not None for z in alle):
            # Rückfall: lieber die gerade angezeigten Zeilen filtern als gar
            # nichts tun (18.11.2026 — die Suche blieb sonst wirkungslos).
            alle = [z for z in (zustand.get("zeilen") or []) if z.get("text")]
        # Das Feld bleibt immer bedienbar. Ein graues Feld war am 18.11.2026
        # die falsche Antwort: es ließ sich nicht einmal hineinschreiben, und
        # niemand sah warum. Statt dessen sagt die Statuszeile, was fehlt.
        hat_patches = any(z.get("nummer") is not None for z in alle)
        if not alle or not hat_patches:
            if such_var.get().strip():
                try:
                    status_var.set(T("Erst eine Karte oder eine geholte Kopie "
                                     "öffnen — dann läßt sich suchen."))
                except Exception:
                    pass
            return
        muster = such_var.get().strip().lower()
        art = sort_var.get()
        zeilen = [z for z in alle if z.get("nummer") is not None]
        hinweise = [z for z in alle if z.get("nummer") is None]
        if muster:
            zeilen = [z for z in zeilen if muster in z["text"].lower()]
        if art == "Name":
            zeilen.sort(key=lambda z: (z.get("name") or "").lower())
        elif art == "Dauer":
            zeilen.sort(key=lambda z: -(z.get("dauer") or 0))
        elif art == "Länge":
            zeilen.sort(key=lambda z: (z.get("name") or "").lower(),
                        reverse=True)
        rest = [] if muster else hinweise
        anzeige = zeilen + rest
        if muster:
            anzeige = anzeige + [{"text": "  " + T("%d von %d Patches passen "
                                                  "zu „%s“.")
                                  % (len(zeilen), len(alle), muster)}]
        liste_setzen(anzeige)
        if muster:
            print("  Suche: %d von %d Patches" % (
                len(zeilen), sum(1 for z in alle if z.get("nummer") is not None)))

    def liste_setzen(zeilen):
        liste.delete(0, "end")
        zustand["zeilen"] = zeilen
        zustand["nummern"] = [z.get("nummer") for z in zeilen]
        for zeile in zeilen:
            liste.insert("end", zeile["text"])
        zustand["marker"] = None
        liste.yview_moveto(0.0)

    def liste_auswahl():
        return tuple(liste.curselection())

    def liste_waehlen(i, mehrfach=False):
        if i is None or not (0 <= i < liste.size()):
            liste.selection_clear(0, "end")
        elif mehrfach:
            if i in liste.curselection():
                liste.selection_clear(i)
            else:
                liste.selection_set(i)
        else:
            liste.selection_clear(0, "end")
            liste.selection_set(i)

    def liste_zeigen(i):
        if 0 <= i < liste.size():
            liste.see(i)

    def liste_index(y):
        return liste.nearest(y)
    def strich_setzen(pos):
        """Markierte Zeile "hier ablegen" an der gewünschten Stelle."""
        if pos == zustand.get("marker"):
            return
        strich_weg()
        if pos is None:
            return
        pos = max(0, min(liste.size(), pos))
        liste.insert(pos, "  ═══ hier ablegen ═══")
        zustand["marker"] = pos
        liste.itemconfig(pos, background="#ffe9a8", foreground="#000000",
                         selectbackground="#ffe9a8")
        liste.see(pos)

    def strich_weg():
        pos = zustand.get("marker")
        if pos is not None and 0 <= pos < liste.size():
            liste.delete(pos)
        zustand["marker"] = None

    def seite(richtung):
        """Blätterknöpfe: fünf Zeilen je Druck.

        Eine ganze Seite war zu grob - die Liste sprang fast ans Ende
        (Rückmeldung vom 10.11.2026). Ganze Seiten gibt es über Bild auf/ab.
        """
        zustand["nutzer_scroll"] = time.time()
        liste.yview_scroll(richtung * 5, "units")
    import atexit
    atexit.register(lambda: stoppen_ton(zustand.get("spieler")))
    status_var = tk.StringVar(value="")
    _status_setzen = status_var.set

    def _status_uebersetzt(text):
        return _status_setzen(T(text) if isinstance(text, str) else text)

    status_var.set = _status_uebersetzt

    def zeit_laeuft():
        """Läuft die Wiedergabe - aus der Zeit gerechnet.

        Nötig auf Windows: winsound gibt keinen Prozess zurück, dessen
        Zustand man abfragen könnte. Aber wir kennen Startzeit und Länge.
        """
        if not zustand.get("t0"):
            return False
        lauf = zustand["start"] + (time.time() - zustand["t0"])
        if zustand.get("abschnitte"):
            return lauf < zustand["abschnitte"][-1][2]
        if zustand.get("dauer"):
            return lauf < zustand["dauer"]
        return False

    def laeuft():
        p = zustand["spieler"]
        if p is None:
            return False
        if hasattr(p, "poll"):
            return p.poll() is None
        return zeit_laeuft()

    

    def stoppen(merker_weg=True):
        """Wiedergabe beenden. merker_weg=False lässt eine Vormerkung stehen
        (z. B. beim Springen in der Wellenform)."""
        if merker_weg:
            zeige_vormerken(None)
            zustand["vorgemerkt"] = None
        p = zustand["spieler"]
        if p is not None:
            stoppen_ton(p)
        zustand["spieler"] = None
        if zustand.get("temp"):
            try:
                os.unlink(zustand["temp"])
            except OSError:
                pass
            zustand["temp"] = None
        zustand["marke"] = None
        alt = zustand.get("beobachter")
        if alt:
            try:
                fenster.after_cancel(alt)
            except Exception:
                pass
            zustand["beobachter"] = None
        zeichne_welle()
        if knopf_stopp is not None:
            knopf_stopp.config(state="disabled")
        status_var.set("")

    def _auswahl_auf(nummer):
        """Markierung auf einen Patch setzen, ohne etwas zu starten."""
        nummern = zustand["nummern"]
        if nummer not in nummern:
            return
        i = nummern.index(nummer)
        liste.selection_clear(0, "end")
        liste.selection_set(i)
        if time.time() - zustand.get("nutzer_scroll", 0) > 5:
            liste.see(i)
        welle_laden()

    def naechster():
        """Den Patch nach dem gerade gespielten starten."""
        nummern = zustand["nummern"]
        jetzt = zustand.get("spielend")
        if not nummern or jetzt is None or jetzt not in nummern:
            return
        i = nummern.index(jetzt)
        if i + 1 >= len(nummern):
            status_var.set("Ende der Liste erreicht")
            return
        if zustand.get("marker") is not None:
            strich_weg()
        liste.selection_clear(0, "end")
        liste.selection_set(i + 1)
        liste.see(i + 1)
        welle_laden()
        abspielen()

    def plane_beobachten(ms=300):
        """Plant die Überwachung - und hebt eine alte auf.

        Ohne das liefen mehrere Beobachter gleichzeitig (nach einem Sprung
        während der Wiedergabe) und schrieben abwechselnd die Marke.
        """
        alt = zustand.get("beobachter")
        if alt:
            try:
                fenster.after_cancel(alt)
            except Exception:
                pass
        zustand["beobachter"] = fenster.after(ms, beobachten)

    def umschalten_auf_merker():
        """Am Ende des laufenden Patches auf die vorbereitete Kette wechseln."""
        datei = zustand.get("vorgemerkt_datei")
        abschnitte = zustand.get("vorgemerkt_abschnitte") or []
        nummern = zustand["nummern"]
        if not datei or not abschnitte:
            return
        zustand["vorgemerkt_datei"] = None
        zustand["vorgemerkt_abschnitte"] = []
        zustand["vorgemerkt"] = None
        zeige_vormerken(None)
        alt = zustand.get("spieler")
        if alt is not None:
            stoppen_ton(alt)
        if zustand.get("temp"):
            try:
                os.unlink(zustand["temp"])
            except OSError:
                pass
        zustand["spieler"] = abspielen_datei(datei)
        zustand["temp"] = datei
        zustand["abschnitte"] = abschnitte
        zustand["start"] = 0.0
        zustand["t0"] = time.time()
        zustand["spielend"] = abschnitte[0][0]
        zustand["marke"] = 0.0
        if abschnitte[0][0] in nummern:
            _auswahl_auf(abschnitte[0][0])
        zeichne_welle()
        plane_beobachten(120)

    def plane_umschalten():
        """Zeitpunkt für das Umschalten aus der laufenden Wiedergabe planen.

        Muss nach jedem Sprung neu laufen, weil dann eine andere Stelle
        läuft.
        """
        alt = zustand.get("umschalt_plan")
        if alt:
            try:
                fenster.after_cancel(alt)
            except Exception:
                pass
            zustand["umschalt_plan"] = None
        if zustand.get("vorgemerkt") is None:
            return
        jetzt, position = stelle_im_patch()
        rest = None
        if jetzt is not None and position is not None:
            if zustand.get("abschnitte"):
                for nr, von, bis, versatz in zustand["abschnitte"]:
                    if nr == jetzt and versatz <= position < versatz + (bis - von):
                        rest = (bis - von) - (position - versatz)
                        break
            elif zustand.get("dauer"):
                rest = zustand["dauer"] - position
        if rest is not None and rest > 0.3:
            zustand["umschalt_plan"] = fenster.after(
                max(0, int((rest - 0.1) * 1000)), umschalten_auf_merker)

    def zeige_vormerken(nummer=None):
        """Vorgemerkten Zustand am Knopf sichtbar machen.

        Auf macOS zeichnet Tk die Knopffläche nicht um, ein farbiger
        Rahmen (highlightbackground) geht aber.
        """
        if nummer is None:
            knopf_vormerken.config(text=T("vormerken"),
                                   highlightthickness=ursprung_rahmen[0],
                                   highlightbackground=ursprung_rahmen[1],
                                   relief="raised")
            rahmen_vormerken.config(bg=FARBE_HINTERGRUND)
        else:
            knopf_vormerken.config(text=T("vorgemerkt %d") % nummer,
                                   highlightthickness=3,
                                   highlightbackground="#e8b04b",
                                   highlightcolor="#e8b04b",
                                   relief="raised")
            rahmen_vormerken.config(bg="#e8b04b")

    def spiel_ueberwachen():
        """Nachsehen, ob noch etwas klingt - und die Vormerkung ausführen."""
        merker = zustand.get("vorgemerkt")
        if merker is not None and not laeuft():
            zustand["vorgemerkt"] = None
            gehe_zu(merker)
            return
        if zustand.get("spielend") is not None:
            fenster.after(400, spiel_ueberwachen)

    def beobachten():
        if not laeuft() and zustand.pop("weiter_gewuenscht", False):
            # Nach dem Ende der Kette beim letzten gespielten Patch neu
            # ansetzen - die Kette selbst war schon nahtlos.
            zustand["abschnitte"] = []
            naechster()
            return
        if laeuft():
            lauf = zustand["start"] + (time.time() - zustand.get("t0", time.time()))
            if zustand.get("abschnitte"):
                for nr, von, bis, versatz in zustand["abschnitte"]:
                    if von <= lauf < bis:
                        if zustand.get("vorgemerkt") == nr:
                            zustand["vorgemerkt"] = None
                            zeige_vormerken(None)
                        zustand["marke"] = max(0.0, min(zustand["dauer"],
                                                        versatz + (lauf - von)))
                        if zustand.get("spielend") != nr:
                            zustand["spielend"] = nr
                            _auswahl_auf(nr)      # lädt Welle und zeichnet Marke
                        else:
                            zeichne_welle(nur_marke=True)
                        break
                plane_beobachten(120)
                return
            if zustand["dauer"]:
                zustand["marke"] = min(zustand["dauer"], lauf)
                zeichne_welle(nur_marke=True)
            plane_beobachten(300)
        else:
            zustand["spieler"] = None
            if zustand.get("temp"):
                try:
                    os.unlink(zustand["temp"])
                except OSError:
                    pass
                zustand["temp"] = None
            if knopf_stopp is not None:
                knopf_stopp.config(state="disabled")
            merker = zustand.get("vorgemerkt")
            if merker is not None:
                zustand["vorgemerkt"] = None
                gehe_zu(merker)
                return
            if weiter_var.get() and zustand.get("spielend") is not None:
                zustand["weiter_gewuenscht"] = True
                plane_beobachten(30)
            else:
                status_var.set("")

    def neu_laden(behalte=True):
        # Auswahl merken: nach dem Neuladen soll derselbe Patch markiert
        # bleiben, sonst muss man nach jeder Änderung neu klicken.
        vorher = None
        i = liste_auswahl()
        if i and zustand["nummern"]:
            if 0 <= i[0] < len(zustand["nummern"]):
                vorher = zustand["nummern"][i[0]]
        zeilen = []
        zustand["nummern"] = []
        # „Neu laden“ muß auch dann helfen, wenn der gemerkte Ordner
        # verschwunden ist — etwa weil die Karte erst nach dem Start
        # eingesteckt wurde (16.11.2026: Knopf wirkte wirkungslos).
        if not zustand["basis"] or not os.path.isdir(zustand["basis"]):
            zustand["basis"] = karte_finden()
        if not zustand["basis"]:
            pfad_var.set(T("keine Karte gefunden"))
            try:
                titel_var.set(T("keine Karte"))
            except Exception:
                pass
            zustand["alle"] = []
            liste_setzen([
                {"text": "  " + T("Keine Karte im Rechner gefunden.")},
                {"text": ""},
                {"text": "  " + T("Karte im Rechner:  „Karte wählen“ drücken.")},
                {"text": "  " + T("Karte im Pedal:    oben rechts „Pedal über USB“ — "
                                  "dort sind Karte und interner Speicher erreichbar.")},
            ])
            fenster.title(T("JamMan Card Tool") + " — " + T("keine Karte"))
            zeilen_anwenden()
            return
        pfad_var.set(zustand["basis"])
        try:
            titel_var.set(_fenster_titel(zustand["basis"], UNTERORDNER))
        except Exception:
            pass
        for nummer, name, pfad in patches(zustand["basis"]):
            w = phrase_wav(pfad)
            stop, rhy, loop, rueck, bpm = patch_einstellungen(pfad)
            dauer = wav_info(w)[0] if w else 0
            marke = "  ← über 10 min" if dauer > MAX_LOOP_SEKUNDEN else ""
            # Rückwärts und Tempo gehörten schon immer ins Pedal, standen aber
            # nirgends (18.11.2026): „↺" hinter der Wiedergabe, BPM als Spalte.
            wiedergabe = loop + (" ↺" if rueck else "")
            tempo = ("%d BPM" % round(bpm)) if bpm else "—"
            zeilen.append({"text": "  %-10s %-28s %8s  %-8s %-9s %-6s %s%s" % (
                name, patch_name(pfad)[:28], sekunden(dauer) if w else "—",
                stop, wiedergabe, tempo, rhy, marke),
                "nummer": nummer, "name": patch_name(pfad),
                "dauer": dauer, "bpm": bpm, "rueck": rueck,
                "hat_ton": bool(w)})
            zustand["nummern"].append(nummer)
        ohne_ton = sum(1 for zeile in zeilen if not zeile.get("hat_ton", True))
        if ohne_ton:
            zeilen.append({"text": "  " + T("%d Patches ohne Ton — nur die "
                                            "Beschreibung ist geholt.") % ohne_ton})
        zustand["alle"] = zeilen
        zeilen_anwenden()
        if zustand["nummern"]:
            fenster.title(T("JamMan Card Tool") + " — %s (%d)" % (
                T("Karte und interner Speicher"), len(zustand["nummern"])))
            if not zustand["nummern"] and zustand["basis"] and not zustand.get("lesefehler_gemeldet"):
                fehler = karte_lesefehler(zustand["basis"])
                if fehler:
                    zustand["lesefehler_gemeldet"] = True
                    messagebox.showerror(
                        T("Karte nicht lesbar"),
                        T("Die Karte ist eingebunden, aber das Dateisystem liefert\n"
                          "Lesefehler:\n\n%s\n\n"
                          "Das liegt an der Karte oder am Einhängepunkt, nicht am\n"
                          "Werkzeug.\n\n"
                          "Erste Schritte:\n"
                          "  - Karte im JamMan einschalten (das Pedal prüft sie)\n"
                          "  - dmesg | tail -20\n"
                          "  - aushängen und prüfen: sudo fsck.vfat -n <gerät>\n"
                          "    bzw. sudo exfatfsck <gerät>")
                        % "\n".join(fehler[:4]))
            if behalte and vorher is not None and vorher in zustand["nummern"]:
                i = zustand["nummern"].index(vorher)
                liste_waehlen(i)
                liste_zeigen(i)
            # Spur mitziehen: nach Löschen/Umbenennen darf keine alte
            # Wellenform stehen bleiben (am 10.11.2026 gesehen).
            welle_laden()
        else:
            fenster.title("JamMan Solo XT — keine Patches")
            liste_setzen([{"text": "  Keine Patches im Ordner %s"
                                    % os.path.basename(zustand["basis"])}])
            zustand["welle"] = []
            zustand["dauer"] = 0.0
            zustand["marke"] = None
            zeichne_welle()
            zeit_var.set(T("kein Patch gewählt"))

    def zeichne_welle(nur_marke=False):
        """Wellenform zeichnen - oder nur die Marke verschieben.

        Die 900 Striche bei jeder Bewegung neu zu zeichnen war der Grund,
        warum das Fenster träge wurde (am 10.11.2026 mit Beachball).
        """
        breite = max(200, leinwand.winfo_width() or 900)
        hoehe = 84
        if nur_marke and zustand.get("marke_id") and zustand["dauer"]:
            x = breite * (zustand["marke"] / zustand["dauer"])
            leinwand.coords(zustand["marke_id"], x, 0, x, hoehe)
            return
        leinwand.delete("all")
        zustand["marke_id"] = None
        if not zustand["welle"]:
            leinwand.create_text(10, hoehe//2, anchor="w", fill=FARBE_LEISE,
                                 text=T("Patch markieren — dann hier klicken zum Springen"))
            return
        werte = zustand["welle"]
        schritt = breite / float(len(werte))
        mitte = hoehe // 2
        for i, wert in enumerate(werte):
            x = i * schritt
            h = max(1, int(wert * (hoehe/2 - 4)))
            leinwand.create_line(x, mitte - h, x, mitte + h, fill="#5a9fd4")
        leinwand.create_line(0, mitte, breite, mitte, fill="#333333")
        if zustand["marke"] is not None and zustand["dauer"]:
            x = breite * (zustand["marke"] / zustand["dauer"])
            zustand["marke_id"] = leinwand.create_line(x, 0, x, hoehe,
                                                       fill="#e05a5a", width=2)

    def welle_laden():
        nummer = gewaehlt()
        if nummer is not None and zustand.get("welle_fuer") == nummer and zustand["welle"]:
            zeichne_welle()
            return
        zustand["welle_fuer"] = nummer
        zustand["welle"] = []
        zustand["dauer"] = 0.0
        # Marke NICHT löschen: beim Weiterspielen wird sie gleich wieder
        # gesetzt, und ein Neuzeichnen soll sie nicht verschlucken.

        if not nummer:
            zeichne_welle()
            zeit_var.set(T("kein Patch gewählt"))
            return
        alle = dict((nr, pf) for nr, _, pf in patches(zustand["basis"]))
        wav = phrase_wav(alle[nummer])
        if not wav:
            zeichne_welle()
            return
        werte, dauer, _ = wellenform(wav, 900)
        zustand["welle"] = werte
        zustand["dauer"] = dauer
        zeit_var.set("%s   •   %s   •   Klick in die Spur springt dorthin"
                     % (patch_name(alle[nummer]), sekunden(dauer)))
        zeichne_welle()

    def kette_bauen(start_nummer, position=0.0, cue=None, grenze=30 * 60):
        """Datei bauen: ab 'position' im Startpatch, dann (cue) und die Folgenden.

        Gibt (datei, abschnitte) zurück; abschnitte = [(plätze, von, bis, versatz)].
        """
        nummern = zustand["nummern"]
        if start_nummer not in nummern:
            return None, []
        i = nummern.index(start_nummer)
        alle = dict((nr, pf) for nr, _, pf in patches(zustand["basis"]))
        pfade = []
        versatz_start = 0.0
        if position > 0.01:
            rest = neue_tempdatei("jamman-rest-", ".wav")
            dauer_patch = wav_info(phrase_wav(alle[start_nummer]))[0]
            if not schneide_bereich(phrase_wav(alle[start_nummer]), rest,
                                    position, dauer_patch):
                return None, []
            pfade.append((start_nummer, rest))
            versatz_start = position
        else:
            pfade.append((start_nummer, phrase_wav(alle[start_nummer])))
        if cue is not None and cue in nummern and cue != start_nummer:
            # Vorgemerkter Patch zuerst, danach die, die NACH IHM kommen.
            j = nummern.index(cue)
            folge = [cue] + nummern[j+1:]
        else:
            folge = nummern[i+1:]
        gesamt = wav_info(pfade[0][1])[0]
        for nr in folge:
            pfad = phrase_wav(alle[nr])
            if not pfad:
                break
            d = wav_info(pfad)[0]
            if len(pfade) >= 2 and gesamt + d > grenze:
                break
            pfade.append((nr, pfad))
            gesamt += d
            if len(pfade) >= 8:
                break
        if len(pfade) < 2:
            return None, []
        datei = neue_tempdatei("jamman-kette-", ".wav")
        dauern, _ = verbinde_patches([pf for _, pf in pfade], datei)
        if not dauern:
            try:
                os.unlink(datei)
            except OSError:
                pass
            return None, []
        abschnitte = []
        lauf = 0.0
        for k, ((nr, _), d) in enumerate(zip(pfade, dauern)):
            abschnitte.append((nr, lauf, lauf + d, versatz_start if k == 0 else 0.0))
            lauf += d
        # Aufräumen der Ausschnittsdatei(en) - die Kette hat den Inhalt
        for nr, pf in pfade:
            if pf != phrase_wav(alle[nr]):
                try:
                    os.unlink(pf)
                except OSError:
                    pass
        return datei, abschnitte

    def stelle_im_patch():
        """Wie weit ist die Wiedergabe im KLINGENDEN Patch? (Sekunden)"""
        if not zustand.get("spieler") or not zustand.get("t0"):
            return None, None
        lauf = zustand["start"] + (time.time() - zustand["t0"])
        for nr, von, bis, versatz in zustand.get("abschnitte") or []:
            if von <= lauf < bis:
                return nr, versatz + (lauf - von)
        if zustand.get("dauer"):
            return zustand.get("spielend"), min(zustand["dauer"], lauf)
        return zustand.get("spielend"), 0.0

    def vormerken(nummer=None):
        """Einen Patch vor den nächsten setzen - er kommt, wenn der jetzige
        zu Ende ist (wie das Vormerken mit dem FS3X am Pedal)."""
        nummern = zustand["nummern"]
        if nummer is None:
            try:
                nummer = int(platz_eingabe.get().strip())
            except (ValueError, AttributeError):
                messagebox.showerror(T("Fehler"), T("Bitte eine Platznummer eintragen."))
                return
        if nummer not in nummern:
            messagebox.showerror(T("Fehler"), "Platz %d gibt es nicht." % nummer)
            return
        zustand["vorgemerkt"] = nummer
        # Kette für den vorgemerkten Patch schon jetzt bauen: reine
        # Dateiarbeit, der Ton läuft ungestört weiter.
        datei, abschnitte = kette_bauen(nummer, 0.0, None)
        if datei:
            alte = zustand.get("vorgemerkt_datei")
            if alte:
                try:
                    os.unlink(alte)
                except OSError:
                    pass
            zustand["vorgemerkt_datei"] = datei
            zustand["vorgemerkt_abschnitte"] = abschnitte
        else:
            zustand["vorgemerkt_datei"] = None
            zustand["vorgemerkt_abschnitte"] = []
        # Bewusst KEIN Neuaufbau des Tonstroms: das verursachte einen
        # kurzen Aussetzer mitten im laufenden Patch (Rückmeldung vom
        # 10.11.2026). Die Vormerkung wird stattdessen am Ende des
        # laufenden Patches ausgeführt.
        status_var.set("vorgemerkt: Platz %d" % nummer)
        zeige_vormerken(nummer)
        # Zeitgesteuert umschalten: das Ende des laufenden Patches ist
        # bekannt, also lässt sich der Wechsel vorher planen. Auf das Ende
        # des Tonprozesses zu warten kostete hörbar Zeit (0,7 s gemessen).
        plane_umschalten()
        fenster.after(400, spiel_ueberwachen)

    def gehe_zu(nummer=None):
        """Einen bestimmten Platz auswählen und abspielen (wie am Pedal)."""
        nummern = zustand["nummern"]
        if nummer is None:
            roh = platz_eingabe.get().strip()
            if not roh:
                # Ohne Nummer: den nächsten Patch - wie "weiter" am Pedal
                jetzt = zustand.get("spielend")
                if jetzt not in nummern:
                    auswahl = liste_auswahl()
                    jetzt = nummern[auswahl[0]] if auswahl else None
                if jetzt is None or jetzt not in nummern:
                    return
                i = nummern.index(jetzt)
                if i + 1 >= len(nummern):
                    status_var.set("Ende der Liste erreicht")
                    return
                nummer = nummern[i + 1]
            else:
                try:
                    nummer = int(roh)
                except ValueError:
                    messagebox.showerror(T("Fehler"), T("Bitte eine Platznummer eintragen."))
                    return
        if nummer not in nummern:
            messagebox.showerror(T("Fehler"),
                                 "Platz %d gibt es auf dieser Karte nicht." % nummer)
            return
        i = nummern.index(nummer)
        if zustand.get("marker") is not None:
            strich_weg()
        liste.selection_clear(0, "end")
        liste.selection_set(i)
        liste.see(i)
        welle_laden()
        abspielen()

    def tippen(e):
        """Ziffern in der Liste tippen und mit Enter zum Platz springen.

        Das ist der Weg wie am Pedal: Nummer eintippen, fertig. Er hängt
        nicht am Fokus eines Eingabefeldes.
        """
        if getattr(e.widget, "winfo_class", lambda: "")() == "Entry":
            return None            # dort tippt Tk selbst
        zeichen = getattr(e, "char", "") or ""
        if zeichen.isdigit():
            zustand["tippen"] = (zustand.get("tippen") or "") + zeichen
            try:
                platz_eingabe.delete(0, "end")
                platz_eingabe.insert(0, zustand["tippen"])
            except Exception:
                pass
            status_var.set("Platz %s  (Eingabetaste springt hin, Esc löscht)"
                           % zustand["tippen"])
            return "break"
        if e.keysym in ("Return", "KP_Enter"):
            roh = zustand.get("tippen")
            zustand["tippen"] = ""
            if roh:
                gehe_zu(int(roh))
            return "break"
        if e.keysym == "Escape":
            zustand["tippen"] = ""
            try:
                platz_eingabe.delete(0, "end")
            except Exception:
                pass
            status_var.set("")
            return "break"
        return None

    def springe(event):
        """Klick in die Spur: ab dieser Stelle abspielen."""
        if not zustand["welle"] or not zustand["dauer"]:
            return
        breite = max(1, leinwand.winfo_width())
        anteil = min(1.0, max(0.0, event.x / float(breite)))
        ziel = anteil * zustand["dauer"]
        nummer = gewaehlt()
        if not nummer:
            return
        alle = dict((nr, pf) for nr, _, pf in patches(zustand["basis"]))
        wav = phrase_wav(alle[nummer])
        if laeuft():
            stoppen(merker_weg=False)
        zustand["abschnitte"] = []
        griff = temp = None
        start = 0.0             # Zeitbasis: Anfang der Kette bzw. des Ausschnitts
        if weiter_var.get():
            # Auch beim Springen nahtlos weiter: Ausschnitt ab der
            # Klickstelle plus die folgenden Patches in EINER Datei.
            # Sonst entsteht am Ende des Ausschnitts wieder eine Pause
            # (am 10.11.2026 bei einer Karte mit langen Teilen gehört).
            nummern = zustand["nummern"]
            i = nummern.index(nummer) if nummer in nummern else -1
            rest = neue_tempdatei("jamman-rest-", ".wav")
            dauer_patch = wav_info(wav)[0]
            if schneide_bereich(wav, rest, ziel, dauer_patch):
                folge = [(nummer, rest)]
                gesamt = dauer_patch - ziel
                for nr in nummern[i+1:]:
                    pfad2 = phrase_wav(alle[nr])
                    if not pfad2:
                        break
                    d2 = wav_info(pfad2)[0]
                    if len(folge) >= 2 and gesamt + d2 > 30 * 60:
                        break
                    folge.append((nr, pfad2))
                    gesamt += d2
                    if len(folge) >= 8:
                        break
                kette = neue_tempdatei("jamman-kette-", ".wav")
                dauern, _ = verbinde_patches([pf for _, pf in folge], kette)
                try:
                    os.unlink(rest)
                except OSError:
                    pass
                if dauern:
                    griff = abspielen_datei(kette)
                    temp = kette
                    lauf = 0.0
                    for kk, ((nr, _), d) in enumerate(zip(folge, dauern)):
                        versatz = ziel if kk == 0 else 0.0
                        zustand["abschnitte"].append((nr, lauf, lauf + d, versatz))
                        lauf += d
        if griff is None:
            zustand["abschnitte"] = []
            griff, temp, start = abspielen_ab(wav, ziel)
        zustand["spieler"] = griff
        zustand["temp"] = temp
        zustand["start"] = start
        zustand["spielend"] = nummer
        zustand["t0"] = time.time()
        zustand["marke"] = ziel
        zeichne_welle()
        if knopf_stopp is not None:
            knopf_stopp.config(state="normal")
        status_var.set("spielt: %s ab %s" % (patch_name(alle[nummer]), sekunden(ziel)))
        plane_umschalten()
        beobachten()

    def karte_waehlen():
        # Dort beginnen, wo Wechselmedien liegen - sonst landet der Dialog
        # im Benutzerordner und man sucht die Karte selbst.
        start = ""
        for ort in karten_wurzeln():
            if os.path.isdir(ort) and os.listdir(ort):
                start = ort
                break
        p = ordner_waehlen(fenster, T("Speicherkarte des JamMan wählen"),
                               start or None)
        if not p:
            return
        try:
            zustand["basis"] = karte_pruefen(p)
            neu_laden()
        except SystemExit as e:
            messagebox.showerror(T("Keine JamMan-Karte"), str(e))

    def wahl_wert(var):
        """Angezeigten Text zurück auf den internen Wert führen."""
        wert = var.get()
        for intern, anzeige in (("wie Vorlage", "wie Vorlage"),
                                ("fade", "fade"),
                                ("instant", "sofort"),
                                ("finish", "ende"),
                                ("silence", "ohne"),
                                ("kick", "klick"),
                                ("loop", "loop"),
                                ("single", "single")):
            if wert in (T(anzeige), anzeige, T(intern), intern):
                return intern
        return wert

    def gewaehlt():
        """Platznummer der markierten Zeile — None bei Hinweiszeilen.

        Hinweiszeilen (etwa „N Patches ohne Ton …") haben keine Nummer; der
        Zugriff auf zustand["nummern"] lief dabei aus dem Index
        (18.11.2026: IndexError beim Klick auf eine solche Zeile).
        """
        i = liste_auswahl()
        if not i:
            return None
        if 0 <= i[0] < len(zustand["nummern"]):
            return zustand["nummern"][i[0]]
        return None

    def abspielen():
        # Läuft schon etwas, wird es beendet und der gewählte Patch
        # gestartet. Die Beschriftung bleibt "Anhören" - sonst stünden
        # zwei Knöpfe mit "Stopp" nebeneinander (am 10.11.2026 gesehen).
        if laeuft():
            stoppen(merker_weg=False)
        n = gewaehlt()
        if not n:
            messagebox.showinfo(T("Hinweis"), T("Erst einen Patch in der Liste wählen."))
            return
        alle = dict((nr, pf) for nr, _, pf in patches(zustand["basis"]))
        w = phrase_wav(alle[n])
        if not w:
            messagebox.showerror(T("Fehler"), "Patch%d hat keine WAV." % n)
            return
        # Nahtloses Weiterspielen: lieber eine einzige Datei aus mehreren
        # Patches bauen als jeden Patch einzeln zu starten (das ergab eine
        # hörbare Pause, Rückmeldung vom 10.11.2026).
        zustand["abschnitte"] = []
        datei, abschnitte = (None, [])
        if weiter_var.get() or zustand.get("vorgemerkt"):
            datei, abschnitte = kette_bauen(n, 0.0, zustand.get("vorgemerkt"))
        if datei:
            zustand["spieler"] = abspielen_datei(datei)
            zustand["temp"] = datei
            zustand["abschnitte"] = abschnitte
        else:
            # Rückwärts-Patches auch rückwärts vorspielen: das Pedal macht
            # das anhand von IsReversed, das Fenster spielte sonst das
            # Original (Rückmeldung 18.11.2026).
            _, _, _, rueck, _ = patch_einstellungen(alle[n])
            probe = wav_rueckwaerts(w) if rueck else None
            if rueck and not probe:
                status_var.set(T("Rückwärts: ohne ffmpeg keine Hörprobe — "
                                 "das Pedal spielt es trotzdem rückwärts."))
            zustand["spieler"] = abspielen_datei(probe or w)
            if probe:
                zustand["temp"] = probe
        zustand["vorgemerkt"] = None
        zustand["t0"] = time.time(); zustand["start"] = 0.0
        zustand["spielend"] = n
        zustand["marke"] = 0.0
        zeichne_welle()
        if knopf_stopp is not None:
            knopf_stopp.config(state="normal")
        status_var.set("spielt: %s" % patch_name(alle[n]))
        plane_umschalten()
        plane_beobachten(300)

    def pegel_lesen():
        """Gibt (gain, normieren) zurück; wirft ValueError bei Unsinn."""
        if normieren_var.get():
            return None, True
        roh = pegel_eingabe.get().strip().replace(",", ".").replace("+", "")
        if not roh:
            return None, False
        return float(roh), False

    def aufspielen():
        if not zustand["basis"]:
            messagebox.showinfo(T("Hinweis"), T("Erst eine Karte wählen."))
            return
        datei = datei_waehlen(fenster, T("Audiodatei wählen"), "*.wav *.aif *.aiff *.aifc *.caf *.mp3 *.m4a *.aac *.flac *.ogg *.opus *.wma",
                                einstellung_lesen("audio_pfad") or None)
        einstellung_schreiben("audio_pfad", os.path.dirname(datei))
        if not datei:
            return

        # Zu lange Dateien: gleich anbieten zu kürzen statt nur abzulehnen
        kuerzen = False
        dauer_datei = wav_info(datei)[0]
        if dauer_datei > MAX_LOOP_SEKUNDEN:
            if not frage_ja_nein(fenster, "Datei zu lang",
                    "Die Datei ist %.1f Minuten lang.\n\n"
                    "Der JamMan Solo XT nimmt höchstens %d Minuten pro Loop an\n"
                    "(Handbuch, Seite 60). Längere Loops erscheinen auf der Karte,\n"
                    "werden vom Pedal aber nicht erkannt und können verschwinden.\n\n"
                    "Auf 9:59 kürzen? Der Schnitt liegt am Ende; die letzten\n"
                    "12 ms werden ausgeblendet, damit es nicht knackt."
                    % (dauer_datei / 60.0, MAX_LOOP_SEKUNDEN // 60)):
                return
            kuerzen = True

        if not frage_ja_nein(fenster, "Aufspielen",
                "Die Karte wird verändert:\n\n%s\n\nFortfahren?"
                % os.path.basename(datei)):
            return
        try:
            gain_wert, normieren = pegel_lesen()
        except ValueError:
            messagebox.showerror(T("Fehler"),
                "Beim Pegel bitte eine Zahl in dB eintragen (z. B. -3), "
                "oder das Feld leer lassen.")
            return
        try:
            st = None if wahl_wert(stopp_wahl) == "wie Vorlage" else wahl_wert(stopp_wahl)
            rh = None if wahl_wert(rhythmus_wahl) == "wie Vorlage" else wahl_wert(rhythmus_wahl)
            wg = None if wahl_wert(wiedergabe_wahl) == "wie Vorlage" else wahl_wert(wiedergabe_wahl)
            fade_wert = None
            roh = fade_eingabe.get().strip().replace(",", ".")
            if roh:
                try:
                    fade_wert = float(roh)
                except ValueError:
                    messagebox.showerror(T("Fehler"), T("Bei Fade am Ende bitte eine Zahl in Sekunden eintragen."))
                    return
                if fade_wert <= 0:
                    fade_wert = None
            befehl_aufspielen(zustand["basis"], datei, None, True, st, rh, wg,
                              fade_wert, gain_wert, normieren,
                              begrenzen_var.get(), kuerzen=kuerzen)
            neu_laden()
        except SystemExit as e:
            messagebox.showerror(T("Fehler"), str(e))

    def sichern():
        if not zustand["basis"]:
            messagebox.showinfo(T("Hinweis"), T("Erst eine Karte wählen."))
            return
        ziel = ordner_waehlen(fenster, T("Zielordner für die Sicherung"))
        if not ziel:
            return
        befehl_sichern(zustand["basis"], ziel, True)
        messagebox.showinfo(T("Sicherung"), "Fertig. Ordner:\n%s" % ziel)

    def umbenennen():
        """Namen des gewählten Patches ändern."""
        n = gewaehlt()
        if not n:
            messagebox.showinfo(T("Hinweis"), T("Erst einen Patch in der Liste wählen."))
            return
        alle = dict((nr, pf) for nr, _, pf in patches(zustand["basis"]))
        alt = patch_name(alle[n])
        neu = simpledialog.askstring("Umbenennen",
                "Neuer Name für Patch%d:" % n, initialvalue=alt, parent=fenster)
        if neu is None:
            return
        neu = neu.strip()
        if not neu:
            messagebox.showinfo(T("Hinweis"), T("Der Name darf nicht leer sein."))
            return
        if neu == alt:
            return
        try:
            befehl_aendern(zustand["basis"], n, True, None, None, None, neu)
            neu_laden()
            status_var.set("umbenannt: %s → %s" % (alt, neu))
        except SystemExit as e:
            messagebox.showerror(T("Fehler"), str(e))

    def zuruecknehmen():
        """Sicherung des gewählten Patches zurückholen (z. B. Fade rückgängig)."""
        n = gewaehlt()
        if not n:
            messagebox.showinfo(T("Hinweis"), T("Erst einen Patch in der Liste wählen."))
            return
        ordner = os.path.expanduser(SICHERUNG_WURZEL)
        kandidaten = zuruecknehmen_kandidaten(ordner, n)
        if not kandidaten:
            messagebox.showerror(T("Fehler"),
                "Für Patch%d liegt in %s keine Sicherung mit Aufnahme."
                % (n, ordner))
            return
        quelle = kandidaten[0]
        name = os.path.basename(os.path.dirname(quelle))
        stand = name.replace("Sicherung_", "").replace("_", " ")
        if not frage_ja_nein(fenster, "Sicherung zurückholen",
                "Patch%d wird aus dieser Sicherung wiederhergestellt:\n\n"
                "  %s\n\n"
                "Damit lässt sich zum Beispiel ein Fade zurücknehmen.\n"
                "Der jetzige Stand wird vorher gesichert." % (n, stand)):
            return
        try:
            befehl_zuruecknehmen(zustand["basis"], n, True)
            neu_laden()
            status_var.set("Patch%d zurückgeholt" % n)
        except SystemExit as e:
            messagebox.showerror(T("Fehler"), str(e))

    def pegel_zurueck():
        """Pegeländerung aus einem Protokoll zurückrechnen."""
        if not zustand["basis"]:
            messagebox.showinfo(T("Hinweis"), T("Erst eine Karte wählen."))
            return
        ordner = os.path.expanduser(SICHERUNG_WURZEL)
        datei = datei_waehlen(fenster, T("Pegelprotokoll wählen"), "*.txt",
                              ordner if os.path.isdir(ordner) else None)
        if not datei:
            return
        if not frage_ja_nein(fenster, "Pegel zurückrechnen",
                "Die Pegeländerungen aus diesem Protokoll werden umgekehrt:\n\n"
                "%s\n\nVorher wird alles gesichert. Fortfahren?"
                % os.path.basename(datei)):
            return
        try:
            befehl_pegel_umkehren(zustand["basis"], datei, True)
            neu_laden()
            status_var.set(T("Pegel zurückgerechnet"))
        except SystemExit as e:
            messagebox.showerror(T("Fehler"), str(e))

    def datei_teilen():
        """Eine Datei in mehrere Patches aufteilen."""
        if not zustand["basis"]:
            messagebox.showinfo(T("Hinweis"), T("Erst eine Karte wählen."))
            return
        try:
            anzahl = int(teile_eingabe.get().strip() or "2")
        except ValueError:
            messagebox.showerror(T("Fehler"), T("Bei Teile bitte eine ganze Zahl eintragen."))
            return
        if not (2 <= anzahl <= 60):
            messagebox.showerror(T("Fehler"), T("Bitte 2 bis 60 Teile angeben."))
            return
        datei = datei_waehlen(fenster, T("Audiodatei zum Teilen auswählen"),
                              "*.wav *.aif *.aiff *.aifc *.caf *.mp3 *.m4a *.aac *.flac *.ogg *.opus")
        if not datei:
            return
        dauer = wav_info(datei)[0]
        if dauer <= 0:
            messagebox.showerror(T("Fehler"), T("Die Datei ist nicht lesbar."))
            return
        art = ("an Pausen geschnitten" if sinnvoll_var.get()
               else "gleichmäßig geschnitten")
        if not frage_ja_nein(fenster, "Datei teilen",
                "%s\n\n%s wird in %d Teile geteilt (%s).\n"
                "Jeder Teil landet auf einem freien Platz.\n\nFortfahren?"
                % (os.path.basename(datei), sekunden(dauer), anzahl, art)):
            return
        def melde(text):
            status_var.set(text)
            fenster.update_idletasks(); fenster.update()
        try:
            befehl_teilen(zustand["basis"], datei,
                          os.path.splitext(os.path.basename(datei))[0],
                          True, anzahl, None, None, None, None,
                          sinnvoll=sinnvoll_var.get())
            neu_laden()
            status_var.set("in %d Patches geteilt" % anzahl)
        except SystemExit as e:
            messagebox.showerror(T("Fehler"), str(e))

    def alle_normieren():
        """Alle Patches auf gleiche Lautheit bringen."""
        if not zustand["basis"]:
            messagebox.showinfo(T("Hinweis"), T("Erst eine Karte wählen."))
            return
        if not frage_ja_nein(fenster, "Alle Patches normieren",
                "Alle Patches werden auf gleiche Lautheit gebracht:\n\n"
                "  Ziel -18 dB Lautheit, Spitze höchstens -1 dB\n\n"
                "Vorher wird die ganze Karte gesichert - das dauert bei\n"
                "900 MB ein bis zwei Minuten. Fortfahren?"):
            return
        def melde(text):
            status_var.set(text)
            fenster.update_idletasks()
            fenster.update()
        try:
            befehl_alle_normieren(zustand["basis"], True, fortschritt=melde)
            neu_laden()
            status_var.set("alle Patches normiert")
        except SystemExit as e:
            messagebox.showerror(T("Fehler"), str(e))

    def platz_aendern():
        """Patch auf eine bestimmte Platznummer legen."""
        n = gewaehlt()
        if not n:
            messagebox.showinfo(T("Hinweis"), T("Erst einen Patch in der Liste wählen."))
            return
        neu = simpledialog.askinteger("Platz ändern",
                "Patch%d soll auf welchen Platz?\n"
                "(der Platz muss frei sein)" % n, parent=fenster, minvalue=1, maxvalue=999)
        if not neu or neu == n:
            return
        try:
            befehl_platzieren(zustand["basis"], n, neu, True)
            neu_laden()
            status_var.set("Patch%d ist jetzt auf Platz %d" % (n, neu))
        except SystemExit as e:
            messagebox.showerror(T("Fehler"), str(e))

    def tauschen():
        """Zwei markierte Patches tauschen die Plätze."""
        i = liste_auswahl()
        if len(i) != 2:
            messagebox.showinfo(T("Hinweis"),
                T("Zum Tauschen genau ZWEI Patches markieren:\n"
                  "ersten anklicken, zweiten mit gedrückter Cmd-Taste (⌘)."))
            return
        alle = [nr for nr, _, _ in patches(zustand["basis"])]
        a, b = alle[i[0]], alle[i[1]]
        if not frage_ja_nein(fenster, "Tauschen",
                "Patch%d und Patch%d tauschen die Plätze?\n\n"
                "Der Inhalt bleibt, nur die Ordnernamen wechseln. "
                "Vorher wird die Reihenfolge gesichert." % (a, b)):
            return
        try:
            befehl_tauschen(zustand["basis"], a, b, True)
            neu_laden()
            liste_waehlen(i[0], mehrfach=True)
            liste_waehlen(i[1], mehrfach=True)
            status_var.set("getauscht: Patch%d ↔ Patch%d" % (a, b))
        except SystemExit as f:
            messagebox.showerror(T("Fehler"), str(f))

    def suchen_dialog():
        """Suchtext über einen Dialog setzen — zweiter Weg zum Filtern.

        Anlaß (18.11.2026): Auf Windows ließ sich in das Suchfeld über der
        Liste nicht schreiben. Dieser Dialog kommt ohne das Feld aus.
        """
        alt = such_var.get().strip()
        text = _frage_text(fenster, T("Suchen"),
                           T("Text im Namen (leer lassen = alles zeigen):"), alt)
        if text is None:
            return
        such_var.set(text)
        zeilen_anwenden()

    def beschreibung_aendern():
        n = gewaehlt()
        if not n or not zustand["basis"]:
            messagebox.showinfo(T("Hinweis"), T("Erst einen Patch wählen."))
            return
        alle = dict((nr, pf) for nr, _, pf in patches(zustand["basis"]))
        if n not in alle:
            return
        beschreibung_fenster(fenster, alle[n])
        neu_laden()

    def einstellungen_anwenden():
        """Setzt die drei Auswahlfelder am GEWÄHLTEN Patch."""
        n = gewaehlt()
        if not n:
            messagebox.showinfo(T("Hinweis"), T("Erst einen Patch in der Liste wählen."))
            return
        st = None if wahl_wert(stopp_wahl) == "wie Vorlage" else wahl_wert(stopp_wahl)
        rh = None if wahl_wert(rhythmus_wahl) == "wie Vorlage" else wahl_wert(rhythmus_wahl)
        wg = None if wahl_wert(wiedergabe_wahl) == "wie Vorlage" else wahl_wert(wiedergabe_wahl)
        fade_wert = None
        roh = fade_eingabe.get().strip().replace(",", ".")
        if roh:
            try:
                fade_wert = float(roh)
            except ValueError:
                messagebox.showerror(T("Fehler"), T("Bei Fade am Ende bitte eine Zahl in Sekunden eintragen."))
                return
            if fade_wert <= 0:
                fade_wert = None
        try:
            gain_wert, normieren = pegel_lesen()
        except ValueError:
            messagebox.showerror(T("Fehler"), T("Beim Pegel bitte eine Zahl in dB eintragen (z. B. -3)."))
            return
        if not (st or rh or wg or fade_wert or gain_wert or normieren):
            messagebox.showinfo(T("Hinweis"), T("Oben steht überall „wie Vorlage - dann gibt es nichts zu ändern."))
            return
        teile = []
        if st: teile.append("Stopp: %s" % st)
        if wg: teile.append("Wiedergabe: %s" % wg)
        if rh: teile.append("Rhythmus: %s" % rh)
        if fade_wert:
            teile.append("Fade: letzte %.1f s ausblenden" % fade_wert)
        if normieren:
            teile.append("Pegel: auf −1 dB Spitze normieren")
        elif gain_wert:
            teile.append("Pegel: %+.1f dB" % gain_wert)
        if fade_wert or gain_wert or normieren:
            teile.append("")
            teile.append("Achtung: Das verändert die Aufnahme selbst. "
                         "Zweimal angewendet wirkt es zweimal.")
        if not frage_ja_nein(fenster, "Einstellungen anwenden",
                "Patch%d bekommt:\n\n%s\n\nVorher wird gesichert. Fortfahren?"
                % (n, "\n".join(teile))):
            return
        try:
            befehl_aendern(zustand["basis"], n, True, st, rh, wg, None, None,
                           fade_wert, gain_wert, normieren, begrenzen_var.get())
            neu_laden()
        except SystemExit as e:
            messagebox.showerror(T("Fehler"), str(e))

    def verschieben(schritt):
        n = gewaehlt()
        if not n or not zustand["basis"]:
            messagebox.showinfo(T("Hinweis"), T("Erst einen Patch wählen."))
            return
        alle = [nr for nr, _, _ in patches(zustand["basis"])]
        platz = alle.index(n) + 1 + schritt if n in alle else 0
        if not (1 <= platz <= len(alle)):
            return
        try:
            befehl_verschieben(zustand["basis"], alle.index(n) + 1, platz, True)
            neu_laden()
        except SystemExit as e:
            messagebox.showerror(T("Fehler"), str(e))

    def loeschen():
        n = gewaehlt()
        if not n or not zustand["basis"]:
            messagebox.showinfo(T("Hinweis"), T("Erst einen Patch wählen."))
            return
        if ist_kopie(zustand["basis"]):
            frage_text = (T("Patch%d wird aus der Kopie auf dem Rechner entfernt. "
                            "Das Pedal bleibt unverändert. Fortfahren?") % n)
        else:
            frage_text = (T("Patch%d wird von der Karte entfernt. Vorher wird er "
                            "auf dem Rechner gesichert. Fortfahren?") % n)
        if not frage_ja_nein(fenster, T("Löschen"), frage_text):
            return
        try:
            befehl_loeschen(zustand["basis"], n, True)
            neu_laden()
        except SystemExit as e:
            messagebox.showerror(T("Fehler"), str(e))

    def karte_auswerfen():
        if laeuft():
            stoppen()
        if not frage_ja_nein(fenster, "Auswerfen",
                "Karte auswerfen? Danach kann sie abgezogen werden."):
            return
        quelle = zustand["basis"] or pfad_var.get()
        try:
            befehl_auswerfen(quelle if os.path.isdir(quelle) else standard_karte())
            zustand["basis"] = None
            pfad_var.set(T("keine Karte gefunden"))
            liste_setzen([{"text": "  Karte ausgeworfen. Neue Karte einlegen und Karte wählen."}])
            zustand["welle"] = []
            zustand["dauer"] = 0.0
            zustand["marke"] = None
            zeichne_welle()
            zeit_var.set(T("kein Patch gewählt"))
            fenster.title(T("JamMan Card Tool") + " — " + T("Karte und interner Speicher"))
        except SystemExit as e:
            messagebox.showerror(T("Fehler"), str(e))

    # --- Auswahlfelder -----------------------------------------------------
    wahl = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    wahl.pack(pady=(4, 2))
    # Gilt für beide Wege: "Datei aufspielen" und "Einstellungen anwenden".
    # "Beim Aufspielen" war irreführend, weil dieselben Felder auch einen
    # vorhandenen Patch ändern.
    tk.Label(wahl, text=T("Einstellungen:"), bg=FARBE_HINTERGRUND,
             fg=FARBE_LEISE).pack(side="left", padx=(0, 10))
    # Angezeigt wird dasselbe Wort wie in der Liste (17.11.2026: dort stand
    # „sofort“, im Feld „instant“ — das verwirrte zu Recht).
    for beschriftung, werte in (
            ("Stoppart", ("wie Vorlage", "fade", "sofort", "ende")),
            ("Rhythmus", ("wie Vorlage", "ohne", "klick")),
            ("Wiedergabe", ("wie Vorlage", "loop", "single",
                            "loop \u21ba",
                            "single \u21ba"))):
        tk.Label(wahl, text=T(beschriftung), bg=FARBE_HINTERGRUND,
                 fg=FARBE_LEISE).pack(side="left")
        # Anzeige übersetzt, gespeichert wird der deutsche Wert
        anzeige = [T(w) for w in werte]
        var = tk.StringVar(value=T("wie Vorlage"))
        tk.OptionMenu(wahl, var, *anzeige).pack(side="left", padx=(2, 14))
        if beschriftung == "Stoppart":
            stopp_wahl = var
        elif beschriftung == "Rhythmus":
            rhythmus_wahl = var
        else:
            wiedergabe_wahl = var

    # --- Knöpfe: zwei Reihen, damit nichts abgeschnitten wird ---------------
    def knopfreihe(zeile):
        rahmen2 = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
        rahmen2.pack(fill="x", padx=14, pady=2)
        return rahmen2

    knopf_ton = None
    knopf_stopp = None

    knopf_refs = {}

    def baue_reihe(nummer, eintraege):
        rahmen3 = knopfreihe(nummer)
        for spalte, (beschriftung, befehl) in enumerate(eintraege):
            knopf = tk.Button(rahmen3, text=T(beschriftung), command=befehl)
            knopf_refs[beschriftung] = knopf
            knopf.grid(row=0, column=spalte, sticky="ew", padx=3)
            rahmen3.grid_columnconfigure(spalte, weight=1, uniform="r%d" % nummer)
        return rahmen3

    baue_reihe(1, (("Karte wählen", karte_waehlen),
                   ("Datei aufspielen", aufspielen),
                   ("Alles sichern", sichern),
                   ("Pegel zurückrechnen", pegel_zurueck),
                   ("Suchen \u2026", suchen_dialog),
                   ("Neu laden", neu_laden)))

    reihe2 = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    reihe2.pack(fill="x", padx=14, pady=2)
    for spalte, (beschriftung, befehl) in enumerate(
            (("Anhören", abspielen),
             ("Stopp", stoppen),
             ("Umbenennen", umbenennen),
             ("Platz ändern", platz_aendern),
             ("Sicherung zurückholen", zuruecknehmen),
             ("Einstellungen anwenden", einstellungen_anwenden),
             ("Beschreibung …", beschreibung_aendern))):
        knopf = tk.Button(reihe2, text=T(beschriftung), command=befehl)
        knopf_refs[beschriftung] = knopf
        knopf.grid(row=0, column=spalte, sticky="ew", padx=3)
        reihe2.grid_columnconfigure(spalte, weight=1, uniform="r2")
        if beschriftung == "Anhören":
            knopf_ton = knopf
        elif beschriftung == "Stopp":
            knopf_stopp = knopf

    baue_reihe(3, (("▲ nach oben", lambda: verschieben(-1)),
                   ("▼ nach unten", lambda: verschieben(1)),
                   ("Tauschen", tauschen),
                   ("Datei teilen", datei_teilen),
                   ("Alle normieren", alle_normieren),
                   ("Löschen", loeschen),
                   ("Karte auswerfen", karte_auswerfen)))

    braucht_auswahl = ("Anhören", "Umbenennen", "Platz ändern", "Tauschen",
                       "Löschen", "Datei teilen", "▲ nach oben", "▼ nach unten",
                       "Einstellungen anwenden")

    def knopf_zustand():
        hat = bool(liste_auswahl())
        for name in braucht_auswahl:
            k = knopf_refs.get(name)
            if k is not None:
                k.config(state="normal" if hat else "disabled")

    tk.Label(wahl, text=T("Fade am Ende"), bg=FARBE_HINTERGRUND,
             fg=FARBE_LEISE).pack(side="left", padx=(10, 2))
    fade_eingabe = tk.Entry(wahl, width=5, justify="center",
                            bg="#ffffff", fg="#000000", insertbackground="#000000",
                            relief="solid", borderwidth=1)
    fade_eingabe.pack(side="left")
    tk.Label(wahl, text="s", bg=FARBE_HINTERGRUND,
             fg=FARBE_LEISE).pack(side="left")

    wahl2 = tk.Frame(fenster, bg=FARBE_HINTERGRUND)
    wahl2.pack(pady=(2, 0))
    tk.Label(wahl2, text=T("Pegel"), bg=FARBE_HINTERGRUND,
             fg=FARBE_LEISE).pack(side="left", padx=(0, 4))
    pegel_eingabe = tk.Entry(wahl2, width=6, justify="center",
                             bg="#ffffff", fg="#000000", insertbackground="#000000",
                             relief="solid", borderwidth=1)
    pegel_eingabe.pack(side="left")
    tk.Label(wahl2, text="dB", bg=FARBE_HINTERGRUND,
             fg=FARBE_LEISE).pack(side="left", padx=(3, 14))
    normieren_var = tk.BooleanVar(value=False)
    tk.Checkbutton(wahl2, text=T("auf −1 dB Spitze normieren"),
                   variable=normieren_var, bg=FARBE_HINTERGRUND,
                   fg=FARBE_LEISE, activebackground=FARBE_HINTERGRUND,
                   activeforeground=FARBE_SCHRIFT,
                   selectcolor=FARBE_HINTERGRUND).pack(side="left", padx=(0, 16))
    begrenzen_var = tk.BooleanVar(value=True)
    tk.Label(wahl2, text=T("Teile"), bg=FARBE_HINTERGRUND,
             fg=FARBE_LEISE).pack(side="left", padx=(16, 4))
    teile_eingabe = tk.Entry(wahl2, width=4, justify="center",
                             bg="#ffffff", fg="#000000", insertbackground="#000000",
                             relief="solid", borderwidth=1)
    teile_eingabe.insert(0, "2")
    teile_eingabe.pack(side="left")
    sinnvoll_var = tk.BooleanVar(value=True)
    tk.Checkbutton(wahl2, text=T("an Pausen schneiden"), variable=sinnvoll_var,
                   bg=FARBE_HINTERGRUND, fg=FARBE_LEISE,
                   activebackground=FARBE_HINTERGRUND,
                   activeforeground=FARBE_SCHRIFT,
                   selectcolor=FARBE_HINTERGRUND).pack(side="left", padx=(8, 0))
    tk.Checkbutton(wahl2, text=T("Anhebung auf 0 dB begrenzen"),
                   variable=begrenzen_var, bg=FARBE_HINTERGRUND,
                   fg=FARBE_LEISE, activebackground=FARBE_HINTERGRUND,
                   activeforeground=FARBE_SCHRIFT,
                   selectcolor=FARBE_HINTERGRUND).pack(side="left")

    if knopf_stopp is not None:
        knopf_stopp.config(state="disabled")

    tk.Label(fenster, textvariable=status_var, bg=FARBE_HINTERGRUND,
             fg=FARBE_TON).pack(pady=(6, 0))
    tk.Label(fenster, text=T("Doppelklick spielt · Ziehen ordnet um · ⌘ für zwei Markierungen, dann Tauschen · Esc hält an."),
             bg=FARBE_HINTERGRUND, fg=FARBE_LEISE).pack(pady=(0, 10))

    # --- Ziehen und Ablegen: Reihenfolge ändern -------------------------
    def zieh_start(e):
        strich_weg()
        i = liste_index(e.y)
        # Nur echte Patch-Zeilen lassen sich ziehen: Hinweiszeilen (etwa
        # „N Patches ohne Ton …") haben keine Platznummer. Am 18.11.2026
        # führte das Ziehen einer solchen Zeile zu
        # „KeyError: None" in auto_scroll.
        zeile = zustand["zeilen"][i] if 0 <= i < len(zustand["zeilen"]) else None
        zustand["von"] = (i if zeile is not None
                          and zeile.get("nummer") is not None else None)
        # Auswahl: einfacher Klick wählt, ⌘ klickt dazu
        if zustand["von"] is not None:
            mehrfach = bool(e.state & 0x0008)      # Command-Taste
            liste_waehlen(zustand["von"], mehrfach=mehrfach)
            welle_laden()
            teile = zeile["text"].split()
            status_var.set("ziehen: %s" % (teile[1] if len(teile) > 1 else ""))
        zustand["strich"] = None
        if zustand["von"] is not None:
            liste.config(cursor="fleur")
        auto_scroll()

    def auto_scroll():
        """Beim Ziehen an den Rand scrollen - sonst ist es Blindflug.

        Läuft, solange gezogen wird, und plant sich selbst neu ein.
        """
        if zustand.get("von") is None:
            zustand["auto"] = None
            return
        y = zustand.get("maus_y")
        if y is not None:
            hoehe = liste.winfo_height() or 400
            schritt = 0
            if y < ZEILE_H * 1.5:
                schritt = -1
            elif y > hoehe - ZEILE_H * 1.5:
                schritt = 1
            if schritt:
                liste.yview_scroll(schritt, "units")
        zustand["auto"] = fenster.after(90, auto_scroll)

    def zieh_bewegen(e):
        zustand["maus_y"] = e.y
        # Beim Ziehen neu bestimmen, wo der Strich stehen soll
        strich_weg()
        pos = liste.nearest(e.y)
        if pos < 0:
            pos = liste.size()
        strich_setzen(pos)
        strich = zustand.get("marker")
        ziel = strich
        von = zustand.get("von")
        if von is not None and strich > von:
            ziel = strich          # Einfügen hinter der gezogenen Zeile
        if ziel >= len(zustand["zeilen"]):
            status_var.set("ablegen auf Platz %d (ans Ende)" % (ziel + 1))
        else:
            status_var.set("ablegen auf Platz %d" % (ziel + 1))

    def zieh_ende(e):
        von = zustand.get("von")
        strich = zustand.get("marker")
        zustand["von"] = None
        zustand["maus_y"] = None
        if zustand.get("auto"):
            try:
                fenster.after_cancel(zustand["auto"])
            except Exception:
                pass
            zustand["auto"] = None
        liste.config(cursor="arrow")
        strich_weg()
        if von is None or strich is None or not zustand["basis"]:
            status_var.set("")
            return
        nach = strich if strich < von else strich - 1
        alle = [nr for nr, _, _ in patches(zustand["basis"])]
        if von == nach or not (0 <= von < len(alle) and 0 <= nach < len(alle)):
            status_var.set("")
            return
        try:
            befehl_verschieben(zustand["basis"], von + 1, nach + 1, True, kopie=False)
            neu_laden()
            liste_waehlen(nach)
            liste_zeigen(nach)
            status_var.set("verschoben: %s → Platz %d" % (alle[von], nach + 1))
        except SystemExit as f:
            messagebox.showerror(T("Fehler"), str(f))

    leinwand.bind("<Button-1>", springe)
    def umplan_welle(e=None):
        if zustand.get("t_welle"):
            try:
                fenster.after_cancel(zustand["t_welle"])
            except Exception:
                pass
        def spaeter():
            zustand["t_welle"] = None
            zeichne_welle()
        zustand["t_welle"] = fenster.after(150, spaeter)

    leinwand.bind("<Configure>", umplan_welle)
    # Kein Fokusklau beim Überfahren der Liste: das nahm dem Suchfeld den Fokus
    # (18.11.2026). Fokus kommt beim Klick — siehe fokus_zurueck.


    # Klicks irgendwo im Fenster geben den Tastatur-/Rad-Fokus an die Liste
    # zurück. Auf macOS bekommt das Rad Ereignisse des Widgets mit Fokus.
    def fokus_zurueck(e=None):
        """Fokus zurück an die Liste - aber NICHT aus Eingabefeldern.

        Sonst kann man in "Platz", "Pegel", "Fade" oder "Teile" nichts
        eintippen (am 10.11.2026 gemeldet).
        """
        try:
            widget = e.widget if e is not None else None
            if widget is None or widget is liste:
                return
            if widget.winfo_class() in ("Entry", "Spinbox", "TEntry"):
                return
            fenster.after(20, liste.focus_set)
        except Exception:
            pass

    def fokus_halten(e=None):
        """Fokus an die Liste — aber niemals aus einem Eingabefeld heraus.

        Anlaß (18.11.2026): Die alte Regel „Fokus immer zur Liste" nahm dem
        Suchfeld den Fokus in dem Augenblick, in dem man hineinklickte — es
        war weiß und nahm nichts an. FocusIn kommt auch für das Fenster selbst,
        deshalb wird nach kurzer Wartezeit der TATSÄCHLICHE Fokus geprüft.
        """
        try:
            fenster.after(10, spaet_pruefen)
        except Exception:
            pass

    def spaet_pruefen():
        try:
            jetzt = fenster.focus_get()
            if jetzt is not None and jetzt.winfo_class() in (
                    "Entry", "Spinbox", "TEntry", "TCombobox", "Text"):
                return
            liste.focus_set()
        except Exception:
            pass

    # Klicks geben den Fokus an die Liste zurück — außer aus Eingabefeldern.
    fenster.bind("<ButtonRelease-1>", fokus_zurueck, add="+")
    fenster.bind("<FocusIn>", fokus_halten, add="+")
    liste.focus_set()
    def umplan_liste(e=None):
        if zustand.get("t_liste"):
            try:
                fenster.after_cancel(zustand["t_liste"])
            except Exception:
                pass
        def spaeter():
            zustand["t_liste"] = None
            liste_zeichnen()
        zustand["t_liste"] = fenster.after(120, spaeter)

    liste.bind("<Configure>", umplan_liste)
    liste.bind("<Key>", tippen)
    fenster.bind("<Key>", tippen, add="+")
    fenster.bind("<F2>", lambda e: vormerken())
    fenster.bind("<F3>", lambda e: gehe_zu())
    def auswahl_geaendert(e=None):
        knopf_zustand()
        welle_laden()

    liste.bind("<<ListboxSelect>>", auswahl_geaendert)
    liste.bind("<ButtonPress-1>", zieh_start, add="+")
    liste.bind("<B1-Motion>", zieh_bewegen, add="+")
    liste.bind("<ButtonRelease-1>", zieh_ende, add="+")
    def beim_schliessen():
        """Ton beenden, dann das Fenster schließen.

        Am 10.11.2026 lief nach dem Schließen die Wiedergabe weiter -
        der Tonprozess war ein Kindprozess und blieb übrig.
        """
        if laeuft():
            stoppen()
        fenster.destroy()

    fenster.protocol("WM_DELETE_WINDOW", beim_schliessen)

    liste.bind("<Double-Button-1>", lambda e: abspielen())
    fenster.bind("<Escape>", lambda e: stoppen())

    def feld_fokus(e):
        """Klick in ein Eingabefeld gibt ihm den Tastaturfokus."""
        e.widget.focus_set()

    def alle_felder(w):
        for kind in w.winfo_children():
            if kind.winfo_class() == "Entry":
                kind.bind("<Button-1>", feld_fokus, add="+")
            alle_felder(kind)

    alle_felder(fenster)
    knopf_zustand()

    neu_laden()
    # Erst nach dem ersten Layout steht die Höhe der Liste fest - sonst
    # wird beim Start nur eine Zeile gezeichnet.
    fenster.update_idletasks()
    liste_zeichnen()
    zeichne_welle()
    # Fenstergröße erst jetzt bestimmen - vorher gibt es noch keine
    # Bedienelemente und die Messung wäre wertlos.
    fenster.update_idletasks()
    fenster.title(T("JamMan Card Tool") + " — " + T("Karte und interner Speicher"))
    # Größe aus dem Bedarf der Bedienelemente - sonst werden die rechten
    # Knöpfe abgeschnitten (am 10.11.2026 gesehen: 1162 statt 1080 px).
    fenster.update_idletasks()
    bildschirm_b = fenster.winfo_screenwidth()
    bildschirm_h = fenster.winfo_screenheight()
    breite = min(max(1000, fenster.winfo_reqwidth() + 24), bildschirm_b - 40)
    hoehe = min(max(720, fenster.winfo_reqheight() + 24), bildschirm_h - 80)
    fenster.geometry("%dx%d" % (breite, hoehe))
    fenster.minsize(min(breite, 1000), min(hoehe, 700))

    # Ohne Karte, aber mit Pedal am Kabel: gleich zeigen, was drin ist.
    if basis is None:
        def von_selbst_usb():
            try:
                if _greenglass().finde_anschluesse():
                    usb_oeffnen()
            except Exception:
                pass          # kein Pedal, kein USB-Modul: der Hinweis genügt
        fenster.after(400, von_selbst_usb)

    fenster.mainloop()


# -------------------------------------------------------------------- Start

def main():
    # Sprache VOR dem Bau des Parsers bestimmen - die Hilfetexte werden beim
    # Anlegen übersetzt, nicht beim Ausgeben.
    for i, arg in enumerate(sys.argv):
        if arg == "--sprache" and i + 1 < len(sys.argv):
            sprache_setzen(sys.argv[i + 1])
        elif arg.startswith("--sprache="):
            sprache_setzen(arg.split("=", 1)[1])
    p = argparse.ArgumentParser(
        description=T("JamMan Solo XT — Speicherkarte verwalten"),
        epilog=befehlsliste(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version="JamMan Card Tool " + FASSUNG)
    p.add_argument("befehl", metavar=T("BEFEHL"),
                   choices=sorted(list(BEFEHLE) + list(BEFEHL_ALIAS)),
                   help=T("was zu tun ist (Liste unten; englische Namen gehen auch)"))
    p.add_argument("dateien", nargs="*",
                   help=T("Dateien für „speicherabbild“ und „vergleich“"))
    p.add_argument("--sprache", choices=sorted(SPRACHEN),
                   help=T("Sprache der Ausgabe (Vorgabe: aus dem System)"))
    p.add_argument("--karte", default=standard_karte(),
                   help=T("Laufwerk der Karte (Vorgabe: automatisch erkannt)"))
    p.add_argument("--ziel", default="~/JamMan-Sicherung", help=T("Zielordner für 'sichern'"))
    p.add_argument("--wav", help=T("Audiodatei für 'aufspielen'"))
    p.add_argument("--name", help=T("Name des Patches (Vorgabe: Dateiname)"))
    p.add_argument("--patch", type=int, help=T("Patch-Nummer"))
    p.add_argument("--nach", type=int, help=T("Zielplatz für 'verschieben'"))
    p.add_argument("--mit", type=int, help=T("Tauschpartner für 'tauschen'"))
    p.add_argument("--auf", type=int, help=T("Zielplatz für 'platzieren'"))
    p.add_argument("--aus", help=T("Sicherungsordner für 'zuruecknehmen'"))
    p.add_argument("--protokoll", help=T("Pegelprotokoll für 'pegel-umkehren'"))
    p.add_argument("--alle", action="store_true",
                   help=T("bei 'zuruecknehmen': alle Patches aus der Sicherung"))
    p.add_argument("--ziel-rms", type=float, default=-18.0,
                   help=T("Ziellautheit für 'alle-normieren' (Vorgabe -18 dB)"))
    p.add_argument("--grenze", type=float, default=-1.0,
                   help=T("Spitzengrenze für 'alle-normieren' (Vorgabe -1 dB)"))
    p.add_argument("--kuerzen", action="store_true",
                   help=T("zu lange Datei auf 9:59 kürzen"))
    p.add_argument("--mit-pause", action="store_true",
                   help=T("beim Teilen die Pause an der Naht stehen lassen"))
    p.add_argument("--einfach", action="store_true",
                   help=T("beim Teilen gleichmäßig schneiden statt an Pausen"))
    p.add_argument("--teile", type=int, metavar="N",
                   help=T("Datei in N gleich lange Patches aufteilen"))
    p.add_argument("--laenge", type=float, metavar="SEKUNDEN",
                   help=T("zu lange Datei auf diese Länge kürzen"))
    p.add_argument("--trotzdem", action="store_true",
                   help=T("auch Loops über 10 Minuten schreiben (Pedal nimmt sie nicht an)"))
    p.add_argument("--fade", type=float, metavar="SEKUNDEN",
                   help=T("Fade am Ende der Datei einrechnen (z. B. 3)"))
    p.add_argument("--gain", type=float, metavar="DB",
                   help=T("Lautstärke um DB ändern (z. B. -3 oder +2)"))
    p.add_argument("--ohne-begrenzung", action="store_true",
                   help=T("Anhebung NICHT auf 0 dBFS begrenzen (kann übersteuern)"))
    p.add_argument("--normieren", action="store_true",
                   help=T("auf -1 dB Spitzenpegel normieren"))
    p.add_argument("--stopp", choices=["fade", "instant", "finish"],
                   help=T("Stopp-Verhalten des neuen Patches (ohne Angabe: wie Vorlage)"))
    p.add_argument("--wiedergabe", choices=["loop", "single"],
                   help=T("Loop oder Single (IsLoop in phrase.xml; ohne Angabe: wie Vorlage)"))
    p.add_argument("--rhythmus", choices=["silence", "kick"],
                   help=T("Rhythmus des neuen Patches (ohne Angabe: wie Vorlage)"))
    p.add_argument("--keine-kopie", action="store_true",
                   help=T("beim Verschieben nur die Namen sichern (schnell, kein Kopieren)"))
    p.add_argument("--sicherung", default="~/JamMan-Sicherung",
                   help=T("Ordner für die Sicherung vor Löschen/Verschieben"))
    p.add_argument("--schreiben", action="store_true", help=T("wirklich verändern (sonst Trockenlauf)"))
    a = p.parse_args()
    sprache_setzen(a.sprache or einstellung_lesen("sprache") or sprache_aus_system())
    a.befehl = BEFEHL_ALIAS.get(a.befehl, a.befehl)

    if a.befehl == "gui":
        gui(a.karte)
        return
    if a.befehl == "selbsttest":
        selbsttest(a.karte)
        return
    if a.befehl == "menu":
        try:
            menu(a.karte)
        except KeyboardInterrupt:
            print()
        return
    if a.befehl == "aktualisieren":
        ok, text = aktualisieren(melden=lambda t: print("  " + t))
        print(text)
        return 0 if ok else 1
    if a.befehl in ("firmware-holen", "firmware-schreiben"):
        if not a.dateien:
            print(T("Aufruf:") + " jamman.py " + a.befehl + " <ordner>")
            return 2
        g = _geraet_holen(_greenglass())
        try:
            if a.befehl == "firmware-holen":
                geholt = firmware_holen(
                    g, a.dateien[0],
                    melden=lambda p: print("  " + str(p).split("nandfs/")[-1]))
                print(T("Firmware geholt: %d Dateien") % len(geholt))
            else:
                if not frage_ja_nein_konsole():
                    print(T("Abgebrochen."))
                    return 1
                ergebnis = firmware_schreiben(
                    g, a.dateien[0],
                    melden=lambda p, n: print("  " + str(p).split("nandfs/")[-1]
                                              + " (%d Bytes)" % n))
                print(T("Fertig: %d Dateien geschrieben") % len(ergebnis))
        finally:
            _geraet_schliessen()
        return 0

    if a.befehl in ("jamlists", "jamlist-holen", "jamlist-schreiben"):
        dateien = a.dateien or []
        if a.befehl == "jamlists":
            if not dateien:
                print(T("Aufruf:") + " jamman.py jamlists <JamManager.db.sqlite>")
                return 2
            for liste in jamlists_lesen(dateien[0]):
                print("  %-16s id=%-3d %d " % (liste["name"], liste["id"],
                                               len(liste["platz"]))
                      + T("belegte Plätze"))
            return 0
        if a.befehl == "jamlist-holen":
            if len(dateien) < 3:
                print(T("Aufruf:") + " jamman.py jamlist-holen <db> <Name> <ziel.txt>")
                return 2
            for liste in jamlists_lesen(dateien[0]):
                if liste["name"] == dateien[1]:
                    patchliste = [(("Patch%02d" % p), n, "") for p, n, _ in liste["platz"]]
                    _setlist_speichern(patchliste,
                                       list(range(len(patchliste))), dateien[2])
                    print(T("Geschrieben: %s (%d Plätze)")
                          % (dateien[2], len(patchliste)))
                    return 0
            print(T("Keine JamList mit dem Namen %s.") % dateien[1])
            return 1
        if len(dateien) < 3:
            print(T("Aufruf:") + " jamman.py jamlist-schreiben <db> <Name> <quelle.txt>")
            return 2
        eintraege = [(int(platz.lstrip("Patch") if platz.startswith("Patch")
                          else platz), name, "")
                     for platz, name in _setlist_lesen(dateien[2])]
        kennung = jamlist_schreiben(dateien[0], dateien[1], eintraege)
        print(T("JamList %s angelegt (id %d, %d Plätze)")
              % (dateien[1], kennung, len(eintraege)))
        return 0

    if a.befehl in ("speicherabbild", "vergleich"):
        if a.befehl == "vergleich":
            if len(a.dateien or []) < 2:
                print(T("Aufruf:") + " jamman.py vergleich <a.tsv> <b.tsv>")
                return 2
            nur_a, nur_b, anders = speicherabbilder_vergleichen(
                a.dateien[0], a.dateien[1])
            print(T("Nur im ersten Bild: %d") % len(nur_a))
            for pfad in nur_a[:20]:
                print("  + " + pfad)
            print(T("Nur im zweiten Bild: %d") % len(nur_b))
            for pfad in nur_b[:20]:
                print("  - " + pfad)
            print(T("Gleicher Pfad, andere Größe: %d") % len(anders))
            for pfad, groesse_a, groesse_b in anders[:40]:
                print("  %s  %d → %d" % (pfad, groesse_a, groesse_b))
            return 0
        ziel = (a.dateien or [None])[0]
        if not ziel:
            print(T("Aufruf:") + " jamman.py speicherabbild <datei.tsv>")
            return 2
        g = _geraet_holen(_greenglass())
        try:
            anzahl = speicherabbild(g, ziel,
                                    melden=lambda n, p: print("  %d  %s" % (n, p)))
            print(T("Speicherabbild geschrieben: %d Einträge nach %s")
                  % (anzahl, ziel))
        finally:
            _geraet_schliessen()
        return 0

    if a.befehl == "auswerfen":
        befehl_auswerfen(a.karte)
        return

    basis = karte_pruefen(a.karte)
    if a.befehl == "liste":
        befehl_liste(basis)
    elif a.befehl == "namensform":
        befehl_namensform(basis, a.schreiben, a.sicherung)
    elif a.befehl == "sichern":
        befehl_sichern(basis, a.ziel, a.schreiben)
    elif a.befehl == "aufspielen":
        if not a.wav:
            raise SystemExit(T("Fehler: --wav fehlt."))
        befehl_aufspielen(basis, a.wav, a.name, a.schreiben, a.stopp, a.rhythmus,
                          a.wiedergabe, a.fade, a.gain, a.normieren,
                          not a.ohne_begrenzung, a.trotzdem,
                          a.kuerzen, a.laenge)
    elif a.befehl == "abspielen":
        if not a.patch:
            raise SystemExit(T("Fehler: --patch fehlt."))
        befehl_abspielen(basis, a.patch)
    elif a.befehl == "loeschen":
        if not a.patch:
            raise SystemExit(T("Fehler: --patch fehlt."))
        befehl_loeschen(basis, a.patch, a.schreiben, a.sicherung)
    elif a.befehl == "teilen":
        if not a.wav:
            raise SystemExit(T("Fehler: --wav fehlt."))
        befehl_teilen(basis, a.wav, a.name or os.path.splitext(os.path.basename(a.wav))[0],
                      a.schreiben, a.teile, a.laenge, a.sicherung,
                      a.stopp, a.wiedergabe, sinnvoll=not a.einfach,
                      ohne_pause=not a.mit_pause)
    elif a.befehl == "pegel-umkehren":
        if not a.protokoll:
            raise SystemExit(T("Fehler: --protokoll fehlt."))
        befehl_pegel_umkehren(basis, os.path.expanduser(a.protokoll),
                              a.schreiben, a.sicherung)
    elif a.befehl == "alle-normieren":
        befehl_alle_normieren(basis, a.schreiben, a.ziel_rms, a.grenze, a.sicherung)
    elif a.befehl == "zuruecknehmen":
        if a.alle:
            befehl_zuruecknehmen_alle(basis, a.aus, a.schreiben, a.sicherung)
        else:
            if not a.patch:
                raise SystemExit(T("Fehler: --patch fehlt (oder --alle angeben)."))
            befehl_zuruecknehmen(basis, a.patch, a.schreiben, a.sicherung, a.aus)
    elif a.befehl == "platzieren":
        if not a.patch or not a.auf:
            raise SystemExit(T("Fehler: --patch und --auf fehlen."))
        befehl_platzieren(basis, a.patch, a.auf, a.schreiben, a.sicherung)
    elif a.befehl == "tauschen":
        if not a.patch or not a.mit:
            raise SystemExit(T("Fehler: --patch und --mit fehlen."))
        befehl_tauschen(basis, a.patch, a.mit, a.schreiben, a.sicherung,
                        kopie=not a.keine_kopie)
    elif a.befehl == "aendern":
        if not a.patch:
            raise SystemExit(T("Fehler: --patch fehlt."))
        befehl_aendern(basis, a.patch, a.schreiben, a.stopp, a.rhythmus,
                       a.wiedergabe, a.name, a.sicherung, a.fade,
                       a.gain, a.normieren, not a.ohne_begrenzung)
    elif a.befehl == "saeubern":
        befehl_saeubern(basis, a.schreiben)
    elif a.befehl == "verschieben":
        if not a.patch or not a.nach:
            raise SystemExit(T("Fehler: --patch und --nach fehlen."))
        befehl_verschieben(basis, a.patch, a.nach, a.schreiben, a.sicherung,
                           kopie=not a.keine_kopie)


def _absturz_protokoll(fehler):
    """Unerwarteten Fehler festhalten und zeigen.

    Nötig, weil das Werkzeug unter Windows ohne Konsolenfenster startet
    (pythonw): Ohne das Protokoll wäre ein Absturz unsichtbar (17.11.2026).
    """
    import traceback
    text = "".join(traceback.format_exception(fehler))
    ordner = os.path.dirname(os.path.abspath(__file__))
    pfad = os.path.join(ordner, "jamman-fehler.log")
    try:
        with open(pfad, "a", encoding="utf-8") as datei:
            datei.write("\n" + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n")
            datei.write(text)
    except OSError:
        pfad = ""
    try:
        from tkinter import messagebox
        messagebox.showerror(
            T("Unerwarteter Fehler"),
            (T("Das Werkzeug wurde unerwartet beendet.") + "\n\n"
             + text[-1200:]
             + ("\n\n" + T("Protokoll:") + " " + pfad if pfad else "")))
    except Exception:
        pass
    return pfad


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as fehler:        # auch Strg+C beim Start
        _absturz_protokoll(fehler)
        raise
