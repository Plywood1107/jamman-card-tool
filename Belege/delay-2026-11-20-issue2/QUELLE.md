# Woher diese Dateien kommen

**Beitrag:** Plywood1107, GitHub-Issue #2, 20.11.2026
**Gerät:** JamMan Delay, über USB abgefragt (`greenglass.py --cat`)
**Pfad im Pedal:** `/mnt/nandfs/JamManDelay/Patch01/`

Der Beitragende hat sie auf Bitte geschickt, damit der Kartenaufbau geprüft
werden kann. Sie enthalten keine Musik und keinen Personenbezug.

**Was damit belegt ist** (geprüft am 20.11.2026, Fassung 3.134.1):

```
patch.xml    PatchName · RhythmType · StopMode · SettingsVersion · ID ·
             Metadata · Wurzel device="JamManDelay"
phrase.xml   BeatsPerMinute · BeatsPerMeasure · IsLoop · IsReversed ·
             SettingsVersion · AudioVersion · ID · Metadata
```

Also derselbe Aufbau wie beim Solo XT und beim Stereo, nur mit anderer
Geräteangabe in der Wurzel.

**Was NICHT belegt ist:** der Name der Audiodatei in `PhraseA` — die XML-Dateien
wurden gezeigt, die WAV-Datei nicht. Deshalb steht im README weiter „vor dem
ersten Schreiben die Karte kopieren".
