# Emulator für den Busch Microtronic 2090

Ein Terminal-Emulator für den Lerncomputer Busch Microtronic 2090 (TMS1600, 4 Bit, 256 Programmschritte), in Python, ohne Zusatzpakete.

```
python3 microtronic.py
```

Benötigt ein Terminal mit mindestens 80 x 22 Zeichen. Läuft unter Linux, macOS und Windows.

## Was er kann

- **Befehlssatz** des 2090 vollständig, mit der am Gerät gemessenen Befehlsdauer: 9,3 ms bei ausgeschalteter Anzeige, länger mit DISP.
- **Zufallszahlen wie am Gerät:** RND liest einen 12-Bit-Zähler. Er zählt +3 je Befehl und etwa 205 Schritte/s im Leerlauf, und einige Befehle setzen ihn auf feste Werte. Taste `u` schaltet auf echten Zufall um.
- **Einschaltzustand wie am Gerät:** RF = F, alle anderen Register 0.
- **Festprogramme:** PGM 3/4 (Uhr), PGM 5/6.
- **Peripherie:** Eingangsquellen (Kabel, Rechteck, Rauschen, Zufallszahl), 4 Taster, ein 4x4-Keypad.
- **LED-Matrix 8x8:**
  - **ESP:** Protokoll des [ESP2090 Studios](https://github.com/rab-berlin/ESP2090) mit Anfrage/Antwort, Blockmodus, allen Zeichenbefehlen und Schlafen/Wecken (ACK 15, gehaltenes F, ein Befehl je Wecken), farbig dargestellt.
  - **alt:** das frühere Blockprotokoll.
- **Pause:** Leertaste während des Laufs.
- **Tempo:** einstellbar; Reset behält das Tempo.
- **Debug-Modus:** Haltepunkte, Einzelschritt.

## Bedienung

| Tasten | Wirkung |
|---|---|
| `0`-`9` `a`-`f` | Hex-Eingabe |
| `h` `n` `r` `s` `g` `k` `p` `x` | HALT, NEXT, RUN, STEP, REG, BKP, PGM, C/CE |
| `l` / `w` | Programm laden / sichern (.mic) |
| `m` | Matrix: aus → ESP → alt → aus |
| `v` | rechtes Feld: Programm oder Matrix |
| `o` / `t` | Eingangsquellen / Pfeiltasten den Tastern zuordnen |
| `u` | RND: Zähler wie das Gerät (mic) oder Zufall (emu) |
| `i` | Debug-Modus |
| `+` / `-` | Tempo |
| `z` / `?` / `q` | Reset / Hilfe / Ende |

## Programme

Programme sind Textdateien (`.mic`): je Zeile ein dreistelliger Hex-Befehl, Kommentare nach `;`. Ein Beispiel für alle Zeichenbefehle der Matrix ist [demo_matrix.mic](https://github.com/rab-berlin/ESP2090/tree/main/program/demo_matrix) im ESP2090-Repo.

## Dokumentation

[docs/microtronic.xml](docs/microtronic.xml) beschreibt den Befehlssatz mit allen am Gerät geklärten Details, die Peripherie, Programmier-Idiome und das Modell des Emulators.
