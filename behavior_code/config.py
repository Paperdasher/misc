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
  🏠 Chambers    — chamber→camera mapping, start+stop TTL signatures,
                   record toggle, timer duration
  🎬 Recording   — FPS, JPEG quality, ROI, auto-start toggle
  ⚡ Trigger/CSV — camera GPIO (leave disabled if TTL read by PC),
                   per-frame CSV column flags
"""

import sys
import os
import argparse
import glob
from datetime import date

import yaml

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
# Palette
# ---------------------------------------------------------------------------
DARK_BG  = "#1a1d23"
PANEL_BG = "#22262f"
CARD_BG  = "#2b303b"
ACCENT   = "#4f8ef7"
ACCENT2  = "#5ecfa8"
WARN     = "#f7934f"
TEXT_PRI = "#e8eaf0"
TEXT_SEC = "#8a90a0"
BORDER   = "#363c4a"
INPUT_BG = "#1e222a"
BTN_BG   = "#333844"
BTN_HOV  = "#404759"

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

def lbl(t):  w = QLabel(t); w.setObjectName("lbl");  return w
def note(t): w = QLabel(t); w.setObjectName("note"); w.setWordWrap(True); return w

def scrollable(w):
    sa = QScrollArea(); sa.setWidget(w); sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame); return sa


# ---------------------------------------------------------------------------
# Tab: Experiment metadata
# ---------------------------------------------------------------------------
class MetadataTab(QWidget):
    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0)
        content = QWidget(); layout = QVBoxLayout(content)
        layout.setSpacing(16); layout.setContentsMargins(20,20,20,20)

        id_box = QGroupBox("Session Identity")
        g = QGridLayout(id_box); g.setSpacing(10)
        g.setColumnStretch(1,1); g.setColumnStretch(3,1)
        self.experimenter    = QLineEdit(); self.experimenter.setPlaceholderText("e.g. Harley")
        self.experiment_name = QLineEdit(); self.experiment_name.setPlaceholderText("e.g. Cohort1")
        self.session_date    = QLineEdit(date.today().strftime("%Y-%m-%d"))
        self.schedule_name   = QLineEdit(); self.schedule_name.setPlaceholderText("e.g. FR5")
        self.pc_name         = QLineEdit()
        self.pc_name.setPlaceholderText("leave blank to use system hostname automatically")
        g.addWidget(lbl("Experimenter"),    0,0); g.addWidget(self.experimenter,    0,1)
        g.addWidget(lbl("Experiment Name"), 0,2); g.addWidget(self.experiment_name, 0,3)
        g.addWidget(lbl("Date"),            1,0); g.addWidget(self.session_date,    1,1)
        g.addWidget(lbl("Schedule Name"),   1,2); g.addWidget(self.schedule_name,   1,3)
        g.addWidget(lbl("PC / Station Name"),2,0); g.addWidget(self.pc_name,        2,1,1,3)
        g.addWidget(note("Optional — overrides the auto-detected hostname in session CSV. "
                         "Useful when multiple PCs run the same script and you need to "
                         "identify which machine recorded which file."), 3,0,1,4)
        layout.addWidget(id_box)

        an_box = QGroupBox("Animal")
        g2 = QGridLayout(an_box); g2.setSpacing(10)
        g2.setColumnStretch(1,1); g2.setColumnStretch(3,1)
        self.animal_id = QLineEdit(); self.animal_id.setPlaceholderText("e.g. M123")
        self.genotype  = QLineEdit(); self.genotype.setPlaceholderText("e.g. WT")
        self.group     = QLineEdit(); self.group.setPlaceholderText("e.g. control")
        g2.addWidget(lbl("Animal ID"), 0,0); g2.addWidget(self.animal_id, 0,1)
        g2.addWidget(lbl("Genotype"),  0,2); g2.addWidget(self.genotype,  0,3)
        g2.addWidget(lbl("Group"),     1,0); g2.addWidget(self.group,     1,1)
        layout.addWidget(an_box)

        co_box = QGroupBox("Co-recordings")
        co_lay = QHBoxLayout(co_box)
        self.eeg_path = QLineEdit(); self.eeg_path.setPlaceholderText("EEG / fiber path (optional)")
        eb = QToolButton(); eb.setText("…"); eb.clicked.connect(lambda: self._browse(self.eeg_path))
        co_lay.addWidget(lbl("EEG/Fiber")); co_lay.addWidget(self.eeg_path,1); co_lay.addWidget(eb)
        layout.addWidget(co_box)

        n_box = QGroupBox("Notes")
        nl = QVBoxLayout(n_box)
        self.notes = QTextEdit(); self.notes.setFixedHeight(80)
        self.notes.setPlaceholderText("Session notes…")
        nl.addWidget(self.notes)
        layout.addWidget(n_box)
        layout.addStretch()
        outer.addWidget(scrollable(content))

    def _browse(self, t):
        p, _ = QFileDialog.getOpenFileName(self, "Select File","","All (*)");
        if p: t.setText(p)

    def load(self, m):
        self.experimenter.setText(m.get("experimenter_name",""))
        self.experiment_name.setText(m.get("experiment_name",""))
        self.animal_id.setText(m.get("animal_id",""))
        self.genotype.setText(m.get("genotype",""))
        self.group.setText(m.get("group",""))
        self.schedule_name.setText(m.get("schedule_name",""))
        self.pc_name.setText(m.get("pc_name",""))
        self.eeg_path.setText(m.get("eeg_fiber_photometry_path",""))
        self.notes.setPlainText(m.get("notes",""))

    def dump(self):
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
# Tab: Cameras
# ---------------------------------------------------------------------------
class SingleCameraWidget(QGroupBox):
    def __init__(self, key, cfg):
        super().__init__(key)
        g = QGridLayout(self); g.setSpacing(10)
        g.setColumnStretch(1,1); g.setColumnStretch(3,1)
        self.serial      = QLineEdit(str(cfg.get("serial","")))
        self.name_e      = QLineEdit(str(cfg.get("name",key)))
        self.enabled     = QCheckBox("Enabled"); self.enabled.setChecked(cfg.get("enabled",True))
        self.exposure    = QSpinBox();  self.exposure.setRange(1,15000); self.exposure.setSuffix(" µs")
        self.exposure.setValue(cfg.get("exposure_us",14000))
        self.gain        = QDoubleSpinBox(); self.gain.setRange(0,48); self.gain.setSuffix(" dB")
        self.gain.setValue(cfg.get("gain_db",10))
        self.black_level = QDoubleSpinBox(); self.black_level.setRange(0,32)
        self.black_level.setDecimals(1); self.black_level.setValue(cfg.get("black_level",2.0))
        self.throughput  = QSpinBox(); self.throughput.setRange(1_000_000,300_000_000)
        self.throughput.setSingleStep(1_000_000); self.throughput.setSuffix(" bps")
        self.throughput.setValue(cfg.get("throughput_limit",90_000_000))

        g.addWidget(lbl("Serial"),           0,0); g.addWidget(self.serial,      0,1)
        g.addWidget(lbl("Friendly Name"),    0,2); g.addWidget(self.name_e,      0,3)
        g.addWidget(self.enabled,            1,0,1,2)
        g.addWidget(lbl("Exposure"),         2,0); g.addWidget(self.exposure,    2,1)
        g.addWidget(lbl("Gain"),             2,2); g.addWidget(self.gain,        2,3)
        g.addWidget(lbl("Black Level"),      3,0); g.addWidget(self.black_level, 3,1)
        g.addWidget(lbl("Throughput Limit"), 3,2); g.addWidget(self.throughput,  3,3)

    def dump(self):
        return {
            "serial": self.serial.text().strip(), "name": self.name_e.text().strip(),
            "enabled": self.enabled.isChecked(), "exposure_us": self.exposure.value(),
            "gain_db": self.gain.value(), "black_level": self.black_level.value(),
            "throughput_limit": self.throughput.value(),
        }

class CamerasTab(QWidget):
    def __init__(self):
        super().__init__()
        self._cards = {}
        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0)
        self._cw = QWidget(); self._cl = QVBoxLayout(self._cw)
        self._cl.setSpacing(12); self._cl.setContentsMargins(20,20,20,20)
        self._cl.addStretch()
        outer.addWidget(scrollable(self._cw))

    def load(self, cfg):
        for c in self._cards.values():
            self._cl.removeWidget(c); c.deleteLater()
        self._cards.clear()
        for key, ccfg in cfg.items():
            card = SingleCameraWidget(key, ccfg)
            self._cards[key] = card
            self._cl.insertWidget(self._cl.count()-1, card)

    def dump(self):
        return {k: c.dump() for k, c in self._cards.items()}

    def camera_keys(self):
        return list(self._cards.keys())


# ---------------------------------------------------------------------------
# Arduino analog connection widget  (one per chamber)
# ---------------------------------------------------------------------------
class ArduinoSigWidget(QGroupBox):
    """
    Configures the Arduino analog TTL connection for one chamber.

    Each chamber uses 3 consecutive analog channels on the Arduino sketch
    (chamber_ttl_analog.ino), numbered sequentially across all chambers
    starting at 1 (chamber 1 -> TTL 1,2,3; chamber 2 -> TTL 4,5,6; etc).
    Within a chamber: one TTL number STARTS recording, one STOPS it, and
    the third (if used) is a spare/unused channel.

    The script requires 3 consecutive confirmed-high readings of a TTL
    number before acting — this is fixed in multiAcquisition.py and not
    configurable here, since it's a noise-filtering safeguard rather than
    a per-chamber setting.

    Multiple chambers can share one Arduino (same port, different TTL
    numbers) — just give them the same port and different TTL ranges.
    """
    BAUDS = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]

    def __init__(self, title: str, cfg: dict, chamber_key: str, parent=None):
        super().__init__(title, parent)
        self._chamber_key = chamber_key
        g = QGridLayout(self); g.setSpacing(8)
        g.setColumnStretch(1, 1); g.setColumnStretch(3, 1)

        # Port
        self.port = QLineEdit(cfg.get("port", "COM3"))
        self.port.setPlaceholderText("e.g. COM3 or /dev/ttyUSB0")
        self.scan_btn = QPushButton("Scan ports")
        self.scan_btn.setFixedWidth(90)
        self.scan_btn.clicked.connect(self._scan)
        self._scan_lbl = QLabel(""); self._scan_lbl.setObjectName("note")

        port_row = QWidget(); pr = QHBoxLayout(port_row); pr.setContentsMargins(0,0,0,0)
        pr.addWidget(self.port, 1); pr.addWidget(self.scan_btn)

        # Baud
        self.baud = QComboBox()
        for b in self.BAUDS: self.baud.addItem(str(b), b)
        bi = self.baud.findText(str(cfg.get("baud", 115200)))
        if bi >= 0: self.baud.setCurrentIndex(bi)

        # Board voltage toggle — 5V vs 3.3V, affects threshold in the sketch
        self.vref = QComboBox()
        self.vref.addItem("5.0 V  (Uno / Mega / Nano)", 5.0)
        self.vref.addItem("3.3 V  (Due / Zero / MKR)",  3.3)
        vref_val = cfg.get("vref", 5.0)
        for i in range(self.vref.count()):
            if abs(self.vref.itemData(i) - vref_val) < 0.01:
                self.vref.setCurrentIndex(i); break

        # Start / Stop TTL channel numbers
        self.start_ttl = QSpinBox(); self.start_ttl.setRange(1, 999)
        self.start_ttl.setValue(cfg.get("start_ttl", 1))
        self.stop_ttl  = QSpinBox(); self.stop_ttl.setRange(1, 999)
        self.stop_ttl.setValue(cfg.get("stop_ttl", 2))

        g.addWidget(lbl("Port"),            0, 0); g.addWidget(port_row,       0, 1, 1, 3)
        g.addWidget(self._scan_lbl,         1, 0, 1, 4)
        g.addWidget(lbl("Baud rate"),       2, 0); g.addWidget(self.baud,      2, 1)
        g.addWidget(lbl("Board voltage"),   2, 2); g.addWidget(self.vref,      2, 3)
        g.addWidget(lbl("Start TTL #"),     3, 0); g.addWidget(self.start_ttl, 3, 1)
        g.addWidget(lbl("Stop TTL #"),      3, 2); g.addWidget(self.stop_ttl,  3, 3)
        g.addWidget(note(
            "TTL numbers are sequential across ALL chambers, matching pin order in "
            "ANALOG_PINS in chamber_ttl_analog.ino (chamber 1 = TTL 1,2,3; chamber 2 = "
            "TTL 4,5,6; etc). 3 consecutive confirmed-high reads of the Start TTL begins "
            "recording; 3 consecutive reads of the Stop TTL ends it.\n"
            "Board voltage must match BOARD_VREF set in the sketch — this only affects "
            "documentation here; update the sketch constant yourself to match."
        ), 4, 0, 1, 4)

    def _scan(self):
        try:
            import serial.tools.list_ports as lp
            ports = list(lp.comports())
            if ports:
                self._scan_lbl.setText(
                    "  " + "   ".join(f"{p.device} ({p.description[:30]})" for p in sorted(ports))
                )
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
        self.start_ttl.setValue(cfg.get("start_ttl", 1))
        self.stop_ttl.setValue(cfg.get("stop_ttl", 2))

    def dump(self) -> dict:
        return {
            "port":      self.port.text().strip(),
            "baud":      int(self.baud.currentText()),
            "vref":      self.vref.currentData(),
            "start_ttl": self.start_ttl.value(),
            "stop_ttl":  self.stop_ttl.value(),
        }


# ---------------------------------------------------------------------------
# Tab: Chambers
# ---------------------------------------------------------------------------
class SingleChamberWidget(QWidget):
    def __init__(self, chamber_key: str, cfg: dict, camera_keys: list):
        super().__init__()
        self._key = chamber_key
        layout = QVBoxLayout(self); layout.setContentsMargins(0,0,0,0); layout.setSpacing(8)

        # ---- Camera assignment + record toggle ----
        top_box = QGroupBox(f"Chamber: {chamber_key}")
        top_g   = QGridLayout(top_box); top_g.setSpacing(10)
        top_g.setColumnStretch(1,1); top_g.setColumnStretch(3,1)

        self.camera_combo = QComboBox()
        for ck in camera_keys: self.camera_combo.addItem(ck)
        assigned = cfg.get("camera","")
        idx = self.camera_combo.findText(assigned)
        if idx >= 0: self.camera_combo.setCurrentIndex(idx)

        self.record_chk = QCheckBox("Record this chamber")
        self.record_chk.setChecked(cfg.get("record", True))

        # Timer
        self.timer_enabled = QCheckBox("Stop recording after")
        self.timer_enabled.setChecked(cfg.get("timer_enabled", False))
        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 86400); self.duration_spin.setSuffix(" s")
        self.duration_spin.setValue(cfg.get("duration_s", 1800))
        timer_note = QLabel("seconds  (default 1800 = 30 min)")
        timer_note.setObjectName("note")

        top_g.addWidget(lbl("Mapped camera"),    0,0); top_g.addWidget(self.camera_combo, 0,1)
        top_g.addWidget(self.record_chk,         0,2,1,2)
        top_g.addWidget(self.timer_enabled,      1,0); top_g.addWidget(self.duration_spin, 1,1)
        top_g.addWidget(timer_note,              1,2,1,2)
        layout.addWidget(top_box)

        # ---- Arduino connection ----
        ard_cfg = cfg.get("arduino", {})
        self.arduino_sig = ArduinoSigWidget(
            "Arduino Connection",
            ard_cfg,
            chamber_key,
        )
        layout.addWidget(self.arduino_sig)

    def dump(self) -> dict:
        return {
            "camera":        self.camera_combo.currentText(),
            "record":        self.record_chk.isChecked(),
            "timer_enabled": self.timer_enabled.isChecked(),
            "duration_s":    self.duration_spin.value(),
            "arduino":       self.arduino_sig.dump(),
        }


class ChambersTab(QWidget):
    def __init__(self, cameras_tab: CamerasTab):
        super().__init__()
        self._cameras_tab = cameras_tab
        self._cards: dict[str, SingleChamberWidget] = {}
        self._wrappers: dict[str, QWidget] = {}

        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0)

        # Toolbar
        tb = QWidget(); tl = QHBoxLayout(tb)
        tl.setContentsMargins(12,8,12,4)
        self._new_key_edit = QLineEdit()
        self._new_key_edit.setPlaceholderText("New chamber key, e.g. chamber_B")
        self._new_key_edit.setFixedWidth(220)
        add_btn = QPushButton("+ Add Chamber")
        add_btn.clicked.connect(self._add_from_field)
        tl.addWidget(lbl("Key:")); tl.addWidget(self._new_key_edit)
        tl.addWidget(add_btn); tl.addStretch()
        outer.addWidget(tb)

        self._cw = QWidget(); self._cl = QVBoxLayout(self._cw)
        self._cl.setSpacing(16); self._cl.setContentsMargins(20,8,20,20)
        self._cl.addStretch()
        outer.addWidget(scrollable(self._cw))

    def _camera_keys(self):
        return self._cameras_tab.camera_keys()

    def _add_from_field(self):
        key = self._new_key_edit.text().strip()
        if key:
            self._new_key_edit.clear()
            self._add_chamber(key, {})

    def _add_chamber(self, key: str, cfg: dict):
        if not key or key in self._cards:
            return

        card    = SingleChamberWidget(key, cfg, self._camera_keys())
        wrapper = QWidget()
        wl      = QVBoxLayout(wrapper); wl.setContentsMargins(0,0,0,4)

        # Remove button row
        hdr   = QWidget(); hl = QHBoxLayout(hdr); hl.setContentsMargins(0,0,0,0)
        rbtn  = QPushButton(f"✕  Remove {key}")
        rbtn.setObjectName("danger"); rbtn.setFixedWidth(160)
        rbtn.clicked.connect(lambda: self._remove_chamber(key))
        hl.addStretch(); hl.addWidget(rbtn)

        wl.addWidget(hdr); wl.addWidget(card)

        self._cards[key]    = card
        self._wrappers[key] = wrapper
        self._cl.insertWidget(self._cl.count()-1, wrapper)

    def _remove_chamber(self, key: str):
        if key not in self._cards:
            return
        wrapper = self._wrappers.pop(key)
        self._cl.removeWidget(wrapper)
        wrapper.deleteLater()
        del self._cards[key]

    def load(self, chambers_cfg: dict):
        for k in list(self._cards.keys()):
            self._remove_chamber(k)
        for key, cfg in chambers_cfg.items():
            self._add_chamber(key, cfg)

    def dump(self) -> dict:
        return {k: c.dump() for k, c in self._cards.items()}


# ---------------------------------------------------------------------------
# Tab: Recording / ROI / Auto-start / Timer
# ---------------------------------------------------------------------------
class RecordingTab(QWidget):
    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0)
        content = QWidget(); layout = QVBoxLayout(content)
        layout.setSpacing(16); layout.setContentsMargins(20,20,20,20)

        # Acquisition mode
        mode_box = QGroupBox("Acquisition Mode")
        ml = QVBoxLayout(mode_box)
        self.auto_start = QCheckBox("Auto-start recording when script launches")
        self.auto_start.setChecked(False)
        ml.addWidget(self.auto_start)
        ml.addWidget(note(
            "Auto-start ON  → recording begins immediately on script launch for all\n"
            "                 chambers that have 'Record this chamber' enabled.\n"
            "Auto-start OFF → cameras stream to preview only; recording starts per-chamber\n"
            "                 via its TTL start pulse or manual start in the stats popup.\n"
            "Per-chamber timer → configure inside each chamber card in the Chambers tab."
        ))
        layout.addWidget(mode_box)

        # Save path
        path_box = QGroupBox("Save Location")
        pl = QHBoxLayout(path_box)
        self.save_dir = QLineEdit(); self.save_dir.setPlaceholderText("./recordings")
        bb = QToolButton(); bb.setText("…"); bb.clicked.connect(self._browse_dir)
        pl.addWidget(self.save_dir,1); pl.addWidget(bb)
        layout.addWidget(path_box)

        # Recording settings
        rec_box = QGroupBox("Recording")
        rg = QGridLayout(rec_box); rg.setSpacing(10)
        rg.setColumnStretch(1,1); rg.setColumnStretch(3,1)
        self.fps = QDoubleSpinBox(); self.fps.setRange(1,500); self.fps.setDecimals(2)
        self.fps.setSuffix(" Hz"); self.fps.setValue(59.99)
        self.jpeg_quality = QSpinBox(); self.jpeg_quality.setRange(0,100)
        self.jpeg_quality.setSuffix("%"); self.jpeg_quality.setValue(90)
        rg.addWidget(lbl("Frame Rate"),   0,0); rg.addWidget(self.fps,          0,1)
        rg.addWidget(lbl("JPEG Quality"), 0,2); rg.addWidget(self.jpeg_quality, 0,3)
        layout.addWidget(rec_box)

        # ROI
        roi_box = QGroupBox("ROI")
        roig = QGridLayout(roi_box); roig.setSpacing(10)
        roig.setColumnStretch(1,1); roig.setColumnStretch(3,1)
        self.roi_w  = QSpinBox(); self.roi_w.setRange(0,8192);  self.roi_w.setSuffix(" px")
        self.roi_h  = QSpinBox(); self.roi_h.setRange(0,8192);  self.roi_h.setSuffix(" px")
        self.roi_ox = QSpinBox(); self.roi_ox.setRange(0,8192); self.roi_ox.setSuffix(" px")
        self.roi_oy = QSpinBox(); self.roi_oy.setRange(0,8192); self.roi_oy.setSuffix(" px")
        roig.addWidget(lbl("Width"),    0,0); roig.addWidget(self.roi_w,  0,1)
        roig.addWidget(lbl("Height"),   0,2); roig.addWidget(self.roi_h,  0,3)
        roig.addWidget(lbl("Offset X"), 1,0); roig.addWidget(self.roi_ox, 1,1)
        roig.addWidget(lbl("Offset Y"), 1,2); roig.addWidget(self.roi_oy, 1,3)
        layout.addWidget(roi_box)
        layout.addStretch()
        outer.addWidget(scrollable(content))

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self,"Select Directory")
        if d: self.save_dir.setText(d)

    def load(self, config):
        self.save_dir.setText(config.get("save_dir","./recordings"))
        acq = config.get("acquisition",{})
        self.auto_start.setChecked(acq.get("auto_start",False))
        rec = config.get("recording",{})
        self.fps.setValue(rec.get("fps",59.99))
        self.jpeg_quality.setValue(rec.get("jpeg_quality",90))
        roi = config.get("roi",{})
        self.roi_w.setValue(roi.get("width",1020) or 0)
        self.roi_h.setValue(roi.get("height",1020) or 0)
        self.roi_ox.setValue(roi.get("offset_x",0))
        self.roi_oy.setValue(roi.get("offset_y",0))

    def dump(self):
        return {
            "save_dir":   self.save_dir.text().strip() or "./recordings",
            "acquisition": {
                "auto_start": self.auto_start.isChecked(),
            },
            "recording": {"fps": self.fps.value(), "jpeg_quality": self.jpeg_quality.value(),
                          "split_size_mb": None},
            "roi": {"width": self.roi_w.value() or None, "height": self.roi_h.value() or None,
                    "offset_x": self.roi_ox.value(), "offset_y": self.roi_oy.value()},
        }


# ---------------------------------------------------------------------------
# Tab: Camera GPIO Trigger + Metadata CSV flags
# ---------------------------------------------------------------------------
class TriggerTab(QWidget):
    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0)
        content = QWidget(); layout = QVBoxLayout(content)
        layout.setSpacing(16); layout.setContentsMargins(20,20,20,20)

        note_box = QGroupBox("Camera GPIO Trigger")
        nl = QVBoxLayout(note_box)
        nl.addWidget(note(
            "If TTL is read by this script over serial (the default), leave "
            "'Enable hardware trigger' OFF.  The Spinnaker GPIO trigger is only "
            "needed if you are routing a physical TTL wire directly into the "
            "camera's GPIO line to gate individual frame acquisition."
        ))
        self.trigger_enabled    = QCheckBox("Enable camera hardware trigger (GPIO)")
        self.trigger_line       = QComboBox(); self.trigger_line.addItems(["Line0","Line1","Line2","Line3"])
        self.trigger_activation = QComboBox()
        self.trigger_activation.addItems(["RisingEdge","FallingEdge","AnyEdge","LevelHigh","LevelLow"])
        self.trigger_selector   = QComboBox()
        self.trigger_selector.addItems(["AcquisitionStart","FrameStart","FrameBurstStart"])
        self.trigger_timeout    = QSpinBox(); self.trigger_timeout.setRange(100,60000)
        self.trigger_timeout.setSuffix(" ms"); self.trigger_timeout.setValue(5000)
        tg = QGridLayout(); tg.setSpacing(10); tg.setColumnStretch(1,1); tg.setColumnStretch(3,1)
        tg.addWidget(self.trigger_enabled,          0,0,1,4)
        tg.addWidget(lbl("GPIO Line"),              1,0); tg.addWidget(self.trigger_line,       1,1)
        tg.addWidget(lbl("Activation"),             1,2); tg.addWidget(self.trigger_activation, 1,3)
        tg.addWidget(lbl("Trigger Selector"),       2,0); tg.addWidget(self.trigger_selector,   2,1)
        tg.addWidget(lbl("Timeout"),                2,2); tg.addWidget(self.trigger_timeout,    2,3)
        nl.addLayout(tg)
        layout.addWidget(note_box)

        meta_box = QGroupBox("Per-frame Metadata CSV Columns")
        mg = QGridLayout(meta_box); mg.setSpacing(8)
        self.meta_enabled    = QCheckBox("Save per-frame timestamps CSV")
        self.meta_framecount = QCheckBox("Frame count")
        self.meta_timestamp  = QCheckBox("Camera hardware timestamp")
        self.meta_sestime    = QCheckBox("Session time")
        self.meta_cputime    = QCheckBox("CPU wall clock")
        for cb in [self.meta_enabled, self.meta_framecount, self.meta_timestamp,
                   self.meta_sestime, self.meta_cputime]:
            cb.setChecked(True)
        mg.addWidget(self.meta_enabled,    0,0,1,2)
        mg.addWidget(self.meta_framecount, 1,0); mg.addWidget(self.meta_timestamp, 1,1)
        mg.addWidget(self.meta_sestime,    2,0); mg.addWidget(self.meta_cputime,   2,1)
        mg.addWidget(note(
            "CSV row_type: 'frame' = captured frame  |  "
            "'ttl_event' = TTL pulse with ttl_chamber / ttl_kind / ttl_label columns."
        ), 3,0,1,2)
        layout.addWidget(meta_box)
        layout.addStretch()
        outer.addWidget(scrollable(content))

    def load(self, config):
        t = config.get("trigger",{})
        self.trigger_enabled.setChecked(t.get("enabled",False))
        for combo, key, default in [
            (self.trigger_line,       "line",       "Line0"),
            (self.trigger_activation, "activation", "RisingEdge"),
            (self.trigger_selector,   "selector",   "AcquisitionStart"),
        ]:
            idx = combo.findText(t.get(key,default)); combo.setCurrentIndex(max(0,idx))
        self.trigger_timeout.setValue(t.get("timeout_ms",5000))
        m = config.get("metadata",{})
        self.meta_enabled.setChecked(m.get("enabled",True))
        self.meta_framecount.setChecked(m.get("save_framecount",True))
        self.meta_timestamp.setChecked(m.get("save_timestamp",True))
        self.meta_sestime.setChecked(m.get("save_sestime",True))
        self.meta_cputime.setChecked(m.get("save_cputime",True))

    def dump(self):
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
    def __init__(self, initial_path=None):
        super().__init__()
        self.setWindowTitle("Camera Acquisition — Config Editor")
        self.resize(1000, 820)
        self.setStyleSheet(SS)
        self._config_path = None
        self._config_data = {}
        self._dirty = False
        self._build_ui()
        if initial_path and os.path.isfile(initial_path):
            self._load_file(initial_path)
        else:
            self._try_auto_discover()

    def _build_ui(self):
        central = QWidget(); self.setCentralWidget(central)
        ml = QVBoxLayout(central); ml.setSpacing(0); ml.setContentsMargins(0,0,0,0)

        # Header
        hdr = QWidget()
        hdr.setStyleSheet(f"background:{PANEL_BG}; border-bottom:1px solid {BORDER};")
        hdr.setFixedHeight(56)
        hl = QHBoxLayout(hdr); hl.setContentsMargins(16,8,16,8)
        title = QLabel("◈  ACQUISITION CONFIG")
        title.setStyleSheet(f"color:{ACCENT}; font-size:15px; font-weight:bold; letter-spacing:3px;")
        self._path_edit = QLineEdit(); self._path_edit.setReadOnly(True)
        self._path_edit.setPlaceholderText("No config loaded"); self._path_edit.setFixedWidth(400)
        browse = QPushButton("Browse…"); browse.setFixedWidth(80); browse.clicked.connect(self._browse_config)
        save   = QPushButton("Save");    save.setObjectName("success"); save.setFixedWidth(70)
        save.clicked.connect(self._save)
        saveas = QPushButton("Save As…"); saveas.setFixedWidth(90); saveas.clicked.connect(self._save_as)
        hl.addWidget(title); hl.addStretch()
        hl.addWidget(QLabel("Config:")); hl.addWidget(self._path_edit)
        hl.addWidget(browse); hl.addSpacing(8)
        hl.addWidget(save); hl.addWidget(saveas)
        ml.addWidget(hdr)

        self.tabs = QTabWidget(); self.tabs.setDocumentMode(True)
        self.meta_tab      = MetadataTab()
        self.cameras_tab   = CamerasTab()
        self.chambers_tab  = ChambersTab(self.cameras_tab)
        self.recording_tab = RecordingTab()
        self.trigger_tab   = TriggerTab()

        self.tabs.addTab(self.meta_tab,      "🧪  Experiment")
        self.tabs.addTab(self.cameras_tab,   "📷  Cameras")
        self.tabs.addTab(self.chambers_tab,  "🏠  Chambers & TTL")
        self.tabs.addTab(self.recording_tab, "🎬  Recording / ROI")
        self.tabs.addTab(self.trigger_tab,   "⚡  Trigger / CSV")
        ml.addWidget(self.tabs, 1)

        self.status_bar = QStatusBar(); self.setStatusBar(self.status_bar)
        self._status("No config loaded.")

    # ---- file I/O ----

    def _try_auto_discover(self):
        yamls = sorted(glob.glob("*.yaml") + glob.glob("*.yml"))
        if len(yamls) == 1: self._load_file(yamls[0])
        elif len(yamls) > 1:
            self._status(f"Multiple YAMLs: {', '.join(yamls[:4])} — use Browse.", WARN)

    def _browse_config(self):
        p, _ = QFileDialog.getOpenFileName(self,"Open Config",os.getcwd(),
                                            "YAML (*.yaml *.yml);;All (*)")
        if p: self._load_file(p)

    def _load_file(self, path):
        try:
            with open(path) as f: data = yaml.safe_load(f)
            if not isinstance(data, dict): raise ValueError("YAML root must be a mapping.")
        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e)); return

        self._config_path = path; self._config_data = data
        self._path_edit.setText(os.path.abspath(path))

        self.meta_tab.load(data.get("experiment_metadata",{}))
        self.cameras_tab.load(data.get("cameras",{}))
        self.chambers_tab.load(data.get("chambers",{}))
        self.recording_tab.load(data)
        self.trigger_tab.load(data)

        self._dirty = False
        self._status(f"Loaded: {os.path.abspath(path)}", ACCENT2)
        self.setWindowTitle(f"Config Editor — {os.path.basename(path)}")

    def _collect(self):
        rec  = self.recording_tab.dump()
        trig = self.trigger_tab.dump()
        merged = dict(self._config_data)
        merged["save_dir"]            = rec.pop("save_dir")
        merged["acquisition"]         = rec["acquisition"]
        merged["cameras"]             = self.cameras_tab.dump()
        merged["chambers"]            = self.chambers_tab.dump()
        merged["recording"]           = rec["recording"]
        merged["roi"]                 = rec["roi"]
        merged["trigger"]             = trig["trigger"]
        merged["metadata"]            = trig["metadata"]
        merged["experiment_metadata"] = self.meta_tab.dump()
        return merged

    def _save(self):
        if not self._config_path: self._save_as(); return
        self._write(self._config_path)

    def _save_as(self):
        p, _ = QFileDialog.getSaveFileName(self,"Save As",os.getcwd(),"YAML (*.yaml);;All (*)")
        if p:
            if not p.endswith((".yaml",".yml")): p += ".yaml"
            self._config_path = p; self._path_edit.setText(os.path.abspath(p))
            self._write(p)

    def _write(self, path):
        try:
            with open(path,"w") as f:
                yaml.dump(self._collect(), f, default_flow_style=False, sort_keys=False)
            self._dirty = False
            self._status(f"Saved: {os.path.abspath(path)}", ACCENT2)
            self.setWindowTitle(f"Config Editor — {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    def _status(self, msg, color=None):
        c = color or TEXT_SEC
        self.status_bar.setStyleSheet(
            f"QStatusBar{{background:{CARD_BG};color:{c};border-top:1px solid {BORDER};font-size:11px;}}")
        self.status_bar.showMessage(msg)

    def closeEvent(self, event):
        if self._dirty:
            r = QMessageBox.question(self,"Unsaved Changes","Save before closing?",
                                     QMessageBox.Save|QMessageBox.Discard|QMessageBox.Cancel)
            if r == QMessageBox.Save: self._save()
            elif r == QMessageBox.Cancel: event.ignore(); return
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c","--config", default=None)
    parser.add_argument("--dir", default=None)
    args = parser.parse_args()
    if args.dir: os.chdir(args.dir)

    app = QApplication(sys.argv); app.setStyle("Fusion")
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

    w = ConfigEditor(initial_path=args.config); w.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
