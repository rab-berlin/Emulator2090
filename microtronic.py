#!/usr/bin/env python3
"""Emulator fuer den Busch Microtronic 2090 - interaktiv, ohne Startargumente.

Start:   python3 microtronic.py

Monitor, Panel und alle Einstellungen sind immer aktiv:
  Geraetetasten  0-9 a-f, h HALT, n NEXT, r RUN, s STEP, g REG, k BKP, p PGM, x C/CE
  Emulator       l Laden, w Sichern, i Debug, o Quellen, m Matrix, t Tasterbelegung, +/- Tempo, v Ansicht,
                 u RND-Modus (Zaehler / Zufall), z Reset, ? Hilfe, q Ende
                 Leertaste: NEXT, im Programmlauf Pause / weiter
"""
import datetime
import os
import random
import re
import select
import shutil
import sys
import time
from collections import deque

try:
    import termios
    import tty
except ImportError:          # Windows
    termios = tty = None
try:
    import msvcrt
except ImportError:          # Linux / macOS
    msvcrt = None

# Zeitmodell in Einheiten von 0,1 ms (ganzzahlig, damit die Phasenlage exakt bleibt)
T_CMD = 93            # Befehl bei abgeschaltetem Display: 9,3 ms
T_DISP_FIRST = 37     # erste angezeigte Stelle: +3,7 ms
T_DISP_MORE = 25      # jede weitere Stelle: +2,5 ms
T_DEFAULT_DISP = 205  # Standardanzeige ohne DISP (Adresse, Befehl): 20,5 ms
NIBBLE_MIN = 80       # Abstand aufeinanderfolgender Datenwerte: 9,3 ms (Display aus) +-
NIBBLE_MAX = 110      # Toleranz; bei laengerer Befehlsdauer wird das Bild verworfen
TOGGLE_MIN = 150      # F auf den Eingaengen: 15 ms ...
TOGGLE_MAX = 250      # ... bis 25 ms schaltet die Matrix um

# RND-Zaehler (am echten Geraet gemessen, siehe rnd-zaehler-protokoll.md):
# 12 Bit, +3 je ausgefuehrtem Befehl (unabhaengig von der Anzeige), im Leerlauf, im HALT-Zustand
# und waehrend KIN wartet zeitabhaengig ca. 205 Schritte/s; RESET setzt ihn auf 000.
CTR_STEP = 3          # Zaehlschritte je Befehl
CTR_RATE = 205        # Zaehlschritte je Sekunde Echtzeit im Leerlauf

HEXRE = re.compile(r"^[0-9A-Fa-f]{3}$")
TWO = ["MOV", "MOVI", "AND", "ANDI", "ADD", "ADDI", "SUB", "SUBI", "CMP", "CMPI", "OR"]
JUMP = {0xB: "CALL", 0xC: "GOTO", 0xD: "BRC", 0xE: "BRZ"}
ONE = {7: "MAS", 8: "INV", 9: "SHR", 0xA: "SHL", 0xB: "ADC", 0xC: "SUBC",
       0xD: "DIN", 0xE: "DOT", 0xF: "KIN"}
SPECIAL = ["HALT", "NOP", "DISOUT", "HXDZ", "DZHX", "RND", "TIME", "RET",
           "CLEAR", "STC", "RSC", "MULT", "DIV", "EXRL", "EXRM", "EXRA"]


def disasm(code):
    op, a, b = code >> 8, (code >> 4) & 0xF, code & 0xF
    if op <= 0xA:
        return f"{TWO[op]} {a:X},{b:X}"
    if op in JUMP:
        return f"{JUMP[op]} {code & 0xFF:02X}"
    if a == 0:
        return SPECIAL[b]
    if 1 <= a <= 6:
        return f"DISP {a},{b:X}"
    return f"{ONE[a]} {b:X}"


def load_program(path, warnings=None):
    """Liest eine Programmdatei (ein 3-stelliger Hex-Befehl pro Zeile) ab Adresse 00.

    Leere und ungueltige Zeilen werden ignoriert, Kommentare nach ';' oder '#'
    abgeschnitten. Warnungen landen in 'warnings' (sonst auf stderr).
    """
    prog = [0x000] * 256   # alle Adressen sind standardmaessig mit 000 belegt
    n = 0

    def warn(text):
        if warnings is not None:
            warnings.append(text)
        else:
            print("Warnung: " + text, file=sys.stderr)

    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            text = re.split(r"[;#]", line, maxsplit=1)[0].strip()
            if not text:
                continue
            if not HEXRE.match(text):
                warn(f"Zeile {lineno} ignoriert (ungueltig): {text!r}")
                continue
            if n >= 256:
                warn(f"Zeile {lineno} ignoriert (Programmspeicher voll)")
                continue
            prog[n] = int(text, 16)
            n += 1
    return prog, n


# Signalquellen fuer die Eingaenge (Zeitbasis: simulierte CPU-Zeit, Einheit 0,1 ms)
FREQS = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50]   # waehlbare Frequenzen in Hz
FREQ_DEFAULT = FREQS.index(1)                    # Standard: 1 Hz
SOURCE_KINDS = ["-", "Low", "High", "Ausgang", "Square", "Noise", "Zufall"]


class Signal:
    """Signalquelle: Low / High (dauerhaft, ohne Frequenz), Square (50 % Tastverhaeltnis), Noise (je Periode zufaelliger
    Pegel 0/1) oder Zufallszahl lo..hi (je Periode neu, binaer auf E1-E4)."""

    def __init__(self, kind, freq, lo=0, hi=15):
        self.kind, self.freq, self.lo, self.hi = kind, freq, lo, hi
        self._k = None
        self._v = 0

    def value(self, t):
        if self.kind in ("Low", "High"):
            return 1 if self.kind == "High" else 0
        phase = t * self.freq / 10000          # Perioden seit Zeit 0
        if self.kind == "Square":
            return 1 if phase % 1.0 < 0.5 else 0
        k = int(phase)
        if k != self._k:                       # neue Periode: neuer Zufallswert
            self._k = k
            self._v = (random.getrandbits(1) if self.kind == "Noise"
                       else random.randint(self.lo, self.hi))
        return self._v


class Clock:
    """Uhr des Betriebssystems (TIME, PGM 3, PGM 4).

    Standardmaessig laeuft die Systemzeit; PGM 3 stellt die Uhr auf eine
    eingegebene Zeit um (Versatz zur Systemzeit), danach laeuft sie von dort weiter.
    """

    def __init__(self):
        self.offset = datetime.timedelta(0)

    def now(self):
        return datetime.datetime.now() + self.offset

    def set(self, hour, minute):
        """Uhr auf hour:minute:00 stellen; die Uhr laeuft ab diesem Moment."""
        n = datetime.datetime.now()
        self.offset = n.replace(hour=hour, minute=minute, second=0, microsecond=0) - n

    def digits(self):
        """Sechs Anzeigestellen hhmmss."""
        return f"{self.now():%H%M%S}"


class Microtronic:
    def __init__(self, prog, din=(0,), keys=(), trace=False):
        self.prog = prog
        self.R = [0] * 15 + [0xF]  # Arbeitsregister: nach Einschalten/Reset RF = F, alle anderen 0 (Geraet)
        self.M = [0] * 16      # Speicherregister
        self.C = 0
        self.Z = 0
        self.pc = 0
        # einzige Ruecksprungadresse. Startwert: Calladresse 00, RET springt dorthin + 1 = 01.
        # Gespeichert wird hier schon die Adresse hinter dem CALL; jedes CALL ersetzt sie,
        # RET loescht sie nicht.
        self.ret = 1
        self.dout = 0
        self.display_on = True
        self.din = list(din) or [0]
        self.din_pos = 0
        self.keys = deque(keys)
        self.trace = trace
        self.halted = False
        self.error = None
        self.peripherals = []      # angeschlossene Geraete: dot(wert, zeit), din_bits(zeit)
        self.wires = []            # Kabel: Liste (Ausgang-Bit, Eingang-Bit), Bits 1-4, 4 = msb
        self.signals = [None] * 4  # Signalquelle je Eingang 1-4 (Square / Noise) oder None
        self.rnd = None            # Zufallszahl an allen 4 Eingaengen (Signal) oder None
        self.live = False          # True waehrend RUN: Signale laufen auf der simulierten Zeit
        self._t0 = time.monotonic()
        self.on_display = None     # Hook: f(zellen) - Monitor-Modus
        self.on_dot = None         # Hook: f(wert)
        self.key_source = None     # Hook: liefert Taste fuer KIN (blockierend)
        self.disp = None           # aktive DISP-Anzeige (n, s): bleibt live bis DISP/DISOUT/HALT
        self.last_cells = None     # zuletzt angezeigte Zellen
        self.steps = 0             # ausgefuehrte Befehle
        self.time = 0              # simulierte Zeit in 0,1 ms (Beginn des aktuellen Befehls)
        self.clock = Clock()       # Uhr (Systemzeit, per PGM 3 umstellbar); bleibt bei Reset erhalten
        self.ctr = 0               # RND-Zaehler (12 Bit)
        self.rnd_counter = True    # True: RND liest den Zaehler (wie das Geraet), False: echter Zufall
        self._idle_t = None        # Leerlauf: Startzeitpunkt (monotonic), None = Zaehler steht in der Zeit

    # ---- RND-Zaehler ----
    def ctr_sync(self):
        """Leerlauf-Zaehlschritte (Echtzeit, CTR_RATE/s) seit dem letzten Abgleich addieren."""
        if self._idle_t is not None:
            n = int((time.monotonic() - self._idle_t) * CTR_RATE)
            if n:
                self.ctr = (self.ctr + n) & 0xFFF
                self._idle_t += n / CTR_RATE

    def ctr_now(self):
        self.ctr_sync()
        return self.ctr

    def idle(self, on):
        """Leerlauf ein (Zaehler laeuft in Echtzeit) oder aus (Zaehler laeuft nur je Befehl)."""
        if on:
            if self._idle_t is None:
                self._idle_t = time.monotonic()
        else:
            self.ctr_sync()
            self._idle_t = None

    # ---- Hilfsfunktionen ----
    def step_duration(self):
        """Ausfuehrungsdauer des naechsten Befehls (0,1 ms) je nach Display-Zustand.

        Display aus: 9,3 ms. Display an: +3,7 ms fuer die erste Stelle, +2,5 ms je weitere.
        Ohne DISP-Befehl zeigt das Display Adresse und Befehl: 20,5 ms.
        """
        if not self.display_on:
            return T_CMD
        if self.disp is None:
            return T_DEFAULT_DISP
        return T_CMD + T_DISP_FIRST + T_DISP_MORE * (self.disp[0] - 1)

    def default_cells(self):
        """Standardanzeige ohne DISP: aktuell ausgefuehrte Adresse, Leerstelle, Befehl."""
        a, c = f"{self.pc:02X}", f"{self.prog[self.pc]:03X}"
        return [a[0], a[1], " ", c[0], c[1], c[2]]

    def show_default(self):
        cells = self.default_cells()
        if cells != self.last_cells:
            self.emit(cells)

    def setz(self, v):
        self.Z = 1 if v == 0 else 0

    def dec(self, base, lo, hi):
        """Dezimalzahl aus Registern lo..hi (lo = Einer); None bei Ziffer > 9."""
        regs = base[lo:hi + 1]
        if any(x > 9 for x in regs):
            return None
        return sum(x * 10 ** i for i, x in enumerate(regs))

    def put_dec(self, base, lo, hi, value):
        for i in range(lo, hi + 1):
            base[i] = value % 10
            value //= 10

    def live_cells(self):
        n, s = self.disp
        digits = [f"{self.R[(s + i) % 16]:X}" for i in range(n)]
        return [" "] * (6 - n) + digits[::-1]

    def emit(self, cells):
        self.last_cells = cells
        if self.on_display:
            self.on_display(cells)
        else:
            print("ANZEIGE: [" + " ".join(cells) + "]")

    def show(self, n, s):
        """DISP n,s: Anzeige bleibt aktiv und live, bis DISP/DISOUT/HALT."""
        self.display_on = True
        self.disp = (n, s)
        cells = self.live_cells()
        if cells != self.last_cells:
            self.emit(cells)

    def reset(self):
        """Power-on-Zustand: Register, Flags, Zaehler, Ausgaenge loeschen (Programm bleibt).
        Wie am Geraet steht danach RF = F, alle anderen Register R und M sind 0."""
        self.R = [0] * 15 + [0xF]
        self.M = [0] * 16
        self.C = self.Z = 0
        self.pc = 0
        self.ret = 1               # Calladresse 00 + 1, siehe __init__
        self.dout = 0
        self.display_on = True
        self.disp = None
        self.last_cells = None
        self.steps = 0
        self.time = 0
        self.halted = False
        self.error = None
        self.din_pos = 0
        self.ctr = 0               # RESET: Zaehler auf 000, im Leerlauf zaehlt er sofort weiter
        if self._idle_t is not None:
            self._idle_t = time.monotonic()

    def read_inputs(self, consume=False):
        """Wert der 4 Eingaenge: eingestellter Wert, Kabel und Geraete (z.B. LED-Matrix)."""
        v = self.din[min(self.din_pos, len(self.din) - 1)] & 0xF
        if consume:
            self.din_pos += 1
        for o, i in self.wires:                  # Kabel: Ausgang o -> Eingang i
            bit = (self.dout >> (o - 1)) & 1
            v = (v & ~(1 << (i - 1))) | (bit << (i - 1))
        if self.rnd:                             # Zufallszahl: alle 4 Eingaenge binaer
            v = self.rnd.value(self.signal_time()) & 0xF
        else:
            for idx, sig in enumerate(self.signals):
                if sig:
                    v = (v & ~(1 << idx)) | ((sig.value(self.signal_time()) & 1) << idx)
        for per in self.peripherals:             # Geraete setzen Eingangs-Bits
            v |= per.din_bits(self.time) & 0xF
        return v

    # ---- Eingangsquellen: je Eingang genau eine (Zufallszahl belegt alle vier) ----
    def source_kind(self, i):
        """Quelle von Eingang i (0-3): '-', 'Ausgang', 'Square', 'Noise' oder 'Zufall'."""
        if self.rnd:
            return "Zufall"
        if self.signals[i]:
            return self.signals[i].kind
        if any(ii == i + 1 for o, ii in self.wires):
            return "Ausgang"
        return "-"

    def signal_time(self):
        """Zeit der Signalquellen (0,1 ms): im Lauf die simulierte CPU-Zeit, sonst Echtzeit,
        damit die Signale auch bei stehendem Programm sichtbar laufen."""
        if self.live:
            return self.time
        return int((time.monotonic() - self._t0) * 10000)

    def has_signals(self):
        return bool(self.rnd) or any(self.signals)

    def source_signal(self, i):
        return self.rnd or self.signals[i]

    def set_source(self, i, kind, out=None):
        """Quelle fuer Eingang i setzen; kind aus SOURCE_KINDS, out = Ausgang-Bit 1-4."""
        sig = self.source_signal(i)
        freq = sig.freq if sig and sig.freq else FREQS[FREQ_DEFAULT]
        lo, hi = (self.rnd.lo, self.rnd.hi) if self.rnd else (0, 15)
        self.rnd = None
        self.signals[i] = None
        self.wires[:] = [w for w in self.wires if w[1] != i + 1]
        if kind == "Ausgang":
            self.wires.append((out or i + 1, i + 1))
        elif kind in ("Low", "High"):
            self.signals[i] = Signal(kind, None)
        elif kind in ("Square", "Noise"):
            self.signals[i] = Signal(kind, freq)
        elif kind == "Zufall":
            self.signals = [None] * 4
            self.wires.clear()
            self.rnd = Signal("Zufall", freq, lo, hi)

    def clear_sources(self):
        self.wires.clear()
        self.signals = [None] * 4
        self.rnd = None

    def next_key(self):
        if self.keys:
            return self.keys.popleft()
        if self.key_source:
            return self.key_source()
        while True:
            try:
                t = input("KIN wartet - Taste (0-F, q = Abbruch): ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if t.lower() == "q":
                return None
            if re.fullmatch(r"[0-9A-Fa-f]", t):
                return int(t, 16)

    # ---- Ausfuehrung ----
    def step(self):
        self.ctr_sync()                  # Einzelschritt aus dem Leerlauf: bisherige Zeit zaehlt
        code = self.prog[self.pc]
        self.steps += 1
        dur = self.step_duration()
        if self.trace:
            print(f"{self.pc:02X}: {code:03X}  {disasm(code):<10} C={self.C} Z={self.Z} "
                  f"R={''.join(f'{x:X}' for x in self.R)}")
        op, a, b = code >> 8, (code >> 4) & 0xF, code & 0xF
        nxt = (self.pc + 1) & 0xFF   # Adressraum 00-FF, nach FF folgt 00
        R = self.R

        if op <= 0xA:
            self.two_address(op, a, b)
        elif op in JUMP:
            aa = code & 0xFF
            if op == 0xB:
                self.ret = nxt
                nxt = aa
            elif op == 0xC:
                nxt = aa
            elif op == 0xD and self.C:
                nxt = aa
            elif op == 0xE and self.Z:
                nxt = aa
        elif a == 0:
            nxt = self.special(b, nxt)
            if self.error:
                return False
        elif a <= 6:
            self.show(a, b)
        else:
            d = s = b
            if a == 7:                       # MAS
                self.M[d] = R[d]
            elif a == 8:                     # INV
                R[d] ^= 0xF
                self.C = 0
                self.setz(R[d])
            elif a == 9:                     # SHR
                self.C = R[d] & 1
                R[d] >>= 1
                self.setz(R[d])
            elif a == 0xA:                   # SHL
                self.C = (R[d] >> 3) & 1
                R[d] = (R[d] << 1) & 0xF
                self.setz(R[d])
            elif a == 0xB:                   # ADC
                v = R[d] + self.C
                self.C = 1 if v > 15 else 0
                R[d] = v & 0xF
                self.setz(R[d])
            elif a == 0xC:                   # SUBC
                v = R[d] - self.C
                self.C = 1 if v < 0 else 0
                R[d] = v & 0xF
                self.setz(R[d])
            elif a == 0xD:                   # DIN
                R[d] = self.read_inputs(consume=True)
                self.C = 0
                self.setz(R[d])
            elif a == 0xE:                   # DOT
                self.dout = R[s]
                for per in self.peripherals:
                    per.dot(self.dout, self.time)
                if self.on_dot:
                    self.on_dot(self.dout)
                elif not self.peripherals:
                    print(f"AUSGANG: {self.dout:04b} ({self.dout:X})")
                self.C = 0
                self.setz(R[s])
            elif a == 0xF:                   # KIN
                k = self.next_key()
                if k is None:
                    self.error = "KIN: Programm waehrend des Wartens auf eine Taste abgebrochen"
                    return False
                R[d] = k
                self.C = 0
                self.setz(k)
        if self.disp is not None:            # Live-Anzeige: Aenderungen sofort zeigen
            cells = self.live_cells()
            if cells != self.last_cells:
                self.emit(cells)
        self.ctr = (self.ctr + CTR_STEP) & 0xFFF   # nach dem Befehl (Sonderbefehle setzen ihn vorher)
        self.time += dur
        for per in self.peripherals:         # Geraete mit eigener Zeit (ESP) bis Befehlsende laufen lassen
            tick = getattr(per, "tick", None)
            if tick:
                tick(self.time)
        self.pc = nxt
        return not self.halted

    def two_address(self, op, a, b):
        R = self.R
        d = b
        imm = op % 2 == 1 and op != 0xA
        src = a if imm else R[a]
        if op in (0, 1):                     # MOV / MOVI
            R[d] = src
            self.setz(R[d])
        elif op in (2, 3):                   # AND / ANDI
            R[d] &= src
            self.C = 0
            self.setz(R[d])
        elif op in (4, 5):                   # ADD / ADDI
            v = R[d] + src
            self.C = 1 if v > 15 else 0
            R[d] = v & 0xF
            self.setz(R[d])
        elif op in (6, 7):                   # SUB / SUBI
            v = R[d] - src
            self.C = 1 if v < 0 else 0
            R[d] = v & 0xF
            self.setz(R[d])
        elif op in (8, 9):                   # CMP / CMPI
            self.C = 1 if src < R[d] else 0
            self.Z = 1 if src == R[d] else 0
        elif op == 0xA:                      # OR
            R[d] |= R[a]
            self.C = 0
            self.setz(R[d])

    def special(self, b, nxt):
        R, M = self.R, self.M
        name = SPECIAL[b]
        if name == "HALT":
            self.halted = True
            self.disp = None                 # HALT beendet die DISP-Anzeige
            return self.pc
        if name == "NOP":
            pass
        elif name == "DISOUT":
            self.display_on = False
            self.disp = None
            self.last_cells = [" "] * 6
            if self.on_display:
                self.on_display([" "] * 6)
        elif name == "HXDZ":
            v = (R[0xF] << 8) | (R[0xE] << 4) | R[0xD]
            self.C = 0
            if v > 0x3E7:
                self.Z = 1
                R[0xD] = R[0xE] = R[0xF] = 0
                self.ctr = (v - 0x3E8) & 0xFFF   # Zaehler = Arbeitsspeicher: Vergleich mit 3E8 bleibt stehen
            else:
                self.Z = 0
                R[0xD], R[0xE], R[0xF] = v % 10, (v // 10) % 10, v // 100
                self.ctr = 0xFFF             # hexadezimal bis unter 000 heruntergezaehlt
        elif name == "DZHX":
            v = R[0xF] * 100 + R[0xE] * 10 + R[0xD]
            R[0xD], R[0xE], R[0xF] = v & 0xF, (v >> 4) & 0xF, (v >> 8) & 0xF
            self.C = 0
            self.Z = 0
            self.ctr = 0x999                 # dezimal bis unter 000 heruntergezaehlt
        elif name == "RND":
            v = self.ctr if self.rnd_counter else random.getrandbits(12)
            R[0xD], R[0xE], R[0xF] = v & 0xF, (v >> 4) & 0xF, (v >> 8) & 0xF
        elif name == "TIME":
            t = self.clock.now()
            R[0xA], R[0xB] = t.second % 10, t.second // 10
            R[0xC], R[0xD] = t.minute % 10, t.minute // 10
            R[0xE], R[0xF] = t.hour % 10, t.hour // 10
        elif name == "RET":
            nxt = self.ret                   # ohne vorheriges CALL: Sprung nach 01; ret bleibt erhalten
        elif name == "CLEAR":
            self.R[:] = [0] * 16
            self.C, self.Z = 0, 1
        elif name == "STC":
            self.C = 1
        elif name == "RSC":
            self.C = 0
        elif name == "MULT":
            w, m = self.dec(R, 0, 5), self.dec(M, 0, 5)
            if w is None or m is None:
                self.C = 1                   # Hex-Eingabe A-F: nur Carry, Register und Zero bleiben
            else:
                if w * m > 999999:
                    self.C, self.Z = 1, 0
                    R[0:6] = [0xE] * 6
                else:
                    self.C, self.Z = 0, 0
                    self.put_dec(R, 0, 5, w * m)
                # Zaehler = Stellenzeiger: hoechste Stelle != 0 des Multiplikanden M0-M5 (0 bei M = 0)
                self.ctr = max([i for i in range(6) if M[i]] or [0])
        elif name == "DIV":
            w, m = self.dec(R, 0, 3), self.dec(M, 0, 3)
            if w is None or m is None:
                self.C = 1                   # Hex-Eingabe A-F: nur Carry, Arbeits- und Speicherregister bleiben
            elif w == 0:
                self.C = 1                   # Divisor 0: alle 6 Arbeitsregister = E, Speicherregister bleiben
                R[0:6] = [0xE] * 6
                self.ctr = 0x000
            else:
                self.ctr = 0x100             # erfolgreiche Division: Zaehler fest 100
                self.C = 0
                self.put_dec(R, 0, 3, m // w)
                self.put_dec(M, 0, 3, m % w)
                self.Z = 1 if m % w else 0
        elif name == "EXRL":
            R[0:8], M[0:8] = M[0:8], R[0:8]
        elif name == "EXRM":
            R[8:16], M[8:16] = M[8:16], R[8:16]
        elif name == "EXRA":
            R[0:8], R[8:16] = R[8:16], R[0:8]
            self.ctr = 0x700 | (self.ctr & 0xF)   # obere Nibbles 7, 0; unterstes zaehlt weiter
        return nxt


SEG = {
    "0": (" _ ", "| |", "|_|"), "1": ("   ", "  |", "  |"), "2": (" _ ", " _|", "|_ "),
    "3": (" _ ", " _|", " _|"), "4": ("   ", "|_|", "  |"), "5": (" _ ", "|_ ", " _|"),
    "6": (" _ ", "|_ ", "|_|"), "7": (" _ ", "  |", "  |"), "8": (" _ ", "|_|", "|_|"),
    "9": (" _ ", "|_|", " _|"), "A": (" _ ", "|_|", "| |"), "B": ("   ", "|_ ", "|_|"),
    "C": (" _ ", "|  ", "|_ "), "D": ("   ", " _|", "|_|"), "E": (" _ ", "|_ ", "|_ "),
    "F": (" _ ", "|_ ", "|  "), " ": ("   ", "   ", "   "), "_": ("   ", "   ", " _ "),
}


# ---- LED-Matrix des ESP2090-Studios (Protokoll v2) ----
# Zeiten in 0,1 ms (simulierte CPU-Zeit), Werte aus firmware/main.py
ESP_DETECT = 7          # WAIT_AFTER_DETECT 0,7 ms: nach der ersten Flanke nochmal lesen, dann REQ low
ESP_REQ = 300           # WAIT_AFTER_REQ 30 ms: Timeout fuer das ACK, REQ geht low
ESP_GUARD = 400         # WAIT_GUARD 40 ms: danach noch auf ein spaetes ACK achten
ESP_IDLE = 200          # IDLE_SLEEP_MS 20 ms ohne ACK, dann neues REQ
ESP_AFTER_ACK = 135     # WAIT_AFTER_ACK / WAIT_AFTER_1 13,5 ms nach dem Kontroll-Lesen
ESP_NIBBLE = 92.17      # NIBBLE_INTERVAL 9,217 ms
ESP_AFTER_CMD = 100     # nach jeder Uebertragung 10 ms, dann neues REQ
ESP_NIBBLES = (0, 16, 0, 1, 2, 0, 2, 2, 2, 0, 2, 1, 0, 1, 1, 1)   # Nibbles je ACK
ESP_STACK = 8           # Plaetze je Stapel (ACK 13)
# Palette der Firmware (PALETTE_DEFAULT, Helligkeit 0-10) und Namen
PALETTE = ((0, 0, 0), (10, 0, 0), (10, 4, 0), (10, 10, 0), (0, 8, 0), (0, 8, 6), (0, 0, 10),
           (6, 0, 10), (10, 0, 6), (10, 10, 10), (3, 0, 0), (0, 3, 0), (0, 0, 3), (3, 3, 0),
           (10, 3, 3), (3, 3, 3))
PALETTE_NAMES = ("schwarz", "rot", "orange", "gelb", "gruen", "tuerkis", "blau", "violett",
                 "magenta", "weiss", "dunkelrot", "dunkelgruen", "dunkelblau", "oliv", "rosa", "grau")


class LedMatrix:
    """8x8-LED-Matrix am ESP2090-Studio als Peripherie (Protokoll v2, docs/LED-Matrix.md).

    Zwei Ebenen mit je 64 Eintraegen, Index y*8 + x (x = Spalte 0 links, y = Zeile 0 unten):
    bits (Pixel an/aus) und colors (Farbnummer 0-15 der Palette, anfangs 1 = rot).

    Handshake: Der ESP setzt Eingang 1 auf 1 (REQ). Ein ACK erkennt er erst, nachdem die
    Ausgaenge einmal 0 waren; das ACK ist der erste Wert != 0, den der 2090 mit DOT ausgibt.
    0,7 ms danach liest er es nochmal und nimmt REQ zurueck. Ohne ACK geht REQ nach 30 ms low,
    bis 70 ms nach REQ gilt ein spaetes ACK noch, sonst 20 ms Pause und neues REQ.
    ACK 1 (Blockmodus): ein Befehl Abstand, dann 16 Nibbles (Register 0-F) in die Bitmatrix.
    ACK 2-15 (Zeichenmodus): die 1 oder 2 Nibbles folgen direkt in den naechsten Schritten.
    Gelesen wird zeitgesteuert: erstes Nibble 13,5 ms (+9,217 ms bei ACK 1) nach dem
    Kontroll-Lesen, dann alle 9,217 ms - es gilt der Wert, der dann auf den Ausgaengen liegt.
    Nach der Uebertragung 10 ms Pause, dann neues REQ.

    Die Anzeige wird nur neu aufgebaut, wenn der Befehl das verlangt (wie die Firmware):
    Farbe waehlen (ACK 14), Zeichenart 0 und Stapel (ACK 13) aendern nur den Speicher;
    sichtbar wird das mit dem naechsten Neuaufbau.

    Protokoll 'alt' (Blockmodus der fruehen Programme, z.B. Game of Life): Ready dauerhaft
    auf Eingang 1, Startnibble 1, 16 Nibbles im 9,3-ms-Takt, Endnibble 0 uebernimmt das Bild;
    ein F von 15-25 ms schaltet die Matrix um.
    """

    def __init__(self):
        self.on_frame = None
        self.active = False        # LED on/off der Weboberflaeche (Taste m), anfangs aus
        self.proto = "esp"         # esp = ESP2090-Studio, alt = altes Blockprotokoll
        self.reset()

    # ---- Speicher (matrix_draw.Matrix) ----
    def clear_memory(self):
        self.bits = [0] * 64
        self.colors = [1] * 64     # alles rot
        self.c1x = self.c1y = self.c2x = self.c2y = 0
        self.paint = 1
        self.shown_on = True       # ACK 15: Anzeige an/aus
        self.stack_bits, self.stack_colors = [], []
        self.redraw(notify=False)

    def reset(self):
        """Einschalten des ESP: Speicher leer, Anzeige leer."""
        self.clear_memory()
        self.reset_protocol()

    def reset_protocol(self):
        """CPU-Reset: angefangene Uebertragung verwerfen, Zaehler auf 0; Bild, Farben und
        aktiv/inaktiv bleiben (der ESP laeuft weiter)."""
        self.frames = 0            # empfangene Bilder (ACK 1 bzw. altes Protokoll)
        self.cmds = 0              # empfangene Zeichenbefehle (ACK 2-15)
        self.last_ack = None
        self.out = 0               # Ausgaenge des 2090 (nach Reset 0)
        self.phase = "sleep"       # sleep, wait, read
        self.wake = 0              # sleep: Zeitpunkt des naechsten REQ (None = sofort)
        self.req_t = 0
        self.req_low = 0
        self.seen_zero = False
        self.ack = 0
        self.reads = []            # read: ausstehende Lesezeitpunkte
        self.data = []
        # altes Protokoll
        self.state, self.buf = "idle", []
        self.f_since = None
        self.last_t = 0

    def toggle(self):
        """LED on/off (Taste m bzw. altes Umschaltsignal)."""
        self.active = not self.active
        self.phase, self.wake, self.reads = "sleep", None, []
        self.state, self.buf, self.f_since = "idle", [], None

    def set_proto(self, proto):
        self.proto = proto
        self.phase, self.wake, self.reads = "sleep", None, []
        self.state, self.buf, self.f_since = "idle", [], None

    def redraw(self, notify=True):
        """Anzeige neu aufbauen: Pixel an -> Farbnummer, sonst None."""
        self.shown = [self.colors[i] if self.shown_on and self.bits[i] else None for i in range(64)]
        if notify and self.on_frame:
            self.on_frame(self)

    def pixel(self, x, y):
        return self.shown[y * 8 + x]

    def _apply(self, i, art):
        if art == 0:
            self.colors[i] = self.paint
        elif art == 1:
            self.colors[i] = self.paint
            self.bits[i] = 1 if self.paint else 0
        elif art == 2:
            self.bits[i] = 1
        else:
            self.bits[i] = 0

    def _rect(self, art, filled):
        x0, x1 = sorted((self.c1x, self.c2x))
        y0, y1 = sorted((self.c1y, self.c2y))
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                if filled or x in (x0, x1) or y in (y0, y1):
                    self._apply(y * 8 + x, art)

    def _line(self, art):
        x, y, x1, y1 = self.c1x, self.c1y, self.c2x, self.c2y
        dx, dy = abs(x1 - x), -abs(y1 - y)
        sx, sy = (1 if x < x1 else -1), (1 if y < y1 else -1)
        err = dx + dy
        while True:
            self._apply(y * 8 + x, art)
            if x == x1 and y == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x += sx
            if e2 <= dx:
                err += dx
                y += sy

    def _shift(self, r):
        d, wrap = r & 3, r & 8
        nb, nc = [0] * 64, list(self.colors)
        for y in range(8):
            for x in range(8):
                sx = x + 1 if d == 0 else x - 1 if d == 1 else x
                sy = y - 1 if d == 2 else y + 1 if d == 3 else y
                if wrap:
                    sx, sy = sx % 8, sy % 8
                elif not (0 <= sx < 8 and 0 <= sy < 8):
                    continue                  # frei werdende Stelle: Bit aus, Farbe bleibt
                nb[y * 8 + x] = self.bits[sy * 8 + sx]
                nc[y * 8 + x] = self.colors[sy * 8 + sx]
        self.bits, self.colors = nb, nc

    def _stack(self, w):
        action = w >> 2
        for bit in (1, 2):
            if not w & bit:
                continue
            st = self.stack_colors if bit == 1 else self.stack_bits
            if action == 0:                   # push, voll: die aelteste faellt heraus
                if len(st) >= ESP_STACK:
                    st.pop(0)
                st.append(list(self.colors if bit == 1 else self.bits))
            elif action == 3:                 # leeren
                st.clear()
            elif st:                          # pop / peek
                top = st.pop() if action == 1 else st[-1]
                if bit == 1:
                    self.colors = list(top)
                else:
                    self.bits = list(top)
        return False

    def frame(self, nibbles):
        """Blockmodus: Register k -> Pixel 4k..4k+3, Bit 0 = linkes Pixel."""
        for k, v in enumerate(nibbles):
            for j in range(4):
                self.bits[4 * k + j] = (v >> j) & 1
        return True

    def command(self, ack, data):
        """Befehl ausfuehren (matrix_draw.Matrix.command); True = Anzeige neu aufbauen."""
        if ack == 1:
            return self.frame(data)
        if ack in (4, 6, 7, 8):
            x, y = data
            art = ((x >> 3) & 1) | (((y >> 3) & 1) << 1)
            x, y = x & 7, y & 7
            if ack == 4:
                self.c1x, self.c1y = x, y
                self._apply(y * 8 + x, art)
            else:
                self.c2x, self.c2y = x, y
                if ack == 8:
                    self._line(art)
                    self.c1x, self.c1y = x, y
                else:
                    self._rect(art, ack == 6)
            return art != 0
        if ack == 3:
            art = data[0] & 3
            for i in range(64):
                self._apply(i, art)
            return art != 0
        if ack == 10:
            k, w = data
            for j in range(4):
                self.bits[4 * k + j] = (w >> j) & 1
            return True
        if ack == 11:
            self._shift(data[0])
            return True
        if ack == 13:
            return self._stack(data[0])
        if ack == 14:
            self.paint = data[0] & 15
            return False
        if ack == 15:
            w = data[0]
            if w not in (1, 2, 3):
                return False
            self.shown_on = (not self.shown_on) if w == 1 else (w == 2)
            return True
        return False                          # ACK 2, 5, 9, 12: ohne Bedeutung

    # ---- Handshake auf der simulierten Zeit ----
    def _start_req(self, t):
        self.phase, self.req_t, self.req_low = "wait", t, t + ESP_REQ
        self.seen_zero = self.out == 0

    def advance(self, t):
        """ESP bis zum Zeitpunkt t laufen lassen (Ereignisse vor t, Ausgaenge = self.out)."""
        if not self.active or self.proto != "esp":
            return
        while True:
            if self.phase == "sleep":
                if self.wake is None:
                    self.wake = t
                if self.wake > t:
                    return
                self._start_req(self.wake)
            elif self.phase == "wait":
                end = self.req_t + ESP_REQ + ESP_GUARD
                if end >= t:
                    return
                self.phase, self.wake = "sleep", end + ESP_IDLE      # kein ACK
            else:                                                     # read
                while self.reads and self.reads[0] < t:
                    self.data.append(self.out)
                    last = self.reads.pop(0)
                    if not self.reads:
                        self._finish(last)
                        break
                else:
                    return

    def _finish(self, last):
        ack = self.ack
        if ack == 1:
            self.frames += 1
        else:
            self.cmds += 1
        self.last_ack = ack
        if self.command(ack, self.data):
            self.redraw()
        self.phase, self.wake = "sleep", last + ESP_AFTER_CMD

    def tick(self, t):
        self.advance(t)

    def din_bits(self, t=0):
        if not self.active:
            return 0
        if self.proto == "alt":
            return 1                                  # Ready dauerhaft 1
        self.advance(t)
        return 1 if self.phase == "wait" and self.req_t <= t < self.req_low else 0

    def dot(self, v, t=0):
        v &= 0xF
        if self.proto == "alt":
            self._dot_alt(v, t)
            return
        self.advance(t)
        self.out = v
        if not self.active or self.phase != "wait":
            return
        if not self.seen_zero:
            self.seen_zero = v == 0
        elif v:                                       # ACK erkannt
            ack_t = t + ESP_DETECT
            self.req_low = min(self.req_low, ack_t)
            self.ack, self.data = v, []
            n = ESP_NIBBLES[v]
            first = ack_t + ESP_AFTER_ACK + (ESP_NIBBLE if v == 1 else 0)
            if n:
                self.phase = "read"
                self.reads = [first + k * ESP_NIBBLE for k in range(n)]
            else:
                self.cmds += 1
                self.last_ack = v
                self.phase, self.wake = "sleep", ack_t + ESP_AFTER_CMD

    # ---- altes Protokoll (Startnibble 1, 16 Werte, Endnibble 0, F-Umschaltsignal) ----
    def _dot_alt(self, v, t):
        if self.f_since is not None and v != 0xF:        # F-Pegel endet
            dur = t - self.f_since
            self.f_since = None
            if TOGGLE_MIN <= dur <= TOGGLE_MAX:
                self.toggle()
                return
        if self.state == "data" and self.buf and not NIBBLE_MIN <= t - self.last_t <= NIBBLE_MAX:
            self.state, self.buf = "idle", []         # Werte nicht im 9-ms-Takt: Bild verworfen
        self.last_t = t
        if self.state == "idle":
            if v == 0xF:
                if self.f_since is None:
                    self.f_since = t
            elif self.active and v == 1:
                self.state, self.buf = "data", []
        elif self.state == "data":
            self.buf.append(v)
            if len(self.buf) == 16:
                self.state = "end"
        else:                                   # Endenibble
            if v == 0:
                self.frame(self.buf)
                self.frames += 1
                self.redraw()
            self.state = "data" if v == 1 else "idle"
            if v == 1:
                self.buf = []

    def receiving(self):
        """Fortschritt einer laufenden Uebertragung als Text (oder '')."""
        if self.proto == "alt":
            return f"({len(self.buf)}/16)" if self.active and self.state == "data" else ""
        if self.phase == "read":
            n = len(self.data) + len(self.reads)
            return f"({len(self.data)}/{n})"
        return ""


class LiveKeys:
    """Windows: echter Tastenzustand ueber GetAsyncKeyState (user32).

    Eine Taste gilt genau so lange als gedrueckt, wie sie gehalten wird, auch wenn
    das Terminalfenster im Hintergrund ist. Abfrage bei jedem DIN und beim Zeichnen.
    """
    VK_ARROW = {"LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28}

    def __init__(self):
        import ctypes
        f = ctypes.windll.user32.GetAsyncKeyState
        f.argtypes = [ctypes.c_int]
        f.restype = ctypes.c_short
        self._state = f

    def down(self, vk):
        return bool(self._state(vk) & 0x8000)      # Bit 15 = Taste ist jetzt gedrueckt

    def arrow(self, name):
        return self.down(self.VK_ARROW[name])

    def hexkey(self, k):
        """Hex-Taste k (0-F): Ziffern 0-9 (auch Ziffernblock), Buchstaben A-F."""
        if k < 10:
            return self.down(0x30 + k) or self.down(0x60 + k)
        return self.down(0x41 + k - 10)


def live_keys():
    """Echtzeit-Tasten nur unter Windows; sonst (Linux, macOS, Termux) None = Zeitschaetzung.
    Umgebungsvariable MICROTRONIC_KEYS=terminal erzwingt die Zeitschaetzung auch unter Windows."""
    if os.name != "nt" or os.environ.get("MICROTRONIC_KEYS", "").lower() == "terminal":
        return None
    try:
        return LiveKeys()
    except (ImportError, AttributeError, OSError):
        return None


class Keypad:
    """4 Taster an Eingang 1-4 und 4x4-Keypad (Strobe-Abfrage) als Peripherie.

    Keypad: Reihe r (von unten 1-4) liegt an Ausgang r, Spalte c (von links 1-4)
    an Eingang c; Taste k (0 unten links, zeilenweise nach oben bis F) liegt in
    Reihe k//4 + 1 und Spalte k%4 + 1. Eine gedrueckte Taste setzt ihren Eingang
    auf 1, solange ihre Reihe am Ausgang aktiv ist (letzter DOT-Wert).
    Taster: Taster i setzt Eingang i direkt auf 1.

    Zwei Arten, den Tastenzustand zu bestimmen:
    live = LiveKeys (Windows): echter Zustand der Computertasten; die Taster folgen der
           Pfeilbelegung arrows (Liste je Taster, None = nicht angeschlossen); gate()
           sagt, ob die Tasten gerade als Keypad/Taster gelten (nur im Lauf, nicht im
           Quellen- oder Belegungsmodus).
    live = None (Terminal): ein Terminal meldet kein Loslassen; press_key/press_button
           werten die Tastendruecke mit KEY_HOLD_FIRST / KEY_HOLD_REPEAT aus.
    """

    def __init__(self, live=None):
        self.live = live
        self.arrows = list(ARROWS)
        self.gate = lambda: True
        self.reset()

    def reset(self):
        self.out = 0
        self.keys = {}          # Taste 0-15 -> gedrueckt bis (monotonic)
        self.buttons = {}       # Taster 0-3 -> gedrueckt bis

    @staticmethod
    def _press(table, k):
        now = time.monotonic()
        if table.get(k, 0) > now:                       # wird gehalten (Tastenwiederholung)
            table[k] = max(table[k], now + KEY_HOLD_REPEAT)
        else:
            table[k] = now + KEY_HOLD_FIRST

    def press_key(self, k):
        self._press(self.keys, k & 0xF)

    def press_button(self, i):
        self._press(self.buttons, i & 3)

    def pressed_keys(self):
        if self.live:
            if not self.gate():
                return set()
            return {k for k in range(16) if self.live.hexkey(k)}
        now = time.monotonic()
        return {k for k, t in self.keys.items() if t > now}

    def pressed_buttons(self):
        if self.live:
            if not self.gate():
                return set()
            return {i for i, a in enumerate(self.arrows) if a and self.live.arrow(a)}
        now = time.monotonic()
        return {i for i, t in self.buttons.items() if t > now}

    def dot(self, v, t=0):
        self.out = v & 0xF

    def din_bits(self, t=0):
        v = 0
        for i in self.pressed_buttons():
            v |= 1 << i
        for k in self.pressed_keys():
            if (self.out >> (k // 4)) & 1:
                v |= 1 << (k % 4)
        return v


def screen_start():
    if os.name == "nt":
        os.system("")              # ANSI-Escape-Sequenzen unter Windows aktivieren
    sys.stdout.write("\033[2J\033[H\033[?25l")
    sys.stdout.flush()


def screen_stop():
    sys.stdout.write("\033[?25h\n")
    sys.stdout.flush()


# Farben: Im Text stehen Markierungen aus dem Unicode-Privatbereich (Breite 0 fuer fit()),
# erst screen_draw macht daraus ANSI-Sequenzen. Farbe c = ANSI-Index 0-15
# (0 schwarz, 1 rot, 2 gruen, 3 gelb, 4 blau, 5 magenta, 6 cyan, 7 weiss, +8 = hell).
MARK_LO, MARK_HI = "\ue000", "\ue0ff"
MARK_RESET = "\ue0ff"
LED_RED = 9                                  # Farbe der LEDs, Segmente und Matrix-Sterne
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def col(text, c):
    """Text in Farbe c (0-15) setzen."""
    return chr(0xE000 + c) + text + MARK_RESET


def vlen(text):
    """Sichtbare Laenge ohne Farbmarkierungen."""
    return sum(1 for ch in text if not MARK_LO <= ch <= MARK_HI)


def colorize(line):
    out = []
    for ch in line:
        if MARK_LO <= ch <= MARK_HI:
            if not COLOR:
                continue
            n = ord(ch) - 0xE000
            if ch == MARK_RESET:
                out.append("\033[0m")
            elif n >= 16:                        # Matrix-Palette (16 + Farbnummer), 24 Bit
                r, g, b = (min(255, v * 25 + (5 if v else 0)) for v in PALETTE[n - 16])
                out.append(f"\033[38;2;{r};{g};{b}m")
            else:
                out.append(f"\033[{30 + n if n < 8 else 82 + n}m")
        else:
            out.append(ch)
    return "".join(out)


def screen_draw(lines):
    """Zeichnet immer ab Position links oben: kein Scrolling."""
    lines = [colorize(l) for l in lines]
    sys.stdout.write("\033[H" + "\033[K\n".join(lines) + "\033[K\033[J")
    sys.stdout.flush()


class HaltRequest(Exception):
    """HALT-Taste waehrend des Programmlaufs (oder waehrend KIN wartet)."""


class QuitRequest(Exception):
    """Emulator beenden."""


WIRE_KEYS = ("UP", "DOWN", "LEFT", "RIGHT", "FASTER", "SLOWER", "CCE", "ESC")
HEXDIGITS = "0123456789abcdef"
# Tasten ausserhalb der Hex-Ziffern: Geraetetasten und Emulator-Funktionen
KEYMAP = {"h": "HALT", "n": "NEXT", " ": "SPACE", "r": "RUN", "s": "STEP",
          "g": "REG", "k": "BKP", "p": "PGM", "x": "CCE",
          "l": "LOAD", "w": "SAVE", "i": "DEBUG", "o": "WIRE", "v": "VIEW",
          "z": "RESET", "m": "MATRIX", "t": "TMAP", "?": "HELP", "+": "FASTER", "=": "FASTER",
          "-": "SLOWER", "_": "SLOWER", "q": "QUIT", "u": "RNDMODE"}


class Keyboard:
    """Liest einzelne Tastendruecke (ohne Enter), auch aus einer Pipe."""

    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.old = None

    def __enter__(self):
        if sys.stdin.isatty() and termios:
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if self.old is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def _getch(self, timeout):
        """Zeichen lesen; None bei Timeout, '' bei Ende der Eingabe."""
        if msvcrt:
            end = None if timeout is None else time.time() + timeout
            while True:
                if msvcrt.kbhit():
                    return msvcrt.getwch()
                if end is not None and time.time() >= end:
                    return None
                time.sleep(0.005)
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return None
        return os.read(self.fd, 1).decode("latin-1")

    def read_raw(self, timeout=None):
        """Zeichen oder Name: None, EOF, ESC, ENTER, BKSP, TAB, UP, DOWN, LEFT, RIGHT, OTHER."""
        c = self._getch(timeout)
        if c is None:
            return None
        if c in ("", "\x03"):
            return "EOF"
        if c in ("\r", "\n"):
            return "ENTER"
        if c in ("\x7f", "\x08"):
            return "BKSP"
        if c == "\t":
            return "TAB"
        if c == "\x1b":
            c2 = self._getch(0.03)
            if not c2:
                return "ESC"
            if c2 in "[O":
                c3 = self._getch(0.03)
                tok = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(c3, "OTHER")
                while c3 and not c3.isalpha() and c3 != "~":   # Rest der Sequenz verschlucken
                    c3 = self._getch(0.03)
                return tok
            return "OTHER"
        if c in ("\x00", "\xe0"):                      # Windows: Sondertasten
            # Das zweite Zeichen des Paars steckt schon in der CRT: kbhit() meldet es nicht,
            # getwch() liefert es sofort (sonst wuerde z.B. Pfeil hoch als Taste H ankommen)
            c2 = msvcrt.getwch() if msvcrt else self._getch(0.03)
            return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}.get(c2, "OTHER")
        return c if c.isprintable() else "OTHER"

    def read(self, timeout=None):
        """Wie read_raw, aber als Geraete-/Emulator-Taste: 0-15, Name oder None."""
        t = self.read_raw(timeout)
        if t is None:
            return None
        if t == "EOF":
            return "EXIT"
        if t == "ENTER":
            return "NEXT"
        if t == "BKSP":
            return "CCE"
        if t == "ESC" or t in TASTER_KEYS:
            return t
        if len(t) == 1:
            lc = t.lower()
            if lc in HEXDIGITS:
                return HEXDIGITS.index(lc)
            return KEYMAP.get(lc, "OTHER")
        return "OTHER"


# Festprogramme PGM 0-7 (Geraet):
#   0 Hardwaretest, 1/2 Kassette laden/sichern: entfallen im Emulator
#   3 Uhrzeit einstellen (hhmm, ab Minuten von rechts), HALT startet die Uhr
#   4 Uhrzeit hhmmss auf dem Display, Ausgaenge = Sekunden-Einer binaer
#   5 Programmspeicher loeschen (alle 000), 6 Programmspeicher auf F01 setzen
#   7 Nim-Spiel: Befehlsliste (Zahlen oder 3-stellige Hex-Strings), wird nachgereicht
FIXED_PROGRAMS = {}

# Tempo = Faktor auf die Originalzeit (9,3 ms + Display-Zuschlag je Befehl); 0 = max
SPEEDS = [0, 20, 10, 5, 2, 1, 0.5, 0.25, 0.1, 0.05, 0.01]
SPEED_ORIGINAL = SPEEDS.index(1)

# Untere Matrix-Begrenzung: ASCII kennt kein hochliegendes waagerechtes Zeichen,
# daher der Macron (U+00AF); ASCII-Ersatz: "-"
BOTTOM_CHAR = "\u00af"

# Matrix-Ansicht: Blockbreite (Zeilennummer, Rand, 15 Zeichen, Rand) und Abstand zum rechten Feldrand
MATRIX_WIDTH = 19
MATRIX_MARGIN = 1

# Fensterbreiten (Zeichen): links | Mitte (Tastaturen) | rechts; Summe 76 + 4 Rahmenzeichen = 80
W_LEFT, W_MID, W_RIGHT = 38, 17, 21

# Taster 1-4 (Eingang 1-4) auf der Computertastatur: Pfeiltasten links, hoch, runter, rechts
TASTER_KEYS = {"LEFT": 0, "UP": 1, "DOWN": 2, "RIGHT": 3}    # Grundbelegung
ARROWS = ["LEFT", "UP", "DOWN", "RIGHT"]
ARROW_CHAR = {"LEFT": "<", "UP": "^", "DOWN": "v", "RIGHT": ">"}
ARROW_NAME = {"LEFT": "Pfeil links", "UP": "Pfeil hoch", "DOWN": "Pfeil runter",
              "RIGHT": "Pfeil rechts", None: "nicht angeschlossen"}
ARROW_CYCLE = [None] + ARROWS        # Reihenfolge beim Durchschalten im Belegungsmodus (t)
# Tastendruecke kennen im Terminal kein Loslassen: eine Taste gilt als gedrueckt
# (nicht unter Windows: dort liefert LiveKeys den echten Tastenzustand)
KEY_HOLD_FIRST = 0.7     # s nach dem ersten Druck (ueberbrueckt die Wartezeit bis zur Tastenwiederholung)
KEY_HOLD_REPEAT = 0.15   # s nach jeder Wiederholung (Taste wird gehalten)


def speed_text(f):
    if f == 0:
        return "max"
    return f"{f:g}x" + (" (Original)" if f == 1 else "")


LOAD_KEYS = "0123456789abcdefghijklmnopqrstuvwxyz"   # Auswahltasten im Ladedialog


def short_name(name, width=16):
    """Dateinamen kuerzen: Anfang bleiben, Endung bleibt, Tilde markiert die Kuerzung."""
    if len(name) <= width:
        return name
    stem, ext = os.path.splitext(name)
    return stem[:max(1, width - len(ext) - 1)] + "~" + ext


def fit(text, width):
    """Auf sichtbare Breite bringen (Farbmarkierungen zaehlen nicht)."""
    n = vlen(text)
    if n <= width:
        return text + " " * (width - n)
    out, n = [], 0
    for ch in text:
        if not MARK_LO <= ch <= MARK_HI:
            if n == width:
                break
            n += 1
        out.append(ch)
    return "".join(out) + MARK_RESET


class Emulator:
    """Monitor + Panel + Einstellungen in einem festen 80 x 22 Bildschirm."""

    def __init__(self, kb):
        self.kb = kb
        self.cpu = Microtronic([0] * 256, din=[0])
        self.matrix = LedMatrix()
        self.keypad = Keypad(live_keys())
        self.view = "prog"            # rechtes Feld: prog oder matrix (Taste v)
        self.speed_idx = SPEED_ORIGINAL   # Faktor 1 = Originalgeschwindigkeit
        self.debug = False            # Debug-Modus: HALT/Haltepunkt behalten den Display-Zustand
        self.filename = ""
        self.modified = False
        self.mode = "ADR"             # ADR, GOTO, REGSEL, REG, BKP, PGM
        self.buf = ""
        self.reg = 0
        self.after_halt = False
        self.last = None
        self.bkp = None
        self.override = None
        self.msg = "Willkommen!  l = Laden, ? = Hilfe - oder Befehle direkt eintippen"
        self.running = False
        self.paused = False           # Pause im Programmlauf (Leertaste): alles steht, auch Zeit und Zaehler
        self.taster_arrow = list(ARROWS)   # Pfeiltaste je Taster 1-4 (None = nicht angeschlossen),
                                           # bleibt bei Reset erhalten
        self.taster_mode = False          # Belegungsmodus (Taste t)
        self.keypad.arrows = self.taster_arrow             # Echtzeit-Tasten: dieselbe Belegung
        self.keypad.gate = lambda: (self.running and not self.paused
                                    and not (self.wire_mode or self.taster_mode))
        self.wire_mode = False
        self.wire_cursor = 0          # gewaehlter Eingang (0-3) im Quellenmodus
        self.wire_buf = ""            # Bereichseingabe der Zufallszahl (von-bis)
        self.wire_last = None
        self.abort_run = False
        self.dialog = None
        self.last_draw = 0.0
        cpu = self.cpu
        cpu.on_display = self.on_display
        cpu.on_dot = self.on_dot
        cpu.key_source = self.kin_key
        cpu.peripherals.append(self.matrix)      # Matrix und Tastaturen sind immer angeschlossen
        cpu.peripherals.append(self.keypad)
        cpu.idle(True)                           # RND-Zaehler laeuft im Leerlauf in Echtzeit
        self.matrix.on_frame = lambda m: self.render(force=False)

    # ------------------------------------------------------------------ Zustand
    @property
    def delay(self):
        return SPEEDS[self.speed_idx]

    def interrupt_display(self):
        """HALT, Haltepunkt und Programmende unterbrechen auf dem Geraet den Display-Zustand
        (Anzeige: Adresse + Befehl). Im Debug-Modus bleibt der Zustand fuer das Timing erhalten."""
        if not self.debug:
            self.cpu.display_on = True
            self.cpu.disp = None

    def reset_all(self):
        self.cpu.reset()
        self.matrix.reset_protocol()           # Matrix: Bildzaehler 0; Zustand (aktiv/inaktiv) und Bild bleiben
        self.keypad.reset()
        self.override = None
        self.mode, self.buf, self.reg = "ADR", "", 0
        self.abort_run = True
        self.taster_mode = False
        self.wire_mode = False                 # Quelleneingabe beenden
        self.wire_buf = ""
        self.debug = False                     # Debug-Modus aus; Tempo und RND-Modus bleiben
        self.msg = "Reset: Register, Flags, Ausgaenge, Zaehler und Debug zurueck, Tempo bleibt"

    # ------------------------------------------------------------------ Anzeige
    def cells(self):
        if self.override is not None:
            return self.override
        c = self.cpu
        if self.mode == "ADR":
            a = f"{c.pc:02X}"
            cmd = self.buf.rjust(3) if self.buf else f"{c.prog[c.pc]:03X}"   # Puffer getrennt vom Inhalt
            return [a[0], a[1], " ", cmd[0], cmd[1], cmd[2]]
        if self.mode in ("GOTO", "BKP"):
            b = self.buf.ljust(2, "_")
            return [b[0], b[1]] + [" "] * 4
        if self.mode == "REG":
            return [f"{self.reg:X}", " ", " ", " ", " ", f"{c.R[self.reg]:X}"]
        if self.mode == "TIMESET":           # PGM 3: hhmm von rechts eingeben, Sekunden 00
            return list(self.buf.rjust(4, "0") + "00" if self.buf else c.clock.digits())
        return ["_"] + [" "] * 5            # REGSEL, PGM

    def hint(self):
        if self.running:
            if self.paused:
                return "PAUSE  -  Leertaste = weiter, h = HALT"
            return "Programm laeuft  -  Leertaste = Pause, h = HALT"
        return {
            "ADR": f"Adresse {self.cpu.pc:02X}: Ziffern schieben von rechts in den Puffer, n speichert und geht weiter",
            "GOTO": "Adresse waehlen: 2 Hex-Ziffern",
            "REGSEL": "Register waehlen: 0-F",
            "REG": "Ziffer = neuer Inhalt, n = naechstes Register, h = zurueck",
            "BKP": "Haltepunkt-Adresse: 2 Hex-Ziffern, 00 loescht den Haltepunkt",
            "PGM": "Festprogramm waehlen: 0-7",
            "TIMESET": "PGM 3: Uhrzeit hhmm eingeben (schiebt von rechts ein), h = Uhr starten, Esc = Abbruch",
            "UHR": "PGM 4: Uhr laeuft, Ausgaenge = Sekunden-Einer binaer  -  h = HALT",
        }[self.mode]

    def left_rows(self, cells):
        c = self.cpu
        code = c.prog[c.pc]
        seg = [SEG.get(str(x).upper(), SEG[" "]) for x in cells]
        rows3 = ["  " + " ".join(col(g[r], LED_RED) if g[r].strip() else g[r] for g in seg)
                 for r in range(3)]
        boxes = lambda v, c=None: " ".join(
            (col("[#]", c) if c is not None else "[#]") if (v >> b) & 1 else "[ ]" for b in range(4))
        out, inp = c.dout, c.read_inputs()
        b = "--" if self.bkp is None else f"{self.bkp:02X}"
        # RND-Modus ganz rechts neben Ausgang/Eingang: mic = Zaehler wie das Geraet, emu = Zufall
        rnd_sel = "mic" if c.rnd_counter else "emu"
        return [
            " DISPLAY",
            fit(rows3[0], 28) + f"PC   {c.pc:02X}",
            fit(rows3[1], 28) + f"Code {code:03X}",
            fit(rows3[2], 28) + disasm(code),
            "",
            " Bit".ljust(10) + " ".join(f" {n} " for n in (1, 2, 3, 4)),
            fit(" AUSGANG".ljust(10) + boxes(out, LED_RED) + f"  = {out:X}", W_LEFT - 4) + "RND",
            fit(" EINGANG".ljust(10) + boxes(inp) + f"  = {inp:X}", W_LEFT - 4) + rnd_sel,
            "",
            " Reg".ljust(6) + " ".join(f"{i:X}" for i in range(16)),
            " R  ".ljust(6) + " ".join(f"{x:X}" for x in c.R),
            " M  ".ljust(6) + " ".join(f"{x:X}" for x in c.M),
            " " + (col(f"C={c.C}", LED_RED) if c.C else f"C={c.C}") + " "
            + (col(f"Z={c.Z}", LED_RED) if c.Z else f"Z={c.Z}") + f"  Schritte {c.steps}  BKP {b}",
        ]

    def taster_label(self, i):
        """Pfeilsymbol, das Taster i (0-3) bedient; leer = nicht angeschlossen."""
        a = self.taster_arrow[i]
        return ARROW_CHAR[a] if a else " "

    def cycle_taster(self, i):
        """Belegungsmodus: Pfeiltaste von Taster i weiterschalten (leer, <, ^, v, >)."""
        n = ARROW_CYCLE.index(self.taster_arrow[i]) + 1
        self.taster_arrow[i] = ARROW_CYCLE[n % len(ARROW_CYCLE)]
        self.msg = (f"Taster {i + 1} = {ARROW_NAME[self.taster_arrow[i]]}"
                    "   (1-4 weiter, t oder andere Taste = fertig)")

    def press_arrow(self, arrow):
        """Pfeiltaste im Lauf: bedient alle Taster, denen sie zugeordnet ist."""
        for i, a in enumerate(self.taster_arrow):
            if a == arrow:
                self.keypad.press_button(i)

    def source_rows(self):
        """4 Zeilen unter PERIPHERIE: Quelle je Eingang (Zufallszahl: Block ueber alle vier)."""
        c, wm = self.cpu, self.wire_mode
        if c.rnd:
            r = c.rnd
            v = r.value(c.signal_time()) & 0xF
            rng = f"{self.wire_buf}-_" if self.wire_buf else f"{r.lo:X}-{r.hi:X}"
            return [(" >" if wm else "  ") + "Zufall E1-4", f"  Bereich {rng}",
                    f"  Takt {r.freq:g}Hz", f"  Wert {v:X} = {v:04b}"]
        rows = []
        for i in range(4):
            kind = c.source_kind(i)
            if kind == "-":
                txt = f"E{i + 1} -"
            elif kind == "Ausgang":
                txt = f"E{i + 1} <- A{[o for o, ii in c.wires if ii == i + 1][0]}"
            elif c.signals[i].freq is None:
                txt = f"E{i + 1} {kind}"
            else:
                txt = f"E{i + 1} {kind} {c.signals[i].freq:g}Hz"
            rows.append((" >" if wm and self.wire_cursor == i else "  ") + txt)
        return rows

    def mid_rows(self):
        """Mittleres Fenster: 4x4-Keypad (Reihen = Ausgang, Spalten = Eingang) und 4 Taster."""
        kp = self.keypad
        keys, btns = kp.pressed_keys(), kp.pressed_buttons()
        box = lambda on, label: "[#]" if on else f"[{label}]"
        rows = [" PERIPHERIE"] + self.source_rows() + [" Taster" + (" Belegung" if self.taster_mode
                                                                     else " live" if kp.live else ""),
                "   " + "".join(box(i in btns, self.taster_label(i)) for i in range(4)),
                " Keypad"]
        for r in range(3, -1, -1):                       # Reihe 4 oben, Reihe 1 unten
            mark = ">" if (kp.out >> r) & 1 else " "     # > = Reihe am Ausgang aktiv
            rows.append(f" {r + 1}{mark}" + "".join(box(4 * r + c in keys, f"{4 * r + c:x}")
                                                   for c in range(4)))
        rows.append("    1  2  3  4")                    # Spaltennummern unten
        return rows

    def right_rows(self):
        c = self.cpu
        if self.view == "matrix":
            m = self.matrix
            status = f"B{m.frames} Z{m.cmds}"             # Bilder (Blockmodus) und Zeichenbefehle
            if m.proto == "esp" and m.last_ack is not None:
                status += f" A{m.last_ack}"               # letztes ACK
            status = f"{status} {m.receiving()}".rstrip()
            top = "   " + "_" * 15                       # obere Begrenzung
            sep = lambda y: " " if y == 0 else "."       # Reihe 1 ohne Trennpunkte
            dot = lambda c: " " if c is None else col("*", 16 + c)
            rows = [f"{y + 1} |" + sep(y).join(dot(m.pixel(x, y)) for x in range(8)) + "|"
                    for y in range(7, -1, -1)]
            bottom = "   " + BOTTOM_CHAR * 15              # untere Begrenzung
            title = "MATRIX " + ("ESP" if m.proto == "esp" else "alt") + (" an" if m.active else " aus")
            if m.proto == "esp" and not m.shown_on:
                title += " dunkel"                        # ACK 15: Anzeige aus
            body = [title, top] + rows + [bottom, "   " + " ".join(str(x + 1) for x in range(8)),
                                              status]
            pad = " " * (W_RIGHT - MATRIX_WIDTH - MATRIX_MARGIN)   # Block ganz rechts im Feld
            return [pad + line for line in body]
        rows = [" PROGRAMM"]
        for i in range(12):
            a = (c.pc - 4 + i) & 0xFF
            code = c.prog[a]
            mark = ">" if a == c.pc else ("*" if a == self.bkp else " ")
            rows.append(f"{mark}{a:02X}  {code:03X}  {disasm(code)}")
        return rows

    def build_lines(self):
        bar = "+" + "-" * 78 + "+"
        split = "+" + "-" * W_LEFT + "+" + "-" * W_MID + "+" + "-" * W_RIGHT + "+"
        label = ("PAUSE" if self.paused else "RUN") if self.running else {"TIMESET": "PGM 3", "UHR": "PGM 4"}.get(self.mode, self.mode)
        name = (self.filename or "(kein Programm)") + ("*" if self.modified else "")
        right = f"Modus {label}{' DEBUG' if self.debug else ''}   Tempo {speed_text(self.delay)} "
        left = f" MICROTRONIC 2090   Datei: {name}"[:77 - len(right)]
        head = "|" + left.ljust(78 - len(right)) + right + "|"
        if self.dialog is not None:
            top = bottom = bar
            body = ["|" + fit(l, 78) + "|" for l in (self.dialog + [""] * 13)[:13]]
        else:
            top = bottom = split
            body = ["|" + fit(l, W_LEFT) + "|" + fit(m, W_MID) + "|" + fit(r, W_RIGHT) + "|"
                    for l, m, r in zip(self.left_rows(self.cells()), self.mid_rows() + [""] * 13,
                                       self.right_rows())]
        msg = self.msg or ("Taster-Belegung: 1-4 schaltet den Pfeil weiter (leer = offen), t = fertig"
                           if self.taster_mode else "hoch/runter Eingang  links/rechts Quelle  +/- Takt  x loescht  o/Esc fertig"
                           if self.wire_mode else self.hint())
        text = lambda t: "| " + fit(t, 76) + " |"
        return ([bar, head, top] + body + [bottom, text(msg),
                text("h HALT  n NEXT  r RUN  s STEP  g REG  k BKP  p PGM  x C/CE  0-9 a-f Hex"),
                text("l Laden  w Sichern  i Debug  o Quelle  m Matrix  +/- Tempo  v Sicht  z Reset"),
                text("? Hilfe  q Ende  t Taster  u RND  Leertaste: NEXT, im Lauf Pause  Esc"), bar])

    def render(self, force=True):
        now = time.time()
        if self.running and not force and now - self.last_draw < 0.03:
            return
        self.last_draw = now
        cols, rows = shutil.get_terminal_size((80, 24))
        if cols < 80 or rows < 22:
            lines = ["Terminal zu klein: mindestens 80 x 22 Zeichen noetig.",
                     f"Aktuell {cols} x {rows} - bitte das Fenster vergroessern."]
        else:
            lines = self.build_lines()
        screen_draw(lines)

    def on_display(self, cells):
        self.override = cells
        if self.running:
            self.render(force=False)

    def on_dot(self, value):
        if self.running:
            self.render(force=False)

    # ------------------------------------------------------------------ Dialoge
    def show(self, title, lines, hint="", blank=True):
        rows = ([" " + title] + ([""] if blank else []) + lines)[:12]
        rows += [""] * (12 - len(rows))
        self.dialog = rows + [" " + hint]
        self.render()

    def prompt_text(self, title, default=""):
        """Texteingabe; die erste getippte Taste ersetzt den Vorschlag, Backspace bearbeitet ihn."""
        text, replace = default, bool(default)
        try:
            while True:
                self.show(title, ["", " > " + text + "_"],
                          "Enter = OK   Esc = Abbruch   Tippen ersetzt, Backspace aendert")
                t = self.kb.read_raw()
                if t == "EOF":
                    raise QuitRequest
                if t == "ENTER":
                    return text
                if t == "ESC":
                    return None
                if t == "BKSP":
                    text, replace = text[:-1], False
                elif len(t) == 1:
                    text, replace = (t if replace else text + t), False
        finally:
            self.dialog = None

    def confirm(self, question):
        try:
            self.show(question, ["", " j = ja      sonst = nein"], "")
            t = self.kb.read_raw()
            if t == "EOF":
                raise QuitRequest
            return t in ("j", "J", "y", "Y")
        finally:
            self.dialog = None

    def ask_quit(self):
        if self.modified and not self.confirm("Programm wurde geaendert und nicht gesichert - trotzdem beenden?"):
            return
        raise QuitRequest

    def dlg_help(self):
        lines = [
            " Geraet:   0-9 a-f Hex   h HALT   n NEXT   r RUN   s STEP   g REG   k BKP",
            "           p PGM   x C/CE (auch Backspace)   Enter und Leertaste = NEXT",
            " Ablauf:   h n aa Adresse   h g r Register   h k aa Haltepunkt (00 = loeschen)",
            "           h p n = Festprogramm: 3 Uhr stellen, 4 Uhr, 5 = 000, 6 = F01, 7 Nim",
            " Lauf:     Leertaste = Pause / weiter   0-9 a-f Keypad   Pfeile = Taster",
            " Emulator: l Laden  w Sichern  z Reset (Tempo bleibt)  v Ansicht  t 1-4 Taster",
            "           u RND: mic = Zaehler wie das Geraet, emu = Zufall      q Ende",
            "           m Matrix: aus -> ESP2090 (REQ/ACK, Zeichenbefehle) -> alt -> aus",
            "           i Debug: HALT/Haltepunkt behalten Display-Zustand und Timing",
            "           o Quellen: ^v Eingang, <> Quelle, +/- Takt, x loescht (2x = alle)",
            " Anzeige:  DISP bleibt live bis DISP/DISOUT/HALT; HALT zeigt Adresse + Befehl",
        ]
        self.show("HILFE", lines, "beliebige Taste = zurueck", blank=False)
        try:
            if self.kb.read_raw() == "EOF":
                raise QuitRequest
        finally:
            self.dialog = None

    def load_file(self, path, reset=True):
        path = os.path.expanduser(path)
        if not os.path.isfile(path) and not os.path.splitext(path)[1]:
            for ext in (".mic", ".txt"):                  # Endung weggelassen: .mic, dann .txt
                if os.path.isfile(path + ext):
                    path += ext
                    break
        warns = []
        try:
            prog, n = load_program(path, warns)
        except OSError as e:
            self.msg = f"Fehler beim Laden: {e.strerror or e}"
            return False
        self.cpu.prog[:] = prog
        self.filename = path
        self.modified = False
        self.cpu.pc = 0
        if reset:
            self.reset_all()
        self.abort_run = True
        self.msg = f"Geladen: {os.path.basename(path)}, {n} Befehle" + (
            f", {len(warns)} ungueltige Zeilen ignoriert" if warns else "")
        return True

    def dlg_load(self):
        try:
            files = sorted((f for f in os.listdir(".") if f.lower().endswith((".mic", ".txt"))),
                           key=lambda f: (not f.lower().endswith(".mic"), f.lower()))
        except OSError:
            files = []
        shown = files[:len(LOAD_KEYS)]
        nrows = -(-len(shown) // 4)                         # 4 Spalten, erst senkrecht, dann nach rechts
        lines = [" " + "".join(
            fit(f"{LOAD_KEYS[c * nrows + r]} {short_name(shown[c * nrows + r])}", 19)
            for c in range(4) if c * nrows + r < len(shown)) for r in range(nrows)]
        if not shown:
            lines = [" (keine .mic- oder .txt-Dateien im Ordner)"]
        elif len(files) > len(shown):
            lines.append(f" ... {len(files) - len(shown)} weitere Dateien (Leertaste: Namen eingeben)")
        choice = None
        try:
            self.show(f"PROGRAMM LADEN   Ordner: {os.getcwd()}", lines,
                      "Taste waehlen   Leertaste = Name eingeben   Esc = Abbruch")
            while choice is None:
                t = self.kb.read_raw()
                if t == "EOF":
                    raise QuitRequest
                if t == "ESC":
                    return
                if t == " ":
                    choice = " "
                elif len(t) == 1 and t.lower() in LOAD_KEYS[:len(shown)]:
                    choice = t.lower()
        finally:
            self.dialog = None
        if choice == " ":
            name = self.prompt_text("PROGRAMM LADEN - Dateiname", self.filename)
            if name:
                self.load_file(name)
        else:
            self.load_file(shown[LOAD_KEYS.index(choice)])

    def save_program(self, path):
        path = os.path.expanduser(path)
        if not os.path.splitext(path)[1]:
            path += ".mic"                                # Standardendung
        prog = self.cpu.prog
        last = max([a for a in range(256) if prog[a]] or [0])
        try:
            with open(path, "w", encoding="utf-8") as f:
                for a in range(last + 1):
                    f.write(f"{prog[a]:03X}   ; {a:02X}  {disasm(prog[a])}\n")
        except OSError as e:
            self.msg = f"Fehler beim Sichern: {e.strerror or e}"
            return
        self.filename = path
        self.modified = False
        self.msg = f"Gesichert: {os.path.basename(path)}, {last + 1} Befehle"

    def dlg_save(self):
        name = self.prompt_text("PROGRAMM SICHERN - Dateiname", self.filename or "programm.mic")
        if name:
            self.save_program(name)

    # ------------------------------------------------------------------ Tasten
    def change_speed(self, delta):
        self.speed_idx = max(0, min(len(SPEEDS) - 1, self.speed_idx - delta))
        self.msg = f"Tempo: {speed_text(self.delay)}"

    def wire_key(self, k):
        """Taste im Quellenmodus: Eingang waehlen, Quelle blaettern, Takt, Wert, loeschen."""
        cpu, i = self.cpu, self.wire_cursor
        prev, self.wire_last = self.wire_last, k
        self.msg = ""
        kind = cpu.source_kind(i)
        if k == "ESC":
            self.wire_mode, self.wire_buf = False, ""
        elif k in ("UP", "DOWN"):
            self.wire_cursor = (i + (1 if k == "DOWN" else -1)) % 4
            self.wire_buf = ""
        elif k in ("LEFT", "RIGHT"):
            n = SOURCE_KINDS.index(kind) + (1 if k == "RIGHT" else -1)
            cpu.set_source(i, SOURCE_KINDS[n % len(SOURCE_KINDS)])
            self.wire_buf = ""
        elif k in ("FASTER", "SLOWER"):
            sig = cpu.source_signal(i)
            if sig and sig.freq is not None:
                n = FREQS.index(sig.freq) + (1 if k == "FASTER" else -1)
                sig.freq = FREQS[max(0, min(len(FREQS) - 1, n))]
        elif k == "CCE":                                  # wie C/CE: einmal Zeile, zweimal alles
            if prev == "CCE":
                cpu.clear_sources()
                self.msg = "Alle Quellen entfernt"
            else:
                cpu.set_source(i, "-")
        elif kind == "Ausgang" and 1 <= k <= 4:
            cpu.set_source(i, "Ausgang", out=k)
        elif kind == "Zufall":                            # Bereich von-bis: zwei Hex-Ziffern
            self.wire_buf += f"{k:X}"
            if len(self.wire_buf) == 2:
                a, b = int(self.wire_buf[0], 16), int(self.wire_buf[1], 16)
                cpu.rnd.lo, cpu.rnd.hi = min(a, b), max(a, b)
                self.wire_buf = ""
        elif 1 <= k <= 4:                                 # sonst: Ziffer waehlt den Eingang
            self.wire_cursor = k - 1

    def common_key(self, k):
        """Emulator-Tasten (auch waehrend RUN und KIN). True = Taste verbraucht."""
        if k == "EXIT":
            raise QuitRequest
        if self.wire_mode and k != "WIRE":
            if isinstance(k, int) or k in WIRE_KEYS:
                self.wire_key(k)
                return True
            self.wire_mode = False                        # jede andere Taste beendet den
            self.wire_buf = ""                            # Quellenmodus und wirkt dann normal
        if self.taster_mode:                              # Belegungsmodus: 1-4 schalten weiter
            if isinstance(k, int) and 1 <= k <= 4:
                self.cycle_taster(k - 1)
                return True
            self.taster_mode = False                      # t oder jede andere Taste beendet ihn
            self.msg = ""
            if k == "TMAP":
                return True
        if k == "QUIT":
            self.ask_quit()
            return True
        if k == "FASTER":
            self.change_speed(+1)
            return True
        if k == "SLOWER":
            self.change_speed(-1)
            return True
        if k == "VIEW":
            self.view = "prog" if self.view == "matrix" else "matrix"
            return True
        if k == "TMAP":                                   # Belegungsmodus der Taster einschalten
            self.taster_mode = True
            self.msg = ""
            return True
        if k == "MATRIX":
            m = self.matrix                               # Zyklus: aus -> ESP2090 -> alt -> aus
            if not m.active:
                m.set_proto("esp")
                m.toggle()
            elif m.proto == "esp":
                m.set_proto("alt")
            else:
                m.toggle()
            self.msg = ("Matrix aus" if not m.active else
                        "Matrix an: ESP2090-Studio (REQ/ACK, Blockmodus ACK 1 und Zeichenbefehle)"
                        if m.proto == "esp" else
                        "Matrix an: altes Protokoll (Startnibble 1, 16 Werte, Endnibble 0)")
            return True
        if k == "DEBUG":
            self.debug = not self.debug
            self.msg = ("Debug AN: HALT/Haltepunkt behalten den Display-Zustand (Timing wie im Lauf)"
                        if self.debug else
                        "Debug AUS: HALT/Haltepunkt stellen Adresse + Befehl dar (wie das Geraet)")
            return True
        if k == "RNDMODE":
            cpu = self.cpu
            cpu.rnd_counter = not cpu.rnd_counter
            self.msg = ("RND: Zaehler wie das Geraet (+3 je Befehl, 205/s im Leerlauf)"
                        if cpu.rnd_counter else "RND: echter Zufall (3 zufaellige Hex-Ziffern)")
            return True
        if k == "WIRE":
            self.wire_mode = not self.wire_mode
            self.wire_buf, self.wire_last = "", None
            self.msg = ""
            return True
        if k in ("LOAD", "SAVE", "RESET", "HELP"):
            {"LOAD": self.dlg_load, "SAVE": self.dlg_save,
             "RESET": self.reset_all, "HELP": self.dlg_help}[k]()
            return True
        return False

    def handle(self, k):
        if k == "OTHER":
            return
        if k == "SPACE":                                     # ausserhalb des Laufs: NEXT
            k = "NEXT"
        if k in TASTER_KEYS:                                 # Taster nur waehrend RUN aktiv
            if self.wire_mode or self.taster_mode:
                self.common_key(k)                           # Quellen-/Belegungsmodus
            return
        if self.common_key(k):
            return
        self.override = None
        self.msg = ""
        prefix, self.after_halt = self.after_halt, False
        prev, self.last = self.last, k
        cpu = self.cpu
        if self.mode == "TIMESET":
            self.handle_timeset(k, prev)
            return
        if k == "HALT":
            self.mode, self.buf, self.after_halt = "ADR", "", True
            self.interrupt_display()
        elif k == "ESC":
            self.mode, self.buf = "ADR", ""
        elif k == "CCE":
            if self.mode in ("ADR", "GOTO", "BKP"):
                self.buf = "" if prev == "CCE" else self.buf[:-1]
        elif k == "NEXT":
            if prefix:                                   # HALT NEXT aa
                self.mode, self.buf = "GOTO", ""
            elif self.mode == "ADR":
                if self.buf:                             # Befehl nur bei NEXT speichern
                    code = int(self.buf.rjust(3, "0"), 16)
                    if cpu.prog[cpu.pc] != code:
                        cpu.prog[cpu.pc] = code
                        self.modified = True
                self.buf = ""
                cpu.pc = (cpu.pc + 1) & 0xFF
            elif self.mode == "REG":
                self.reg = (self.reg + 1) & 0xF
        elif k in ("REG", "BKP", "PGM"):
            if prefix:                                   # HALT REG / BKP / PGM
                self.mode = {"REG": "REGSEL", "BKP": "BKP", "PGM": "PGM"}[k]
                self.buf = ""
        elif k == "RUN":
            self.mode, self.buf = "ADR", ""
            self.run()
        elif k == "STEP":
            self.mode, self.buf = "ADR", ""
            self.single_step()
        elif isinstance(k, int):
            self.digit(k)

    def digit(self, k):
        d, cpu = f"{k:X}", self.cpu
        if self.mode == "ADR":
            self.buf = (self.buf + d)[-3:]               # Puffer: von rechts einschieben
        elif self.mode == "GOTO":
            self.buf += d
            if len(self.buf) == 2:
                cpu.pc = int(self.buf, 16)
                self.mode, self.buf = "ADR", ""
        elif self.mode == "BKP":
            self.buf += d
            if len(self.buf) == 2:
                value = int(self.buf, 16)
                self.bkp = value or None                  # 00 loescht den Haltepunkt
                self.msg = (f"Haltepunkt bei {value:02X} gesetzt" if value else "Haltepunkt geloescht")
                self.mode, self.buf = "ADR", ""
        elif self.mode == "REGSEL":
            self.reg, self.mode = k, "REG"
        elif self.mode == "REG":
            cpu.R[self.reg] = k
        elif self.mode == "PGM" and k <= 7:
            self.mode = "ADR"
            self.load_pgm(k)

    def load_pgm(self, n):
        """HALT PGM n: Festprogramm n (siehe FIXED_PROGRAMS)."""
        cpu = self.cpu
        if n in (0, 1, 2):
            what = {0: "Hardwaretest", 1: "Kassette laden", 2: "Kassette sichern"}[n]
            self.msg = f"Festprogramm {n} ({what}) entfaellt im Emulator"
        elif n == 3:
            self.mode, self.buf = "TIMESET", ""
            self.msg = ""
        elif n == 4:
            self.run_clock()
        elif n in (5, 6):
            fill = 0x000 if n == 5 else 0xF01
            cpu.prog[:] = [fill] * 256
            cpu.pc = 0
            self.modified = True
            self.msg = f"Festprogramm {n}: alle Adressen auf {fill:03X} gesetzt, Adresse 00"
        else:
            prog = FIXED_PROGRAMS.get(n)
            if prog is None:
                self.msg = f"Festprogramm {n} (Nim) ist noch nicht hinterlegt"
                return
            codes = [int(c, 16) if isinstance(c, str) else c for c in prog][:256]
            cpu.prog[:] = codes + [0] * (256 - len(codes))
            cpu.pc = 0
            self.filename = f"PGM {n}"
            self.modified = False
            self.abort_run = True
            self.msg = f"Festprogramm {n} geladen"

    def handle_timeset(self, k, prev):
        """PGM 3: Uhrzeit hhmm eingeben; HALT stellt und startet die Uhr."""
        cpu = self.cpu
        if k == "HALT":
            if self.buf:
                b = self.buf.rjust(4, "0")
                h, m = int(b[:2]), int(b[2:])
                if h > 23 or m > 59:
                    self.msg = f"Ungueltige Uhrzeit {b[:2]}:{b[2:]} - bitte neu eingeben"
                    self.buf = ""
                    return
                cpu.clock.set(h, m)
                self.msg = f"Uhr gestellt und gestartet: {h:02d}:{m:02d}:00"
            else:
                self.msg = "Uhr unveraendert (Systemzeit bzw. zuletzt gestellte Zeit laeuft weiter)"
            self.mode, self.buf, self.after_halt = "ADR", "", True
            self.interrupt_display()
        elif k == "ESC":
            self.mode, self.buf = "ADR", ""
            self.msg = "PGM 3 abgebrochen: Uhr unveraendert"
        elif k == "CCE":
            self.buf = "" if prev == "CCE" else self.buf[:-1]
        elif isinstance(k, int):
            if k <= 9:
                self.buf = (self.buf + str(k))[-4:]
            else:
                self.msg = "Nur Ziffern 0-9"

    def run_clock(self):
        """PGM 4: Uhrzeit hhmmss auf allen 6 Stellen, Ausgaenge = Sekunden-Einer binaer.

        Die Ausgaenge werden direkt gesetzt (Kabel sehen sie); die Peripherie wird nicht
        benachrichtigt, damit die Matrix die Sekundenwerte nicht als Bild-Protokoll liest.
        Beendet mit HALT.
        """
        cpu = self.cpu
        self.mode, self.buf, self.msg = "UHR", "", ""
        self.abort_run = False
        last = None
        try:
            while True:
                t = cpu.clock.now()
                cpu.dout = t.second % 10
                state = (f"{t:%H%M%S}", cpu.dout)
                if state != last:
                    last = state
                    self.override = list(state[0])
                    self.render()
                k = self.kb.read(0.1)
                if k is None:
                    continue
                if k == "HALT":
                    break
                if isinstance(k, int) or k in TASTER_KEYS or k == "OTHER":
                    continue
                self.common_key(k)
                if self.abort_run:
                    break
                last = None                      # neu zeichnen (z.B. nach Ansichtswechsel)
        finally:
            self.override = None
            self.mode, self.buf, self.after_halt = "ADR", "", True
            self.interrupt_display()
        self.msg = "Uhr beendet (HALT)"

    # ------------------------------------------------------------------ Ablauf
    def single_step(self):
        cpu = self.cpu
        self.abort_run = False
        try:
            cpu.step()
        except HaltRequest:
            self.after_halt = True
            self.interrupt_display()
        if cpu.disp is not None:
            self.override = cpu.live_cells()
        if cpu.error:
            self.msg, cpu.error = cpu.error, None
        cpu.halted = False

    def poll_keys(self):
        """Alle anstehenden Tasten waehrend RUN bearbeiten (Tastenwiederholung).
        True = Lauf beenden (HALT oder Emulator-Taste mit Abbruch)."""
        while True:
            k = self.kb.read(0)
            if k is None:
                return False
            if k == "HALT":
                self.after_halt = True
                return True
            if k == "SPACE":                         # Pause / weiter
                self.paused = not self.paused
                self.msg = ""
                self.render()
                continue
            if (self.wire_mode or self.taster_mode) and k != "OTHER":   # Modus: Tasten gehoeren ihm
                self.common_key(k)
                if self.abort_run:
                    return True
            elif isinstance(k, int):
                if not self.paused:                  # in der Pause zaehlen Keypad und Taster nicht
                    self.keypad.press_key(k)
            elif k in TASTER_KEYS:
                if not self.paused:
                    self.press_arrow(k)
            elif k != "OTHER":
                self.common_key(k)
                if self.abort_run:
                    return True

    def run(self):
        cpu = self.cpu
        cpu.halted = False
        self.running = True
        cpu.live = True
        cpu.idle(False)                      # im Lauf zaehlt der Zaehler nur je Befehl
        self.abort_run = False
        if not cpu.display_on:
            self.override = [" "] * 6
        else:
            self.override = cpu.live_cells() if cpu.disp is not None else cpu.default_cells()
        cpu.last_cells = self.override
        self.render()
        first = True
        deadline = time.monotonic()
        try:
            while True:
                if not first and cpu.pc == self.bkp:
                    self.msg = "Haltepunkt erreicht"
                    break
                first = False
                stop = False
                if self.poll_keys():
                    break
                if self.paused:                      # Pause: Ausfuehrung, Zeit und Zaehler stehen
                    if self.pause_wait():
                        break
                    deadline = time.monotonic()      # nach der Pause nicht aufholen
                t0 = cpu.time
                if cpu.disp is None and cpu.display_on:
                    cpu.show_default()           # ohne DISP: ausgefuehrte Adresse + Befehl
                try:
                    ok = cpu.step()
                except HaltRequest:
                    self.after_halt = True
                    break
                if cpu.error:
                    self.msg, cpu.error = cpu.error, None
                    break
                if not ok:
                    break
                if self.delay:               # Soll-Dauer = Originalzeit / Faktor
                    deadline += (cpu.time - t0) / 10000 / self.delay
                    if deadline - time.monotonic() < -0.1:   # Rechner hinkt hinterher: nicht aufholen
                        deadline = time.monotonic()
                    stop = False
                    while True:              # in Scheiben warten: Tasten bleiben bedienbar
                        wait = deadline - time.monotonic()
                        if wait <= 0:
                            break
                        time.sleep(min(wait, 0.05))
                        if self.poll_keys():
                            stop = True
                            break
                        self.render(force=False)
                    if stop:
                        break
                else:
                    deadline = time.monotonic()
                self.render(force=False)
        finally:
            self.running = False
            self.paused = False
            cpu.live = False
            self.keypad.keys.clear()         # Keypad und Taster sind nur im Lauf aktiv
            self.keypad.buttons.clear()
            self.override = None
            cpu.halted = False
            cpu.idle(True)                   # Leerlauf: Zaehler wieder in Echtzeit
            self.interrupt_display()         # Programmunterbrechung: Adresse + Befehl (ausser Debug)

    def pause_wait(self):
        """Pause im Lauf: warten, bis die Leertaste weiterlaufen laesst. True = Lauf beenden."""
        self.render()
        while self.paused:
            if self.poll_keys():
                return True
            time.sleep(0.03)
            self.render(force=False)
        self.render()
        return False

    def kin_key(self):
        cpu = self.cpu
        was_live = cpu.live
        cpu.live = False                     # waehrend KIN wartet laufen die Signale in Echtzeit
        was_idle = cpu._idle_t is not None
        cpu.idle(True)                       # ... und der RND-Zaehler (205/s)
        self.msg = "KIN wartet - Hex-Taste druecken (h = abbrechen)"
        try:
            self.render()
            while True:
                k = self.kb.read(0.1 if cpu.has_signals() else None)
                if k is None:                # Zeitablauf: Signalanzeige auffrischen
                    self.render()
                    continue
                if self.common_key(k):
                    if self.abort_run:
                        raise HaltRequest
                    self.render()
                    continue
                if k == "HALT":
                    self.msg = ""
                    raise HaltRequest
                if isinstance(k, int):
                    self.msg = ""
                    return k
        finally:
            cpu.live = was_live
            if not was_idle:
                cpu.idle(False)

    def loop(self):
        screen_start()
        self.render()
        try:
            while True:
                # PGM 3 ohne eingetippte Ziffern: Uhr live anzeigen -> regelmaessig neu zeichnen
                timeout = 0.25 if self.mode == "TIMESET" and not self.buf else None
                if self.cpu.has_signals():          # Signale laufen auch ohne Programm
                    timeout = 0.1
                k = self.kb.read(timeout)
                if k is not None:
                    self.handle(k)
                self.render()
        except QuitRequest:
            pass
        finally:
            screen_stop()


def main():
    try:
        with Keyboard() as kb:
            Emulator(kb).loop()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
