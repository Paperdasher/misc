"""
preview.py — Live camera preview (no recording)
================================================
Minimal script: reads only cameras + chambers from config.yaml.
Shows all cameras in ONE resizable window, tiled automatically:

    1 camera  → full window
    2 cameras → side by side
    3 cameras → left column + right column (top + bottom)
    4 cameras → 2×2 grid

Window is resizable — drag the corner freely.

Usage
-----
    python preview.py -c config.yaml
    python preview.py --setup     # auto-detect cameras, write minimal config

Config needed (subset of full config.yaml)
------------------------------------------
    cameras:
      cam0:
        serial: "12345678"
        name: "Box A"
        chamber: chamber_A
        enabled: true
        exposure_us: 14000
        gain_db: 10
        black_level: 2.0
        throughput_limit: 90000000

    chambers:            # optional — shown in overlay
      chamber_A:
        camera: cam0

Keys under recording/trigger/metadata are ignored.

Press ESC or Q to quit.
"""

import os
import sys
import threading
import time
import argparse
from collections import deque
from datetime import datetime

import numpy as np
import yaml
import cv2
import PySpin


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Camera preview streamer
# ---------------------------------------------------------------------------

class PreviewStreamer:
    """
    Initialises cameras in continuous mode (no trigger, no writer)
    and exposes latest frames for display.
    """

    FRAME_TIMEOUT_MS = 2000

    def __init__(self, config: dict, system: "PySpin.SystemPtr"):
        self.config  = config
        self.system  = system

        self.cam_configs = {
            name: cfg
            for name, cfg in config.get("cameras", {}).items()
            if cfg.get("enabled", True)
        }
        self.cam_names = list(self.cam_configs.keys())
        self.cameras: dict[str, PySpin.Camera] = {}

        self._frames = {n: None             for n in self.cam_names}
        self._flocks = {n: threading.Lock() for n in self.cam_names}
        self._stats  = {n: {"fps": 0.0, "total": 0,
                             "_ring": deque(maxlen=30)}
                        for n in self.cam_names}
        self._slocks = {n: threading.Lock() for n in self.cam_names}

        self._stop    = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---- camera init -------------------------------------------------------

    def _find_cameras(self) -> bool:
        cam_list = self.system.GetCameras()
        wanted   = {cfg["serial"]: name for name, cfg in self.cam_configs.items()}
        for cam in cam_list:
            node = PySpin.CStringPtr(
                cam.GetTLDeviceNodeMap().GetNode("DeviceSerialNumber"))
            if PySpin.IsAvailable(node) and PySpin.IsReadable(node):
                s = node.GetValue()
                if s in wanted:
                    self.cameras[wanted[s]] = cam
        cam_list.Clear()
        missing = [n for n in self.cam_names if n not in self.cameras]
        if missing:
            print(f"[Preview] Cameras not found: {missing}")
            return False
        return True

    def _configure(self, cam, name: str):
        nodemap = cam.GetNodeMap()
        cfg     = self.cam_configs[name]

        # Ensure hardware trigger is OFF — free-running preview
        tm = PySpin.CEnumerationPtr(nodemap.GetNode("TriggerMode"))
        if PySpin.IsAvailable(tm) and PySpin.IsWritable(tm):
            tm.SetIntValue(tm.GetEntryByName("Off").GetValue())

        # Pixel format
        pf = PySpin.CEnumerationPtr(nodemap.GetNode("PixelFormat"))
        if PySpin.IsAvailable(pf) and PySpin.IsWritable(pf):
            for fmt in ["Mono8", "Mono16", "BayerRG8", "BGR8"]:
                e = PySpin.CEnumEntryPtr(pf.GetEntryByName(fmt))
                if PySpin.IsAvailable(e) and PySpin.IsReadable(e):
                    pf.SetIntValue(e.GetValue())
                    print(f"  {name}: pixel format {fmt}")
                    break

        # Continuous acquisition
        acq = PySpin.CEnumerationPtr(nodemap.GetNode("AcquisitionMode"))
        if PySpin.IsAvailable(acq) and PySpin.IsWritable(acq):
            cont = acq.GetEntryByName("Continuous")
            if PySpin.IsAvailable(cont) and PySpin.IsReadable(cont):
                acq.SetIntValue(cont.GetValue())

        # Exposure
        cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)
        cam.ExposureTime.SetValue(min(15000, cfg.get("exposure_us", 14000)))

        # Gain
        cam.GainAuto.SetValue(PySpin.GainAuto_Off)
        cam.Gain.SetValue(min(cam.Gain.GetMax(), cfg.get("gain_db", 10)))

        # ISP / gamma off for raw preview
        for node_name in ["IspEnable", "GammaEnable"]:
            n = PySpin.CBooleanPtr(nodemap.GetNode(node_name))
            if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                n.SetValue(False)

        # Black level
        n = PySpin.CFloatPtr(nodemap.GetNode("BlackLevel"))
        if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
            n.SetValue(cfg.get("black_level", 2.0))

        # Throughput
        n = PySpin.CIntegerPtr(nodemap.GetNode("DeviceLinkThroughputLimit"))
        if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
            tl = max(n.GetMin(), min(n.GetMax(), cfg.get("throughput_limit", 90_000_000)))
            n.SetValue(tl)

        print(f"  {name}: exposure={cam.ExposureTime.GetValue():.0f}µs  "
              f"gain={cam.Gain.GetValue():.1f}dB")

    def init(self):
        if not self._find_cameras():
            raise RuntimeError("Not all cameras found.")
        for name, cam in self.cameras.items():
            print(f"\n[Preview] Configuring {name}...")
            cam.Init()
            self._configure(cam, name)
            cam.BeginAcquisition()
            print(f"  {name}: streaming.")

    # ---- capture thread ----------------------------------------------------

    def _capture(self, name: str, cam):
        idx = 0
        while not self._stop.is_set():
            try:
                img = cam.GetNextImage(self.FRAME_TIMEOUT_MS)
                if img.IsIncomplete():
                    img.Release(); continue

                frame = np.array(img.GetNDArray(), copy=True)
                img.Release()
                idx += 1

                now = time.perf_counter()
                with self._slocks[name]:
                    s = self._stats[name]
                    s["total"] = idx
                    s["_ring"].append(now)
                    if len(s["_ring"]) >= 2:
                        el = s["_ring"][-1] - s["_ring"][0]
                        s["fps"] = (len(s["_ring"]) - 1) / el if el > 0 else 0.0

                with self._flocks[name]:
                    self._frames[name] = frame

            except PySpin.SpinnakerException as ex:
                if not self._stop.is_set():
                    print(f"[Preview] {name} capture error: {ex}")

    def start(self):
        for name, cam in self.cameras.items():
            t = threading.Thread(target=self._capture, args=(name, cam),
                                 daemon=True, name=f"prev-{name}")
            t.start()
            self._threads.append(t)

    def get_frame(self, name: str):
        with self._flocks[name]:
            return self._frames[name]

    def get_stats(self, name: str) -> dict:
        with self._slocks[name]:
            s = self._stats[name]
            return {"fps": s["fps"], "total": s["total"]}

    def stop(self):
        self._stop.set()
        for cam in self.cameras.values():
            try:
                cam.EndAcquisition()
                cam.DeInit()
            except Exception:
                pass
        self.cameras.clear()


# ---------------------------------------------------------------------------
# Per-tile overlay
# ---------------------------------------------------------------------------

def draw_tile_overlay(frame: np.ndarray, label: str, chamber: str,
                      fps: float, total: int) -> np.ndarray:
    """Burn a compact HUD into one camera tile (already BGR)."""
    display = frame.copy()

    lines = [
        label,
        f"{chamber}",
        f"FPS {fps:5.1f}",
        "PREVIEW",
    ]

    font  = cv2.FONT_HERSHEY_SIMPLEX
    sc    = 0.45
    th    = 1
    lh    = 17
    pad   = 5

    max_w = max(cv2.getTextSize(l, font, sc, th)[0][0] for l in lines)
    box_h = lh * len(lines) + pad
    box_w = max_w + pad * 2

    ov = display.copy()
    cv2.rectangle(ov, (0, 0), (box_w, box_h), (0, 0, 0), -1)
    cv2.addWeighted(ov, 0.55, display, 0.45, 0, display)

    colors = [(255, 255, 255), (0, 220, 220), (0, 220, 220), (70, 70, 180)]
    for i, line in enumerate(lines):
        y = pad + (i + 1) * lh - 2
        cv2.putText(display, line, (pad, y), font, sc, colors[i], th, cv2.LINE_AA)

    return display


# ---------------------------------------------------------------------------
# Tiled compositor
# ---------------------------------------------------------------------------

def tile_frames(frames: list[np.ndarray], win_w: int, win_h: int) -> np.ndarray:
    """
    Arrange up to 4 BGR frames into a single canvas of size win_w × win_h.

    Layout:
        1 cam  → full canvas
        2 cams → left | right
        3 cams → left | right-top
                       | right-bottom
        4 cams → TL | TR
                 BL | BR
    """
    n = len(frames)
    canvas = np.zeros((win_h, win_w, 3), dtype=np.uint8)

    def _fit(img: np.ndarray, tw: int, th: int) -> np.ndarray:
        """Scale img to fit inside tw×th, preserving aspect ratio, centered."""
        h, w = img.shape[:2]
        scale = min(tw / w, th / h)
        nw, nh = int(w * scale), int(h * scale)
        resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
        out = np.zeros((th, tw, 3), dtype=np.uint8)
        y0 = (th - nh) // 2
        x0 = (tw - nw) // 2
        out[y0:y0 + nh, x0:x0 + nw] = resized
        return out

    def _place(img: np.ndarray, x: int, y: int, w: int, h: int):
        tile = _fit(img, w, h)
        canvas[y:y + h, x:x + w] = tile

    if n == 1:
        _place(frames[0], 0, 0, win_w, win_h)

    elif n == 2:
        hw = win_w // 2
        _place(frames[0], 0,  0, hw, win_h)
        _place(frames[1], hw, 0, win_w - hw, win_h)
        cv2.line(canvas, (hw, 0), (hw, win_h), (40, 40, 40), 1)

    elif n == 3:
        hw  = win_w // 2
        hh  = win_h // 2
        _place(frames[0], 0,  0,   hw,          win_h)
        _place(frames[1], hw, 0,   win_w - hw,  hh)
        _place(frames[2], hw, hh,  win_w - hw,  win_h - hh)
        cv2.line(canvas, (hw, 0),  (hw, win_h),   (40, 40, 40), 1)
        cv2.line(canvas, (hw, hh), (win_w, hh),   (40, 40, 40), 1)

    else:   # 4
        hw = win_w // 2
        hh = win_h // 2
        _place(frames[0], 0,  0,  hw,         hh)
        _place(frames[1], hw, 0,  win_w - hw, hh)
        _place(frames[2], 0,  hh, hw,         win_h - hh)
        _place(frames[3], hw, hh, win_w - hw, win_h - hh)
        cv2.line(canvas, (hw, 0),  (hw, win_h),  (40, 40, 40), 1)
        cv2.line(canvas, (0, hh),  (win_w, hh),  (40, 40, 40), 1)

    return canvas


# ---------------------------------------------------------------------------
# Camera discovery helper (for --setup)
# ---------------------------------------------------------------------------

def get_connected_serials(system) -> list[dict]:
    found    = []
    cam_list = system.GetCameras()
    for cam in cam_list:
        tlmap = cam.GetTLDeviceNodeMap()
        def _r(n):
            node = PySpin.CStringPtr(tlmap.GetNode(n))
            return node.GetValue() if PySpin.IsAvailable(node) and PySpin.IsReadable(node) else "unknown"
        found.append({"serial": _r("DeviceSerialNumber"),
                      "model":  _r("DeviceModelName")})
    cam_list.Clear()
    return found


def run_setup_wizard(system, output_path: str):
    print("\n=== Preview Setup Wizard ===\n")
    devices = get_connected_serials(system)
    if not devices:
        print("No cameras detected.")
        return
    print(f"Found {len(devices)} camera(s):\n")
    for i, d in enumerate(devices):
        print(f"  [{i}]  Serial: {d['serial']}   Model: {d['model']}")

    raw      = input("\nIndices to include (Enter = all): ").strip()
    selected = list(range(len(devices))) if not raw else [int(x) for x in raw.split(",")]

    cameras_cfg  = {}
    chambers_cfg = {}
    for i in selected:
        d    = devices[i]
        name = input(f"\nCam {i} ({d['serial']}) name [cam{i}]: ").strip() or f"cam{i}"
        ch   = input(f"  Chamber label [chamber_{i}]: ").strip() or f"chamber_{i}"
        cameras_cfg[f"cam{i}"] = {
            "serial": d["serial"], "name": name, "chamber": ch,
            "enabled": True, "exposure_us": 14000, "gain_db": 10,
            "black_level": 2.0, "throughput_limit": 90_000_000,
        }
        chambers_cfg[ch] = {"camera": f"cam{i}"}

    config = {"cameras": cameras_cfg, "chambers": chambers_cfg}
    with open(output_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    print(f"\nConfig written → {os.path.abspath(output_path)}")
    print(f"Run:  python preview.py -c {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Live camera preview — no recording.")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--setup", action="store_true",
                        help="Auto-detect cameras and write a minimal preview config")
    args = parser.parse_args()

    system  = PySpin.System.GetInstance()
    version = system.GetLibraryVersion()
    print(f"Spinnaker {version.major}.{version.minor}.{version.type}.{version.build}")

    if args.setup:
        run_setup_wizard(system, args.config)
        system.ReleaseInstance()
        return

    if not os.path.isfile(args.config):
        print(f"Config not found: {args.config}")
        print("Tip: python preview.py --setup")
        system.ReleaseInstance()
        return

    config   = load_config(args.config)
    streamer = PreviewStreamer(config, system)

    try:
        streamer.init()
    except RuntimeError as e:
        print(f"[Preview] Init failed: {e}")
        system.ReleaseInstance()
        return

    streamer.start()

    # Chamber lookup: camera key → chamber name
    chambers_cfg   = config.get("chambers", {})
    cam_to_chamber = {
        ch_cfg.get("camera", ""): ch_name
        for ch_name, ch_cfg in chambers_cfg.items()
    }

    n_cams = len(streamer.cam_names)
    print(f"\nPreviewing {n_cams} camera(s) in one window.  ESC or Q to quit.\n")

    WIN_NAME = "Preview"
    cv2.namedWindow(WIN_NAME, cv2.WINDOW_NORMAL)
    # Sensible default size; user can resize freely
    default_w = 1280 if n_cams <= 2 else 1280
    default_h = 480  if n_cams <= 2 else 960
    cv2.resizeWindow(WIN_NAME, default_w, default_h)

    try:
        while True:
            # --- Collect BGR tiles, one per camera ---
            tiles = []
            for name in streamer.cam_names:
                frame = streamer.get_frame(name)
                if frame is None:
                    # Placeholder while camera warms up
                    tiles.append(np.zeros((480, 640, 3), dtype=np.uint8))
                    continue

                # Convert to BGR
                if frame.ndim == 2:
                    bgr = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
                else:
                    bgr = frame.copy()

                cam_cfg = config["cameras"][name]
                label   = cam_cfg.get("name", name)
                chamber = cam_to_chamber.get(name, cam_cfg.get("chamber", ""))
                stats   = streamer.get_stats(name)
                bgr     = draw_tile_overlay(bgr, label, chamber,
                                            stats["fps"], stats["total"])
                tiles.append(bgr)

            # --- Get current window size so tiling respects user resizes ---
            try:
                _, _, win_w, win_h = cv2.getWindowImageRect(WIN_NAME)
                if win_w < 64 or win_h < 64:
                    win_w, win_h = default_w, default_h
            except Exception:
                win_w, win_h = default_w, default_h

            # Cap at 4 tiles for the tiler
            composite = tile_frames(tiles[:4], win_w, win_h)
            cv2.imshow(WIN_NAME, composite)

            key = cv2.waitKey(20) & 0xFF
            if key in (27, ord("q"), ord("Q")):
                break

    finally:
        streamer.stop()
        cv2.destroyAllWindows()
        cam_list = system.GetCameras()
        cam_list.Clear()
        del cam_list
        system.ReleaseInstance()
        print("Preview closed.")


if __name__ == "__main__":
    main()