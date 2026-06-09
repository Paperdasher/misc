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
  🧪  Experiment   — experimenter, animal, co-recording paths, notes
  📷  Cameras      — per-camera serial, exposure, gain, etc.
  🏠  Chambers     — map each chamber to a camera + configure its TTL signature
  🎬  Recording    — FPS, JPEG quality, ROI, preview, auto-start toggle
  ⚡  Trigger/CSV  — camera GPIO trigger, per-frame metadata flags
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
    QToolButton, QStatusBar, QSizePolicy,
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
QTabWidget::pane {{ border: 1px solid {BORDER}; background: {PANEL_BG}; border-radius: 4px; }}
QTabBar::tab {{
    background: {CARD_BG}; color: {TEXT_SEC}; padding: 8px 18px;
    border: 1px solid {BORDER}; border-bottom: none;
    border-top-left-radius: 4px; border-top-right-radius: 4px;
    margin-right: 2px; font-size: 12px;
}}
QTabBar::tab:selected {{ background: {ACCENT}; color: white; font-weight: bold; }}
QTabBar::tab:hover:!selected {{ background: {BTN_HOV}; color: {TEXT_PRI}; }}
QGroupBox {{
    border: 1px solid {BORDER}; border-radius: 6px;
    margin-top: 14px; padding-top: 10px;
    background: {CARD_BG}; font-size: 11px; color: {TEXT_SEC};
    letter-spacing: 1px; text-transform: uppercase;
}}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left;
    padding: 0 8px; left: 12px; top: -2px; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit {{
    background: {INPUT_BG}; color: {TEXT_PRI}; border: 1px solid {BORDER};
    border-radius: 4px; padding: 5px 8px; selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QTextEdit:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {CARD_BG}; color: {TEXT_PRI};
    border: 1px solid {BORDER}; selection-background-color: {ACCENT};
}}
QPushButton {{
    background: {BTN_BG}; color: {TEXT_PRI}; border: 1px solid {BORDER};
    border-radius: 4px; padding: 7px 18px; font-size: 12px;
}}
QPushButton:hover {{ background: {BTN_HOV}; border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {ACCENT}; color: white; }}
QPushButton#success {{ background: {ACCENT2}; color: #111; border: none; font-weight: bold; }}
QPushButton#success:hover {{ background: #7de0ba; }}
QCheckBox {{ color: {TEXT_PRI}; spacing: 8px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border: 1px solid {BORDER};
    border-radius: 3px; background: {INPUT_BG};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: {DARK_BG}; width: 8px; border-radius: 4px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 20px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QLabel#lbl {{ color: {TEXT_SEC}; font-size: 12px; }}
QLabel#note {{ color: {TEXT_SEC}; font-size: 11px; }}
QStatusBar {{
    background: {CARD_BG}; color: {TEXT_SEC};
    border-top: 1px solid {BORDER}; font-size: 11px;
}}
QToolButton {{
    background: {BTN_BG}; border: 1px solid {BORDER};
    border-radius: 4px; padding: 4px 8px; color: {TEXT_SEC};
}}
QToolButton:hover {{ background: {BTN_HOV}; color: {TEXT_PRI}; }}
"""

def lbl(text): w = QLabel(text); w.setObjectName("lbl"); return w
def note(text): w = QLabel(text); w.setObjectName("note"); w.setWordWrap(True); return w
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

        # Session identity
        id_box = QGroupBox("Session Identity")
        g = QGridLayout(id_box); g.setSpacing(10)
        g.setColumnStretch(1,1); g.setColumnStretch(3,1)
        self.experimenter    = QLineEdit(); self.experimenter.setPlaceholderText("e.g. Harley")
        self.experiment_name = QLineEdit(); self.experiment_name.setPlaceholderText("e.g. Sema6aKD_cohort1")
        self.session_date    = QLineEdit(date.today().strftime("%Y-%m-%d"))
        self.schedule_name   = QLineEdit(); self.schedule_name.setPlaceholderText("e.g. FR5_extinction")
        g.addWidget(lbl("Experimenter"),    0,0); g.addWidget(self.experimenter,    0,1)
        g.addWidget(lbl("Experiment Name"), 0,2); g.addWidget(self.experiment_name, 0,3)
        g.addWidget(lbl("Date"),            1,0); g.addWidget(self.session_date,    1,1)
        g.addWidget(lbl("Schedule Name"),   1,2); g.addWidget(self.schedule_name,   1,3)
        layout.addWidget(id_box)

        # Animal
        an_box = QGroupBox("Animal")
        g2 = QGridLayout(an_box); g2.setSpacing(10)
        g2.setColumnStretch(1,1); g2.setColumnStretch(3,1)
        self.animal_id = QLineEdit(); self.animal_id.setPlaceholderText("e.g. M123")
        self.genotype  = QLineEdit(); self.genotype.setPlaceholderText("e.g. WT, HET, KO")
        self.group     = QLineEdit(); self.group.setPlaceholderText("e.g. control")
        g2.addWidget(lbl("Animal ID"), 0,0); g2.addWidget(self.animal_id, 0,1)
        g2.addWidget(lbl("Genotype"),  0,2); g2.addWidget(self.genotype,  0,3)
        g2.addWidget(lbl("Group"),     1,0); g2.addWidget(self.group,     1,1)
        layout.addWidget(an_box)

        # Co-recordings
        co_box = QGroupBox("Co-recordings")
        co_lay = QHBoxLayout(co_box)
        self.eeg_path = QLineEdit(); self.eeg_path.setPlaceholderText("EEG / fiber photometry path (optional)")
        eb = QToolButton(); eb.setText("…"); eb.clicked.connect(lambda: self._browse(self.eeg_path))
        co_lay.addWidget(lbl("EEG/Fiber Path")); co_lay.addWidget(self.eeg_path); co_lay.addWidget(eb)
        layout.addWidget(co_box)

        # Notes
        n_box = QGroupBox("Notes")
        n_lay = QVBoxLayout(n_box)
        self.notes = QTextEdit(); self.notes.setPlaceholderText("Session notes…"); self.notes.setFixedHeight(90)
        n_lay.addWidget(self.notes)
        layout.addWidget(n_box)
        layout.addStretch()
        outer.addWidget(scrollable(content))

    def _browse(self, target):
        p, _ = QFileDialog.getOpenFileName(self, "Select File", "", "All Files (*)")
        if p: target.setText(p)

    def load(self, m):
        self.experimenter.setText(m.get("experimenter_name",""))
        self.experiment_name.setText(m.get("experiment_name",""))
        self.animal_id.setText(m.get("animal_id",""))
        self.genotype.setText(m.get("genotype",""))
        self.group.setText(m.get("group",""))
        self.schedule_name.setText(m.get("schedule_name",""))
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
        self.name_e      = QLineEdit(str(cfg.get("name", key)))
        self.chamber_e   = QLineEdit(str(cfg.get("chamber","")))
        self.chamber_e.setPlaceholderText("must match a chamber key")
        self.enabled     = QCheckBox("Enabled"); self.enabled.setChecked(cfg.get("enabled",True))
        self.exposure    = QSpinBox();  self.exposure.setRange(1,15000); self.exposure.setSuffix(" µs")
        self.exposure.setValue(cfg.get("exposure_us",14000))
        self.gain        = QDoubleSpinBox(); self.gain.setRange(0,48); self.gain.setSuffix(" dB")
        self.gain.setValue(cfg.get("gain_db",10))
        self.black_level = QDoubleSpinBox(); self.black_level.setRange(0,32); self.black_level.setDecimals(1)
        self.black_level.setValue(cfg.get("black_level",2.0))
        self.throughput  = QSpinBox(); self.throughput.setRange(1_000_000,300_000_000)
        self.throughput.setSingleStep(1_000_000); self.throughput.setSuffix(" bps")
        self.throughput.setValue(cfg.get("throughput_limit",90_000_000))

        g.addWidget(lbl("Serial"),           0,0); g.addWidget(self.serial,      0,1)
        g.addWidget(lbl("Friendly Name"),    0,2); g.addWidget(self.name_e,      0,3)
        g.addWidget(lbl("Chamber Key"),      1,0); g.addWidget(self.chamber_e,   1,1)
        g.addWidget(self.enabled,            1,2,1,2)
        g.addWidget(lbl("Exposure"),         2,0); g.addWidget(self.exposure,    2,1)
        g.addWidget(lbl("Gain"),             2,2); g.addWidget(self.gain,        2,3)
        g.addWidget(lbl("Black Level"),      3,0); g.addWidget(self.black_level, 3,1)
        g.addWidget(lbl("Throughput Limit"), 3,2); g.addWidget(self.throughput,  3,3)

    def dump(self):
        return {
            "serial": self.serial.text().strip(), "name": self.name_e.text().strip(),
            "chamber": self.chamber_e.text().strip(), "enabled": self.enabled.isChecked(),
            "exposure_us": self.exposure.value(), "gain_db": self.gain.value(),
            "black_level": self.black_level.value(), "throughput_limit": self.throughput.value(),
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

    def load(self, cameras_cfg):
        for c in self._cards.values():
            self._cl.removeWidget(c); c.deleteLater()
        self._cards.clear()
        for key, cfg in cameras_cfg.items():
            card = SingleCameraWidget(key, cfg)
            self._cards[key] = card
            self._cl.insertWidget(self._cl.count()-1, card)

    def dump(self):
        return {k: c.dump() for k, c in self._cards.items()}

    def camera_keys(self):
        return list(self._cards.keys())


# ---------------------------------------------------------------------------
# Tab: Chambers  (NEW — core of this update)
# ---------------------------------------------------------------------------
class SingleChamberWidget(QGroupBox):
    """
    One chamber card. Holds:
      - Camera assignment (dropdown of known camera keys)
      - Full TTL signature: port, baud, command, pin, polarity, poll interval, label
    """
    BAUD_OPTIONS = [9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600]

    def __init__(self, chamber_key: str, cfg: dict, camera_keys: list[str]):
        super().__init__(chamber_key)
        self._key = chamber_key
        outer = QVBoxLayout(self)

        # ---- Camera assignment ----
        cam_box = QGroupBox("Camera Assignment")
        cam_lay = QHBoxLayout(cam_box)
        self.camera_combo = QComboBox()
        for ck in camera_keys:
            self.camera_combo.addItem(ck)
        assigned = cfg.get("camera", "")
        idx = self.camera_combo.findText(assigned)
        if idx >= 0: self.camera_combo.setCurrentIndex(idx)
        cam_lay.addWidget(lbl("Mapped camera"))
        cam_lay.addWidget(self.camera_combo, 1)
        outer.addWidget(cam_box)

        # ---- TTL Signature ----
        ttl_cfg = cfg.get("ttl", {})
        ttl_box = QGroupBox("TTL Signature")
        g = QGridLayout(ttl_box); g.setSpacing(10)
        g.setColumnStretch(1,1); g.setColumnStretch(3,1)

        self.port  = QLineEdit(ttl_cfg.get("port","COM1"))
        self.port.setPlaceholderText("e.g. COM3 or /dev/ttyUSB0")

        self.baud  = QComboBox()
        for b in self.BAUD_OPTIONS: self.baud.addItem(str(b), b)
        bi = self.baud.findText(str(ttl_cfg.get("baud",115200)))
        if bi >= 0: self.baud.setCurrentIndex(bi)

        self.scan_btn = QPushButton("Scan")
        self.scan_btn.setFixedWidth(60)
        self.scan_btn.clicked.connect(self._scan)
        self._scan_lbl = QLabel("")
        self._scan_lbl.setObjectName("note")

        self.command = QSpinBox(); self.command.setRange(0,255)
        self.command.setValue(ttl_cfg.get("command",105))

        self.pin     = QSpinBox(); self.pin.setRange(0,3)
        self.pin.setValue(ttl_cfg.get("pin",0))

        self.polarity = QComboBox()
        self.polarity.addItem("Rising edge  (High / 1)", 1)
        self.polarity.addItem("Falling edge  (Low / 0)", 0)
        pval = ttl_cfg.get("polarity", 1)
        for i in range(self.polarity.count()):
            if self.polarity.itemData(i) == pval:
                self.polarity.setCurrentIndex(i); break

        self.poll_ms = QSpinBox(); self.poll_ms.setRange(1,1000); self.poll_ms.setSuffix(" ms")
        self.poll_ms.setValue(ttl_cfg.get("poll_interval_ms",10))

        self.label_e = QLineEdit(ttl_cfg.get("label", chamber_key))
        self.label_e.setPlaceholderText("Label written to CSV on each pulse")

        # Port row
        port_row = QHBoxLayout()
        port_row.addWidget(self.port); port_row.addWidget(self.scan_btn)
        port_widget = QWidget(); port_widget.setLayout(port_row)

        g.addWidget(lbl("Serial port"),      0,0); g.addWidget(port_widget,    0,1,1,3)
        g.addWidget(self._scan_lbl,          1,0,1,4)
        g.addWidget(lbl("Baud rate"),        2,0); g.addWidget(self.baud,      2,1)
        g.addWidget(lbl("Command number"),   2,2); g.addWidget(self.command,   2,3)
        g.addWidget(lbl("Pin  (0–3)"),       3,0); g.addWidget(self.pin,       3,1)
        g.addWidget(lbl("Trigger on"),       3,2); g.addWidget(self.polarity,  3,3)
        g.addWidget(lbl("Poll interval"),    4,0); g.addWidget(self.poll_ms,   4,1)
        g.addWidget(lbl("CSV event label"),  4,2); g.addWidget(self.label_e,   4,3)

        outer.addWidget(ttl_box)

        # Packet reference
        ref = QLabel(
            "Packet: [cmd U8][pin U8] → [cmd U8][state U8]    "
            "State: 0 = Low  1 = High\n"
            "Each pulse (while recording) is logged as a ttl_event row in the camera's CSV."
        )
        ref.setObjectName("note"); outer.addWidget(ref)

    def _scan(self):
        try:
            import serial.tools.list_ports
            ports = list(serial.tools.list_ports.comports())
            if ports:
                self._scan_lbl.setText("  " + "    ".join(p.device for p in sorted(ports)))
                if self.port.text() in ("COM1",""):
                    self.port.setText(sorted(ports)[0].device)
            else:
                self._scan_lbl.setText("  No ports found.")
        except ImportError:
            self._scan_lbl.setText("  pyserial not installed.")

    def dump(self) -> dict:
        return {
            "camera": self.camera_combo.currentText(),
            "ttl": {
                "port":             self.port.text().strip(),
                "baud":             int(self.baud.currentText()),
                "command":          self.command.value(),
                "pin":              self.pin.value(),
                "polarity":         self.polarity.currentData(),
                "poll_interval_ms": self.poll_ms.value(),
                "label":            self.label_e.text().strip(),
            },
        }


class ChambersTab(QWidget):
    """
    Dynamic list of chamber cards.
    Add / remove chambers. Each maps to one camera and has its own TTL config.
    """
    def __init__(self, cameras_tab: CamerasTab):
        super().__init__()
        self._cameras_tab = cameras_tab
        self._cards: dict[str, SingleChamberWidget] = {}

        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0)

        # Toolbar: Add chamber button
        toolbar = QWidget()
        tl = QHBoxLayout(toolbar); tl.setContentsMargins(12,8,12,4)
        self._new_key = QLineEdit(); self._new_key.setPlaceholderText("New chamber key, e.g. chamber_B")
        self._new_key.setFixedWidth(200)
        add_btn = QPushButton("+ Add Chamber"); add_btn.clicked.connect(self._add_chamber)
        tl.addWidget(lbl("Chamber key:")); tl.addWidget(self._new_key)
        tl.addWidget(add_btn); tl.addStretch()
        outer.addWidget(toolbar)

        self._cw = QWidget(); self._cl = QVBoxLayout(self._cw)
        self._cl.setSpacing(12); self._cl.setContentsMargins(20,8,20,20)
        self._cl.addStretch()
        outer.addWidget(scrollable(self._cw))

    def _camera_keys(self):
        return self._cameras_tab.camera_keys()

    def _add_chamber(self, key=None, cfg=None):
        if key is None:
            key = self._new_key.text().strip()
        if not key or key in self._cards:
            return
        cfg = cfg or {}
        card = SingleChamberWidget(key, cfg, self._camera_keys())

        # Remove button header
        header = QWidget(); hl = QHBoxLayout(header); hl.setContentsMargins(0,0,0,0)
        rem = QPushButton(f"✕  Remove {key}"); rem.setFixedWidth(160)
        rem.clicked.connect(lambda: self._remove_chamber(key))
        hl.addStretch(); hl.addWidget(rem)

        wrapper = QWidget(); wl = QVBoxLayout(wrapper); wl.setContentsMargins(0,0,0,0)
        wl.addWidget(header); wl.addWidget(card)
        wrapper.setObjectName(f"wrap_{key}")

        self._cards[key] = card
        self._cl.insertWidget(self._cl.count()-1, wrapper)
        self._new_key.clear()

    def _remove_chamber(self, key):
        if key not in self._cards: return
        card = self._cards.pop(key)
        # Find and remove the wrapper widget
        for i in range(self._cl.count()):
            item = self._cl.itemAt(i)
            if item and item.widget() and item.widget().objectName() == f"wrap_{key}":
                w = item.widget()
                self._cl.removeWidget(w)
                w.deleteLater()
                break

    def load(self, chambers_cfg: dict):
        for k in list(self._cards.keys()):
            self._remove_chamber(k)
        for key, cfg in chambers_cfg.items():
            self._add_chamber(key, cfg)

    def dump(self) -> dict:
        return {k: c.dump() for k, c in self._cards.items()}


# ---------------------------------------------------------------------------
# Tab: Recording / ROI / Auto-start
# ---------------------------------------------------------------------------
class RecordingTab(QWidget):
    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0)
        content = QWidget(); layout = QVBoxLayout(content)
        layout.setSpacing(16); layout.setContentsMargins(20,20,20,20)

        # Auto-start toggle — prominent at the top
        mode_box = QGroupBox("Acquisition Mode")
        mode_lay = QVBoxLayout(mode_box)
        self.auto_start = QCheckBox("Auto-start recording when script launches")
        self.auto_start.setChecked(False)
        mode_note = note(
            "ON  — recording begins immediately for all cameras on script launch.\n"
            "OFF — preview runs; recording starts via R key or incoming TTL pulse from a chamber."
        )
        mode_lay.addWidget(self.auto_start)
        mode_lay.addWidget(mode_note)
        layout.addWidget(mode_box)

        # Save path
        path_box = QGroupBox("Save Location")
        pl = QHBoxLayout(path_box)
        self.save_dir = QLineEdit(); self.save_dir.setPlaceholderText("./recordings")
        bb = QToolButton(); bb.setText("…"); bb.clicked.connect(self._browse_dir)
        pl.addWidget(self.save_dir); pl.addWidget(bb)
        layout.addWidget(path_box)

        # Recording settings
        rec_box = QGroupBox("Recording")
        rg = QGridLayout(rec_box); rg.setSpacing(10)
        rg.setColumnStretch(1,1); rg.setColumnStretch(3,1)
        self.fps = QDoubleSpinBox(); self.fps.setRange(1,500); self.fps.setDecimals(2)
        self.fps.setSuffix(" Hz"); self.fps.setValue(59.99)
        self.jpeg_quality = QSpinBox(); self.jpeg_quality.setRange(0,100)
        self.jpeg_quality.setSuffix("%"); self.jpeg_quality.setValue(90)
        self.split_size = QSpinBox(); self.split_size.setRange(0,100_000)
        self.split_size.setSuffix(" MB"); self.split_size.setSpecialValueText("No splitting")
        rg.addWidget(lbl("Frame Rate"),   0,0); rg.addWidget(self.fps,           0,1)
        rg.addWidget(lbl("JPEG Quality"), 0,2); rg.addWidget(self.jpeg_quality,  0,3)
        rg.addWidget(lbl("Split Size"),   1,0); rg.addWidget(self.split_size,    1,1)
        layout.addWidget(rec_box)

        # ROI
        roi_box = QGroupBox("Region of Interest (ROI)")
        roig = QGridLayout(roi_box); roig.setSpacing(10)
        roig.setColumnStretch(1,1); roig.setColumnStretch(3,1)
        self.roi_w = QSpinBox(); self.roi_w.setRange(0,8192); self.roi_w.setSuffix(" px")
        self.roi_h = QSpinBox(); self.roi_h.setRange(0,8192); self.roi_h.setSuffix(" px")
        self.roi_ox = QSpinBox(); self.roi_ox.setRange(0,8192); self.roi_ox.setSuffix(" px")
        self.roi_oy = QSpinBox(); self.roi_oy.setRange(0,8192); self.roi_oy.setSuffix(" px")
        roig.addWidget(lbl("Width"),    0,0); roig.addWidget(self.roi_w,  0,1)
        roig.addWidget(lbl("Height"),   0,2); roig.addWidget(self.roi_h,  0,3)
        roig.addWidget(lbl("Offset X"), 1,0); roig.addWidget(self.roi_ox, 1,1)
        roig.addWidget(lbl("Offset Y"), 1,2); roig.addWidget(self.roi_oy, 1,3)
        roig.addWidget(note("Offset auto-centered when Width/Height set."), 2,0,1,4)
        layout.addWidget(roi_box)

        # Preview
        prev_box = QGroupBox("Live Preview")
        pg = QGridLayout(prev_box); pg.setSpacing(10); pg.setColumnStretch(1,1)
        self.preview_enabled    = QCheckBox("Show preview window"); self.preview_enabled.setChecked(True)
        self.preview_downsample = QSpinBox(); self.preview_downsample.setRange(1,60)
        self.preview_downsample.setSuffix("  (show every Nth frame)"); self.preview_downsample.setValue(1)
        pg.addWidget(self.preview_enabled,          0,0,1,2)
        pg.addWidget(lbl("Downsample Preview"),     1,0)
        pg.addWidget(self.preview_downsample,       1,1)
        layout.addWidget(prev_box)
        layout.addStretch()
        outer.addWidget(scrollable(content))

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select Save Directory")
        if d: self.save_dir.setText(d)

    def load(self, config):
        self.save_dir.setText(config.get("save_dir","./recordings"))
        acq = config.get("acquisition", {})
        self.auto_start.setChecked(acq.get("auto_start", False))
        rec = config.get("recording", {})
        self.fps.setValue(rec.get("fps",59.99))
        self.jpeg_quality.setValue(rec.get("jpeg_quality",90))
        self.split_size.setValue(rec.get("split_size_mb",0) or 0)
        roi = config.get("roi", {})
        self.roi_w.setValue(roi.get("width",1020) or 0)
        self.roi_h.setValue(roi.get("height",1020) or 0)
        self.roi_ox.setValue(roi.get("offset_x",0))
        self.roi_oy.setValue(roi.get("offset_y",0))
        prev = config.get("preview", {})
        self.preview_enabled.setChecked(prev.get("enabled",True))
        self.preview_downsample.setValue(prev.get("downsample",1))

    def dump(self):
        sv = self.split_size.value()
        return {
            "save_dir":   self.save_dir.text().strip() or "./recordings",
            "acquisition": {"auto_start": self.auto_start.isChecked()},
            "recording":  {"fps": self.fps.value(), "jpeg_quality": self.jpeg_quality.value(),
                           "split_size_mb": sv if sv > 0 else None},
            "roi":        {"width": self.roi_w.value() or None, "height": self.roi_h.value() or None,
                           "offset_x": self.roi_ox.value(), "offset_y": self.roi_oy.value()},
            "preview":    {"enabled": self.preview_enabled.isChecked(),
                           "downsample": self.preview_downsample.value()},
        }


# ---------------------------------------------------------------------------
# Tab: Camera Trigger & Metadata CSV flags
# ---------------------------------------------------------------------------
class TriggerTab(QWidget):
    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self); outer.setContentsMargins(0,0,0,0)
        content = QWidget(); layout = QVBoxLayout(content)
        layout.setSpacing(16); layout.setContentsMargins(20,20,20,20)

        trig_box = QGroupBox("Hardware TTL Trigger (Camera GPIO — optional)")
        tg = QGridLayout(trig_box); tg.setSpacing(10)
        tg.setColumnStretch(1,1); tg.setColumnStretch(3,1)
        self.trigger_enabled    = QCheckBox("Enable camera hardware trigger")
        self.trigger_line       = QComboBox(); self.trigger_line.addItems(["Line0","Line1","Line2","Line3"])
        self.trigger_activation = QComboBox()
        self.trigger_activation.addItems(["RisingEdge","FallingEdge","AnyEdge","LevelHigh","LevelLow"])
        self.trigger_selector   = QComboBox()
        self.trigger_selector.addItems(["AcquisitionStart","FrameStart","FrameBurstStart"])
        self.trigger_timeout    = QSpinBox(); self.trigger_timeout.setRange(100,60000)
        self.trigger_timeout.setSuffix(" ms"); self.trigger_timeout.setValue(5000)
        tg.addWidget(self.trigger_enabled,          0,0,1,4)
        tg.addWidget(lbl("GPIO Line"),              1,0); tg.addWidget(self.trigger_line,       1,1)
        tg.addWidget(lbl("Activation"),             1,2); tg.addWidget(self.trigger_activation, 1,3)
        tg.addWidget(lbl("Trigger Selector"),       2,0); tg.addWidget(self.trigger_selector,   2,1)
        tg.addWidget(lbl("Timeout"),                2,2); tg.addWidget(self.trigger_timeout,    2,3)
        layout.addWidget(trig_box)

        meta_box = QGroupBox("Per-frame Metadata CSV Columns")
        mg = QGridLayout(meta_box); mg.setSpacing(8)
        self.meta_enabled    = QCheckBox("Save per-frame timestamps CSV")
        self.meta_framecount = QCheckBox("Frame count")
        self.meta_timestamp  = QCheckBox("Camera hardware timestamp")
        self.meta_sestime    = QCheckBox("Session time (perf_counter)")
        self.meta_cputime    = QCheckBox("CPU wall clock (time.time)")
        for cb in [self.meta_enabled, self.meta_framecount, self.meta_timestamp,
                   self.meta_sestime, self.meta_cputime]:
            cb.setChecked(True)
        mg.addWidget(self.meta_enabled,    0,0,1,2)
        mg.addWidget(self.meta_framecount, 1,0); mg.addWidget(self.meta_timestamp, 1,1)
        mg.addWidget(self.meta_sestime,    2,0); mg.addWidget(self.meta_cputime,   2,1)
        mg.addWidget(note(
            "CSV rows have a row_type column: 'frame' for normal frames, "
            "'ttl_event' for chamber pulses (with ttl_chamber and ttl_label columns)."
        ), 3,0,1,2)
        layout.addWidget(meta_box)
        layout.addStretch()
        outer.addWidget(scrollable(content))

    def load(self, config):
        t = config.get("trigger", {})
        self.trigger_enabled.setChecked(t.get("enabled",False))
        for combo, key, default in [
            (self.trigger_line,       "line",       "Line0"),
            (self.trigger_activation, "activation", "RisingEdge"),
            (self.trigger_selector,   "selector",   "AcquisitionStart"),
        ]:
            idx = combo.findText(t.get(key, default))
            combo.setCurrentIndex(max(0, idx))
        self.trigger_timeout.setValue(t.get("timeout_ms",5000))
        m = config.get("metadata", {})
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
        self.resize(980, 800)
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
        header = QWidget()
        header.setStyleSheet(f"background:{PANEL_BG}; border-bottom:1px solid {BORDER};")
        header.setFixedHeight(56)
        hl = QHBoxLayout(header); hl.setContentsMargins(16,8,16,8)
        title = QLabel("◈  ACQUISITION CONFIG")
        title.setStyleSheet(f"color:{ACCENT}; font-size:15px; font-weight:bold; letter-spacing:3px;")

        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("No config loaded")
        self._path_edit.setReadOnly(True); self._path_edit.setFixedWidth(420)

        browse_btn = QPushButton("Browse…"); browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse_config)

        save_btn = QPushButton("Save"); save_btn.setObjectName("success")
        save_btn.setFixedWidth(70); save_btn.clicked.connect(self._save)
        saveas_btn = QPushButton("Save As…"); saveas_btn.setFixedWidth(90)
        saveas_btn.clicked.connect(self._save_as)

        hl.addWidget(title); hl.addStretch()
        hl.addWidget(QLabel("Config:")); hl.addWidget(self._path_edit)
        hl.addWidget(browse_btn); hl.addSpacing(8)
        hl.addWidget(save_btn); hl.addWidget(saveas_btn)
        ml.addWidget(header)

        # Tabs
        self.tabs = QTabWidget(); self.tabs.setDocumentMode(True)
        self.meta_tab     = MetadataTab()
        self.cameras_tab  = CamerasTab()
        self.chambers_tab = ChambersTab(self.cameras_tab)
        self.recording_tab = RecordingTab()
        self.trigger_tab  = TriggerTab()

        self.tabs.addTab(self.meta_tab,      "🧪  Experiment")
        self.tabs.addTab(self.cameras_tab,   "📷  Cameras")
        self.tabs.addTab(self.chambers_tab,  "🏠  Chambers & TTL")
        self.tabs.addTab(self.recording_tab, "🎬  Recording / ROI")
        self.tabs.addTab(self.trigger_tab,   "⚡  Trigger / CSV")
        ml.addWidget(self.tabs, 1)

        self.status_bar = QStatusBar(); self.setStatusBar(self.status_bar)
        self._status("No config loaded.")

    def _try_auto_discover(self):
        yamls = sorted(glob.glob("*.yaml") + glob.glob("*.yml"))
        if len(yamls) == 1: self._load_file(yamls[0])
        elif len(yamls) > 1:
            self._status(f"Multiple YAMLs found: {', '.join(yamls[:4])} — use Browse.", WARN)

    def _browse_config(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open Config", os.getcwd(),
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

        self.meta_tab.load(data.get("experiment_metadata", {}))
        self.cameras_tab.load(data.get("cameras", {}))
        # Chambers loaded after cameras so dropdown is populated
        self.chambers_tab.load(data.get("chambers", {}))
        self.recording_tab.load(data)
        self.trigger_tab.load(data)

        self._dirty = False
        self._status(f"Loaded: {os.path.abspath(path)}", ACCENT2)
        self.setWindowTitle(f"Config Editor — {os.path.basename(path)}")

    def _collect(self):
        rec = self.recording_tab.dump()
        trig = self.trigger_tab.dump()
        merged = dict(self._config_data)
        merged["save_dir"]            = rec.pop("save_dir")
        merged["acquisition"]         = rec["acquisition"]
        merged["cameras"]             = self.cameras_tab.dump()
        merged["chambers"]            = self.chambers_tab.dump()
        merged["recording"]           = rec["recording"]
        merged["roi"]                 = rec["roi"]
        merged["preview"]             = rec["preview"]
        merged["trigger"]             = trig["trigger"]
        merged["metadata"]            = trig["metadata"]
        merged["experiment_metadata"] = self.meta_tab.dump()
        return merged

    def _save(self):
        if not self._config_path: self._save_as(); return
        self._write(self._config_path)

    def _save_as(self):
        p, _ = QFileDialog.getSaveFileName(self, "Save As", os.getcwd(), "YAML (*.yaml);;All (*)")
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
            r = QMessageBox.question(self, "Unsaved Changes", "Save before closing?",
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

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
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
