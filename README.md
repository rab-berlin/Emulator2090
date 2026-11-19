# Emulator für den Busch Microtronic 2090

Ein Terminal-Emulator für den Lerncomputer Busch Microtronic 2090 (TMS1600, 4 Bit, 256 Programmschritte), in Python, ohne Zusatzpakete.

```
python3 microtronic.py
```

Benötigt ein Terminal mit mindestens 80 x 22 Zeichen. Läuft unter Linux, macOS und Windows.

## Was er kann

- **Befehlssatz** des 2090 vollständig, mit der am Gerät gemessenen Befehlsdauer: 9,3 ms bei ausgeschalteter Anzeige, länger mit DISP.
- **Zufallszahlen wie am Gerät:** RND liest einen 12-Bit-Zähler. Er zählt +3 je Befehl (BRC/BRZ ohne Sprung +1, DISP +4) und etwa 210 Schritte/s im Leerlauf, und einige Befehle setzen ihn auf feste Werte. Taste `u` schaltet auf echten Zufall um.
- **Einschaltzustand wie am Gerät:** RF = F, alle anderen Register 0.
- **Festprogramme:** PGM 3/4 (Uhr), PGM 5/6.
- **Peripherie:** Eingangsquellen (Kabel, Rechteck, Rauschen, Zufallszahl), 4 Taster, ein 4x4-Keypad.
- **LED-Matrix 8x8:**
  - **ESP:** Protokoll des [ESP2090 Studios](https://github.com/rab-berlin/ESP2090) mit Anfrage/Antwort, Blockmodus, allen Zeichenbefehlen und Schlafen/Wecken (ACK 15, gehaltenes F, ein Befehl je Wecken), farbig dargestellt.
- **Userscripts** des ESP2090 Studios: Python-Skripte wie auf dem ESP laden, starten und stoppen. Sie laufen neben dem Programm, mit derselben Prüfung vor dem Start und denselben Funktionen (`setOutputs`, `getInputs`, `waitInputs`, `tasteG`/`tasteH`, `log`, `beep`, `setColors`, `setPalette`, `np`). Im Programmlauf folgen sie der simulierten Zeit, sonst der Echtzeit.
- **Pause:** Leertaste während des Laufs.
- **Tempo:** einstellbar; Reset behält das Tempo.
- **Debug-Modus:** Haltepunkte, Einzelschritt.

## Bedienung

| Tasten | Wirkung |
|---|---|
| `0`-`9` `a`-`f` | Hex-Eingabe |
| `h` `n` `r` `s` `g` `k` `p` `x` | HALT, NEXT, RUN, STEP, REG, BKP, PGM, C/CE |
| `l` / `w` | Programm laden / sichern (.mic) |
| `m` | Matrix ein / aus |
| `v` | rechtes Feld: Programm, Matrix oder Userscript |
| `y` / `j` | Userscript laden (.py) / starten bzw. stoppen |
| `G` / `H` (mit Shift) | Web-Tasten G und H, solange ein Userscript läuft |
| `o` / `t` | Eingangsquellen / Pfeiltasten den Tastern zuordnen |
| `u` | RND: Zähler wie das Gerät (mic) oder Zufall (emu) |
| `i` | Debug-Modus |
| `+` / `-` | Tempo |
| `z` / `?` / `q` | Reset / Hilfe / Ende |

## Programme

Programme sind Textdateien (`.mic`): je Zeile ein dreistelliger Hex-Befehl, Kommentare nach `;`. Ein Beispiel für alle Zeichenbefehle der Matrix ist [demo_matrix.mic](https://github.com/rab-berlin/ESP2090/tree/main/program/demo_matrix) im ESP2090-Repo.

Userscripts sind `.py`-Dateien im aktuellen Ordner oder im Unterordner `userscripts/`, genau wie auf dem ESP. Beispiele mit passendem Programm liegen ebenfalls im ESP2090-Repo unter [program](https://github.com/rab-berlin/ESP2090/tree/main/program) (`zufallszahl`, `ton`, `webtaster`). Unterschiede zum ESP: `print()` erscheint im Log, das Modul `machine` gibt es nicht, der Ton wird nur angezeigt.

## Dokumentation

[docs/microtronic.xml](docs/microtronic.xml) beschreibt den Befehlssatz mit allen am Gerät geklärten Details, die Peripherie, Programmier-Idiome und das Modell des Emulators.
