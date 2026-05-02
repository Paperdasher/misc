"""
config_gui.py — Camera Acquisition Configuration GUI
=====================================================
A PyQt5 interface for loading, editing, and saving camera acquisition configs.

Usage
-----
    python config_gui.py                     # opens file browser to pick a config
    python config_gui.py -c config.yaml      # opens with a specific config
    python config_gui.py --dir /path/to/dir  # searches that directory for *.yaml files

Features
--------
  • Browse for / auto-discover YAML configs in the project folder
  • Edit all camera, recording, ROI, trigger and metadata settings
  • Full experiment + animal metadata panel (experimenter, animal ID, genotype, etc.)
  • Per-camera chamber assignment
  • Live validation (required fields highlighted)
  • Save back to the same file or Save As
"""

import sys
import os
import argparse
import glob
from datetime import date

import yaml

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QLabel, QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox,
    QComboBox, QPushButton, QFileDialog, QTextEdit,
    QGroupBox, QScrollArea, QFrame, QSizePolicy, QMessageBox,
    QToolButton, QSplitter, QStatusBar,
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QFont, QColor, QPalette, QIcon


# ---------------------------------------------------------------------------
# Palette & style constants
# ---------------------------------------------------------------------------

DARK_BG    = "#1a1d23"
PANEL_BG   = "#22262f"
CARD_BG    = "#2b303b"
ACCENT     = "#4f8ef7"
ACCENT2    = "#5ecfa8"
WARN       = "#f7934f"
TEXT_PRI   = "#e8eaf0"
TEXT_SEC   = "#8a90a0"
BORDER     = "#363c4a"
INPUT_BG   = "#1e222a"
BTN_BG     = "#333844"
BTN_HOVER  = "#404759"

STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {DARK_BG};
    color: {TEXT_PRI};
    font-family: 'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace;
    font-size: 13px;
}}

QTabWidget::pane {{
    border: 1px solid {BORDER};
    background-color: {PANEL_BG};
    border-radius: 4px;
}}

QTabBar::tab {{
    background-color: {CARD_BG};
    color: {TEXT_SEC};
    padding: 8px 20px;
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
    font-size: 12px;
    letter-spacing: 0.5px;
}}

QTabBar::tab:selected {{
    background-color: {ACCENT};
    color: white;
    font-weight: bold;
}}

QTabBar::tab:hover:!selected {{
    background-color: {BTN_HOVER};
    color: {TEXT_PRI};
}}

QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 14px;
    padding-top: 10px;
    background-color: {CARD_BG};
    font-size: 11px;
    color: {TEXT_SEC};
    letter-spacing: 1px;
    text-transform: uppercase;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 8px;
    left: 12px;
    top: -2px;
}}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit {{
    background-color: {INPUT_BG};
    color: {TEXT_PRI};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
}}

QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QComboBox:focus, QTextEdit:focus {{
    border: 1px solid {ACCENT};
}}

QLineEdit[required="true"] {{
    border: 1px solid {WARN};
}}

QComboBox::drop-down {{
    border: none;
    width: 24px;
}}

QComboBox QAbstractItemView {{
    background-color: {CARD_BG};
    color: {TEXT_PRI};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
}}

QPushButton {{
    background-color: {BTN_BG};
    color: {TEXT_PRI};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 7px 18px;
    font-size: 12px;
}}

QPushButton:hover {{
    background-color: {BTN_HOVER};
    border-color: {ACCENT};
}}

QPushButton:pressed {{
    background-color: {ACCENT};
    color: white;
}}

QPushButton#primary {{
    background-color: {ACCENT};
    color: white;
    border: none;
    font-weight: bold;
}}

QPushButton#primary:hover {{
    background-color: #6fa3ff;
}}

QPushButton#success {{
    background-color: {ACCENT2};
    color: #111;
    border: none;
    font-weight: bold;
}}

QPushButton#success:hover {{
    background-color: #7de0ba;
}}

QPushButton#danger {{
    background-color: {WARN};
    color: #111;
    border: none;
}}

QCheckBox {{
    color: {TEXT_PRI};
    spacing: 8px;
}}

QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background-color: {INPUT_BG};
}}

QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    image: url("");
}}

QScrollArea {{
    border: none;
    background-color: transparent;
}}

QScrollBar:vertical {{
    background: {DARK_BG};
    width: 8px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 4px;
    min-height: 20px;
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QLabel#section_title {{
    color: {ACCENT};
    font-size: 11px;
    letter-spacing: 2px;
    text-transform: uppercase;
    font-weight: bold;
    padding-bottom: 4px;
}}

QLabel#field_label {{
    color: {TEXT_SEC};
    font-size: 12px;
}}

QFrame#divider {{
    color: {BORDER};
}}

QStatusBar {{
    background-color: {CARD_BG};
    color: {TEXT_SEC};
    border-top: 1px solid {BORDER};
    font-size: 11px;
}}

QToolButton {{
    background-color: {BTN_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    color: {TEXT_SEC};
}}

QToolButton:hover {{
    background-color: {BTN_HOVER};
    color: {TEXT_PRI};
}}
"""


# ---------------------------------------------------------------------------
# Helper widgets
# ---------------------------------------------------------------------------

def make_label(text: str, obj_name: str = "field_label") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName(obj_name)
    return lbl


def make_section_title(text: str) -> QLabel:
    lbl = QLabel(text.upper())
    lbl.setObjectName("section_title")
    return lbl


def hline() -> QFrame:
    f = QFrame()
    f.setObjectName("divider")
    f.setFrameShape(QFrame.HLine)
    f.setFrameShadow(QFrame.Sunken)
    return f


def scrollable(widget: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidget(widget)
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    return sa


# ---------------------------------------------------------------------------
# File picker bar
# ---------------------------------------------------------------------------

class FilePicker(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("No config loaded — browse or drag a .yaml file here")
        self.path_edit.setReadOnly(True)

        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.setFixedWidth(90)

        layout.addWidget(QLabel("Config:"))
        layout.addWidget(self.path_edit, 1)
        layout.addWidget(self.browse_btn)


# ---------------------------------------------------------------------------
# Tab: Experiment & Animal Metadata
# ---------------------------------------------------------------------------

class MetadataTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        content = QWidget()
        layout  = QVBoxLayout(content)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)

        # ---- Session identity ----
        id_box = QGroupBox("Session Identity")
        id_grid = QGridLayout(id_box)
        id_grid.setSpacing(10)
        id_grid.setColumnStretch(1, 1)
        id_grid.setColumnStretch(3, 1)

        self.experimenter = QLineEdit()
        self.experimenter.setPlaceholderText("e.g. Harley")
        self.experiment_name = QLineEdit()
        self.experiment_name.setPlaceholderText("e.g. Sema6aKD_cohort1")
        self.session_date = QLineEdit(date.today().strftime("%Y-%m-%d"))
        self.schedule_name = QLineEdit()
        self.schedule_name.setPlaceholderText("e.g. FR5_extinction")

        id_grid.addWidget(make_label("Experimenter"),   0, 0)
        id_grid.addWidget(self.experimenter,            0, 1)
        id_grid.addWidget(make_label("Experiment Name"),0, 2)
        id_grid.addWidget(self.experiment_name,         0, 3)
        id_grid.addWidget(make_label("Date"),           1, 0)
        id_grid.addWidget(self.session_date,            1, 1)
        id_grid.addWidget(make_label("Schedule Name"),  1, 2)
        id_grid.addWidget(self.schedule_name,           1, 3)

        layout.addWidget(id_box)

        # ---- Animal info ----
        animal_box = QGroupBox("Animal")
        animal_grid = QGridLayout(animal_box)
        animal_grid.setSpacing(10)
        animal_grid.setColumnStretch(1, 1)
        animal_grid.setColumnStretch(3, 1)

        self.animal_id = QLineEdit()
        self.animal_id.setPlaceholderText("e.g. M123")
        self.genotype = QLineEdit()
        self.genotype.setPlaceholderText("e.g. WT, HET, KO")
        self.group = QLineEdit()
        self.group.setPlaceholderText("e.g. control, treatment")

        animal_grid.addWidget(make_label("Animal ID"), 0, 0)
        animal_grid.addWidget(self.animal_id,          0, 1)
        animal_grid.addWidget(make_label("Genotype"),  0, 2)
        animal_grid.addWidget(self.genotype,           0, 3)
        animal_grid.addWidget(make_label("Group"),     1, 0)
        animal_grid.addWidget(self.group,              1, 1)

        layout.addWidget(animal_box)

        # ---- Co-recording paths ----
        corecord_box = QGroupBox("Co-recordings & Paths")
        corecord_grid = QGridLayout(corecord_box)
        corecord_grid.setSpacing(10)
        corecord_grid.setColumnStretch(1, 1)

        self.eeg_path = QLineEdit()
        self.eeg_path.setPlaceholderText("Path to EEG / fiber photometry data file (optional)")
        eeg_browse = QToolButton()
        eeg_browse.setText("…")
        eeg_browse.clicked.connect(lambda: self._browse_file(self.eeg_path))

        eeg_row = QHBoxLayout()
        eeg_row.addWidget(self.eeg_path)
        eeg_row.addWidget(eeg_browse)

        corecord_grid.addWidget(make_label("EEG / Fiber Photometry Path"), 0, 0)
        corecord_grid.addLayout(eeg_row, 0, 1)

        layout.addWidget(corecord_box)

        # ---- Notes ----
        notes_box = QGroupBox("Notes")
        notes_layout = QVBoxLayout(notes_box)
        self.notes = QTextEdit()
        self.notes.setPlaceholderText("Any session notes, deviations from protocol, animal behaviour observations…")
        self.notes.setFixedHeight(100)
        notes_layout.addWidget(self.notes)
        layout.addWidget(notes_box)

        layout.addStretch()
        outer.addWidget(scrollable(content))

    def _browse_file(self, target: QLineEdit):
        path, _ = QFileDialog.getOpenFileName(self, "Select File", "", "All Files (*)")
        if path:
            target.setText(path)

    # ------------------------------------------------------------------
    def load(self, meta: dict):
        self.experimenter.setText(meta.get("experimenter_name", ""))
        self.experiment_name.setText(meta.get("experiment_name", ""))
        self.animal_id.setText(meta.get("animal_id", ""))
        self.genotype.setText(meta.get("genotype", ""))
        self.group.setText(meta.get("group", ""))
        self.schedule_name.setText(meta.get("schedule_name", ""))
        self.eeg_path.setText(meta.get("eeg_fiber_photometry_path", ""))
        self.notes.setPlainText(meta.get("notes", ""))

    def dump(self) -> dict:
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
    """Editable card for one camera entry."""

    def __init__(self, cam_key: str, cfg: dict, parent=None):
        super().__init__(cam_key, parent)
        self.cam_key = cam_key
        grid = QGridLayout(self)
        grid.setSpacing(10)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self.serial   = QLineEdit(str(cfg.get("serial", "")))
        self.name_e   = QLineEdit(str(cfg.get("name", cam_key)))
        self.chamber  = QLineEdit(str(cfg.get("chamber", "")))
        self.chamber.setPlaceholderText("e.g. A1, box3")
        self.enabled  = QCheckBox("Enabled")
        self.enabled.setChecked(cfg.get("enabled", True))

        self.exposure = QSpinBox()
        self.exposure.setRange(1, 15000)
        self.exposure.setSuffix(" µs")
        self.exposure.setValue(cfg.get("exposure_us", 14000))

        self.gain = QDoubleSpinBox()
        self.gain.setRange(0, 48)
        self.gain.setSuffix(" dB")
        self.gain.setValue(cfg.get("gain_db", 10))

        self.black_level = QDoubleSpinBox()
        self.black_level.setRange(0, 32)
        self.black_level.setDecimals(1)
        self.black_level.setValue(cfg.get("black_level", 2.0))

        self.throughput = QSpinBox()
        self.throughput.setRange(1_000_000, 300_000_000)
        self.throughput.setSingleStep(1_000_000)
        self.throughput.setSuffix(" bps")
        self.throughput.setValue(cfg.get("throughput_limit", 90_000_000))

        grid.addWidget(make_label("Serial"),          0, 0)
        grid.addWidget(self.serial,                   0, 1)
        grid.addWidget(make_label("Friendly Name"),   0, 2)
        grid.addWidget(self.name_e,                   0, 3)

        grid.addWidget(make_label("Chamber / Arena"), 1, 0)
        grid.addWidget(self.chamber,                  1, 1)
        grid.addWidget(self.enabled,                  1, 2, 1, 2)

        grid.addWidget(make_label("Exposure"),        2, 0)
        grid.addWidget(self.exposure,                 2, 1)
        grid.addWidget(make_label("Gain"),            2, 2)
        grid.addWidget(self.gain,                     2, 3)

        grid.addWidget(make_label("Black Level"),     3, 0)
        grid.addWidget(self.black_level,              3, 1)
        grid.addWidget(make_label("Throughput Limit"),3, 2)
        grid.addWidget(self.throughput,               3, 3)

    def dump(self) -> dict:
        return {
            "serial":           self.serial.text().strip(),
            "name":             self.name_e.text().strip(),
            "chamber":          self.chamber.text().strip(),
            "enabled":          self.enabled.isChecked(),
            "exposure_us":      self.exposure.value(),
            "gain_db":          self.gain.value(),
            "black_level":      self.black_level.value(),
            "throughput_limit": self.throughput.value(),
        }


class CamerasTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards: dict[str, SingleCameraWidget] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._content_w  = QWidget()
        self._content_layout = QVBoxLayout(self._content_w)
        self._content_layout.setSpacing(12)
        self._content_layout.setContentsMargins(20, 20, 20, 20)
        self._content_layout.addStretch()

        outer.addWidget(scrollable(self._content_w))

    def load(self, cameras_cfg: dict):
        # Remove old cards
        for card in self._cards.values():
            self._content_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

        stretch_item = self._content_layout.itemAt(self._content_layout.count() - 1)

        for key, cfg in cameras_cfg.items():
            card = SingleCameraWidget(key, cfg)
            self._cards[key] = card
            # Insert before the stretch
            self._content_layout.insertWidget(self._content_layout.count() - 1, card)

    def dump(self) -> dict:
        return {key: card.dump() for key, card in self._cards.items()}


# ---------------------------------------------------------------------------
# Tab: Recording & ROI
# ---------------------------------------------------------------------------

class RecordingTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        content = QWidget()
        layout  = QVBoxLayout(content)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)

        # ---- Save path ----
        path_box = QGroupBox("Save Location")
        path_layout = QHBoxLayout(path_box)
        self.save_dir = QLineEdit()
        self.save_dir.setPlaceholderText("./recordings")
        browse_btn = QToolButton()
        browse_btn.setText("…")
        browse_btn.clicked.connect(self._browse_dir)
        path_layout.addWidget(self.save_dir)
        path_layout.addWidget(browse_btn)
        layout.addWidget(path_box)

        # ---- Recording settings ----
        rec_box  = QGroupBox("Recording")
        rec_grid = QGridLayout(rec_box)
        rec_grid.setSpacing(10)
        rec_grid.setColumnStretch(1, 1)
        rec_grid.setColumnStretch(3, 1)

        self.fps = QDoubleSpinBox()
        self.fps.setRange(1, 500)
        self.fps.setDecimals(2)
        self.fps.setSuffix(" Hz")
        self.fps.setValue(59.99)

        self.jpeg_quality = QSpinBox()
        self.jpeg_quality.setRange(0, 100)
        self.jpeg_quality.setSuffix("%")
        self.jpeg_quality.setValue(90)

        self.split_size = QSpinBox()
        self.split_size.setRange(0, 100_000)
        self.split_size.setSuffix(" MB")
        self.split_size.setSpecialValueText("No splitting")
        self.split_size.setValue(0)

        rec_grid.addWidget(make_label("Frame Rate"),    0, 0)
        rec_grid.addWidget(self.fps,                    0, 1)
        rec_grid.addWidget(make_label("JPEG Quality"),  0, 2)
        rec_grid.addWidget(self.jpeg_quality,           0, 3)
        rec_grid.addWidget(make_label("Split Size"),    1, 0)
        rec_grid.addWidget(self.split_size,             1, 1)

        layout.addWidget(rec_box)

        # ---- ROI ----
        roi_box  = QGroupBox("Region of Interest (ROI)")
        roi_grid = QGridLayout(roi_box)
        roi_grid.setSpacing(10)
        roi_grid.setColumnStretch(1, 1)
        roi_grid.setColumnStretch(3, 1)

        self.roi_width  = QSpinBox(); self.roi_width.setRange(0, 8192);  self.roi_width.setSuffix(" px")
        self.roi_height = QSpinBox(); self.roi_height.setRange(0, 8192); self.roi_height.setSuffix(" px")
        self.roi_ox     = QSpinBox(); self.roi_ox.setRange(0, 8192);     self.roi_ox.setSuffix(" px")
        self.roi_oy     = QSpinBox(); self.roi_oy.setRange(0, 8192);     self.roi_oy.setSuffix(" px")

        roi_note = QLabel("Offset is auto-centered when Width/Height are set. Override only if needed.")
        roi_note.setStyleSheet(f"color: {TEXT_SEC}; font-size: 11px;")
        roi_note.setWordWrap(True)

        roi_grid.addWidget(make_label("Width"),    0, 0)
        roi_grid.addWidget(self.roi_width,         0, 1)
        roi_grid.addWidget(make_label("Height"),   0, 2)
        roi_grid.addWidget(self.roi_height,        0, 3)
        roi_grid.addWidget(make_label("Offset X"), 1, 0)
        roi_grid.addWidget(self.roi_ox,            1, 1)
        roi_grid.addWidget(make_label("Offset Y"), 1, 2)
        roi_grid.addWidget(self.roi_oy,            1, 3)
        roi_grid.addWidget(roi_note,               2, 0, 1, 4)

        layout.addWidget(roi_box)

        # ---- Preview ----
        prev_box  = QGroupBox("Live Preview")
        prev_grid = QGridLayout(prev_box)
        prev_grid.setSpacing(10)
        prev_grid.setColumnStretch(1, 1)

        self.preview_enabled    = QCheckBox("Show preview window")
        self.preview_enabled.setChecked(True)
        self.preview_downsample = QSpinBox()
        self.preview_downsample.setRange(1, 60)
        self.preview_downsample.setSuffix("  (show every Nth frame)")
        self.preview_downsample.setValue(1)

        prev_grid.addWidget(self.preview_enabled,                0, 0, 1, 2)
        prev_grid.addWidget(make_label("Downsample Preview"),    1, 0)
        prev_grid.addWidget(self.preview_downsample,             1, 1)

        layout.addWidget(prev_box)
        layout.addStretch()

        outer.addWidget(scrollable(content))

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select Save Directory")
        if d:
            self.save_dir.setText(d)

    def load(self, config: dict):
        self.save_dir.setText(config.get("save_dir", "./recordings"))
        rec = config.get("recording", {})
        self.fps.setValue(rec.get("fps", 59.99))
        self.jpeg_quality.setValue(rec.get("jpeg_quality", 90))
        split = rec.get("split_size_mb", None)
        self.split_size.setValue(split if split is not None else 0)

        roi = config.get("roi", {})
        self.roi_width.setValue(roi.get("width", 1020) or 0)
        self.roi_height.setValue(roi.get("height", 1020) or 0)
        self.roi_ox.setValue(roi.get("offset_x", 0))
        self.roi_oy.setValue(roi.get("offset_y", 0))

        prev = config.get("preview", {})
        self.preview_enabled.setChecked(prev.get("enabled", True))
        self.preview_downsample.setValue(prev.get("downsample", 1))

    def dump(self) -> dict:
        split_val = self.split_size.value()
        return {
            "save_dir": self.save_dir.text().strip() or "./recordings",
            "recording": {
                "fps":           self.fps.value(),
                "jpeg_quality":  self.jpeg_quality.value(),
                "split_size_mb": split_val if split_val > 0 else None,
            },
            "roi": {
                "width":    self.roi_width.value()  or None,
                "height":   self.roi_height.value() or None,
                "offset_x": self.roi_ox.value(),
                "offset_y": self.roi_oy.value(),
            },
            "preview": {
                "enabled":    self.preview_enabled.isChecked(),
                "downsample": self.preview_downsample.value(),
            },
        }


# ---------------------------------------------------------------------------
# Tab: Trigger & Metadata flags
# ---------------------------------------------------------------------------

class TriggerTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        content = QWidget()
        layout  = QVBoxLayout(content)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)

        # ---- TTL Trigger ----
        trig_box  = QGroupBox("Hardware TTL Trigger")
        trig_grid = QGridLayout(trig_box)
        trig_grid.setSpacing(10)
        trig_grid.setColumnStretch(1, 1)
        trig_grid.setColumnStretch(3, 1)

        self.trigger_enabled = QCheckBox("Enable hardware trigger")
        self.trigger_line    = QComboBox()
        self.trigger_line.addItems(["Line0", "Line1", "Line2", "Line3"])
        self.trigger_activation = QComboBox()
        self.trigger_activation.addItems(["RisingEdge", "FallingEdge", "AnyEdge", "LevelHigh", "LevelLow"])
        self.trigger_selector = QComboBox()
        self.trigger_selector.addItems(["AcquisitionStart", "FrameStart", "FrameBurstStart"])
        self.trigger_timeout = QSpinBox()
        self.trigger_timeout.setRange(100, 60_000)
        self.trigger_timeout.setSuffix(" ms")
        self.trigger_timeout.setValue(5000)

        trig_grid.addWidget(self.trigger_enabled,               0, 0, 1, 4)
        trig_grid.addWidget(make_label("GPIO Line"),            1, 0)
        trig_grid.addWidget(self.trigger_line,                  1, 1)
        trig_grid.addWidget(make_label("Activation"),           1, 2)
        trig_grid.addWidget(self.trigger_activation,            1, 3)
        trig_grid.addWidget(make_label("Trigger Selector"),     2, 0)
        trig_grid.addWidget(self.trigger_selector,              2, 1)
        trig_grid.addWidget(make_label("Timeout"),              2, 2)
        trig_grid.addWidget(self.trigger_timeout,               2, 3)

        layout.addWidget(trig_box)

        # ---- Metadata CSV flags ----
        meta_box  = QGroupBox("Per-frame Metadata CSV")
        meta_grid = QGridLayout(meta_box)
        meta_grid.setSpacing(8)

        self.meta_enabled    = QCheckBox("Save per-frame timestamps CSV")
        self.meta_framecount = QCheckBox("Frame count")
        self.meta_timestamp  = QCheckBox("Camera hardware timestamp")
        self.meta_sestime    = QCheckBox("Session time (perf_counter)")
        self.meta_cputime    = QCheckBox("CPU wall clock (time.time)")

        for cb in [self.meta_enabled, self.meta_framecount, self.meta_timestamp,
                   self.meta_sestime, self.meta_cputime]:
            cb.setChecked(True)

        meta_grid.addWidget(self.meta_enabled,    0, 0, 1, 2)
        meta_grid.addWidget(self.meta_framecount, 1, 0)
        meta_grid.addWidget(self.meta_timestamp,  1, 1)
        meta_grid.addWidget(self.meta_sestime,    2, 0)
        meta_grid.addWidget(self.meta_cputime,    2, 1)

        layout.addWidget(meta_box)
        layout.addStretch()

        outer.addWidget(scrollable(content))

    def load(self, config: dict):
        trig = config.get("trigger", {})
        self.trigger_enabled.setChecked(trig.get("enabled", False))
        idx = self.trigger_line.findText(trig.get("line", "Line0"))
        self.trigger_line.setCurrentIndex(max(0, idx))
        idx = self.trigger_activation.findText(trig.get("activation", "RisingEdge"))
        self.trigger_activation.setCurrentIndex(max(0, idx))
        idx = self.trigger_selector.findText(trig.get("selector", "AcquisitionStart"))
        self.trigger_selector.setCurrentIndex(max(0, idx))
        self.trigger_timeout.setValue(trig.get("timeout_ms", 5000))

        meta = config.get("metadata", {})
        self.meta_enabled.setChecked(meta.get("enabled", True))
        self.meta_framecount.setChecked(meta.get("save_framecount", True))
        self.meta_timestamp.setChecked(meta.get("save_timestamp", True))
        self.meta_sestime.setChecked(meta.get("save_sestime", True))
        self.meta_cputime.setChecked(meta.get("save_cputime", True))

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
                "enabled":          self.meta_enabled.isChecked(),
                "save_framecount":  self.meta_framecount.isChecked(),
                "save_timestamp":   self.meta_timestamp.isChecked(),
                "save_sestime":     self.meta_sestime.isChecked(),
                "save_cputime":     self.meta_cputime.isChecked(),
            },
        }


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class ConfigEditor(QMainWindow):
    def __init__(self, initial_path: str = None):
        super().__init__()
        self.setWindowTitle("Camera Acquisition — Config Editor")
        self.resize(920, 740)
        self.setStyleSheet(STYLESHEET)

        self._config_path: str | None = None
        self._config_data: dict = {}
        self._dirty = False

        self._build_ui()

        if initial_path and os.path.isfile(initial_path):
            self._load_file(initial_path)
        else:
            self._try_auto_discover()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # ---- Header bar ----
        header = QWidget()
        header.setStyleSheet(f"background-color: {PANEL_BG}; border-bottom: 1px solid {BORDER};")
        header.setFixedHeight(56)
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(16, 8, 16, 8)

        title_lbl = QLabel("◈  ACQUISITION CONFIG")
        title_lbl.setStyleSheet(
            f"color: {ACCENT}; font-size: 15px; font-weight: bold; letter-spacing: 3px;"
        )

        self.file_picker = FilePicker()
        self.file_picker.browse_btn.clicked.connect(self._browse_config)
        self.file_picker.setFixedWidth(500)

        self.save_btn    = QPushButton("Save")
        self.save_btn.setObjectName("success")
        self.save_btn.setFixedWidth(80)
        self.save_btn.clicked.connect(self._save)

        self.save_as_btn = QPushButton("Save As…")
        self.save_as_btn.setFixedWidth(90)
        self.save_as_btn.clicked.connect(self._save_as)

        h_layout.addWidget(title_lbl)
        h_layout.addStretch()
        h_layout.addWidget(self.file_picker)
        h_layout.addSpacing(12)
        h_layout.addWidget(self.save_btn)
        h_layout.addWidget(self.save_as_btn)

        main_layout.addWidget(header)

        # ---- Tab widget ----
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)

        self.meta_tab      = MetadataTab()
        self.cameras_tab   = CamerasTab()
        self.recording_tab = RecordingTab()
        self.trigger_tab   = TriggerTab()

        self.tabs.addTab(self.meta_tab,      "🧪  Experiment")
        self.tabs.addTab(self.cameras_tab,   "📷  Cameras")
        self.tabs.addTab(self.recording_tab, "🎬  Recording / ROI")
        self.tabs.addTab(self.trigger_tab,   "⚡  Trigger / CSV")

        main_layout.addWidget(self.tabs, 1)

        # ---- Status bar ----
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._set_status("No config loaded.", color=TEXT_SEC)

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def _try_auto_discover(self):
        """Look for .yaml files in the current working directory."""
        yamls = sorted(glob.glob("*.yaml") + glob.glob("*.yml"))
        if len(yamls) == 1:
            self._load_file(yamls[0])
        elif len(yamls) > 1:
            self._set_status(
                f"Multiple YAML files found: {', '.join(yamls[:5])} — use Browse to pick one.",
                color=WARN,
            )

    def _browse_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Config File", os.getcwd(), "YAML Files (*.yaml *.yml);;All Files (*)"
        )
        if path:
            self._load_file(path)

    def _load_file(self, path: str):
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                raise ValueError("YAML root must be a mapping.")
        except Exception as e:
            QMessageBox.critical(self, "Load Error", f"Could not load {path}:\n{e}")
            return

        self._config_path = path
        self._config_data = data
        self.file_picker.path_edit.setText(os.path.abspath(path))

        # Populate tabs
        self.meta_tab.load(data.get("experiment_metadata", {}))
        self.cameras_tab.load(data.get("cameras", {}))
        self.recording_tab.load(data)
        self.trigger_tab.load(data)

        self._dirty = False
        self._set_status(f"Loaded: {os.path.abspath(path)}", color=ACCENT2)
        self.setWindowTitle(f"Config Editor — {os.path.basename(path)}")

    def _collect(self) -> dict:
        """Gather all tab values back into a config dict."""
        rec_data = self.recording_tab.dump()
        trig_data = self.trigger_tab.dump()

        merged = dict(self._config_data)  # preserve any unknown keys
        merged["save_dir"]  = rec_data.pop("save_dir")
        merged["cameras"]   = self.cameras_tab.dump()
        merged["recording"] = rec_data["recording"]
        merged["roi"]       = rec_data["roi"]
        merged["preview"]   = rec_data["preview"]
        merged["trigger"]   = trig_data["trigger"]
        merged["metadata"]  = trig_data["metadata"]
        merged["experiment_metadata"] = self.meta_tab.dump()
        return merged

    def _save(self):
        if not self._config_path:
            self._save_as()
            return
        self._write(self._config_path)

    def _save_as(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Config As", os.getcwd(), "YAML Files (*.yaml);;All Files (*)"
        )
        if path:
            if not path.endswith((".yaml", ".yml")):
                path += ".yaml"
            self._config_path = path
            self.file_picker.path_edit.setText(os.path.abspath(path))
            self._write(path)

    def _write(self, path: str):
        data = self._collect()
        try:
            with open(path, "w") as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            self._dirty = False
            self._set_status(f"Saved: {os.path.abspath(path)}", color=ACCENT2)
            self.setWindowTitle(f"Config Editor — {os.path.basename(path)}")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"Could not save:\n{e}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_status(self, msg: str, color: str = TEXT_PRI):
        self.status_bar.setStyleSheet(
            f"QStatusBar {{ background-color: {CARD_BG}; color: {color}; "
            f"border-top: 1px solid {BORDER}; font-size: 11px; }}"
        )
        self.status_bar.showMessage(msg)

    def closeEvent(self, event):
        if self._dirty:
            reply = QMessageBox.question(
                self, "Unsaved Changes",
                "You have unsaved changes. Save before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            )
            if reply == QMessageBox.Save:
                self._save()
            elif reply == QMessageBox.Cancel:
                event.ignore()
                return
        event.accept()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Camera acquisition config GUI.")
    parser.add_argument("-c", "--config", type=str, default=None,
                        help="Path to a config YAML to open on startup.")
    parser.add_argument("--dir", type=str, default=None,
                        help="Working directory to search for YAML files.")
    args = parser.parse_args()

    if args.dir:
        os.chdir(args.dir)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Dark fusion palette as a fallback under the stylesheet
    palette = QPalette()
    palette.setColor(QPalette.Window,          QColor(DARK_BG))
    palette.setColor(QPalette.WindowText,      QColor(TEXT_PRI))
    palette.setColor(QPalette.Base,            QColor(INPUT_BG))
    palette.setColor(QPalette.AlternateBase,   QColor(PANEL_BG))
    palette.setColor(QPalette.Text,            QColor(TEXT_PRI))
    palette.setColor(QPalette.Button,          QColor(BTN_BG))
    palette.setColor(QPalette.ButtonText,      QColor(TEXT_PRI))
    palette.setColor(QPalette.Highlight,       QColor(ACCENT))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)

    window = ConfigEditor(initial_path=args.config)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()