#!/usr/bin/env python3
"""Greenglass — der USB-Zugang zum JamMan (Solo XT und Stereo).

Das Pedal erscheint über USB **nicht** als Laufwerk, sondern als serieller
Anschluß (CDC-ACM): auf macOS `/dev/cu.usbmodem*`, unter Linux
`/dev/ttyACM*`, unter Windows ein COM-Anschluß. Über diesen Port spricht es
ein binäres Protokoll, das DigiTech „Greenglass" nennt. Es enthält eine
vollständige Datei-API — Ordner listen, Dateien lesen und schreiben — und
damit Zugang zum internen Speicher und zur SD-Karte, ohne die Karte
herauszunehmen.

Herkunft des Wissens
    Das Protokoll wurde nicht hier hergeleitet, sondern stammt aus dem
    Projekt **RenanDiaz/jamman-manager** (MIT-Lizenz), Dateien
    `docs/usb-protocol.md` und `tools/greenglass.py`. Dort ist es durch
    Zerlegen des alten JamManager beschrieben und an einem JamMan Stereo
    (Firmware 1.3) mit Lesen und Schreiben geprüft. Dieses Modul ist eine
    eigenständige Python-Umsetzung für dieses Werkzeug — gleiche Rahmen,
    eigener Code, deutsche Meldungen, plattformübergreifender Port.

Rahmenformat (alles little endian)
    u16 msgID · u32 payloadLength · payload
    Anfragen beginnen die Nutzlast mit einem u32 Seriennummer,
    Antworten (msgID < 0x8000) geben sie zurück.
    Zeichenketten: i32 Länge in Bytes, dann UTF-8.

Wichtig beim Senden
    Jeder Rahmen muß **vollständig** hinausgehen. Der Parser im Pedal kennt
    weder Neusynchronisation noch Zeitlimit: kommt ein Rahmen zerstückelt
    an, nimmt er die folgenden Bytes als Rest seiner Nutzlast und antwortet
    nicht mehr. Deshalb wird geschrieben, bis alles weg ist.

Stand 16.11.2026: Lesen **und** Schreiben. Schreibende Zugriffe:
Dateien schreiben, Ordner anlegen, löschen, umbenennen, Einstellungen
setzen. Vor jedem Überschreiben sichert das Werkzeug den alten Platz auf
den Rechner — ein abgebrochener Schreibvorgang kann einen Patch beschädigen.
"""

import glob
import os
import struct
import sys
import time

MIT_QUELLE = "RenanDiaz/jamman-manager (MIT) — docs/usb-protocol.md"

# --- Nachrichten (nur die, die hier gebraucht werden) -----------------------
MSG_WHO_AM_I = 0
MSG_SUCCESS = 2
MSG_FAILURE = 3
MSG_ENTRY_INFO = 4
MSG_FOLDER_CONTENTS = 17
MSG_GET_PROPERTIES = 19
MSG_FILE_OPEN = 10
MSG_FILE_READ = 12
MSG_FILE_WRITE = 14
MSG_FILE_CLOSE = 16
MSG_SET_PROPERTY = 21
MSG_CREATE = 7
MSG_MOVE = 8
MSG_DELETE = 9

PROP_TASTEN = {32788: "WarteschlangeAktuell", 32789: "WarteschlangeLeer",
               32790: "TasteLinks", 32791: "TasteMitte", 32792: "TasteRechts"}

PROP_NAMEN = {
    1: "FamilyID", 2: "DeviceID", 3: "ProtocolVersion", 4: "FirmwareVersion",
    5: "RemovableMediaIsPresent", 6: "SampleRate", 7: "ChannelCount",
    8: "BitDepth", 10: "KilobytesAvailableOnNand",
    11: "KilobytesCapacityOnNand", 12: "KilobytesAvailableOnSd",
    13: "KilobytesCapacityOnSd", 14: "DiskState", 15: "CurrentBankAndPatch",
    32781: "DirectorySeparator", 32779: "InternalPatchFolderPath",
    32780: "RemovablePatchFolderPath", 32777: "Name",
}

LESEN = 1
CHUNK = 8162


def finde_anschluesse():
    """Alle seriellen Anschlüsse, die ein JamMan sein könnten."""
    if sys.platform.startswith("win"):
        try:
            import serial.tools.list_ports as lp
        except ImportError:
            return []
        return [p.device for p in lp.comports()]
    muster = ["/dev/cu.usbmodem*", "/dev/tty.usbmodem*", "/dev/ttyACM*"]
    treffer = []
    for m in muster:
        treffer += glob.glob(m)
    return sorted(set(treffer))


class _Port:
    """Serieller Anschluß — termios auf macOS/Linux, pyserial unter Windows."""

    def __init__(self, geraet):
        import threading
        # Nur ein Sprecher zur Zeit auf diesem Anschluß. Zwei Stränge
        # (Mitlaufen und Schreiben) brachten das Pedal am 16.11.2026 zum
        # Schweigen, weil der eine die Antworten des anderen wegnahm.
        self._sperre = threading.RLock()
        self.geraet = geraet
        self.buf = b""
        self.seriennummer = 1
        self.meldungen = []
        if sys.platform.startswith("win"):
            try:
                import serial
            except ImportError:
                raise RuntimeError("Unter Windows braucht es pyserial: pip install pyserial")
            self.serial = serial.Serial(geraet, 115200, timeout=0)
        else:
            import termios
            self._termios = termios
            self.fd = os.open(geraet, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            a = termios.tcgetattr(self.fd)
            a[0] = 0
            a[1] = 0
            a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            a[3] = 0
            a[4] = a[5] = termios.B115200
            termios.tcsetattr(self.fd, termios.TCSANOW, a)

    # -- lesen und schreiben ------------------------------------------------
    def _fuellen(self, wartezeit):
        if sys.platform.startswith("win"):
            daten = self.serial.read(65536)
            if daten:
                self.buf += daten
            else:
                time.sleep(min(wartezeit, 0.02))
            return
        import select
        r, _, _ = select.select([self.fd], [], [], wartezeit)
        if r:
            try:
                self.buf += os.read(self.fd, 65536)
            except BlockingIOError:
                pass

    def _schreiben(self, rahmen, frist=10.0):
        ende = time.time() + frist
        if sys.platform.startswith("win"):
            self.serial.write(rahmen)
            self.serial.flush()
            return
        import select
        rest = memoryview(rahmen)
        while rest:
            if time.time() > ende:
                raise TimeoutError("Senden steckt — das Pedal nimmt nichts mehr an")
            select.select([], [self.fd], [], 0.2)
            try:
                rest = rest[os.write(self.fd, rest):]
            except BlockingIOError:
                pass
        self._termios.tcdrain(self.fd)

    def _lies_rahmen(self, frist=3.0):
        ende = time.time() + frist
        while True:
            if len(self.buf) >= 6:
                mid, ln = struct.unpack("<HI", self.buf[:6])
                if len(self.buf) >= 6 + ln:
                    nutz = self.buf[6:6 + ln]
                    self.buf = self.buf[6 + ln:]
                    return mid, nutz
            rest = ende - time.time()
            if rest <= 0:
                return None
            self._fuellen(min(rest, 0.2))

    def ausrichten(self, versuche=40):
        """Nach einem Zeitlimit die Rahmenausrichtung wiederherstellen.

        Laut Protokolldokument bleibt der Parser im Pedal stehen, wenn ein
        Rahmen unvollständig ankam; einzelne Nullbytes vervollständigen die
        offene Nutzlast. Rückgabe: True, wenn wieder etwas ankommt.
        """
        import time as _zeit
        for _ in range(versuche):
            try:
                self._schreiben(b"\x00", frist=2)
            except Exception:
                return False
            marker = self._lies_rahmen(0.2)
            if marker is not None:
                self.buf = b""
                return True
            _zeit.sleep(0.02)
        return False

    def anfrage(self, mid, nutz=b"", frist=3.0, wartezeit=20.0):
        """Anfrage senden — mit Frist auf die Sperre.

        Steckt ein anderer Vorgang (etwa ein abgebrochener Schreiblauf),
        wartet das Pedal nicht ewig: nach `wartezeit` Sekunden kommt ein
        klarer Fehler statt eines scheinbar eingefrorenen Fensters.
        """
        if not self._sperre.acquire(timeout=wartezeit):
            raise TimeoutError("Der Anschluß ist durch einen anderen Vorgang "
                               "belegt — „Verbindung neu aufbauen“ hilft.")
        try:
            try:
                return self._anfrage(mid, nutz, frist)
            except TimeoutError:
                # einmal aufräumen und erneut versuchen, statt den Nutzer
                # mit einem stillen Hänger sitzenzulassen
                if not self.ausrichten():
                    raise
                return self._anfrage(mid, nutz, frist)
        finally:
            self._sperre.release()

    def _anfrage(self, mid, nutz=b"", frist=3.0):
        ser = self.seriennummer
        self.seriennummer += 1
        rahmen = struct.pack("<HI", mid, len(nutz) + 4) + struct.pack("<I", ser) + nutz
        self._schreiben(rahmen)
        ende = time.time() + frist
        while time.time() < ende:
            m = self._lies_rahmen(ende - time.time())
            if m is None:
                break
            rid, p = m
            if rid >= 0x8000:
                self.meldungen.append((rid, p))
                continue
            r = _Leser(p)
            if r.u32() != ser:
                continue
            if rid == MSG_FAILURE:
                raise RuntimeError("Das Pedal meldet einen Fehler: " + r.s())
            return rid, r
        raise TimeoutError("keine Antwort auf Nachricht %d — Kabel, Anschluß oder Pedal prüfen" % mid)

    def neu_ausrichten(self, versuche=64):
        """Nach einem Abbruch die Ausrichtung wiederherstellen.

        Laut Protokolldokument bleibt der Parser im Pedal stehen, wenn ein
        Rahmen unvollständig ankam. Einzelne Nullbytes vervollständigen die
        offene Nutzlast; danach antwortet das Pedal wieder.
        """
        import time as _zeit
        with self.port._sperre:
            for _ in range(versuche):
                try:
                    self.port._schreiben(b"\x00", frist=2)
                except Exception:
                    return False
                self.port.buf = b""
                for _ in range(4):
                    m = self.port._lies_rahmen(0.15)
                    if m is not None:
                        return True
                _zeit.sleep(0.02)
        return False

    def schliessen(self):
        if sys.platform.startswith("win"):
            self.serial.close()
        else:
            os.close(self.fd)


class Abgebrochen(Exception):
    """Der Nutzer hat den Vorgang abgebrochen."""


class _Leser:
    def __init__(self, daten):
        self.b = daten
        self.p = 0

    def take(self, n):
        stueck = self.b[self.p:self.p + n]
        self.p += n
        return stueck

    def u8(self):
        return self.take(1)[0]

    def i16(self):
        return struct.unpack("<h", self.take(2))[0]

    def u16(self):
        return struct.unpack("<H", self.take(2))[0]

    def u32(self):
        return struct.unpack("<I", self.take(4))[0]

    def i32(self):
        return struct.unpack("<i", self.take(4))[0]

    def i64(self):
        return struct.unpack("<q", self.take(8))[0]

    def s(self):
        n = self.i32()
        return self.take(n).decode("utf-8", "replace")


def _gstr(text):
    b = text.encode("utf-8")
    return struct.pack("<i", len(b)) + b


class JamManUSB:
    """Ein angeschlossenes Pedal: lesende und schreibende Zugriffe.

    Alle Zugriffe laufen über eine Sperre — zwei Stränge am selben
    seriellen Anschluß brachten das Pedal am 16.11.2026 zum Schweigen.
    """

    def __init__(self, geraet=None):
        # Die Ausnahme hängt am Objekt, damit aufrufender Code sie fangen kann,
        # ohne das Modul selbst zu kennen (nötig für den Abbruch beim Holen).
        self.Abgebrochen = Abgebrochen
        if geraet and not os.path.exists(geraet):
            geraet = None          # Anschluß nach einem Neustart verschwunden
        if geraet:
            try:
                self.port = _Port(geraet)
                self.kopf = self.who_am_i()
                self.geraet = geraet
                return
            except Exception:
                try:
                    self.port.schliessen()
                except Exception:
                    pass
                geraet = None      # z. B. Errno 6 nach dem Aus- und Einschalten
        self.geraet = self._erstes_pedal()
        self.port = _Port(self.geraet)
        self.kopf = self.who_am_i()

    def close(self):
        self.port.schliessen()

    @staticmethod
    def _erstes_pedal():
        for d in finde_anschluesse():
            try:
                p = _Port(d)
            except Exception:
                continue
            try:
                ser = 1
                rahmen = struct.pack("<HI", MSG_WHO_AM_I, 4) + struct.pack("<I", ser)
                p._schreiben(rahmen)
                m = p._lies_rahmen(2.0)
            finally:
                p.schliessen()
            if m and m[0] < 0x8000 and len(m[1]) > 10:
                return d
        raise RuntimeError("kein JamMan gefunden — Pedal einschalten und Kabel prüfen")

    # -- Gerät -------------------------------------------------------------
    def who_am_i(self):
        _, r = self.port.anfrage(MSG_WHO_AM_I)
        return {
            "familyID": r.u16(), "productID": r.u16(), "reserved": r.u16(),
            "software": hex(r.u16()), "guid": r.s(), "name": r.s(),
        }

    def eigenschaften(self):
        _, r = self.port.anfrage(MSG_GET_PROPERTIES, struct.pack("<B", 0))
        werte = {}
        for _ in range(r.i16()):
            k = r.u16()
            werte[PROP_NAMEN.get(k, str(k))] = r.i32()
        for _ in range(r.i16()):
            k = r.u16()
            werte[PROP_NAMEN.get(k, str(k))] = r.s()
        return werte

    def ordner(self, pfad):
        """(Dateien, Ordner) eines Verzeichnisses im Pedal."""
        _, r = self.port.anfrage(MSG_FOLDER_CONTENTS, _gstr(pfad))
        dateien = [r.s() for _ in range(r.i32())]
        ordner = [r.s() for _ in range(r.i32())]
        return dateien, ordner

    def info(self, pfad, ist_ordner=False):
        _, r = self.port.anfrage(MSG_ENTRY_INFO, struct.pack("<?", ist_ordner) + _gstr(pfad))
        ergebnis = {"istOrdner": r.u8() == 1, "name": r.s(), "existiert": r.u8() == 1,
                    "groesse": r.i64(), "schreibgeschuetzt": r.u8() == 1,
                    "versteckt": r.u8() == 1}
        for feld in ("erstellt", "geaendert", "zugegriffen"):
            jahr = r.i16()
            monat = struct.unpack("<b", r.take(1))[0] + 1
            tag = struct.unpack("<b", r.take(1))[0]
            stunde = struct.unpack("<b", r.take(1))[0]
            minute = struct.unpack("<b", r.take(1))[0]
            ergebnis[feld] = "%04d-%02d-%02d %02d:%02d" % (jahr, monat, tag, stunde, minute)
        return ergebnis

    def lies_datei(self, pfad, stopp=None, hoechstens=None):
        """Eine Datei aus dem Pedal holen (nur lesen).

        stopp: Funktion ohne Argumente; gibt sie True zurück, bricht der
        Lesevorgang beim nächsten Stück ab. Ohne diese Prüfung lief ein
        Kopiervorgang bis zum Ende der Datei weiter, auch wenn „Abbrechen"
        gedrückt war (16.11.2026).

        hoechstens: nur so viele Bytes lesen (21.11.2026). Damit holt das
        Werkzeug den WAV-Kopf, ohne eine 70-MB-Aufnahme zu übertragen —
        daraus läßt sich die Dauer in Millisekunden ausrechnen.
        """
        _, r = self.port.anfrage(MSG_FILE_OPEN, _gstr(pfad) + bytes([LESEN, 1]))
        handle, groesse = r.i64(), r.i64()
        if hoechstens is not None and hoechstens > 0:
            groesse = min(groesse, int(hoechstens))
        daten = b""
        try:
            while len(daten) < groesse:
                if stopp is not None and stopp():
                    raise Abgebrochen()
                _, rr = self.port.anfrage(
                    MSG_FILE_READ, struct.pack("<qi", handle, min(CHUNK, groesse - len(daten))),
                    frist=10)
                rr.i64()
                n = rr.i32()
                stueck = rr.take(n)
                if not stueck:
                    break
                daten += stueck
        finally:
            # Beim Abbruch darf das Schließen nicht auf eine Antwort warten:
            # sonst hängt „Abbrechen“ bis zu 30 Sekunden (16.11.2026).
            try:
                self.port.anfrage(MSG_FILE_CLOSE, struct.pack("<q", handle),
                                  frist=1.0, wartezeit=1.0)
            except Exception:
                pass
        return daten


    # -- schreibende Zugriffe ----------------------------------------------
    # Grundsatz: jede dieser Funktionen prüft ihren Erfolg an der Antwort
    # (Nachricht 2 = Erfolg). Das Pedal quittiert sonst stillschweigend.
    def _erwarte_erfolg(self, antwort):
        rid, _ = antwort
        if rid != MSG_SUCCESS:
            raise RuntimeError("Das Pedal hat den Vorgang abgelehnt (Antwort %d)" % rid)

    def erstelle_ordner(self, pfad):
        """Ordner anlegen. Existiert er schon, gilt das als Erfolg."""
        try:
            self._erwarte_erfolg(self.port.anfrage(
                MSG_CREATE, struct.pack("<?", True) + _gstr(pfad) + struct.pack("<?", False)))
        except RuntimeError:
            _dateien, ordner = self.ordner(os.path.dirname(pfad.rstrip("/")) or "/")
            if os.path.basename(pfad.rstrip("/")) not in ordner:
                raise

    def schreibe_datei(self, pfad, daten, stopp=None, fortschritt=None):
        """Eine Datei ins Pedal schreiben (CreateOrTruncate).

        stopp:     bricht zwischen den Stücken ab (wirft Abgebrochen)
        fortschritt(geschrieben, gesamt)
        """
        _, r = self.port.anfrage(MSG_FILE_OPEN, _gstr(pfad) + bytes([2, 0]))
        handle = r.i64()
        r.i64()
        try:
            for ab in range(0, len(daten), 8158):
                if stopp is not None and stopp():
                    raise Abgebrochen()
                stueck = daten[ab:ab + 8158]
                self._erwarte_erfolg(self.port.anfrage(
                    MSG_FILE_WRITE, struct.pack("<qi", handle, len(stueck)) + stueck,
                    frist=30))
                if fortschritt is not None:
                    fortschritt(min(len(daten), ab + len(stueck)), len(daten))
        finally:
            try:
                self.port.anfrage(MSG_FILE_CLOSE, struct.pack("<q", handle),
                                  frist=1.0, wartezeit=1.0)
            except Exception:
                pass
        return True

    def loesche(self, pfad, ist_ordner=False, rekursiv=True):
        self._erwarte_erfolg(self.port.anfrage(
            MSG_DELETE, struct.pack("<?", ist_ordner) + _gstr(pfad)
            + struct.pack("<??", rekursiv, False)))

    def benenne_um(self, quelle, ziel, ist_ordner=False,
                   original_behalten=False, ueberschreiben=False):
        self._erwarte_erfolg(self.port.anfrage(
            MSG_MOVE, struct.pack("<?", ist_ordner) + _gstr(quelle) + _gstr(ziel)
            + struct.pack("<??", original_behalten, ueberschreiben)))


    # -- mitlaufen und Einstellungen --------------------------------------
    def _decode_meldung(self, mid, nutz):
        """Eine Meldung des Pedals in lesbarer Form (ohne Gegenanfrage)."""
        r = _Leser(nutz)
        if mid == 0x8006:
            anzahl = r.u8()
            pfad = [r.u16() for _ in range(anzahl)]
            k = r.u16()
            v = r.i32() if k <= 17 else r.s()
            return {"art": "Änderung", "pfad": pfad,
                    "name": PROP_NAMEN.get(k, k), "wert": v}
        return {"art": hex(mid), "roh": nutz[:32].hex()}

    def mitlaufen(self, dauer=1.0):
        """Meldungen des Pedals abholen, ohne etwas zu senden.

        Das Pedal schickt von sich aus, wenn ein anderer Patch geladen wird
        oder die Karte kommt/geht. Aufrufer ruft das in einer Schleife auf.
        """
        if not self.port._sperre.acquire(timeout=1.0):
            return None
        try:
            m = self.port._lies_rahmen(dauer)
        finally:
            self.port._sperre.release()
        if m is None:
            return None
        mid, nutz = m
        if mid < 0x8000:
            return None          # Antwort auf eine eigene Anfrage, nicht für uns
        return self._decode_meldung(mid, nutz)

    def eigenschaft_holen(self, nummer):
        _, r = self.port.anfrage(26, struct.pack("<BH", 0, nummer))
        k = r.u16()
        return (r.i32() if k <= 17 else r.s())

    def eigenschaft_setzen(self, nummer, wert):
        körper = struct.pack("<BH", 0, nummer)
        körper += struct.pack("<i", int(wert)) if nummer <= 17 else _gstr(str(wert))
        rid, _ = self.port.anfrage(MSG_SET_PROPERTY, körper)
        if rid != MSG_SUCCESS:
            raise RuntimeError("Das Pedal hat die Einstellung abgelehnt (Antwort %d)" % rid)
        return True


def _cli():
    import argparse
    a = argparse.ArgumentParser(description="USB-Zugang zum JamMan — die Befehle hier lesen; geschrieben wird über das Fenster")
    a.add_argument("--geraet", help="serieller Anschluß, z. B. /dev/cu.usbmodem21101")
    a.add_argument("--status", action="store_true", help="Gerät und Eigenschaften zeigen")
    a.add_argument("--ls", metavar="PFAD", help="Ordner im Pedal auflisten")
    a.add_argument("--cat", metavar="PFAD", help="Datei ausgeben (Text)")
    a.add_argument("--suche", action="store_true", help="nur Anschlüsse auflisten")
    a.add_argument("--namen", metavar="PFAD",
                   help="Patch-Ordner auflisten und die Namen aus patch.xml zeigen")
    args = a.parse_args()

    if args.suche:
        for d in finde_anschluesse():
            print("  " + d)
        return 0

    pedal = JamManUSB(args.geraet)
    print("  Gerät: %s an %s" % (pedal.kopf["name"], pedal.geraet))
    if args.status:
        p = pedal.eigenschaften()
        for k in ("FirmwareVersion", "ProtocolVersion", "SampleRate", "ChannelCount",
                  "BitDepth", "RemovableMediaIsPresent", "KilobytesCapacityOnNand",
                  "KilobytesAvailableOnNand", "KilobytesCapacityOnSd",
                  "KilobytesAvailableOnSd", "InternalPatchFolderPath",
                  "RemovablePatchFolderPath"):
            if k in p:
                print("  %-28s %s" % (k, p[k]))
    if args.ls:
        dateien, ordner = pedal.ordner(args.ls)
        for d in sorted(ordner):
            print("  [Ordner] " + d)
        for f in sorted(dateien):
            print("  " + f)
    if args.namen:
        import re as _re
        _, ordner = pedal.ordner(args.namen)
        for d in sorted(ordner):
            pfad = args.namen.rstrip("/") + "/" + d + "/patch.xml"
            try:
                xml = pedal.lies_datei(pfad).decode("utf-8", "replace")
                m = _re.search(r"<PatchName>(.*?)</PatchName>", xml)
                print("  %-10s %s" % (d, m.group(1) if m else "(ohne Namen)"))
            except Exception as e:
                print("  %-10s (%s)" % (d, e))
    if args.cat:
        daten = pedal.lies_datei(args.cat)
        sys.stdout.write(daten.decode("utf-8", "replace"))
    pedal.close()
    return 0


def _haelt_an():
    """Fenster offen halten, wenn das Skript per Doppelklick gestartet wurde.

    Unter Windows schließt sich das Konsolenfenster sofort, wenn das Skript
    endet — man sieht weder Anschlüsse noch Fehlermeldung (17.11.2026).
    """
    if not sys.platform.startswith("win"):
        return
    try:
        input("\nWeiter mit der Eingabetaste …")
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) == 1:          # per Doppelklick gestartet: keine Argumente
        print("USB-Zugang zum JamMan — nichts angegeben, es wird nur gesucht.")
        print("Beispiele:")
        print("  greenglass.py --suche")
        print("  greenglass.py --status")
        print("  greenglass.py --namen /mnt/nandfs/JamManSoloXT/")
        print()
        for d in finde_anschluesse():
            print("  gefunden:", d)
        _haelt_an()
        sys.exit(0)
    try:
        sys.exit(_cli())
    except SystemExit:
        raise
    except Exception as e:
        print("Fehler:", e)
        _haelt_an()
        sys.exit(1)
