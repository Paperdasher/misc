"""
config.py — Camera Acquisition Configuration GUI
=================================================
Usage
-----
    python config.py
    python config.py -c config.yaml
    python config.py --dir /path/to/dir

Tabs
----
  🧪 Experiment  — experimenter, animal, co-recordings, notes
  📷 Cameras     — per-camera serial/exposure/gain
  🏠 Chambers    — chamber→camera mapping, timer, preview downsample
  🔌 TTL Map     — global analog-pin → chamber → action table
  🎬 Recording   — FPS, JPEG quality, ROI, auto-start toggle
  ⚡ Trigger/CSV — camera GPIO (leave off unless TTL wired to GPIO),
                   per-frame CSV column flags
"""

# ---------------------------------------------------------------------------
# Standard library
# ---------------------------------------------------------------------------
import sys
import os
import argparse
import glob
from datetime import date

# ---------------------------------------------------------------------------
# Third-party
# ---------------------------------------------------------------------------
import yaml   # reads and writes config.yaml

# ---------------------------------------------------------------------------
# Qt / PyQt5
# ---------------------------------------------------------------------------
# QApplication + QMainWindow: the application object and top-level window.
# QWidget / QTabWidget: base widget and the tabbed container.
# Layout managers: QVBoxLayout (vertical stack), QHBoxLayout (horizontal row),
#   QGridLayout (row/column grid).
# Input widgets: QLabel, QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox,
#   QComboBox, QTextEdit.
# Action widgets: QPushButton, QToolButton (compact icon button).
# Dialog widgets: QFileDialog, QMessageBox.
# Container widgets: QGroupBox (labelled border box), QScrollArea (scrollable region),
#   QFrame (plain container, used to suppress ScrollArea border).
# QStatusBar: the thin bar along the bottom of the main window.
# Qt (namespace): alignment/flag constants.
# QColor / QPalette: used to apply the dark Fusion palette.
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox,
    QComboBox, QPushButton, QFileDialog, QTextEdit,
    QGroupBox, QScrollArea, QFrame, QMessageBox,
    QToolButton, QStatusBar,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPalette

# ---------------------------------------------------------------------------
# Colour palette — all colours defined once here
# ---------------------------------------------------------------------------
# Using a consistent palette ensures any future reskin only needs changes here.
DARK_BG  = "#1a1d23"   # main window background
PANEL_BG = "#22262f"   # header bar and tab panel background
CARD_BG  = "#2b303b"   # group box card background
ACCENT   = "#4f8ef7"   # selected tab, focused input border, title text
ACCENT2  = "#5ecfa8"   # success / save button
WARN     = "#f7934f"   # warning status messages
TEXT_PRI = "#e8eaf0"   # primary text
TEXT_SEC = "#8a90a0"   # secondary / label text
BORDER   = "#363c4a"   # widget borders and separators
INPUT_BG = "#1e222a"   # text input background
BTN_BG   = "#333844"   # normal button background
BTN_HOV  = "#404759"   # button hover state

# ---------------------------------------------------------------------------
# Global stylesheet (Qt CSS)
# ---------------------------------------------------------------------------
# Applied to QApplication so every widget inherits it unless overridden.
# f-string lets us embed the palette constants directly.
SS = f"""
QMainWindow, QWidget {{
    background-color: {DARK_BG}; color: {TEXT_PRI};
    font-family: 'JetBrains Mono','Cascadia Code','Consolas',monospace;
    font-size: 13px;
}}
QTabWidget::pane {{ border:1px solid {BORDER}; background:{PANEL_BG}; border-radius:4px; }}
QTabBar::tab {{
    background:{CARD_BG}; color:{TEXT_SEC}; padding:8px 18px;
    border:1px solid {BORDER}; border-bottom:none;
    border-top-left-radius:4px; border-top-right-radius:4px;
    margin-right:2px; font-size:12px;
}}
QTabBar::tab:selected {{ background:{ACCENT}; color:white; font-weight:bold; }}
QTabBar::tab:hover:!selected {{ background:{BTN_HOV}; color:{TEXT_PRI}; }}
QGroupBox {{
    border:1px solid {BORDER}; border-radius:6px;
    margin-top:14px; padding-top:10px;
    background:{CARD_BG}; font-size:11px; color:{TEXT_SEC};
    letter-spacing:1px;
}}
QGroupBox::title {{ subcontrol-origin:margin; subcontrol-position:top left;
    padding:0 8px; left:12px; top:-2px; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit {{
    background:{INPUT_BG}; color:{TEXT_PRI}; border:1px solid {BORDER};
    border-radius:4px; padding:5px 8px; selection-background-color:{ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QTextEdit:focus {{
    border:1px solid {ACCENT};
}}
QComboBox::drop-down {{ border:none; width:24px; }}
QComboBox QAbstractItemView {{
    background:{CARD_BG}; color:{TEXT_PRI};
    border:1px solid {BORDER}; selection-background-color:{ACCENT};
}}
QPushButton {{
    background:{BTN_BG}; color:{TEXT_PRI}; border:1px solid {BORDER};
    border-radius:4px; padding:7px 18px; font-size:12px;
}}
QPushButton:hover {{ background:{BTN_HOV}; border-color:{ACCENT}; }}
QPushButton:pressed {{ background:{ACCENT}; color:white; }}
QPushButton#success {{ background:{ACCENT2}; color:#111; border:none; font-weight:bold; }}
QPushButton#success:hover {{ background:#7de0ba; }}
QPushButton#danger  {{ background:#c04040; color:white; border:none; font-weight:bold; }}
QPushButton#danger:hover  {{ background:#d06060; }}
QCheckBox {{ color:{TEXT_PRI}; spacing:8px; }}
QCheckBox::indicator {{
    width:16px; height:16px; border:1px solid {BORDER};
    border-radius:3px; background:{INPUT_BG};
}}
QCheckBox::indicator:checked {{ background:{ACCENT}; border-color:{ACCENT}; }}
QScrollArea {{ border:none; background:transparent; }}
QScrollBar:vertical {{ background:{DARK_BG}; width:8px; border-radius:4px; }}
QScrollBar::handle:vertical {{ background:{BORDER}; border-radius:4px; min-height:20px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QLabel#lbl  {{ color:{TEXT_SEC}; font-size:12px; }}
QLabel#note {{ color:{TEXT_SEC}; font-size:11px; }}
QStatusBar {{
    background:{CARD_BG}; color:{TEXT_SEC};
    border-top:1px solid {BORDER}; font-size:11px;
}}
QToolButton {{
    background:{BTN_BG}; border:1px solid {BORDER};
    border-radius:4px; padding:4px 8px; color:{TEXT_SEC};
}}
QToolButton:hover {{ background:{BTN_HOV}; color:{TEXT_PRI}; }}
"""

# ---------------------------------------------------------------------------
# Convenience widget constructors
# ---------------------------------------------------------------------------

def lbl(t):
    """Secondary-coloured label for use next to input widgets."""
    w = QLabel(t)
    w.setObjectName("lbl")
    return w

def note(t):
    """Small secondary-coloured multi-line explanatory text."""
    w = QLabel(t)
    w.setObjectName("note")
    w.setWordWrap(True)
    return w

def scrollable(w):
    """
    Wrap a widget in a QScrollArea so it gains a vertical scrollbar when
    its content is taller than the available space.
    NoFrame removes the default border that QScrollArea adds.
    """
    sa = QScrollArea()
    sa.setWidget(w)
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    return sa


# ---------------------------------------------------------------------------
# Tab: 🧪 Experiment metadata
# ---------------------------------------------------------------------------

class MetadataTab(QWidget):
    """
    Collects session-level metadata that is written into each chamber's
    _session.csv file after recording stops.  None of these fields affect
    camera behaviour.
    """
    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)

        # --- Session identity card ---
        id_box = QGroupBox("Session Identity")
        g = QGridLayout(id_box)
        g.setSpacing(10)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        self.experimenter    = QLineEdit(); self.experimenter.setPlaceholderText("e.g. Harley")
        self.experiment_name = QLineEdit(); self.experiment_name.setPlaceholderText("e.g. Cohort1")
        self.session_date    = QLineEdit(date.today().strftime("%Y-%m-%d"))
        self.schedule_name   = QLineEdit(); self.schedule_name.setPlaceholderText("e.g. FR5")
        self.pc_name         = QLineEdit()
        self.pc_name.setPlaceholderText("leave blank to use system hostname automatically")
        g.addWidget(lbl("Experimenter"),     0, 0); g.addWidget(self.experimenter,    0, 1)
        g.addWidget(lbl("Experiment Name"),  0, 2); g.addWidget(self.experiment_name, 0, 3)
        g.addWidget(lbl("Date"),             1, 0); g.addWidget(self.session_date,    1, 1)
        g.addWidget(lbl("Schedule Name"),    1, 2); g.addWidget(self.schedule_name,   1, 3)
        g.addWidget(lbl("PC / Station Name"),2, 0); g.addWidget(self.pc_name,         2, 1, 1, 3)
        g.addWidget(note(
            "Optional — overrides the auto-detected hostname in session CSV. "
            "Useful when multiple PCs run the same script and you need to "
            "identify which machine recorded which file."
        ), 3, 0, 1, 4)
        layout.addWidget(id_box)

        # --- Animal card ---
        an_box = QGroupBox("Animal")
        g2 = QGridLayout(an_box)
        g2.setSpacing(10)
        g2.setColumnStretch(1, 1); g2.setColumnStretch(3, 1)
        self.animal_id = QLineEdit(); self.animal_id.setPlaceholderText("e.g. M123")
        self.genotype  = QLineEdit(); self.genotype.setPlaceholderText("e.g. WT")
        self.group     = QLineEdit(); self.group.setPlaceholderText("e.g. control")
        g2.addWidget(lbl("Animal ID"), 0, 0); g2.addWidget(self.animal_id, 0, 1)
        g2.addWidget(lbl("Genotype"),  0, 2); g2.addWidget(self.genotype,  0, 3)
        g2.addWidget(lbl("Group"),     1, 0); g2.addWidget(self.group,     1, 1)
        layout.addWidget(an_box)

        # --- Co-recordings card (EEG / fiber photometry path) ---
        co_box = QGroupBox("Co-recordings")
        co_lay = QHBoxLayout(co_box)
        self.eeg_path = QLineEdit()
        self.eeg_path.setPlaceholderText("EEG / fiber path (optional)")
        eb = QToolButton(); eb.setText("…")
        eb.clicked.connect(lambda: self._browse(self.eeg_path))
        co_lay.addWidget(lbl("EEG/Fiber"))
        co_lay.addWidget(self.eeg_path, 1)
        co_lay.addWidget(eb)
        layout.addWidget(co_box)

        # --- Free-text notes ---
        n_box = QGroupBox("Notes")
        nl = QVBoxLayout(n_box)
        self.notes = QTextEdit()
        self.notes.setFixedHeight(80)
        self.notes.setPlaceholderText("Session notes…")
        nl.addWidget(self.notes)
        layout.addWidget(n_box)

        layout.addStretch()   # push cards to the top
        outer.addWidget(scrollable(content))

    def _browse(self, t):
        """Open a file picker and put the selected path into text field t."""
        p, _ = QFileDialog.getOpenFileName(self, "Select File", "", "All (*)")
        if p:
            t.setText(p)

    def load(self, m: dict):
        """Populate all fields from a dict (the experiment_metadata block)."""
        self.experimenter.setText(m.get("experimenter_name", ""))
        self.experiment_name.setText(m.get("experiment_name", ""))
        self.animal_id.setText(m.get("animal_id", ""))
        self.genotype.setText(m.get("genotype", ""))
        self.group.setText(m.get("group", ""))
        self.schedule_name.setText(m.get("schedule_name", ""))
        self.pc_name.setText(m.get("pc_name", ""))
        self.eeg_path.setText(m.get("eeg_fiber_photometry_path", ""))
        self.notes.setPlainText(m.get("notes", ""))

    def dump(self) -> dict:
        """Return all fields as a dict suitable for the experiment_metadata YAML block."""
        return {
            "experimenter_name":         self.experimenter.text().strip(),
            "experiment_name":           self.experiment_name.text().strip(),
            "animal_id":                 self.animal_id.text().strip(),
            "genotype":                  self.genotype.text().strip(),
            "group":                     self.group.text().strip(),
            "schedule_name":             self.schedule_name.text().strip(),
            "pc_name":                   self.pc_name.text().strip(),
            "eeg_fiber_photometry_path": self.eeg_path.text().strip(),
            "notes":                     self.notes.toPlainText().strip(),
        }


# ---------------------------------------------------------------------------
# Tab: 📷 Cameras
# ---------------------------------------------------------------------------

class SingleCameraWidget(QGroupBox):
    """
    Card for one camera entry.  The group box title is the camera key
    (e.g. "cam0").  Each field maps directly to a key in the cameras block
    of config.yaml.
    """
    def __init__(self, key, cfg):
        super().__init__(key)
        g = QGridLayout(self)
        g.setSpacing(10)
        g.setColumnStretch(1, 1); g.setColumnStretch(3, 1)

        self.serial      = QLineEdit(str(cfg.get("serial", "")))
        self.name_e      = QLineEdit(str(cfg.get("name", key)))
        self.enabled     = QCheckBox("Enabled")
        self.enabled.setChecked(cfg.get("enabled", True))

        # Exposure: 1–15000 µs (capped by _configure_camera on the acquisition side).
        self.exposure    = QSpinBox()
        self.exposure.setRange(1, 15000); self.exposure.setSuffix(" µs")
        self.exposure.setValue(cfg.get("exposure_us", 14000))

        # Gain: 0–48 dB analogue gain on the sensor.
        self.gain        = QDoubleSpinBox()
        self.gain.setRange(0, 48); self.gain.setSuffix(" dB")
        self.gain.setValue(cfg.get("gain_db", 10))

        # Black level: pedestal added to the raw pixel values (keeps 0 from
        # being ambiguous with "no signal").
        self.black_level = QDoubleSpinBox()
        self.black_level.setRange(0, 32); self.black_level.setDecimals(1)
        self.black_level.setValue(cfg.get("black_level", 2.0))

        # Throughput limit: caps USB/GigE bandwidth per camera to prevent
        # packet collisions when multiple cameras share the same host controller.
        self.throughput  = QSpinBox()
        self.throughput.setRange(1_000_000, 300_000_000)
        self.throughput.setSingleStep(1_000_000); self.throughput.setSuffix(" bps")
        self.throughput.setValue(cfg.get("throughput_limit", 90_000_000))

        g.addWidget(lbl("Serial"),           0, 0); g.addWidget(self.serial,      0, 1)
        g.addWidget(lbl("Friendly Name"),    0, 2); g.addWidget(self.name_e,      0, 3)
        g.addWidget(self.enabled,            1, 0, 1, 2)
        g.addWidget(lbl("Exposure"),         2, 0); g.addWidget(self.exposure,    2, 1)
        g.addWidget(lbl("Gain"),             2, 2); g.addWidget(self.gain,        2, 3)
        g.addWidget(lbl("Black Level"),      3, 0); g.addWidget(self.black_level, 3, 1)
        g.addWidget(lbl("Throughput Limit"), 3, 2); g.addWidget(self.throughput,  3, 3)

    def dump(self) -> dict:
        return {
            "serial":           self.serial.text().strip(),
            "name":             self.name_e.text().strip(),
            "enabled":          self.enabled.isChecked(),
            "exposure_us":      self.exposure.value(),
            "gain_db":          self.gain.value(),
            "black_level":      self.black_level.value(),
            "throughput_limit": self.throughput.value(),
        }


class CamerasTab(QWidget):
    """
    Hosts one SingleCameraWidget per camera defined in config.
    The tab is populated by load() when a config file is opened.
    camera_keys() is used by ChambersTab and TTLMapTab to populate
    their camera dropdowns.
    """
    def __init__(self):
        super().__init__()
        self._cards = {}   # camera_key → SingleCameraWidget
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        self._cw = QWidget(); self._cl = QVBoxLayout(self._cw)
        self._cl.setSpacing(12); self._cl.setContentsMargins(20, 20, 20, 20)
        self._cl.addStretch()
        outer.addWidget(scrollable(self._cw))

    def load(self, cfg: dict):
        # Remove any existing cards before loading new ones.
        for c in self._cards.values():
            self._cl.removeWidget(c); c.deleteLater()
        self._cards.clear()
        for key, ccfg in cfg.items():
            card = SingleCameraWidget(key, ccfg)
            self._cards[key] = card
            # Insert before the stretch at the end.
            self._cl.insertWidget(self._cl.count() - 1, card)

    def dump(self) -> dict:
        return {k: c.dump() for k, c in self._cards.items()}

    def camera_keys(self) -> list:
        """Return the list of camera keys currently defined."""
        return list(self._cards.keys())


# ---------------------------------------------------------------------------
# Arduino connection widget (shared between ChambersTab and TTLMapTab)
# ---------------------------------------------------------------------------

class ArduinoPortWidget(QGroupBox):
    """
    UI card for the serial connection to one Arduino:
      - Port (COM3 / /dev/ttyUSB0)
      - Baud rate (must match BAUD_RATE in chamber_ttl_analog.ino)
      - Board voltage (informational — must also match BOARD_VREF in the sketch)

    The "Scan ports" button enumerates available serial ports using pyserial's
    list_ports tool and auto-fills the port field with the first result.

    This widget does NOT define which pin does what — that is the TTL Map tab's
    job.  It only stores where to find the Arduino on the serial bus.
    """
    BAUDS = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]

    def __init__(self, cfg: dict, parent=None):
        super().__init__("Arduino Connection", parent)
        g = QGridLayout(self); g.setSpacing(8)
        g.setColumnStretch(1, 1); g.setColumnStretch(3, 1)

        self.port = QLineEdit(cfg.get("port", "COM3"))
        self.port.setPlaceholderText("e.g. COM3 or /dev/ttyUSB0")
        self.scan_btn = QPushButton("Scan ports")
        self.scan_btn.setFixedWidth(90)
        self.scan_btn.clicked.connect(self._scan)
        self._scan_lbl = QLabel("")
        self._scan_lbl.setObjectName("note")

        # Port field and scan button on the same row.
        port_row = QWidget(); pr = QHBoxLayout(port_row); pr.setContentsMargins(0, 0, 0, 0)
        pr.addWidget(self.port, 1); pr.addWidget(self.scan_btn)

        self.baud = QComboBox()
        for b in self.BAUDS: self.baud.addItem(str(b), b)
        bi = self.baud.findText(str(cfg.get("baud", 115200)))
        if bi >= 0: self.baud.setCurrentIndex(bi)

        self.vref = QComboBox()
        self.vref.addItem("5.0 V  (Uno / Mega / Nano)", 5.0)
        self.vref.addItem("3.3 V  (Due / Zero / MKR)",  3.3)
        vref_val = cfg.get("vref", 5.0)
        for i in range(self.vref.count()):
            if abs(self.vref.itemData(i) - vref_val) < 0.01:
                self.vref.setCurrentIndex(i); break

        g.addWidget(lbl("Port"),          0, 0); g.addWidget(port_row, 0, 1, 1, 3)
        g.addWidget(self._scan_lbl,       1, 0, 1, 4)
        g.addWidget(lbl("Baud rate"),     2, 0); g.addWidget(self.baud, 2, 1)
        g.addWidget(lbl("Board voltage"), 2, 2); g.addWidget(self.vref, 2, 3)
        g.addWidget(note(
            "Board voltage is informational — it must also match BOARD_VREF "
            "set inside chamber_ttl_analog.ino. "
            "Which pins trigger which actions is configured in the TTL Map tab."
        ), 3, 0, 1, 4)

    def _scan(self):
        """Use pyserial to list available serial ports and display them."""
        try:
            import serial.tools.list_ports as lp
            ports = list(lp.comports())
            if ports:
                self._scan_lbl.setText(
                    "  " + "   ".join(f"{p.device} ({p.description[:28]})"
                                      for p in sorted(ports))
                )
                # Auto-fill the port field if it's still at the default value.
                if self.port.text() in ("COM3", "COM1", ""):
                    self.port.setText(sorted(ports)[0].device)
            else:
                self._scan_lbl.setText("  No serial ports found.")
        except ImportError:
            self._scan_lbl.setText("  pip install pyserial")

    def load(self, cfg: dict):
        self.port.setText(cfg.get("port", "COM3"))
        bi = self.baud.findText(str(cfg.get("baud", 115200)))
        if bi >= 0: self.baud.setCurrentIndex(bi)
        vref_val = cfg.get("vref", 5.0)
        for i in range(self.vref.count()):
            if abs(self.vref.itemData(i) - vref_val) < 0.01:
                self.vref.setCurrentIndex(i); break

    def dump(self) -> dict:
        return {
            "port": self.port.text().strip(),
            "baud": int(self.baud.currentText()),
            "vref": self.vref.currentData(),
        }


# ---------------------------------------------------------------------------
# Tab: 🔌 TTL Map
# ---------------------------------------------------------------------------

class TTLMapRow(QWidget):
    """
    One row in the TTL map table.  Represents one analog pin on the Arduino.
    The row's position in the list determines the TTL channel number:
      row 0 → TTL 1, row 1 → TTL 2, etc.

    Each row specifies:
      Pin        — which Arduino analog pin (A0–A15) this row represents
      Label      — a human-readable name logged in the CSV (e.g. "box1_start")
      Chamber    — which chamber this pin controls (dropdown of defined chambers)
      Action     — what to do when the pin goes high:
                     start_recording, stop_recording, or log_event
      Port override — if this pin's Arduino is on a different port than the
                      global default, enter it here; blank = use global
    """

    ACTIONS  = ["start_recording", "stop_recording", "log_event"]
    PIN_OPTS = [f"A{i}" for i in range(16)] + [f"D{i}" for i in range(54)]

    def __init__(self, cfg: dict, chamber_keys: list, row_num: int):
        super().__init__()
        self._row_num = row_num
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2); lay.setSpacing(6)

        # TTL number label (read-only, derived from position in the list).
        ttl_lbl = QLabel(f"TTL {row_num}")
        ttl_lbl.setFixedWidth(46)
        ttl_lbl.setStyleSheet("color:#e8a020; font-weight:bold; font-size:11px;")

        self.pin = QComboBox(); self.pin.setFixedWidth(68)
        for p in self.PIN_OPTS: self.pin.addItem(p)
        pin_idx = self.pin.findText(cfg.get("pin", f"A{row_num - 1}"))
        if pin_idx >= 0: self.pin.setCurrentIndex(pin_idx)

        self.ttl_label = QLineEdit(cfg.get("ttl_label", f"ttl{row_num}"))
        self.ttl_label.setPlaceholderText("label")
        self.ttl_label.setFixedWidth(130)

        # Chamber dropdown: blank = unassigned.
        self.chamber = QComboBox(); self.chamber.setFixedWidth(120)
        self._populate_chambers(chamber_keys)
        ci = self.chamber.findText(cfg.get("chamber", ""))
        if ci >= 0: self.chamber.setCurrentIndex(ci)

        self.action = QComboBox(); self.action.setFixedWidth(148)
        for a in self.ACTIONS: self.action.addItem(a)
        ai = self.action.findText(cfg.get("action", "log_event"))
        if ai >= 0: self.action.setCurrentIndex(ai)

        # Port override: if blank, the global default port is used.
        self.port_override = QLineEdit(cfg.get("arduino", {}).get("port", ""))
        self.port_override.setPlaceholderText("port override (blank = global)")
        self.port_override.setFixedWidth(140)

        lay.addWidget(ttl_lbl)
        lay.addWidget(lbl("pin"));     lay.addWidget(self.pin)
        lay.addWidget(lbl("label"));   lay.addWidget(self.ttl_label)
        lay.addWidget(lbl("chamber")); lay.addWidget(self.chamber)
        lay.addWidget(lbl("action"));  lay.addWidget(self.action)
        lay.addWidget(lbl("port"));    lay.addWidget(self.port_override)
        lay.addStretch()

    def _populate_chambers(self, chamber_keys: list):
        """Rebuild the chamber dropdown with a blank first entry."""
        self.chamber.clear()
        self.chamber.addItem("")   # blank = no assignment
        for ck in chamber_keys:
            self.chamber.addItem(ck)

    def update_chambers(self, chamber_keys: list):
        """
        Called when the Chambers tab changes so the dropdown stays in sync.
        Preserves the currently selected chamber if it still exists.
        """
        current = self.chamber.currentText()
        self._populate_chambers(chamber_keys)
        ci = self.chamber.findText(current)
        if ci >= 0:
            self.chamber.setCurrentIndex(ci)

    def dump(self) -> dict:
        d = {
            "pin":       self.pin.currentText(),
            "ttl_label": self.ttl_label.text().strip(),
            "chamber":   self.chamber.currentText(),
            "action":    self.action.currentText(),
        }
        # Only write the per-row arduino block if a port override is set.
        port_ov = self.port_override.text().strip()
        if port_ov:
            d["arduino"] = {"port": port_ov}
        return d


class TTLMapTab(QWidget):
    """
    Manages a dynamic ordered list of TTLMapRows.

    The list ORDER is what determines channel numbers — row 1 = TTL 1.
    This must match the order of pins in ANALOG_PINS[] in chamber_ttl_analog.ino.

    The tab also manages a "Global Arduino Defaults" section that provides
    port, baud, and board voltage values for any row that does not have
    its own per-row port override.
    """

    def __init__(self, chambers_tab):
        super().__init__()
        self._chambers_tab = chambers_tab   # ChambersTab reference for chamber_keys()
        self._rows: list = []

        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)

        # --- Global Arduino defaults ---
        ard_box = QGroupBox("Global Arduino Defaults")
        ard_lay = QGridLayout(ard_box); ard_lay.setSpacing(8)
        ard_lay.setColumnStretch(1, 1); ard_lay.setColumnStretch(3, 1)

        self.global_port = QLineEdit("COM3")
        self.global_port.setPlaceholderText("default port for all entries")
        self.global_baud = QComboBox()
        for b in [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]:
            self.global_baud.addItem(str(b), b)
        self.global_baud.setCurrentText("115200")

        self.scan_global = QPushButton("Scan ports"); self.scan_global.setFixedWidth(90)
        self.scan_global.clicked.connect(self._scan_global)
        self._scan_lbl = QLabel(""); self._scan_lbl.setObjectName("note")

        port_row = QWidget(); pr = QHBoxLayout(port_row); pr.setContentsMargins(0, 0, 0, 0)
        pr.addWidget(self.global_port, 1); pr.addWidget(self.scan_global)

        self.global_vref = QComboBox()
        self.global_vref.addItem("5.0 V  (Uno / Mega / Nano)", 5.0)
        self.global_vref.addItem("3.3 V  (Due / Zero / MKR)",  3.3)

        ard_lay.addWidget(lbl("Default port"),  0, 0); ard_lay.addWidget(port_row,         0, 1, 1, 3)
        ard_lay.addWidget(self._scan_lbl,       1, 0, 1, 4)
        ard_lay.addWidget(lbl("Default baud"),  2, 0); ard_lay.addWidget(self.global_baud, 2, 1)
        ard_lay.addWidget(lbl("Board voltage"), 2, 2); ard_lay.addWidget(self.global_vref, 2, 3)
        ard_lay.addWidget(note(
            "These apply to all TTL map rows that don't have a per-row port override. "
            "Board voltage must also match BOARD_VREF in chamber_ttl_analog.ino."
        ), 3, 0, 1, 4)
        outer.addWidget(ard_box)

        # --- Add / remove row toolbar ---
        tb = QWidget(); tl = QHBoxLayout(tb); tl.setContentsMargins(12, 6, 12, 2)
        add_btn = QPushButton("+ Add TTL row")
        add_btn.clicked.connect(lambda: self._add_row({}))
        rem_btn = QPushButton("− Remove last row")
        rem_btn.clicked.connect(self._remove_last_row)
        tl.addWidget(add_btn); tl.addWidget(rem_btn); tl.addStretch()
        tl.addWidget(note("Row order = TTL channel number (row 1 = TTL 1)"))
        outer.addWidget(tb)

        # --- Column header row (purely visual) ---
        hdr = QWidget()
        hl  = QHBoxLayout(hdr); hl.setContentsMargins(12, 0, 12, 0); hl.setSpacing(6)
        for txt, w in [("TTL#", 46), ("Pin", 68+28), ("Label", 130+40),
                       ("Chamber", 120+55), ("Action", 148+46), ("Port override", 140+36)]:
            lb = QLabel(txt); lb.setFixedWidth(w)
            lb.setStyleSheet("color:#556060; font-size:10px;")
            hl.addWidget(lb)
        hl.addStretch()
        outer.addWidget(hdr)

        # --- Scrollable row container ---
        self._row_widget = QWidget()
        self._row_layout = QVBoxLayout(self._row_widget)
        self._row_layout.setSpacing(2); self._row_layout.setContentsMargins(12, 4, 12, 12)
        self._row_layout.addStretch()   # keeps rows pinned to the top
        outer.addWidget(scrollable(self._row_widget))

    def _scan_global(self):
        """Enumerate serial ports and fill the global port field."""
        try:
            import serial.tools.list_ports as lp
            ports = list(lp.comports())
            if ports:
                self._scan_lbl.setText(
                    "  " + "   ".join(f"{p.device}" for p in sorted(ports)))
                if self.global_port.text() in ("COM3", "COM1", ""):
                    self.global_port.setText(sorted(ports)[0].device)
            else:
                self._scan_lbl.setText("  No ports found.")
        except ImportError:
            self._scan_lbl.setText("  pip install pyserial")

    def _chamber_keys(self) -> list:
        """
        Ask the Chambers tab for the current list of chamber keys.
        This is called each time a new row is added or chambers are updated,
        so the dropdowns always reflect the latest chamber definitions.
        """
        return self._chambers_tab.chamber_keys()

    def _add_row(self, cfg: dict):
        """Append a new TTLMapRow to the list."""
        row_num = len(self._rows) + 1
        row     = TTLMapRow(cfg, self._chamber_keys(), row_num)
        self._rows.append(row)
        # Insert before the stretch item at the end of the layout.
        self._row_layout.insertWidget(self._row_layout.count() - 1, row)

    def _remove_last_row(self):
        """Remove the last row (highest TTL number)."""
        if not self._rows:
            return
        row = self._rows.pop()
        self._row_layout.removeWidget(row)
        row.deleteLater()

    def refresh_chambers(self):
        """
        Propagate chamber key changes to all existing rows.
        Should be called whenever the Chambers tab adds or removes a chamber.
        """
        keys = self._chamber_keys()
        for row in self._rows:
            row.update_chambers(keys)

    def load(self, ttl_map: list, arduino_cfg: dict):
        """Replace all rows with entries from ttl_map and set global Arduino defaults."""
        for row in self._rows:
            self._row_layout.removeWidget(row)
            row.deleteLater()
        self._rows.clear()

        # Populate global Arduino defaults from the top-level "arduino" block.
        self.global_port.setText(arduino_cfg.get("port", "COM3"))
        bi = self.global_baud.findText(str(arduino_cfg.get("baud", 115200)))
        if bi >= 0: self.global_baud.setCurrentIndex(bi)
        vref_val = arduino_cfg.get("vref", 5.0)
        for i in range(self.global_vref.count()):
            if abs(self.global_vref.itemData(i) - vref_val) < 0.01:
                self.global_vref.setCurrentIndex(i); break

        # Add one row per ttl_map entry.
        for entry in ttl_map:
            self._add_row(entry)

    def dump(self) -> tuple:
        """
        Return (ttl_map list, arduino global dict) for serialisation to YAML.
        ttl_map is a list of dicts, one per row, in order.
        """
        arduino_cfg = {
            "port": self.global_port.text().strip(),
            "baud": int(self.global_baud.currentText()),
            "vref": self.global_vref.currentData(),
        }
        ttl_map = [row.dump() for row in self._rows]
        return ttl_map, arduino_cfg


# ---------------------------------------------------------------------------
# Tab: 🏠 Chambers
# ---------------------------------------------------------------------------

class SingleChamberWidget(QWidget):
    """
    Card for one chamber.  A chamber groups a camera + recording settings
    + Arduino connection into a named unit (e.g. "chamber_A").

    Fields:
      camera_combo       — which camera key this chamber uses
      record_chk         — whether recording is enabled for this chamber
      timer_enabled      — auto-stop recording after duration_s seconds
      duration_spin      — duration for the auto-stop timer
      preview_downsample — show only every Nth frame in the preview window
                           (1 = every frame; raise to reduce CPU load on the UI)
      arduino_port       — Arduino connection details for this chamber
    """
    def __init__(self, chamber_key: str, cfg: dict, camera_keys: list):
        super().__init__()
        self._key = chamber_key
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(8)

        # --- Camera assignment and recording options ---
        top_box = QGroupBox(f"Chamber: {chamber_key}")
        top_g   = QGridLayout(top_box); top_g.setSpacing(10)
        top_g.setColumnStretch(1, 1); top_g.setColumnStretch(3, 1)

        self.camera_combo = QComboBox()
        for ck in camera_keys: self.camera_combo.addItem(ck)
        assigned = cfg.get("camera", "")
        idx = self.camera_combo.findText(assigned)
        if idx >= 0: self.camera_combo.setCurrentIndex(idx)

        self.record_chk = QCheckBox("Record this chamber")
        self.record_chk.setChecked(cfg.get("record", True))

        # Timer: auto-stop after duration_s seconds.
        self.timer_enabled = QCheckBox("Stop recording after")
        self.timer_enabled.setChecked(cfg.get("timer_enabled", False))
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 86400); self.duration_spin.setSuffix(" s")
        self.duration_spin.setValue(cfg.get("duration_s", 1800))
        timer_note = QLabel("seconds  (default 1800 = 30 min)")
        timer_note.setObjectName("note")

        # Preview downsample: only send every Nth frame to the preview window.
        # Useful on machines where rendering the full-rate preview tiles
        # competes with writing to disk.
        self.preview_downsample = QSpinBox()
        self.preview_downsample.setRange(1, 120)
        self.preview_downsample.setValue(cfg.get("preview_downsample", 1))
        self.preview_downsample.setFixedWidth(70)
        ds_note = QLabel("Display every Nth captured frame in the preview window  (1 = every frame)")
        ds_note.setObjectName("note")

        top_g.addWidget(lbl("Mapped camera"),      0, 0); top_g.addWidget(self.camera_combo,       0, 1)
        top_g.addWidget(self.record_chk,           0, 2, 1, 2)
        top_g.addWidget(self.timer_enabled,        1, 0); top_g.addWidget(self.duration_spin,      1, 1)
        top_g.addWidget(timer_note,                1, 2, 1, 2)
        top_g.addWidget(lbl("Preview downsample"), 2, 0); top_g.addWidget(self.preview_downsample, 2, 1)
        top_g.addWidget(ds_note,                   2, 2, 1, 2)
        layout.addWidget(top_box)

        # --- Arduino connection for this chamber ---
        # pin → action mapping is in the TTL Map tab, not here.
        ard_cfg = cfg.get("arduino", {})
        self.arduino_port = ArduinoPortWidget(ard_cfg)
        layout.addWidget(self.arduino_port)

    def dump(self) -> dict:
        return {
            "camera":             self.camera_combo.currentText(),
            "record":             self.record_chk.isChecked(),
            "timer_enabled":      self.timer_enabled.isChecked(),
            "duration_s":         self.duration_spin.value(),
            "preview_downsample": self.preview_downsample.value(),
            "arduino":            self.arduino_port.dump(),
        }


class ChambersTab(QWidget):
    """
    Hosts one SingleChamberWidget per chamber.
    Chambers can be added via the key field + "Add Chamber" button
    and removed via the per-card "Remove" button.

    chamber_keys() is called by TTLMapTab._chamber_keys() to populate
    the chamber dropdowns in TTL map rows.
    """
    def __init__(self, cameras_tab: CamerasTab):
        super().__init__()
        self._cameras_tab = cameras_tab
        self._cards: dict    = {}   # chamber_key → SingleChamberWidget
        self._wrappers: dict = {}   # chamber_key → wrapper QWidget (card + remove btn)

        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)

        # --- Toolbar for adding chambers ---
        tb = QWidget(); tl = QHBoxLayout(tb); tl.setContentsMargins(12, 8, 12, 4)
        self._new_key_edit = QLineEdit()
        self._new_key_edit.setPlaceholderText("New chamber key, e.g. chamber_B")
        self._new_key_edit.setFixedWidth(220)
        add_btn = QPushButton("+ Add Chamber")
        add_btn.clicked.connect(self._add_from_field)
        tl.addWidget(lbl("Key:")); tl.addWidget(self._new_key_edit)
        tl.addWidget(add_btn); tl.addStretch()
        outer.addWidget(tb)

        self._cw = QWidget(); self._cl = QVBoxLayout(self._cw)
        self._cl.setSpacing(16); self._cl.setContentsMargins(20, 8, 20, 20)
        self._cl.addStretch()
        outer.addWidget(scrollable(self._cw))

    def chamber_keys(self) -> list:
        """Return the list of chamber keys currently defined."""
        return list(self._cards.keys())

    def _camera_keys(self) -> list:
        return self._cameras_tab.camera_keys()

    def _add_from_field(self):
        """Read the key from the text field and add a new chamber card."""
        key = self._new_key_edit.text().strip()
        if key:
            self._new_key_edit.clear()
            self._add_chamber(key, {})

    def _add_chamber(self, key: str, cfg: dict):
        """Create a SingleChamberWidget for key and add it to the layout."""
        if not key or key in self._cards:
            return   # ignore empty keys and duplicates

        card    = SingleChamberWidget(key, cfg, self._camera_keys())
        wrapper = QWidget()
        wl      = QVBoxLayout(wrapper); wl.setContentsMargins(0, 0, 0, 4)

        # Remove button, right-aligned above the card.
        hdr  = QWidget(); hl = QHBoxLayout(hdr); hl.setContentsMargins(0, 0, 0, 0)
        rbtn = QPushButton(f"✕  Remove {key}")
        rbtn.setObjectName("danger"); rbtn.setFixedWidth(160)
        # Lambda captures key by value via default argument.
        rbtn.clicked.connect(lambda checked=False, k=key: self._remove_chamber(k))
        hl.addStretch(); hl.addWidget(rbtn)

        wl.addWidget(hdr); wl.addWidget(card)
        self._cards[key]    = card
        self._wrappers[key] = wrapper
        self._cl.insertWidget(self._cl.count() - 1, wrapper)

    def _remove_chamber(self, key: str):
        if key not in self._cards:
            return
        wrapper = self._wrappers.pop(key)
        self._cl.removeWidget(wrapper)
        wrapper.deleteLater()
        del self._cards[key]

    def load(self, chambers_cfg: dict):
        # Clear all existing chambers then load from config.
        for k in list(self._cards.keys()):
            self._remove_chamber(k)
        for key, cfg in chambers_cfg.items():
            self._add_chamber(key, cfg)

    def dump(self) -> dict:
        return {k: c.dump() for k, c in self._cards.items()}


# ---------------------------------------------------------------------------
# Tab: 🎬 Recording / ROI / Acquisition mode
# ---------------------------------------------------------------------------

class RecordingTab(QWidget):
    """
    Controls that affect what the acquisition script records:
      - Save directory
      - Frame rate and JPEG quality
      - ROI (region of interest) on the sensor
      - Auto-start flag (begin recording immediately on launch)
      - Preview window enabled flag

    Per-chamber timer and preview downsample are in the Chambers tab
    because they differ per chamber.
    """
    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        content = QWidget(); layout = QVBoxLayout(content)
        layout.setSpacing(16); layout.setContentsMargins(20, 20, 20, 20)

        # --- Acquisition mode ---
        mode_box = QGroupBox("Acquisition Mode")
        ml = QVBoxLayout(mode_box)
        self.auto_start = QCheckBox("Auto-start recording when script launches")
        self.auto_start.setChecked(False)
        self.preview_enabled = QCheckBox("Show live preview window during acquisition")
        self.preview_enabled.setChecked(True)
        ml.addWidget(self.auto_start)
        ml.addWidget(self.preview_enabled)
        ml.addWidget(note(
            "Auto-start ON  → recording begins immediately on script launch for all\n"
            "                 chambers that have 'Record this chamber' enabled.\n"
            "Auto-start OFF → cameras stream to preview only; recording starts per-chamber\n"
            "                 via its TTL start pulse or manual start in the stats popup.\n"
            "Per-chamber timer → configure inside each chamber card in the Chambers tab.\n"
            "Per-chamber preview downsample → also in each chamber card in the Chambers tab."
        ))
        layout.addWidget(mode_box)

        # --- Save directory ---
        path_box = QGroupBox("Save Location")
        pl = QHBoxLayout(path_box)
        self.save_dir = QLineEdit(); self.save_dir.setPlaceholderText("./recordings")
        bb = QToolButton(); bb.setText("…"); bb.clicked.connect(self._browse_dir)
        pl.addWidget(self.save_dir, 1); pl.addWidget(bb)
        layout.addWidget(path_box)

        # --- Video encoding settings ---
        rec_box = QGroupBox("Recording")
        rg = QGridLayout(rec_box); rg.setSpacing(10)
        rg.setColumnStretch(1, 1); rg.setColumnStretch(3, 1)
        self.fps = QDoubleSpinBox()
        self.fps.setRange(1, 500); self.fps.setDecimals(2); self.fps.setSuffix(" Hz")
        self.fps.setValue(59.99)
        self.jpeg_quality = QSpinBox()
        self.jpeg_quality.setRange(0, 100); self.jpeg_quality.setSuffix("%")
        self.jpeg_quality.setValue(90)
        rg.addWidget(lbl("Frame Rate"),   0, 0); rg.addWidget(self.fps,          0, 1)
        rg.addWidget(lbl("JPEG Quality"), 0, 2); rg.addWidget(self.jpeg_quality, 0, 3)
        layout.addWidget(rec_box)

        # --- Sensor ROI ---
        # Width/Height: crop the sensor to this many pixels (centred automatically).
        # Offset X/Y: manual offset if you don't want centred crop.
        roi_box = QGroupBox("ROI")
        roig = QGridLayout(roi_box); roig.setSpacing(10)
        roig.setColumnStretch(1, 1); roig.setColumnStretch(3, 1)
        self.roi_w  = QSpinBox(); self.roi_w.setRange(0, 8192);  self.roi_w.setSuffix(" px")
        self.roi_h  = QSpinBox(); self.roi_h.setRange(0, 8192);  self.roi_h.setSuffix(" px")
        self.roi_ox = QSpinBox(); self.roi_ox.setRange(0, 8192); self.roi_ox.setSuffix(" px")
        self.roi_oy = QSpinBox(); self.roi_oy.setRange(0, 8192); self.roi_oy.setSuffix(" px")
        roig.addWidget(lbl("Width"),    0, 0); roig.addWidget(self.roi_w,  0, 1)
        roig.addWidget(lbl("Height"),   0, 2); roig.addWidget(self.roi_h,  0, 3)
        roig.addWidget(lbl("Offset X"), 1, 0); roig.addWidget(self.roi_ox, 1, 1)
        roig.addWidget(lbl("Offset Y"), 1, 2); roig.addWidget(self.roi_oy, 1, 3)
        layout.addWidget(roi_box)

        layout.addStretch()
        outer.addWidget(scrollable(content))

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select Directory")
        if d: self.save_dir.setText(d)

    def load(self, config: dict):
        self.save_dir.setText(config.get("save_dir", "./recordings"))
        acq = config.get("acquisition", {})
        self.auto_start.setChecked(acq.get("auto_start", False))
        prev = config.get("preview", {})
        self.preview_enabled.setChecked(prev.get("enabled", True))
        rec = config.get("recording", {})
        self.fps.setValue(rec.get("fps", 59.99))
        self.jpeg_quality.setValue(rec.get("jpeg_quality", 90))
        roi = config.get("roi", {})
        self.roi_w.setValue(roi.get("width",  1020) or 0)
        self.roi_h.setValue(roi.get("height", 1020) or 0)
        self.roi_ox.setValue(roi.get("offset_x", 0))
        self.roi_oy.setValue(roi.get("offset_y", 0))

    def dump(self) -> dict:
        return {
            "save_dir":    self.save_dir.text().strip() or "./recordings",
            "acquisition": {"auto_start": self.auto_start.isChecked()},
            "preview":     {"enabled": self.preview_enabled.isChecked()},
            "recording":   {
                "fps":           self.fps.value(),
                "jpeg_quality":  self.jpeg_quality.value(),
                "split_size_mb": None,
            },
            "roi": {
                "width":    self.roi_w.value()  or None,   # None = full sensor
                "height":   self.roi_h.value()  or None,
                "offset_x": self.roi_ox.value(),
                "offset_y": self.roi_oy.value(),
            },
        }


# ---------------------------------------------------------------------------
# Tab: ⚡ Camera GPIO Trigger + CSV column flags
# ---------------------------------------------------------------------------

class TriggerTab(QWidget):
    """
    Two unrelated settings groups that are rarely changed:

    Camera GPIO Trigger
    -------------------
    The Spinnaker GPIO hardware trigger gates individual frame acquisition
    to an external electrical pulse on a camera GPIO pin.  This is SEPARATE
    from the Arduino TTL system — the Arduino TTL signals are read by Python
    over serial and start/stop recording at the software level.
    Leave this OFF unless you have a wire running from a signal source directly
    into the camera's GPIO port.

    Per-frame Metadata CSV Columns
    --------------------------------
    Controls which columns appear in the _timestamps.csv file.  All are enabled
    by default; disable any you don't need to save disk space.
    """
    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        content = QWidget(); layout = QVBoxLayout(content)
        layout.setSpacing(16); layout.setContentsMargins(20, 20, 20, 20)

        # --- Camera GPIO trigger ---
        note_box = QGroupBox("Camera GPIO Trigger")
        nl = QVBoxLayout(note_box)
        nl.addWidget(note(
            "If TTL is read by this script over serial (the default), leave "
            "'Enable hardware trigger' OFF.  The Spinnaker GPIO trigger is only "
            "needed if you are routing a physical TTL wire directly into the "
            "camera's GPIO line to gate individual frame acquisition."
        ))
        self.trigger_enabled    = QCheckBox("Enable camera hardware trigger (GPIO)")
        self.trigger_line       = QComboBox()
        self.trigger_line.addItems(["Line0", "Line1", "Line2", "Line3"])
        self.trigger_activation = QComboBox()
        self.trigger_activation.addItems(["RisingEdge", "FallingEdge", "AnyEdge", "LevelHigh", "LevelLow"])
        self.trigger_selector   = QComboBox()
        self.trigger_selector.addItems(["AcquisitionStart", "FrameStart", "FrameBurstStart"])
        self.trigger_timeout    = QSpinBox()
        self.trigger_timeout.setRange(100, 60000); self.trigger_timeout.setSuffix(" ms")
        self.trigger_timeout.setValue(5000)
        tg = QGridLayout(); tg.setSpacing(10)
        tg.setColumnStretch(1, 1); tg.setColumnStretch(3, 1)
        tg.addWidget(self.trigger_enabled,          0, 0, 1, 4)
        tg.addWidget(lbl("GPIO Line"),              1, 0); tg.addWidget(self.trigger_line,       1, 1)
        tg.addWidget(lbl("Activation"),             1, 2); tg.addWidget(self.trigger_activation, 1, 3)
        tg.addWidget(lbl("Trigger Selector"),       2, 0); tg.addWidget(self.trigger_selector,   2, 1)
        tg.addWidget(lbl("Timeout"),                2, 2); tg.addWidget(self.trigger_timeout,    2, 3)
        nl.addLayout(tg)
        layout.addWidget(note_box)

        # --- CSV column flags ---
        meta_box = QGroupBox("Per-frame Metadata CSV Columns")
        mg = QGridLayout(meta_box); mg.setSpacing(8)
        self.meta_enabled    = QCheckBox("Save per-frame timestamps CSV")
        self.meta_framecount = QCheckBox("Frame count")         # camera hardware frame counter
        self.meta_timestamp  = QCheckBox("Camera hardware timestamp")  # nanosecond-precision camera PTP clock
        self.meta_sestime    = QCheckBox("Session time")        # perf_counter seconds since session start
        self.meta_cputime    = QCheckBox("CPU wall clock")      # Python time.time() at capture
        for cb in [self.meta_enabled, self.meta_framecount, self.meta_timestamp,
                   self.meta_sestime, self.meta_cputime]:
            cb.setChecked(True)
        mg.addWidget(self.meta_enabled,    0, 0, 1, 2)
        mg.addWidget(self.meta_framecount, 1, 0); mg.addWidget(self.meta_timestamp, 1, 1)
        mg.addWidget(self.meta_sestime,    2, 0); mg.addWidget(self.meta_cputime,   2, 1)
        mg.addWidget(note(
            "CSV row_type: 'frame' = captured frame  |  "
            "'ttl_event' = TTL pulse with ttl_chamber / ttl_kind / ttl_label columns."
        ), 3, 0, 1, 2)
        layout.addWidget(meta_box)

        layout.addStretch()
        outer.addWidget(scrollable(content))

    def load(self, config: dict):
        t = config.get("trigger", {})
        self.trigger_enabled.setChecked(t.get("enabled", False))
        for combo, key, default in [
            (self.trigger_line,       "line",       "Line0"),
            (self.trigger_activation, "activation", "RisingEdge"),
            (self.trigger_selector,   "selector",   "AcquisitionStart"),
        ]:
            idx = combo.findText(t.get(key, default)); combo.setCurrentIndex(max(0, idx))
        self.trigger_timeout.setValue(t.get("timeout_ms", 5000))
        m = config.get("metadata", {})
        self.meta_enabled.setChecked(m.get("enabled", True))
        self.meta_framecount.setChecked(m.get("save_framecount", True))
        self.meta_timestamp.setChecked(m.get("save_timestamp", True))
        self.meta_sestime.setChecked(m.get("save_sestime", True))
        self.meta_cputime.setChecked(m.get("save_cputime", True))

    def dump(self) -> dict:
        return {
            "trigger": {
                "enabled":    self.trigger_enabled.isChecked(),
                "line":       self.trigger_line.currentText(),
                "activation": self.trigger_activation.currentText(),
                "selector":   self.trigger_selector.currentText(),
                "timeout_ms": self.trigger_timeout.value(),
            },
            "metadata": {
                "enabled":         self.meta_enabled.isChecked(),
                "save_framecount": self.meta_framecount.isChecked(),
                "save_timestamp":  self.meta_timestamp.isChecked(),
                "save_sestime":    self.meta_sestime.isChecked(),
                "save_cputime":    self.meta_cputime.isChecked(),
            },
        }


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class ConfigEditor(QMainWindow):
    """
    Top-level window.  Contains:
      - A header bar with the config file path and Save / Save As / Browse buttons.
      - A QTabWidget holding all six tabs.
      - A status bar for feedback messages.

    File operations
    ---------------
    _load_file   — opens a YAML, populates all tab widgets, and stores the raw dict
                   so any keys not managed by the GUI are preserved on save.
    _collect     — gathers the current state of all tab widgets into a merged dict.
    _write       — serialises _collect() back to YAML.

    Load order matters: chambers must be loaded BEFORE the TTL map tab so that
    chamber_keys() returns the correct list when TTLMapTab._add_row() is called.
    """
    def __init__(self, initial_path=None):
        super().__init__()
        self.setWindowTitle("Camera Acquisition — Config Editor")
        self.resize(1000, 820)
        self.setStyleSheet(SS)
        self._config_path = None    # path to the currently open file
        self._config_data = {}      # raw dict from the last load (preserves unknown keys)
        self._dirty       = False   # True if there are unsaved changes
        self._build_ui()
        if initial_path and os.path.isfile(initial_path):
            self._load_file(initial_path)
        else:
            self._try_auto_discover()

    def _build_ui(self):
        central = QWidget(); self.setCentralWidget(central)
        ml = QVBoxLayout(central); ml.setSpacing(0); ml.setContentsMargins(0, 0, 0, 0)

        # --- Header bar ---
        hdr = QWidget()
        hdr.setStyleSheet(f"background:{PANEL_BG}; border-bottom:1px solid {BORDER};")
        hdr.setFixedHeight(56)
        hl = QHBoxLayout(hdr); hl.setContentsMargins(16, 8, 16, 8)
        title = QLabel("◈  ACQUISITION CONFIG")
        title.setStyleSheet(f"color:{ACCENT}; font-size:15px; font-weight:bold; letter-spacing:3px;")
        self._path_edit = QLineEdit(); self._path_edit.setReadOnly(True)
        self._path_edit.setPlaceholderText("No config loaded"); self._path_edit.setFixedWidth(400)
        browse = QPushButton("Browse…"); browse.setFixedWidth(80)
        browse.clicked.connect(self._browse_config)
        save   = QPushButton("Save"); save.setObjectName("success"); save.setFixedWidth(70)
        save.clicked.connect(self._save)
        saveas = QPushButton("Save As…"); saveas.setFixedWidth(90)
        saveas.clicked.connect(self._save_as)
        hl.addWidget(title); hl.addStretch()
        hl.addWidget(QLabel("Config:")); hl.addWidget(self._path_edit)
        hl.addWidget(browse); hl.addSpacing(8)
        hl.addWidget(save); hl.addWidget(saveas)
        ml.addWidget(hdr)

        # --- Tab widget ---
        self.tabs = QTabWidget(); self.tabs.setDocumentMode(True)
        self.meta_tab      = MetadataTab()
        self.cameras_tab   = CamerasTab()
        self.chambers_tab  = ChambersTab(self.cameras_tab)
        # TTLMapTab receives the ChambersTab reference so it can call chamber_keys().
        self.ttl_map_tab   = TTLMapTab(self.chambers_tab)
        self.recording_tab = RecordingTab()
        self.trigger_tab   = TriggerTab()

        self.tabs.addTab(self.meta_tab,      "🧪  Experiment")
        self.tabs.addTab(self.cameras_tab,   "📷  Cameras")
        self.tabs.addTab(self.chambers_tab,  "🏠  Chambers")
        self.tabs.addTab(self.ttl_map_tab,   "🔌  TTL Map")
        self.tabs.addTab(self.recording_tab, "🎬  Recording / ROI")
        self.tabs.addTab(self.trigger_tab,   "⚡  Trigger / CSV")
        ml.addWidget(self.tabs, 1)

        self.status_bar = QStatusBar(); self.setStatusBar(self.status_bar)
        self._status("No config loaded.")

    # ------------------------------------------------------------------
    # File discovery
    # ------------------------------------------------------------------

    def _try_auto_discover(self):
        """
        If exactly one .yaml or .yml file exists in the current working
        directory, load it automatically.  If multiple exist, show a hint.
        """
        yamls = sorted(glob.glob("*.yaml") + glob.glob("*.yml"))
        if len(yamls) == 1:
            self._load_file(yamls[0])
        elif len(yamls) > 1:
            self._status(f"Multiple YAMLs: {', '.join(yamls[:4])} — use Browse.", WARN)

    def _browse_config(self):
        p, _ = QFileDialog.getOpenFileName(
            self, "Open Config", os.getcwd(), "YAML (*.yaml *.yml);;All (*)"
        )
        if p: self._load_file(p)

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def _load_file(self, path: str):
        """
        Parse the YAML file and populate all tabs.

        Load order:
          1. Cameras  — provides camera_keys() to ChambersTab
          2. Chambers — provides chamber_keys() to TTLMapTab
          3. TTL map  — uses chamber_keys() when creating row dropdowns
          4. Recording / Trigger — independent of the others
        """
        try:
            with open(path) as f: data = yaml.safe_load(f)
            if not isinstance(data, dict):
                raise ValueError("YAML root must be a mapping.")
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e)); return

        self._config_path = path
        self._config_data = data   # keep the raw dict to preserve unknown keys on save
        self._path_edit.setText(os.path.abspath(path))

        self.meta_tab.load(data.get("experiment_metadata", {}))
        self.cameras_tab.load(data.get("cameras", {}))
        self.chambers_tab.load(data.get("chambers", {}))
        # TTL map loaded AFTER chambers so chamber_keys() is already populated.
        self.ttl_map_tab.load(data.get("ttl_map", []), data.get("arduino", {}))
        self.recording_tab.load(data)
        self.trigger_tab.load(data)

        self._dirty = False
        self._status(f"Loaded: {os.path.abspath(path)}", ACCENT2)
        self.setWindowTitle(f"Config Editor — {os.path.basename(path)}")

    # ------------------------------------------------------------------
    # Collect / save
    # ------------------------------------------------------------------

    def _collect(self) -> dict:
        """
        Merge all tab states back into a single config dict.
        We start from _config_data (the last-loaded raw dict) so that
        any YAML keys we don't manage in the GUI are preserved.
        """
        rec  = self.recording_tab.dump()
        trig = self.trigger_tab.dump()
        ttl_map, arduino_cfg = self.ttl_map_tab.dump()

        merged = dict(self._config_data)   # copy to avoid mutating the stored dict
        merged["save_dir"]            = rec.pop("save_dir")
        merged["acquisition"]         = rec["acquisition"]
        merged["preview"]             = rec["preview"]
        merged["cameras"]             = self.cameras_tab.dump()
        merged["chambers"]            = self.chambers_tab.dump()
        merged["ttl_map"]             = ttl_map
        merged["arduino"]             = arduino_cfg
        merged["recording"]           = rec["recording"]
        merged["roi"]                 = rec["roi"]
        merged["trigger"]             = trig["trigger"]
        merged["metadata"]            = trig["metadata"]
        merged["experiment_metadata"] = self.meta_tab.dump()
        return merged

    def _save(self):
        # If no path is known yet, fall through to Save As.
        if not self._config_path: self._save_as(); return
        self._write(self._config_path)

    def _save_as(self):
        p, _ = QFileDialog.getSaveFileName(
            self, "Save As", os.getcwd(), "YAML (*.yaml);;All (*)"
        )
        if p:
            if not p.endswith((".yaml", ".yml")): p += ".yaml"
            self._config_path = p
            self._path_edit.setText(os.path.abspath(p))
            self._write(p)

    def _write(self, path: str):
        try:
            with open(path, "w") as f:
                # sort_keys=False preserves the order we put keys in _collect().
                yaml.dump(self._collect(), f, default_flow_style=False, sort_keys=False)
            self._dirty = False
            self._status(f"Saved: {os.path.abspath(path)}", ACCENT2)
            self.setWindowTitle(f"Config Editor — {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    # ------------------------------------------------------------------
    # Status bar helper
    # ------------------------------------------------------------------

    def _status(self, msg: str, color=None):
        """Update the status bar text and colour."""
        c = color or TEXT_SEC
        self.status_bar.setStyleSheet(
            f"QStatusBar{{background:{CARD_BG};color:{c};"
            f"border-top:1px solid {BORDER};font-size:11px;}}"
        )
        self.status_bar.showMessage(msg)

    # ------------------------------------------------------------------
    # Close guard
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        """Prompt to save if there are unsaved changes before closing."""
        if self._dirty:
            r = QMessageBox.question(
                self, "Unsaved Changes", "Save before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel
            )
            if r == QMessageBox.Save: self._save()
            elif r == QMessageBox.Cancel: event.ignore(); return
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default=None,
                        help="Config YAML to open on launch")
    parser.add_argument("--dir", default=None,
                        help="Change working directory before starting")
    args = parser.parse_args()
    if args.dir:
        os.chdir(args.dir)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")   # Fusion provides a neutral cross-platform base for the dark theme

    # Apply a dark Fusion palette so native widgets (scrollbars, menus)
    # also use dark colours rather than the system default.
    pal = QPalette()
    pal.setColor(QPalette.Window,          QColor(DARK_BG))
    pal.setColor(QPalette.WindowText,      QColor(TEXT_PRI))
    pal.setColor(QPalette.Base,            QColor(INPUT_BG))
    pal.setColor(QPalette.AlternateBase,   QColor(PANEL_BG))
    pal.setColor(QPalette.Text,            QColor(TEXT_PRI))
    pal.setColor(QPalette.Button,          QColor(BTN_BG))
    pal.setColor(QPalette.ButtonText,      QColor(TEXT_PRI))
    pal.setColor(QPalette.Highlight,       QColor(ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    app.setPalette(pal)

    w = ConfigEditor(initial_path=args.config)
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()