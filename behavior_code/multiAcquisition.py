"""
multiAcquisition.py — Multi-camera MJPEG/AVI acquisition
=========================================================
Usage
-----
    python multiAcquisition.py -c config.yaml
    python multiAcquisition.py --setup

No preview window — use preview.py for live monitoring.
Stats popup shows FPS / buffered frames / elapsed / time-left per chamber.

Recording triggers (per chamber)
---------------------------------
    Arduino START event  → starts recording for that chamber's camera
    Arduino STOP  event  → stops  recording for that chamber's camera
    Manual buttons       → click START/STOP in stats popup
    S / X keys           → start-all / stop-all
    Auto-start           → acquisition.auto_start: true in config
    Per-chamber timer    → chambers.<name>.timer_enabled + duration_s

Arduino protocol (analog)
--------------------------
    The Arduino sketch (chamber_ttl_analog.ino) reads several analog pins
    and reports a threshold-crossing state for each, every loop iteration:

        TTL:<number>:<state>\\n

    <number> is the sequential TTL channel (1, 2, 3, ...) matching pin
    order in the sketch's ANALOG_PINS array. <state> is 1 (above
    threshold) or 0 (below).

    This script requires 3 CONSECUTIVE state=1 reads of the same channel
    before confirming a trigger — this filters analog noise near the
    threshold. Each chamber's config specifies which TTL number starts
    recording (start_ttl) and which stops it (stop_ttl):

        chamber 1 → TTL 1 (start), TTL 2 (stop), TTL 3 (spare)
        chamber 2 → TTL 4 (start), TTL 5 (stop), TTL 6 (spare)

    Multiple chambers can share one Arduino (one port, multiple analog
    pins/TTL numbers). Multiple Arduinos on different ports are also
    supported — one AnalogTTLListener thread per unique port.

Output layout
-------------
    save_dir/
      YYYYMMDD_HHMMSS/
        chamber_A/
          BoxA_20250610_143022.avi
          BoxA_20250610_143022_timestamps.csv
          BoxA_session.csv
        config.yaml

Config (chambers block)
-----------------------
    chambers:
      chamber_A:
        camera: cam0
        record: true
        timer_enabled: false
        duration_s: 1800
        arduino:
          port: COM3          # serial port the Arduino is on
          baud: 115200
          start_ttl: 1         # TTL channel number that starts recording
          stop_ttl: 2          # TTL channel number that stops recording
          vref: 5.0            # 5.0 or 3.3 — informational, set in sketch too

Required
--------
    pip install numpy pyyaml opencv-python pyserial
    PySpin via FLIR Spinnaker SDK
"""

import os
import threading
import queue
import subprocess
import shutil
import time
import argparse
import csv
import math
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
# Inline preview helpers  (replaces tiling.py dependency)
# ---------------------------------------------------------------------------

def draw_tile_overlay(
    bgr: np.ndarray,
    label: str,
    chamber: str,
    fps: float,
    total: int,
    recording: bool = False,
) -> np.ndarray:
    """Burn a status overlay onto a BGR preview tile."""
    out  = bgr.copy()
    h, w = out.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX

    # Semi-transparent top banner
    banner_h = 28
    overlay  = out.copy()
    cv2.rectangle(overlay, (0, 0), (w, banner_h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, out, 0.45, 0, out)

    rec_dot  = "● " if recording else "○ "
    dot_col  = (0, 60, 220) if recording else (120, 120, 120)
    cv2.putText(out, rec_dot, (6, 19), font, 0.52, dot_col, 1, cv2.LINE_AA)

    title = f"{label}" + (f"  [{chamber}]" if chamber else "")
    cv2.putText(out, title, (26, 19), font, 0.50, (220, 220, 220), 1, cv2.LINE_AA)

    stats_str = f"FPS {fps:5.1f}   #{total:,}"
    tw, _ = cv2.getTextSize(stats_str, font, 0.45, 1)[0], None
    cv2.putText(out, stats_str, (w - tw[0] - 8, 18),
                font, 0.45, (0, 210, 210), 1, cv2.LINE_AA)

    return out


def tile_frames(
    frames: list,
    canvas_w: int,
    canvas_h: int,
) -> np.ndarray:
    """Arrange up to 4 BGR frames into a 2×2 (or 1×N) grid."""
    n = len(frames)
    if n == 0:
        return np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)

    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    tw   = canvas_w // cols
    th   = canvas_h // rows

    canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
    for i, frame in enumerate(frames):
        r, c = divmod(i, cols)
        x0, y0 = c * tw, r * th
        if frame.ndim == 2:
            tile = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        else:
            tile = frame

        tile_resized = cv2.resize(tile, (tw, th), interpolation=cv2.INTER_LINEAR)
        canvas[y0:y0 + th, x0:x0 + tw] = tile_resized

    return canvas


# ---------------------------------------------------------------------------
# TTL event  (unchanged — used internally throughout)
# ---------------------------------------------------------------------------

class TTLEvent:
    def __init__(self, chamber: str, kind: str,
                 wall: float, sestime: float, label: str,
                 ttl_number: int = None):
        self.chamber    = chamber
        self.kind       = kind   # "start" or "stop" (resolved) or "ttl_active" (raw)
        self.wall       = wall
        self.sestime    = sestime
        self.label      = label
        self.ttl_number = ttl_number   # raw analog TTL channel number, if applicable


# ---------------------------------------------------------------------------
# Analog TTL listener  — one per unique serial port
# ---------------------------------------------------------------------------

class AnalogTTLListener:
    """
    Reads numbered analog TTL channel states from one Arduino over serial.

    Expected message format (sent every loop by the sketch, for every pin):
        TTL:<number>:<state>\\n
    where <number> is the sequential TTL channel (1, 2, 3, ...) and
    <state> is 1 (voltage above threshold) or 0 (below).

    Confirmation logic
    -------------------
    A single high reading is not trusted — analog noise near the threshold
    can cause spurious blips. This listener requires CONFIRM_COUNT (3)
    consecutive "active" (state=1) reads of the SAME ttl_number before
    firing an event. The count resets to zero the moment that channel
    reads 0 again, so a fired event always corresponds to a clean,
    sustained voltage crossing.

    Each TTL number is tracked independently — multiple chambers' TTLs
    interleave on the same serial stream without interfering with each
    other's confirmation counters.

    Routing to start/stop is NOT done here — this listener only confirms
    "TTL channel N just went active" and pushes that as a TTLEvent with
    kind="ttl_active" and the raw ttl_number. main() maps ttl_number to
    (chamber, start/stop) using each chamber's configured start_ttl/stop_ttl.
    """

    CONFIRM_COUNT = 3   # consecutive active reads required to confirm

    def __init__(self, port: str, baud: int,
                 event_queue: queue.Queue, session_start: float):
        self.port           = port
        self.baud           = baud
        self._eq            = event_queue
        self._session_start = session_start
        self._stop          = threading.Event()
        self._thread        = None

        # Per-ttl-number consecutive active-read counter
        self._consec: dict[int, int] = {}
        # Per-ttl-number: have we already fired for this sustained run?
        # (prevents re-firing every loop while voltage stays high)
        self._fired: dict[int, bool] = {}

    def start(self):
        self._thread = threading.Thread(
            target=self._read_loop, daemon=True,
            name=f"analogttl-{self.port}"
        )
        self._thread.start()
        print(f"[AnalogTTL] Listening on {self.port} @ {self.baud} baud "
             f"(confirm after {self.CONFIRM_COUNT} consecutive reads)")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _read_loop(self):
        try:
            import serial
        except ImportError:
            print(f"[AnalogTTL:{self.port}] pyserial not installed — pip install pyserial")
            return

        try:
            ser = serial.Serial(
                port=self.port, baudrate=self.baud,
                timeout=0.5,
            )
        except Exception as e:
            print(f"[AnalogTTL:{self.port}] Cannot open port: {e}")
            return

        print(f"[AnalogTTL:{self.port}] Port open — reading channels.")
        with ser:
            while not self._stop.is_set():
                try:
                    raw = ser.readline()
                except Exception:
                    break

                if not raw:
                    continue

                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                except Exception:
                    continue

                if not line or line == "READY":
                    continue

                # Expected: "TTL:<number>:<state>"
                parts = line.split(":")
                if len(parts) != 3 or parts[0] != "TTL":
                    continue   # ignore malformed / unrelated lines silently

                try:
                    ttl_number = int(parts[1])
                    state      = int(parts[2])
                except ValueError:
                    continue

                if state == 1:
                    count = self._consec.get(ttl_number, 0) + 1
                    self._consec[ttl_number] = count

                    already_fired = self._fired.get(ttl_number, False)

                    if count >= self.CONFIRM_COUNT and not already_fired:
                        wall    = time.time()
                        sestime = time.perf_counter() - self._session_start
                        ts_str  = datetime.fromtimestamp(wall).strftime("%H:%M:%S.%f")
                        print(f"[AnalogTTL:{self.port}] TTL{ttl_number} confirmed "
                             f"active ({count} consecutive reads) at {ts_str}")

                        self._fired[ttl_number] = True
                        self._eq.put(TTLEvent(
                            chamber    = "",        # resolved by main() via ttl_number
                            kind       = "ttl_active",
                            wall       = wall,
                            sestime    = sestime,
                            label      = f"TTL{ttl_number}",
                            ttl_number = ttl_number,
                        ))
                else:
                    # Voltage dropped — reset both counters so the next
                    # sustained high run can fire again
                    self._consec[ttl_number] = 0
                    self._fired[ttl_number]  = False

        print(f"[AnalogTTL:{self.port}] Read loop exited.")


# ---------------------------------------------------------------------------
# CameraWriter  — per-camera, per-recording-session
# ---------------------------------------------------------------------------

class CameraWriter:
    """
    Writes MJPEG/AVI + timestamps CSV for one recording session.
    Output goes to:  session_dir / chamber_name / videoname.avi
                     session_dir / chamber_name / videoname_timestamps.csv
    TTL events are interleaved as ttl_event rows in the CSV.
    """

    def __init__(self, cam_name: str, cam_cfg: dict,
                 chamber_name: str, chamber_dir: str,
                 fps: float, jpeg_quality: int,
                 metadata_cfg: dict, ttl_queue: queue.Queue):
        self.cam_name     = cam_name
        self.chamber_name = chamber_name
        self.fps          = fps
        self.jpeg_quality = jpeg_quality
        self.metadata_cfg = metadata_cfg
        self._ttl_queue   = ttl_queue

        os.makedirs(chamber_dir, exist_ok=True)

        cam_label  = cam_cfg.get("name", cam_name)
        ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Filename includes chamber ID + camera name + timestamp for unambiguous identification
        base            = f"{chamber_name}_{cam_label}_{ts}"
        self.video_path = os.path.join(chamber_dir, f"{base}.avi")
        self.ts_path    = os.path.join(chamber_dir, f"{base}_timestamps.csv")
        self.label      = cam_label

        self._fq          = queue.Queue()
        self._stop        = threading.Event()
        self._thread      = None
        self.frame_count  = 0
        self.start_wall   = None
        self.end_wall     = None

    @property
    def buffered_frames(self) -> int:
        return self._fq.qsize()

    def start(self):
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"writer-{self.cam_name}"
        )
        self._thread.start()

    def push_frame(self, frame, framecount, timestamp, sestime, cputime):
        self._fq.put(("frame", frame, framecount, timestamp, sestime, cputime))

    def stop(self):
        self._fq.put(("stop", None, None, None, None, None))
        if self._thread:
            self._thread.join(timeout=15.0)

    def _run(self):
        proc     = None
        csv_file = None
        csvw     = None
        self.start_wall = time.time()

        if self.metadata_cfg.get("enabled", True):
            csv_file = open(self.ts_path, "w", newline="")
            csvw     = csv.writer(csv_file)
            hdr = ["row_type"]
            if self.metadata_cfg.get("save_framecount", True): hdr.append("framecount")
            if self.metadata_cfg.get("save_timestamp",  True): hdr.append("camera_hw_ts_s")
            if self.metadata_cfg.get("save_sestime",    True): hdr.append("sestime_s")
            if self.metadata_cfg.get("save_cputime",    True): hdr.append("cpu_wall_s")
            hdr += ["ttl_chamber", "ttl_kind", "ttl_label", "ttl_number"]
            csvw.writerow(hdr)
            csv_file.flush()

        q_val = max(2, min(31, int(2 + (100 - self.jpeg_quality) * 29 / 100)))

        while True:
            # Drain TTL events first
            while not self._ttl_queue.empty():
                try:
                    evt = self._ttl_queue.get_nowait()
                    if csvw:
                        row = ["ttl_event"]
                        cfg = self.metadata_cfg
                        if cfg.get("save_framecount", True): row.append("")
                        if cfg.get("save_timestamp",  True): row.append(f"{evt.wall:.6f}")
                        if cfg.get("save_sestime",    True): row.append(f"{evt.sestime:.6f}")
                        if cfg.get("save_cputime",    True): row.append(f"{evt.wall:.6f}")
                        row += [evt.chamber, evt.kind, evt.label,
                               evt.ttl_number if evt.ttl_number is not None else ""]
                        csvw.writerow(row)
                        csv_file.flush()
                except queue.Empty:
                    break

            try:
                item = self._fq.get(timeout=0.5)
            except queue.Empty:
                continue

            if item[0] == "stop":
                break

            _, frame, framecount, timestamp, sestime, cputime = item

            if proc is None:
                h, w = frame.shape[:2]
                if not shutil.which("ffmpeg"):
                    print(f"[Writer:{self.cam_name}] ffmpeg not on PATH.")
                    break
                cmd = [
                    "ffmpeg", "-y",
                    "-f",       "rawvideo", "-vcodec", "rawvideo",
                    "-pix_fmt", "gray",
                    "-s",       f"{w}x{h}",
                    "-r",       str(self.fps),
                    "-i",       "pipe:0",
                    "-vcodec",  "mjpeg",
                    "-q:v",     str(q_val),
                    "-pix_fmt", "yuvj420p",
                    self.video_path,
                ]
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
                print(f"[Writer:{self.cam_name}] → {self.video_path} ({w}×{h})")
                time.sleep(0.05)

            try:
                proc.stdin.write(frame.tobytes())
                self.frame_count += 1
            except BrokenPipeError:
                print(f"[Writer:{self.cam_name}] ffmpeg pipe broken.")
                break

            if csvw:
                row = ["frame"]
                cfg = self.metadata_cfg
                if cfg.get("save_framecount", True): row.append(framecount)
                if cfg.get("save_timestamp",  True): row.append(f"{timestamp:.6f}")
                if cfg.get("save_sestime",    True): row.append(f"{sestime:.6f}")
                if cfg.get("save_cputime",    True): row.append(f"{cputime:.6f}")
                row += ["", "", "", ""]
                csvw.writerow(row)

        self.end_wall = time.time()
        if proc:
            proc.stdin.close(); proc.wait()
            print(f"[Writer:{self.cam_name}] done.")
        if csv_file:
            csv_file.close()


# ---------------------------------------------------------------------------
# CameraStreamer
# ---------------------------------------------------------------------------

class CameraStreamer:

    def __init__(self, config: dict, system: "PySpin.SystemPtr"):
        self.config        = config
        self.system        = system
        self._stop_event   = threading.Event()
        self.session_start = time.perf_counter()
        self.session_wall  = time.time()

        self.cam_configs = {
            name: cfg
            for name, cfg in config["cameras"].items()
            if cfg.get("enabled", True)
        }
        self.cam_names = list(self.cam_configs.keys())
        self.cameras: dict[str, PySpin.Camera] = {}

        # Recording state — keyed by camera name
        self._recording: dict[str, bool]         = {n: False for n in self.cam_names}
        self._writers:   dict[str, CameraWriter] = {}
        self._rec_lock   = threading.Lock()

        # Per-camera TTL event queues (drained by writer)
        self._ttl_queues: dict[str, queue.Queue] = {
            n: queue.Queue() for n in self.cam_names
        }

        # Live stats
        self._stats_locks = {n: threading.Lock() for n in self.cam_names}
        self._stats = {
            n: {"fps": 0.0, "total": 0, "_ring": deque(maxlen=30)}
            for n in self.cam_names
        }
        self._rec_start_times: dict[str, float] = {}

        # Live preview frames — populated by the capture thread regardless
        # of recording state so the tile window can show them when idle.
        self._preview_frames = {n: None             for n in self.cam_names}
        self._preview_locks  = {n: threading.Lock() for n in self.cam_names}

        self._capture_threads: list[threading.Thread] = []

        rec = config["recording"]
        self.fps          = rec["fps"]
        self.jpeg_quality = rec.get("jpeg_quality", 90)

        roi = config.get("roi", {})
        self.target_w = roi.get("width",  None)
        self.target_h = roi.get("height", None)

        # Camera hardware trigger — disabled by default
        # (TTL is read by PC, not sent to camera GPIO)
        trig = config.get("trigger", {})
        self.hw_trigger_enabled = trig.get("enabled", False)
        self.trigger_timeout    = trig.get("timeout_ms", 5000)

        self.metadata_cfg = config.get("metadata", {})

        # Build output directory: save_dir / session_timestamp
        save_dir   = config["save_dir"]
        experiment = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = os.path.join(save_dir, experiment)
        os.makedirs(self.session_dir, exist_ok=True)

        # Copy config
        with open(os.path.join(self.session_dir, "config.yaml"), "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        # Build chamber → camera map
        self.chambers_cfg = config.get("chambers", {})
        self.chamber_to_cam: dict[str, str] = {}
        self.cam_to_chamber: dict[str, str] = {}
        for ch_name, ch_cfg in self.chambers_cfg.items():
            cam_key = ch_cfg.get("camera", "")
            if cam_key in self.cam_names:
                self.chamber_to_cam[ch_name] = cam_key
                self.cam_to_chamber[cam_key] = ch_name

        # Build TTL action map from global ttl_map list.
        # ttl_map is a list of dicts, each entry defining one pin's role:
        #   { pin: "A0", ttl_label: "session_start", chamber: "chamber_A",
        #     action: "start_recording" }
        # Pin entries appear in the same order as ANALOG_PINS in the Arduino
        # sketch, so TTL channel number = list index + 1.
        # Valid actions: "start_recording", "stop_recording", "log_event"
        #
        # self.ttl_action_map: ttl_number (int) → {chamber, action, label, pin}
        self.ttl_action_map: dict[int, dict] = {}
        for idx, entry in enumerate(config.get("ttl_map", [])):
            ttl_num = idx + 1
            self.ttl_action_map[ttl_num] = {
                "chamber": entry.get("chamber", ""),
                "action":  entry.get("action",  "log_event"),
                "label":   entry.get("ttl_label", f"TTL{ttl_num}"),
                "pin":     entry.get("pin", f"A{idx}"),
            }

        if not self.ttl_action_map:
            print("[TTL] No ttl_map entries found in config — "
                 "TTL triggering will not be active. "
                 "Add entries under 'ttl_map:' in config.yaml or via the "
                 "TTL Map tab in config.py.")

    # ------------------------------------------------------------------
    # Recording control
    # ------------------------------------------------------------------

    def start_recording(self, cam_name: str):
        with self._rec_lock:
            if self._recording.get(cam_name):
                return
            ch_name = self.cam_to_chamber.get(cam_name, cam_name)
            chamber_dir = os.path.join(self.session_dir, ch_name)
            writer = CameraWriter(
                cam_name     = cam_name,
                cam_cfg      = self.cam_configs[cam_name],
                chamber_name = ch_name,
                chamber_dir  = chamber_dir,
                fps          = self.fps,
                jpeg_quality = self.jpeg_quality,
                metadata_cfg = self.metadata_cfg,
                ttl_queue    = self._ttl_queues[cam_name],
            )
            writer.start()
            self._writers[cam_name]        = writer
            self._recording[cam_name]      = True
            self._rec_start_times[cam_name] = time.perf_counter()
            print(f"[Recorder] {cam_name} ({ch_name}) started → {chamber_dir}")

    def stop_recording(self, cam_name: str):
        with self._rec_lock:
            if not self._recording.get(cam_name):
                return
            self._recording[cam_name] = False
            writer = self._writers.pop(cam_name, None)
            if writer:
                writer.stop()
                self._write_session_summary(cam_name, writer)
            self._rec_start_times.pop(cam_name, None)
            print(f"[Recorder] {cam_name} stopped.")

    def start_all_recording(self):
        # Only start cameras whose chamber has record: true
        for ch_name, ch_cfg in self.chambers_cfg.items():
            if not ch_cfg.get("record", True):
                continue
            cam_key = ch_cfg.get("camera")
            if cam_key and cam_key in self.cam_names:
                self.start_recording(cam_key)

    def stop_all_recording(self):
        for name in list(self._writers.keys()):
            self.stop_recording(name)

    def is_recording(self, cam_name: str) -> bool:
        return self._recording.get(cam_name, False)

    def any_recording(self) -> bool:
        return any(self._recording.values())

    def elapsed_recording(self, cam_name: str) -> float:
        """Seconds since this camera's recording started, or 0."""
        t = self._rec_start_times.get(cam_name)
        return (time.perf_counter() - t) if t else 0.0

    def buffered_frames(self, cam_name: str) -> int:
        w = self._writers.get(cam_name)
        return w.buffered_frames if w else 0

    # ------------------------------------------------------------------
    # TTL routing
    # ------------------------------------------------------------------

    def route_ttl_event(self, evt: TTLEvent):
        """
        Resolve a confirmed 'ttl_active' event (carrying ttl_number) into
        a concrete chamber + action using ttl_action_map, then act.

        Actions:
            start_recording  → starts that chamber's camera if not already recording
            stop_recording   → stops it if currently recording
            log_event        → logs to that chamber's CSV only, no recording change
        """
        if evt.kind != "ttl_active" or evt.ttl_number is None:
            return

        mapping = self.ttl_action_map.get(evt.ttl_number)
        if not mapping:
            print(f"[TTL] TTL{evt.ttl_number} confirmed but not in ttl_map — ignored. "
                 f"Add an entry for pin index {evt.ttl_number - 1} in config.yaml.")
            return

        ch_name = mapping["chamber"]
        action  = mapping["action"]
        label   = mapping["label"]
        pin     = mapping["pin"]

        # Resolve the event fields from the map
        evt.chamber = ch_name
        evt.kind    = action    # store action as kind for CSV row
        evt.label   = label

        cam_name = self.chamber_to_cam.get(ch_name)
        if not cam_name:
            print(f"[TTL] ttl_map entry for TTL{evt.ttl_number} references "
                 f"chamber '{ch_name}' which has no mapped camera — ignored.")
            return

        # Log to CSV for this chamber regardless of action type.
        # Writer only queues events while recording is active; if not recording
        # and action is log_event, the event is printed but not persisted.
        if self._recording.get(cam_name):
            self._ttl_queues[cam_name].put(evt)
        else:
            print(f"[TTL] {label} (TTL{evt.ttl_number}, pin {pin}) → "
                 f"{ch_name} — not recording, event not written to CSV.")

        ts = datetime.fromtimestamp(evt.wall).strftime("%H:%M:%S.%f")

        if action == "start_recording":
            if not self.is_recording(cam_name):
                print(f"[TTL] {label} → START {ch_name} at {ts}")
                self.start_recording(cam_name)
                # Re-queue now that writer exists
                self._ttl_queues[cam_name].put(evt)
            else:
                print(f"[TTL] {label} → {ch_name} already recording, logged only.")

        elif action == "stop_recording":
            if self.is_recording(cam_name):
                print(f"[TTL] {label} → STOP {ch_name} at {ts}")
                self.stop_recording(cam_name)
            else:
                print(f"[TTL] {label} → {ch_name} not recording, nothing to stop.")

        elif action == "log_event":
            print(f"[TTL] {label} (TTL{evt.ttl_number}, pin {pin}) → "
                 f"logged to {ch_name} at {ts}")
        else:
            print(f"[TTL] Unknown action '{action}' for TTL{evt.ttl_number} — ignored.")

    # ------------------------------------------------------------------
    # Session summary
    # ------------------------------------------------------------------

    def _write_session_summary(self, cam_name: str, writer: CameraWriter):
        if not self.metadata_cfg.get("enabled", True):
            return

        import socket

        label    = self.cam_configs[cam_name].get("name", cam_name)
        ch_name  = self.cam_to_chamber.get(cam_name, cam_name)
        # Session summary named consistently with the video/timestamps files
        base     = os.path.splitext(os.path.basename(writer.video_path))[0]
        path     = os.path.join(self.session_dir, ch_name, f"{base}_session.csv")
        duration = (writer.end_wall - writer.start_wall) if writer.end_wall else 0
        avg_fps  = writer.frame_count / duration if duration > 0 else 0
        start_dt = datetime.fromtimestamp(writer.start_wall)
        end_dt   = datetime.fromtimestamp(writer.end_wall or writer.start_wall)
        exp_meta = self.config.get("experiment_metadata", {})
        expected = int(round(self.fps * duration))

        # PC name: prefer config override, fall back to system hostname
        pc_name = (
            exp_meta.get("pc_name", "").strip()
            or self.config.get("pc_name", "").strip()
            or socket.gethostname()
        )

        # Chamber number: numeric suffix if key ends in a digit, else the full key
        ch_num_str = ch_name
        for part in reversed(ch_name.replace("-", "_").split("_")):
            if part.isdigit():
                ch_num_str = part
                break

        fields = {
            "experimenter_name":         exp_meta.get("experimenter_name", ""),
            "experiment_name":           exp_meta.get("experiment_name", ""),
            "pc_name":                   pc_name,
            "camera_name":               label,
            "chamber":                   ch_name,
            "chamber_number":            ch_num_str,
            "animal_id":                 exp_meta.get("animal_id", ""),
            "genotype":                  exp_meta.get("genotype", ""),
            "group":                     exp_meta.get("group", ""),
            "schedule_name":             exp_meta.get("schedule_name", ""),
            "date":                      start_dt.strftime("%Y-%m-%d"),
            "acquisition_start":         start_dt.strftime("%H:%M:%S.%f"),
            "acquisition_end":           end_dt.strftime("%H:%M:%S.%f"),
            "duration_s":                f"{duration:.3f}",
            "total_frames":              writer.frame_count,
            "frames_dropped":            max(0, expected - writer.frame_count),
            "configured_fps":            f"{self.fps:.4f}",
            "avg_actual_fps":            f"{avg_fps:.4f}",
            "video_path":                os.path.abspath(writer.video_path),
            "timestamp_file_path":       os.path.abspath(writer.ts_path),
            "eeg_fiber_photometry_path": exp_meta.get("eeg_fiber_photometry_path", ""),
            "notes":                     exp_meta.get("notes", ""),
        }
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(fields.keys()))
            w.writeheader(); w.writerow(fields)
        print(f"[Summary] → {path}")

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self, cam_name: str) -> dict:
        with self._stats_locks[cam_name]:
            s = self._stats[cam_name]
            return {"fps": s["fps"], "total": s["total"]}

    def _update_stats(self, cam_name: str, total: int):
        now = time.perf_counter()
        with self._stats_locks[cam_name]:
            s    = self._stats[cam_name]
            s["total"] = total
            s["_ring"].append(now)
            if len(s["_ring"]) >= 2:
                el   = s["_ring"][-1] - s["_ring"][0]
                s["fps"] = (len(s["_ring"]) - 1) / el if el > 0 else 0.0

    # ------------------------------------------------------------------
    # Camera configuration  (no GPIO trigger — TTL read by PC)
    # ------------------------------------------------------------------

    def _configure_camera(self, cam, cam_name: str) -> bool:
        nodemap = cam.GetNodeMap()
        print(f"\n[Config] {cam_name}...")
        try:
            # ---- Explicitly disable hardware trigger ----
            # The TTL is read by this script over serial; the camera runs
            # free in continuous mode.  Leaving trigger ON causes the
            # "failed to write enum value" Spinnaker error.
            tm = PySpin.CEnumerationPtr(nodemap.GetNode("TriggerMode"))
            if PySpin.IsAvailable(tm) and PySpin.IsWritable(tm):
                off_entry = tm.GetEntryByName("Off")
                if PySpin.IsAvailable(off_entry) and PySpin.IsReadable(off_entry):
                    tm.SetIntValue(off_entry.GetValue())
                    print(f"  TriggerMode: Off")

            # Pixel format
            pf = PySpin.CEnumerationPtr(nodemap.GetNode("PixelFormat"))
            if PySpin.IsAvailable(pf) and PySpin.IsWritable(pf):
                for fmt in ["Mono8", "Mono16", "BayerRG8", "BGR8"]:
                    e = PySpin.CEnumEntryPtr(pf.GetEntryByName(fmt))
                    if PySpin.IsAvailable(e) and PySpin.IsReadable(e):
                        pf.SetIntValue(e.GetValue())
                        print(f"  PixelFormat: {fmt}")
                        break

            # ROI
            if self.target_w and self.target_h:
                ox = PySpin.CIntegerPtr(nodemap.GetNode("OffsetX"))
                oy = PySpin.CIntegerPtr(nodemap.GetNode("OffsetY"))
                nw = PySpin.CIntegerPtr(nodemap.GetNode("Width"))
                nh = PySpin.CIntegerPtr(nodemap.GetNode("Height"))
                for n in [ox, oy]:
                    if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                        n.SetValue(n.GetMin())
                for node, target, lbl in [(nw, self.target_w, "Width"),
                                           (nh, self.target_h, "Height")]:
                    if PySpin.IsAvailable(node) and PySpin.IsWritable(node):
                        mn, inc = node.GetMin(), node.GetInc()
                        v = mn + ((min(target, node.GetMax()) - mn) // inc) * inc
                        node.SetValue(v)
                        print(f"  {lbl}: {node.GetValue()}")
                for node, sensor, actual in [
                    (ox, ox.GetMax() + nw.GetValue(), nw.GetValue()),
                    (oy, oy.GetMax() + nh.GetValue(), nh.GetValue()),
                ]:
                    if PySpin.IsAvailable(node) and PySpin.IsWritable(node):
                        inc = node.GetInc()
                        v   = (((sensor - actual) // 2) // inc) * inc
                        node.SetValue(max(node.GetMin(), min(node.GetMax(), v)))

            # Continuous acquisition
            acq = PySpin.CEnumerationPtr(nodemap.GetNode("AcquisitionMode"))
            if PySpin.IsAvailable(acq) and PySpin.IsWritable(acq):
                cont = acq.GetEntryByName("Continuous")
                if PySpin.IsAvailable(cont) and PySpin.IsReadable(cont):
                    acq.SetIntValue(cont.GetValue())
                    print("  AcquisitionMode: Continuous")

            cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)
            cfg = self.cam_configs[cam_name]
            cam.ExposureTime.SetValue(min(15000, cfg.get("exposure_us", 14000)))
            print(f"  Exposure: {cam.ExposureTime.GetValue():.0f} µs")

            cam.GainAuto.SetValue(PySpin.GainAuto_Off)
            cam.Gain.SetValue(min(cam.Gain.GetMax(), cfg.get("gain_db", 10)))
            print(f"  Gain: {cam.Gain.GetValue():.1f} dB")

            for node_name, val, lbl in [("IspEnable",   False, "ISP disabled"),
                                         ("GammaEnable", False, "Gamma disabled")]:
                n = PySpin.CBooleanPtr(nodemap.GetNode(node_name))
                if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                    n.SetValue(val); print(f"  {lbl}")

            n = PySpin.CFloatPtr(nodemap.GetNode("BlackLevel"))
            if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                n.SetValue(cfg.get("black_level", 2.0))

            n = PySpin.CIntegerPtr(nodemap.GetNode("DeviceLinkThroughputLimit"))
            if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                tl = max(n.GetMin(), min(n.GetMax(), cfg.get("throughput_limit", 90_000_000)))
                n.SetValue(tl)

            return True
        except PySpin.SpinnakerException as ex:
            print(f"  {cam_name} configure error: {ex}")
            return False

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
            print(f"Cameras not found: {missing}")
            return False
        return True

    def _init_cameras(self):
        if not self._find_cameras():
            raise RuntimeError("Not all cameras found.")
        for name, cam in self.cameras.items():
            cam.Init()
            self._configure_camera(cam, name)
            cam.BeginAcquisition()
            print(f"  {name}: streaming.")

    # ------------------------------------------------------------------
    # Capture thread — always runs; only writes to disk when recording
    # ------------------------------------------------------------------

    def _capture_frame(self, cam_name: str, cam):
        downsample = self.config.get("preview", {}).get("downsample", 1)
        idx = 0
        while not self._stop_event.is_set():
            try:
                img = cam.GetNextImage(self.trigger_timeout)
                if img.IsIncomplete():
                    img.Release(); continue

                framecount = img.GetFrameID()
                timestamp  = img.GetTimeStamp() * 1e-9
                sestime    = time.perf_counter() - self.session_start
                cputime    = time.time()
                frame      = np.array(img.GetNDArray(), copy=True)
                img.Release()

                idx += 1
                self._update_stats(cam_name, idx)

                # Cache for live preview tile — independent of recording state
                if (idx - 1) % downsample == 0:
                    with self._preview_locks[cam_name]:
                        self._preview_frames[cam_name] = frame

                if self._recording.get(cam_name):
                    w = self._writers.get(cam_name)
                    if w:
                        w.push_frame(frame, framecount, timestamp, sestime, cputime)

            except PySpin.SpinnakerException as ex:
                if not self._stop_event.is_set():
                    print(f"[Capture:{cam_name}] {ex}")

    def get_preview_frame(self, cam_name: str):
        """Latest cached frame for live preview tile — None if not yet captured."""
        with self._preview_locks[cam_name]:
            return self._preview_frames[cam_name]

    def _start_threads(self):
        for name, cam in self.cameras.items():
            t = threading.Thread(target=self._capture_frame,
                                 args=(name, cam), daemon=True,
                                 name=f"capture-{name}")
            t.start()
            self._capture_threads.append(t)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def stop(self):
        self.stop_all_recording()
        self._stop_event.set()
        for cam in self.cameras.values():
            try:
                cam.EndAcquisition(); cam.DeInit()
            except Exception:
                pass
        self.cameras.clear()


# ---------------------------------------------------------------------------
# Stats popup window  (OpenCV — all chambers side by side)
# ---------------------------------------------------------------------------

POPUP_W  = 320   # width per chamber column
POPUP_H  = 320   # window height
BTN_H    = 28    # height of start/stop buttons at bottom of each column

# Global: maps column index → (start_rect, stop_rect) in pixel coords
_POPUP_BTN_ZONES: list[tuple] = []


def build_stats_popup(
    streamer: "CameraStreamer",
    chamber_to_cam: dict,
) -> np.ndarray:
    """
    Renders one column per chamber.
    Bottom of each column has [▶ Start] and [■ Stop] buttons.
    Click zones are stored in _POPUP_BTN_ZONES for mouse handling.
    """
    global _POPUP_BTN_ZONES
    _POPUP_BTN_ZONES = []

    chambers = list(chamber_to_cam.keys())
    n_ch     = max(len(chambers), 1)
    img      = np.zeros((POPUP_H, POPUP_W * n_ch, 3), dtype=np.uint8)
    img[:]   = (18, 20, 22)

    font = cv2.FONT_HERSHEY_SIMPLEX
    sc, th, lh, pad = 0.50, 1, 22, 10

    for col, ch_name in enumerate(chambers):
        x0       = col * POPUP_W
        cam_name = chamber_to_cam.get(ch_name, "")
        ch_cfg   = streamer.chambers_cfg.get(ch_name, {})
        rec      = streamer.is_recording(cam_name) if cam_name else False
        stats    = streamer.get_stats(cam_name) if cam_name else {"fps": 0.0, "total": 0}
        fps      = stats["fps"]
        buf      = streamer.buffered_frames(cam_name) if cam_name else 0
        elapsed  = streamer.elapsed_recording(cam_name) if cam_name else 0.0

        # Timer / time left
        timer_on  = ch_cfg.get("timer_enabled", False)
        duration  = float(ch_cfg.get("duration_s", 1800))
        if timer_on and rec and duration > 0:
            left     = max(0.0, duration - elapsed)
            lm, ls   = int(left) // 60, int(left) % 60
            left_str = f"{lm:02d}:{ls:02d}"
        else:
            left_str = "--:--"

        em, es      = int(elapsed) // 60, int(elapsed) % 60
        elapsed_str = f"{em:02d}:{es:02d}"

        rec_color = (0, 60, 220) if rec else (50, 50, 50)
        rec_text  = "● REC" if rec else "○ IDLE"
        buf_color = (0, 200, 200) if buf < 100 else (0, 80, 255)

        # Column separator
        if col > 0:
            cv2.line(img, (x0, 0), (x0, POPUP_H), (40, 44, 50), 1)

        # Background tint when recording
        if rec:
            roi = img[4:POPUP_H - BTN_H - 8, x0 + 4:x0 + POPUP_W - 4]
            roi[:] = np.clip(roi.astype(int) + [0, 0, 22], 0, 255).astype(np.uint8)

        lines = [
            (ch_name,                      (220, 220, 220)),
            (cam_name or "no cam",         (100, 120, 120)),
            (rec_text,                     rec_color),
            (f"FPS      {fps:6.2f}",       (0, 220, 220)),
            (f"Buffered {buf:>6,}",        buf_color),
            (f"Elapsed  {elapsed_str}",    (180, 180, 100)),
            (f"Time left {left_str}",      (120, 200, 120)),
        ]

        y = pad + lh
        for text, color in lines:
            cv2.putText(img, text, (x0 + pad, y), font, sc, color, th, cv2.LINE_AA)
            y += lh

        # ---- Per-chamber Start / Stop buttons ----
        btn_y     = POPUP_H - BTN_H - 4
        half_w    = (POPUP_W - 3 * pad) // 2

        # Start button
        sx0, sx1  = x0 + pad,              x0 + pad + half_w
        sy0, sy1  = btn_y,                 btn_y + BTN_H
        s_col     = (30, 140, 30) if not rec else (30, 60, 30)
        cv2.rectangle(img, (sx0, sy0), (sx1, sy1), s_col, -1)
        cv2.rectangle(img, (sx0, sy0), (sx1, sy1), (60, 160, 60), 1)
        cv2.putText(img, "START", (sx0 + 6, sy0 + 18),
                    font, 0.45, (180, 255, 180), 1, cv2.LINE_AA)

        # Stop button
        ex0, ex1  = sx1 + pad,             x0 + POPUP_W - pad
        e_col     = (140, 30, 30) if rec else (60, 30, 30)
        cv2.rectangle(img, (ex0, sy0), (ex1, sy1), e_col, -1)
        cv2.rectangle(img, (ex0, sy0), (ex1, sy1), (160, 60, 60), 1)
        cv2.putText(img, "STOP", (ex0 + 8, sy0 + 18),
                    font, 0.45, (255, 180, 180), 1, cv2.LINE_AA)

        _POPUP_BTN_ZONES.append(
            ((sx0, sy0, sx1, sy1), (ex0, sy0, ex1, sy1))
        )

    # Bottom global hint
    cv2.putText(img, "S: start all   X: stop all   ESC: quit",
                (pad, POPUP_H - 6), font, 0.36, (60, 60, 60), 1, cv2.LINE_AA)

    return img


def _popup_mouse_cb(event, x, y, flags, param):
    """OpenCV mouse callback — routes button clicks to streamer."""
    if event != cv2.EVENT_LBUTTONDOWN:
        return
    streamer: CameraStreamer  = param["streamer"]
    chambers: list[str]       = param["chambers"]
    chamber_to_cam: dict      = param["chamber_to_cam"]

    for col, (start_rect, stop_rect) in enumerate(_POPUP_BTN_ZONES):
        sx0, sy0, sx1, sy1 = start_rect
        ex0, ey0, ex1, ey1 = stop_rect
        if col >= len(chambers):
            continue
        ch_name  = chambers[col]
        cam_name = chamber_to_cam.get(ch_name, "")
        if not cam_name:
            continue
        if sx0 <= x <= sx1 and sy0 <= y <= sy1:
            streamer.start_recording(cam_name)
            print(f"[Popup] Manual START → {ch_name}")
        elif ex0 <= x <= ex1 and ey0 <= y <= ey1:
            streamer.stop_recording(cam_name)
            print(f"[Popup] Manual STOP  → {ch_name}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_connected_serials(system) -> list[dict]:
    found = []
    cam_list = system.GetCameras()
    for cam in cam_list:
        tlmap = cam.GetTLDeviceNodeMap()
        def _r(n):
            nd = PySpin.CStringPtr(tlmap.GetNode(n))
            return nd.GetValue() if PySpin.IsAvailable(nd) and PySpin.IsReadable(nd) else "unknown"
        found.append({"serial": _r("DeviceSerialNumber"),
                      "model":  _r("DeviceModelName"),
                      "vendor": _r("DeviceVendorName")})
    cam_list.Clear()
    return found


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

def run_setup_wizard(system, output_path="config.yaml"):
    print("\n" + "="*60)
    print("  Camera Acquisition — Setup Wizard")
    print("="*60)
    devices = get_connected_serials(system)
    if not devices:
        print("No cameras detected.")
        return
    for i, d in enumerate(devices):
        print(f"  [{i}]  Serial: {d['serial']}   Model: {d['model']}")
    raw      = input("\nCamera indices (Enter = all): ").strip()
    selected = list(range(len(devices))) if not raw else [int(x) for x in raw.split(",")]

    cameras_cfg  = {}
    chambers_cfg = {}
    ttl_map_entries = []

    print("\nEach chamber's analog pins map to TTL channel numbers sequentially")
    print("(first pin in ANALOG_PINS = TTL 1, second = TTL 2, etc).")
    print("The TTL Map defines which channel starts/stops each chamber.\n")

    port = input("Arduino serial port [COM3]: ").strip() or "COM3"
    baud_raw = input("Baud rate [115200]: ").strip()
    baud = int(baud_raw) if baud_raw.isdigit() else 115200

    for i in selected:
        d    = devices[i]
        name = input(f"\nCam {i} ({d['serial']}) name [cam{i}]: ").strip() or f"cam{i}"
        ch   = input(f"  Chamber label [chamber_{i}]: ").strip() or f"chamber_{i}"
        cameras_cfg[f"cam{i}"] = {
            "serial": d["serial"], "name": name, "chamber": ch,
            "enabled": True, "exposure_us": 14000, "gain_db": 10,
            "black_level": 2.0, "throughput_limit": 90_000_000,
        }
        chambers_cfg[ch] = {
            "camera": f"cam{i}",
            "record": True,
            "timer_enabled": False,
            "duration_s": 1800,
        }

        # Default: 3 pins per chamber, start on first, stop on second
        pin_base  = i * 3
        start_pin = f"A{pin_base}"
        stop_pin  = f"A{pin_base + 1}"
        spare_pin = f"A{pin_base + 2}"

        print(f"  Default pins for {ch}: {start_pin}=start, {stop_pin}=stop, {spare_pin}=spare")
        print(f"  TTL numbers will be {pin_base+1} (start), {pin_base+2} (stop), {pin_base+3} (spare)")

        # start entry
        ttl_map_entries.append({
            "pin":       start_pin,
            "ttl_label": f"{ch}_start",
            "chamber":   ch,
            "action":    "start_recording",
            "arduino":   {"port": port, "baud": baud},
        })
        # stop entry
        ttl_map_entries.append({
            "pin":       stop_pin,
            "ttl_label": f"{ch}_stop",
            "chamber":   ch,
            "action":    "stop_recording",
            "arduino":   {"port": port, "baud": baud},
        })
        # spare / log entry
        ttl_map_entries.append({
            "pin":       spare_pin,
            "ttl_label": f"{ch}_event",
            "chamber":   ch,
            "action":    "log_event",
            "arduino":   {"port": port, "baud": baud},
        })

    save_dir = input("\nSave directory [./recordings]: ").strip() or "./recordings"
    config = {
        "save_dir": save_dir,
        "cameras":  cameras_cfg,
        "chambers": chambers_cfg,
        # Global Arduino defaults (can be overridden per ttl_map entry)
        "arduino": {"port": port, "baud": baud, "vref": 5.0},
        # TTL mapping table — edit this to change which pins do what
        "ttl_map": ttl_map_entries,
        "acquisition": {"auto_start": False},
        "recording": {"fps": 59.99, "jpeg_quality": 90, "split_size_mb": None},
        "roi": {"width": 1020, "height": 1020, "offset_x": 0, "offset_y": 0},
        "trigger": {"enabled": False, "timeout_ms": 5000},
        "preview":  {"enabled": True, "downsample": 1},
        "metadata": {
            "enabled": True, "save_framecount": True,
            "save_timestamp": True, "save_sestime": True, "save_cputime": True,
        },
        "experiment_metadata": {
            "experimenter_name": "", "experiment_name": "",
            "animal_id": "", "genotype": "", "group": "",
            "schedule_name": "", "eeg_fiber_photometry_path": "", "notes": "",
        },
    }
    with open(output_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    print(f"\nConfig → {os.path.abspath(output_path)}")
    print("Acquire:  python multiAcquisition.py -c config.yaml")
    print("GUI:      python config.py -c config.yaml")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--setup", action="store_true")
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
        system.ReleaseInstance()
        return

    config = load_config(args.config)

    streamer = CameraStreamer(config, system)
    try:
        streamer._init_cameras()
    except RuntimeError as e:
        print(f"\n[Acquisition] Failed to start: {e}")
        system.ReleaseInstance()
        return
    streamer._start_threads()

    # Build analog TTL listeners — one per unique Arduino serial port.
    # Ports are collected from the global ttl_map, not from chambers,
    # since ttl_map is now the single source of truth for what each
    # analog pin does. A top-level arduino block in config provides defaults.
    ttl_map        = config.get("ttl_map", [])
    ttl_dispatch_q = queue.Queue()
    listeners: list[AnalogTTLListener] = []
    port_to_listener: dict[str, AnalogTTLListener] = {}

    global_ard   = config.get("arduino", {})
    default_port = global_ard.get("port", "")
    default_baud = global_ard.get("baud", 115200)

    for entry in ttl_map:
        ard  = entry.get("arduino", {})
        port = ard.get("port", default_port)
        baud = ard.get("baud", default_baud)

        if not port:
            print(f"[Warning] ttl_map entry for pin {entry.get('pin','?')} "
                 f"has no port — set arduino.port in the entry or a top-level "
                 f"arduino: block in config.yaml.")
            continue

        if port not in port_to_listener:
            lst = AnalogTTLListener(
                port          = port,
                baud          = baud,
                event_queue   = ttl_dispatch_q,
                session_start = streamer.session_start,
            )
            lst.start()
            listeners.append(lst)
            port_to_listener[port] = lst

    if not listeners:
        print("[Warning] No Arduino listeners started — TTL triggering inactive. "
             "Check that ttl_map entries have an arduino.port set.")

    # Acquisition settings
    acq_cfg    = config.get("acquisition", {})
    auto_start = acq_cfg.get("auto_start", False)

    if auto_start:
        print("[Main] Auto-start — recording all eligible chambers.")
        streamer.start_all_recording()

    print("\nStats popup open.")
    print("  Click START / STOP buttons per chamber")
    print("  S = start all   X = stop all   ESC = quit\n")

    POPUP_WIN = "Acquisition Stats"
    cv2.namedWindow(POPUP_WIN, cv2.WINDOW_NORMAL)

    chambers_list = list(streamer.chamber_to_cam.keys())
    cv2.setMouseCallback(POPUP_WIN, _popup_mouse_cb, {
        "streamer":      streamer,
        "chambers":      chambers_list,
        "chamber_to_cam": streamer.chamber_to_cam,
    })

    preview_enabled = config.get("preview", {}).get("enabled", True)
    PREVIEW_WIN = "Acquisition Preview"
    default_w, default_h = 1280, 720

    if preview_enabled:
        cv2.namedWindow(PREVIEW_WIN, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(PREVIEW_WIN, default_w, default_h)

    try:
        while True:
            popup = build_stats_popup(streamer, streamer.chamber_to_cam)
            cv2.imshow(POPUP_WIN, popup)

            if preview_enabled:
                tiles = []
                for name in streamer.cam_names:
                    frame = streamer.get_preview_frame(name)
                    if frame is None:
                        tiles.append(np.zeros((480, 640, 3), dtype=np.uint8))
                    else:
                        bgr = (cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
                              if frame.ndim == 2 else frame.copy())
                        cam_cfg = config["cameras"][name]
                        label   = cam_cfg.get("name", name)
                        chamber = streamer.cam_to_chamber.get(name, "")
                        stats   = streamer.get_stats(name)
                        bgr     = draw_tile_overlay(
                            bgr, label, chamber, stats["fps"], stats["total"],
                            recording=streamer.is_recording(name),
                        )
                        tiles.append(bgr)

                try:
                    _, _, pw, ph = cv2.getWindowImageRect(PREVIEW_WIN)
                    if pw < 64 or ph < 64:
                        pw, ph = default_w, default_h
                except Exception:
                    pw, ph = default_w, default_h

                composite = tile_frames(tiles[:4], pw, ph)
                cv2.imshow(PREVIEW_WIN, composite)

            key = cv2.waitKey(100) & 0xFF

            if key in (ord("s"), ord("S")):
                streamer.start_all_recording()
            elif key in (ord("x"), ord("X")):
                streamer.stop_all_recording()
            elif key == 27:
                break

            # Route TTL events
            while not ttl_dispatch_q.empty():
                try:
                    evt = ttl_dispatch_q.get_nowait()
                    streamer.route_ttl_event(evt)
                except queue.Empty:
                    break

            # Per-chamber timer stop
            for ch_name, ch_cfg in streamer.chambers_cfg.items():
                if not ch_cfg.get("timer_enabled", False):
                    continue
                duration = float(ch_cfg.get("duration_s", 1800))
                cam_name = streamer.chamber_to_cam.get(ch_name)
                if cam_name and streamer.is_recording(cam_name):
                    if streamer.elapsed_recording(cam_name) >= duration:
                        print(f"[Timer] {ch_name} duration reached — stopping.")
                        streamer.stop_recording(cam_name)

    finally:
        streamer.stop()
        for lst in listeners:
            lst.stop()
        cv2.destroyAllWindows()
        cam_list = system.GetCameras()
        cam_list.Clear()
        del cam_list
        system.ReleaseInstance()
        print("Done.")


if __name__ == "__main__":
    main()