"""
preview.py — Live camera preview (no recording)
================================================
Minimal script: reads only cameras + chambers from config.yaml.
Shows a live OpenCV preview window for every enabled camera.
No acquisition, no TTL, no file writing.

Usage
-----
    python preview.py -c config.yaml
    python preview.py -c config.yaml --scale 0.5   # downsample display
    python preview.py --setup                       # auto-detect cameras, write minimal config

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

    # Optional — shown in overlay but not required
    chambers:
      chamber_A:
        camera: cam0

Keys under recording/trigger/metadata are all ignored.

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
# Overlay
# ---------------------------------------------------------------------------

def draw_preview_overlay(frame: np.ndarray, label: str, chamber: str,
                          fps: float, total: int, scale: float) -> np.ndarray:
    if frame.ndim == 2:
        display = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    else:
        display = frame.copy()

    if scale != 1.0:
        h, w = display.shape[:2]
        display = cv2.resize(display, (int(w * scale), int(h * scale)))

    lines = [
        label,
        f"Chamber : {chamber}",
        f"FPS     : {fps:6.2f}",
        f"Frames  : {total:>8,}",
        "PREVIEW ONLY — no recording",
    ]

    font   = cv2.FONT_HERSHEY_SIMPLEX
    sc, th = 0.50, 1
    lh, pad = 19, 7
    max_w  = max(cv2.getTextSize(l, font, sc, th)[0][0] for l in lines)
    box_h  = lh * len(lines) + pad
    box_w  = max_w + pad * 2

    ov = display.copy()
    cv2.rectangle(ov, (0, 0), (box_w, box_h), (0, 0, 0), -1)
    cv2.addWeighted(ov, 0.55, display, 0.45, 0, display)

    colors = [(255,255,255), (0,255,255), (0,255,255), (0,255,255), (80,80,200)]
    for i, line in enumerate(lines):
        y = pad + (i + 1) * lh - 3
        cv2.putText(display, line, (pad, y), font, sc, colors[i], th, cv2.LINE_AA)

    h, w = display.shape[:2]
    cv2.putText(display, "ESC / Q : quit", (pad, h - 8),
                font, 0.38, (80, 80, 80), 1, cv2.LINE_AA)
    return display


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
    parser.add_argument("-c", "--config",  default="config.yaml")
    parser.add_argument("--scale",  type=float, default=1.0,
                        help="Display scale factor (e.g. 0.5 = half size)")
    parser.add_argument("--setup",  action="store_true",
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

    config  = load_config(args.config)
    streamer = PreviewStreamer(config, system)

    try:
        streamer.init()
    except RuntimeError as e:
        print(f"[Preview] Init failed: {e}")
        system.ReleaseInstance()
        return

    streamer.start()

    # Build chamber label lookup
    chambers_cfg  = config.get("chambers", {})
    cam_to_chamber = {
        ch_cfg.get("camera", ""): ch_name
        for ch_name, ch_cfg in chambers_cfg.items()
    }

    print(f"\nPreviewing {len(streamer.cam_names)} camera(s).  ESC or Q to quit.\n")

    try:
        while True:
            for name in streamer.cam_names:
                frame = streamer.get_frame(name)
                if frame is None:
                    continue
                cam_cfg = config["cameras"][name]
                label   = cam_cfg.get("name", name)
                chamber = cam_to_chamber.get(name, cam_cfg.get("chamber", ""))
                stats   = streamer.get_stats(name)
                display = draw_preview_overlay(
                    frame, label, chamber,
                    stats["fps"], stats["total"], args.scale
                )
                cv2.imshow(label, display)

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
