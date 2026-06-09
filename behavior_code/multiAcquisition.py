"""
multiAcquisition.py — Multi-camera MJPEG/AVI acquisition
=========================================================
Usage
-----
    python multiAcquisition.py -c config.yaml
    python multiAcquisition.py --setup

Recording triggers
------------------
    Each chamber has an independent TTL listener on its own serial port.
    A TTL pulse starts recording for the camera mapped to that chamber.
    Subsequent TTL pulses while recording is active are logged but do NOT
    start a new recording.

    Manual trigger:  press R in any preview window (starts ALL cameras).
    Auto-start mode: recording begins immediately when the script starts.
    Preview always runs regardless of recording state.

Config structure (config.yaml)
-------------------------------
    chambers:
      chamber_A:
        ttl:
          port: COM3
          baud: 115200
          command: 105
          pin: 0
          polarity: 1          # 1=High/rising  0=Low/falling
          poll_interval_ms: 10
        camera: cam0           # key matching an entry under cameras:

    acquisition:
      auto_start: false        # true = start recording immediately on launch

Required pip installs:
    numpy pyyaml opencv-python pyserial
    PySpin via Spinnaker SDK
"""

import os
import threading
import queue
import subprocess
import shutil
import time
import argparse
import struct
import csv
from collections import deque
from datetime import datetime

import numpy as np
import yaml
import cv2
import PySpin


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# TTL event log entry
# ---------------------------------------------------------------------------

class TTLEvent:
    """One logged TTL pulse."""
    def __init__(self, chamber: str, wall: float, sestime: float, label: str):
        self.chamber = chamber
        self.wall    = wall
        self.sestime = sestime
        self.label   = label


# ---------------------------------------------------------------------------
# Per-chamber TTL listener
# ---------------------------------------------------------------------------

class ChamberTTLListener:
    """
    Polls a single serial port for one chamber's TTL signature.
    Protocol: [cmd U8][pin U8] → [cmd U8][state U8]
    Fires _triggered on the first matching pulse.
    All subsequent matching pulses are pushed to event_queue for logging.
    """

    def __init__(self, chamber_name: str, ttl_cfg: dict,
                 event_queue: queue.Queue, session_start: float):
        self.chamber      = chamber_name
        self.port         = ttl_cfg.get("port",            "COM1")
        self.baud         = ttl_cfg.get("baud",            115200)
        self.cmd          = ttl_cfg.get("command",         105)
        self.pin          = ttl_cfg.get("pin",             0)
        self.polarity     = ttl_cfg.get("polarity",        1)
        self.interval     = ttl_cfg.get("poll_interval_ms", 10) / 1000.0
        self.label        = ttl_cfg.get("label",           chamber_name)

        self._event_queue  = event_queue
        self._session_start = session_start
        self._stop         = threading.Event()
        self._triggered    = threading.Event()
        self._thread       = None
        # Debounce: ignore repeated High while pin stays High
        self._last_state   = 0

    def start(self):
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True,
            name=f"ttl-{self.chamber}"
        )
        self._thread.start()
        edge = "RISING" if self.polarity == 1 else "FALLING"
        print(f"[TTL:{self.chamber}] Polling {self.port} pin {self.pin} — {edge}")

    def triggered(self) -> bool:
        return self._triggered.is_set()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _poll_loop(self):
        try:
            import serial
        except ImportError:
            print(f"[TTL:{self.chamber}] pyserial not installed.")
            return
        try:
            ser = serial.Serial(port=self.port, baudrate=self.baud, timeout=0.1)
        except Exception as e:
            print(f"[TTL:{self.chamber}] Cannot open {self.port}: {e}")
            return

        print(f"[TTL:{self.chamber}] Serial open.")
        with ser:
            while not self._stop.is_set():
                ser.reset_input_buffer()
                ser.write(struct.pack("BB", self.cmd, self.pin))
                resp = ser.read(2)

                if len(resp) == 2:
                    cmd_echo, state = struct.unpack("BB", resp)
                    if cmd_echo == self.cmd:
                        # Edge detection: only act on transition to polarity state
                        if state == self.polarity and self._last_state != self.polarity:
                            wall    = time.time()
                            sestime = time.perf_counter() - self._session_start
                            ts      = datetime.fromtimestamp(wall).strftime("%H:%M:%S.%f")
                            print(f"[TTL:{self.chamber}] Pulse at {ts}  sestime={sestime:.3f}s")

                            evt = TTLEvent(self.chamber, wall, sestime, self.label)
                            self._event_queue.put(evt)

                            if not self._triggered.is_set():
                                self._triggered.set()   # first pulse fires recording

                        self._last_state = state

                time.sleep(self.interval)

        print(f"[TTL:{self.chamber}] Poll loop exited.")


# ---------------------------------------------------------------------------
# Per-camera writer
# ---------------------------------------------------------------------------

class CameraWriter:
    """
    Owns the ffmpeg subprocess and timestamp CSV for one camera.
    Runs on its own thread, consuming from a queue.Queue.
    TTL events are logged inline into the timestamp CSV.
    """

    def __init__(self, cam_name: str, cam_cfg: dict, output_dir: str,
                 fps: float, jpeg_quality: int, metadata_cfg: dict,
                 ttl_queue: queue.Queue):
        self.cam_name      = cam_name
        self.cam_cfg       = cam_cfg
        self.output_dir    = output_dir
        self.fps           = fps
        self.jpeg_quality  = jpeg_quality
        self.metadata_cfg  = metadata_cfg
        self._ttl_queue    = ttl_queue  # TTLEvents destined for this camera

        label           = cam_cfg.get("name", cam_name)
        ts              = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.video_path = os.path.join(output_dir, f"{label}_{ts}.avi")
        self.ts_path    = os.path.join(output_dir, f"{label}_timestamps.csv")

        self._frame_queue  = queue.Queue()
        self._stop         = threading.Event()
        self._thread       = None
        self.frame_count   = 0
        self.start_wall    = None
        self.end_wall      = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"writer-{self.cam_name}"
        )
        self._thread.start()

    def push_frame(self, frame, framecount, timestamp, sestime, cputime):
        self._frame_queue.put(("frame", frame, framecount, timestamp, sestime, cputime))

    def stop(self):
        self._frame_queue.put(("stop", None, None, None, None, None))
        if self._thread:
            self._thread.join(timeout=15.0)

    def _run(self):
        proc       = None
        csv_file   = None
        csv_writer = None
        self.start_wall = time.time()

        # Open timestamp CSV
        if self.metadata_cfg.get("enabled", True):
            csv_file   = open(self.ts_path, "w", newline="")
            csv_writer = csv.writer(csv_file)
            header = ["row_type"]
            if self.metadata_cfg.get("save_framecount", True):  header.append("framecount")
            if self.metadata_cfg.get("save_timestamp",  True):  header.append("camera_hw_timestamp_s")
            if self.metadata_cfg.get("save_sestime",    True):  header.append("sestime_s")
            if self.metadata_cfg.get("save_cputime",    True):  header.append("cpu_wall_clock_s")
            header += ["ttl_chamber", "ttl_label"]
            csv_writer.writerow(header)
            csv_file.flush()
            print(f"{self.cam_name}: timestamps → {self.ts_path}")

        q_val = max(2, min(31, int(2 + (100 - self.jpeg_quality) * 29 / 100)))

        while True:
            # Drain any pending TTL events first (non-blocking)
            while not self._ttl_queue.empty():
                try:
                    evt = self._ttl_queue.get_nowait()
                    if csv_writer:
                        row = ["ttl_event"]
                        cfg = self.metadata_cfg
                        if cfg.get("save_framecount", True): row.append("")
                        if cfg.get("save_timestamp",  True): row.append(f"{evt.wall:.6f}")
                        if cfg.get("save_sestime",    True): row.append(f"{evt.sestime:.6f}")
                        if cfg.get("save_cputime",    True): row.append(f"{evt.wall:.6f}")
                        row += [evt.chamber, evt.label]
                        csv_writer.writerow(row)
                        csv_file.flush()
                except queue.Empty:
                    break

            # Get next frame item
            try:
                item = self._frame_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            kind = item[0]
            if kind == "stop":
                break

            _, frame, framecount, timestamp, sestime, cputime = item

            # Init ffmpeg on first frame
            if proc is None:
                h, w = frame.shape[:2]
                if not shutil.which("ffmpeg"):
                    print(f"{self.cam_name}: ffmpeg not found — cannot write video.")
                    break
                cmd = [
                    "ffmpeg", "-y",
                    "-f",       "rawvideo",
                    "-vcodec",  "rawvideo",
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
                print(f"{self.cam_name}: writing → {self.video_path} ({w}×{h})")
                time.sleep(0.05)

            try:
                proc.stdin.write(frame.tobytes())
                self.frame_count += 1
            except BrokenPipeError:
                print(f"{self.cam_name}: ffmpeg pipe broken.")
                break

            # Log frame row
            if csv_writer:
                row = ["frame"]
                cfg = self.metadata_cfg
                if cfg.get("save_framecount", True): row.append(framecount)
                if cfg.get("save_timestamp",  True): row.append(f"{timestamp:.6f}")
                if cfg.get("save_sestime",    True): row.append(f"{sestime:.6f}")
                if cfg.get("save_cputime",    True): row.append(f"{cputime:.6f}")
                row += ["", ""]
                csv_writer.writerow(row)

        # Cleanup
        self.end_wall = time.time()
        if proc:
            proc.stdin.close()
            proc.wait()
            print(f"{self.cam_name}: ffmpeg finished.")
        if csv_file:
            csv_file.close()


# ---------------------------------------------------------------------------
# CameraStreamer
# ---------------------------------------------------------------------------

class CameraStreamer:

    def __init__(self, config: dict, system: "PySpin.SystemPtr"):
        self.config      = config
        self.system      = system
        self._stop_event = threading.Event()
        self.session_start = time.perf_counter()

        # Only enabled cameras
        self.cam_configs = {
            name: cfg
            for name, cfg in config["cameras"].items()
            if cfg.get("enabled", True)
        }
        self.cam_names = list(self.cam_configs.keys())
        self.cameras: dict[str, PySpin.Camera] = {}

        # Preview
        self.preview_frames = {n: None             for n in self.cam_names}
        self.preview_locks  = {n: threading.Lock() for n in self.cam_names}

        # Per-camera recording state
        self._recording: dict[str, bool]            = {n: False for n in self.cam_names}
        self._rec_lock  = threading.Lock()
        self._writers:   dict[str, CameraWriter]    = {}

        # TTL event queues: one per camera (writers drain these)
        self._ttl_queues: dict[str, queue.Queue] = {
            n: queue.Queue() for n in self.cam_names
        }

        # Rolling FPS stats
        self._stats_locks = {n: threading.Lock() for n in self.cam_names}
        self._stats = {
            n: {"total_frames": 0, "fps": 0.0,
                "_ts_ring": deque(maxlen=30)}
            for n in self.cam_names
        }

        self._capture_threads: list[threading.Thread] = []

        rec = config["recording"]
        self.fps          = rec["fps"]
        self.jpeg_quality = rec.get("jpeg_quality", 90)

        roi = config.get("roi", {})
        self.target_w = roi.get("width",    None)
        self.target_h = roi.get("height",   None)

        trig = config.get("trigger", {})
        self.trigger_enabled    = trig.get("enabled",    False)
        self.trigger_line       = trig.get("line",       "Line0")
        self.trigger_activation = trig.get("activation", "RisingEdge")
        self.trigger_selector   = trig.get("selector",   "AcquisitionStart")
        self.trigger_timeout    = trig.get("timeout_ms", 5000)

        self.metadata_cfg = config.get("metadata", {})

        save_dir   = config["save_dir"]
        experiment = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = os.path.join(save_dir, experiment)
        os.makedirs(self.output_dir, exist_ok=True)

        with open(os.path.join(self.output_dir, "config.yaml"), "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    # ------------------------------------------------------------------
    # Recording control — per-camera
    # ------------------------------------------------------------------

    def start_recording(self, cam_name: str):
        """Start recording for one camera. No-op if already recording."""
        with self._rec_lock:
            if self._recording.get(cam_name):
                return
            print(f"[Recorder] Starting {cam_name}...")
            writer = CameraWriter(
                cam_name     = cam_name,
                cam_cfg      = self.cam_configs[cam_name],
                output_dir   = self.output_dir,
                fps          = self.fps,
                jpeg_quality = self.jpeg_quality,
                metadata_cfg = self.metadata_cfg,
                ttl_queue    = self._ttl_queues[cam_name],
            )
            writer.start()
            self._writers[cam_name]   = writer
            self._recording[cam_name] = True

    def start_all_recording(self):
        for name in self.cam_names:
            self.start_recording(name)

    def stop_recording(self, cam_name: str):
        with self._rec_lock:
            if not self._recording.get(cam_name):
                return
            print(f"[Recorder] Stopping {cam_name}...")
            self._recording[cam_name] = False
            writer = self._writers.pop(cam_name, None)
            if writer:
                writer.stop()
                self._write_session_summary(cam_name, writer)

    def stop_all_recording(self):
        for name in list(self._writers.keys()):
            self.stop_recording(name)

    def is_recording(self, cam_name: str) -> bool:
        return self._recording.get(cam_name, False)

    def any_recording(self) -> bool:
        return any(self._recording.values())

    # ------------------------------------------------------------------
    # TTL event routing
    # ------------------------------------------------------------------

    def route_ttl_event(self, evt: TTLEvent, cam_name: str):
        """
        Push a TTL event to the correct camera's writer queue.
        If not yet recording, start recording first.
        If already recording, event is logged only (no new recording).
        """
        already = self.is_recording(cam_name)
        if not already:
            self.start_recording(cam_name)

        # Always log the event — queue is drained by writer
        self._ttl_queues[cam_name].put(evt)

        if already:
            print(f"[TTL] {evt.chamber} pulse logged to {cam_name} "
                  f"(already recording — no new file)")

    # ------------------------------------------------------------------
    # Session summary CSV
    # ------------------------------------------------------------------

    def _write_session_summary(self, cam_name: str, writer: CameraWriter):
        if not self.metadata_cfg.get("enabled", True):
            return

        label    = self.cam_configs[cam_name].get("name", cam_name)
        path     = os.path.join(self.output_dir, f"{label}_session.csv")
        duration = (writer.end_wall - writer.start_wall) if writer.end_wall else 0
        avg_fps  = writer.frame_count / duration if duration > 0 else 0
        start_dt = datetime.fromtimestamp(writer.start_wall)
        end_dt   = datetime.fromtimestamp(writer.end_wall or writer.start_wall)
        exp_meta = self.config.get("experiment_metadata", {})
        cam_cfg  = self.cam_configs[cam_name]
        expected = int(round(self.fps * duration))

        fields = {
            "experimenter_name":         exp_meta.get("experimenter_name", ""),
            "experiment_name":           exp_meta.get("experiment_name", ""),
            "camera_name":               label,
            "chamber":                   cam_cfg.get("chamber", ""),
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
            "avg_sampling_rate_hz":      f"{avg_fps:.4f}",
            "video_path":                os.path.abspath(writer.video_path),
            "timestamp_file_path":       os.path.abspath(writer.ts_path),
            "eeg_fiber_photometry_path": exp_meta.get("eeg_fiber_photometry_path", ""),
            "notes":                     exp_meta.get("notes", ""),
        }

        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(fields.keys()))
            w.writeheader()
            w.writerow(fields)
        print(f"{cam_name}: session summary → {path}")

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self, cam_name: str) -> dict:
        with self._stats_locks[cam_name]:
            s = self._stats[cam_name]
            return {"fps": s["fps"], "total_frames": s["total_frames"]}

    def _update_stats(self, cam_name: str, total: int):
        now = time.perf_counter()
        with self._stats_locks[cam_name]:
            s    = self._stats[cam_name]
            s["total_frames"] = total
            ring = s["_ts_ring"]
            ring.append(now)
            if len(ring) >= 2:
                elapsed = ring[-1] - ring[0]
                s["fps"] = (len(ring) - 1) / elapsed if elapsed > 0 else 0.0

    # ------------------------------------------------------------------
    # Spinnaker config
    # ------------------------------------------------------------------

    def _configure_trigger(self, nodemap, cam_name: str) -> bool:
        try:
            tm = PySpin.CEnumerationPtr(nodemap.GetNode("TriggerMode"))
            if PySpin.IsAvailable(tm) and PySpin.IsWritable(tm):
                tm.SetIntValue(tm.GetEntryByName("Off").GetValue())
            ts = PySpin.CEnumerationPtr(nodemap.GetNode("TriggerSelector"))
            if PySpin.IsAvailable(ts) and PySpin.IsWritable(ts):
                ts.SetIntValue(ts.GetEntryByName(self.trigger_selector).GetValue())
            tsrc = PySpin.CEnumerationPtr(nodemap.GetNode("TriggerSource"))
            if PySpin.IsAvailable(tsrc) and PySpin.IsWritable(tsrc):
                tsrc.SetIntValue(tsrc.GetEntryByName(self.trigger_line).GetValue())
            ta = PySpin.CEnumerationPtr(nodemap.GetNode("TriggerActivation"))
            if PySpin.IsAvailable(ta) and PySpin.IsWritable(ta):
                ta.SetIntValue(ta.GetEntryByName(self.trigger_activation).GetValue())
            if PySpin.IsAvailable(tm) and PySpin.IsWritable(tm):
                tm.SetIntValue(tm.GetEntryByName("On").GetValue())
            print(f"  {cam_name}: HW trigger ON — {self.trigger_line} {self.trigger_activation}")
            return True
        except PySpin.SpinnakerException as ex:
            print(f"  {cam_name} trigger config error: {ex}")
            return False

    def _configure_camera(self, cam, cam_name: str) -> bool:
        nodemap = cam.GetNodeMap()
        print(f"\n{cam_name}: configuring...")
        try:
            pf = PySpin.CEnumerationPtr(nodemap.GetNode("PixelFormat"))
            if PySpin.IsAvailable(pf) and PySpin.IsWritable(pf):
                for fmt in ["Mono8", "Mono16", "BayerRG8", "BGR8"]:
                    e = PySpin.CEnumEntryPtr(pf.GetEntryByName(fmt))
                    if PySpin.IsAvailable(e) and PySpin.IsReadable(e):
                        pf.SetIntValue(e.GetValue())
                        print(f"  Pixel format: {fmt}")
                        break

            if self.target_w and self.target_h:
                ox = PySpin.CIntegerPtr(nodemap.GetNode("OffsetX"))
                oy = PySpin.CIntegerPtr(nodemap.GetNode("OffsetY"))
                nw = PySpin.CIntegerPtr(nodemap.GetNode("Width"))
                nh = PySpin.CIntegerPtr(nodemap.GetNode("Height"))
                for n in [ox, oy]:
                    if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                        n.SetValue(n.GetMin())
                for node, target, label in [(nw, self.target_w, "Width"),
                                             (nh, self.target_h, "Height")]:
                    if PySpin.IsAvailable(node) and PySpin.IsWritable(node):
                        mn, inc = node.GetMin(), node.GetInc()
                        v = mn + ((min(target, node.GetMax()) - mn) // inc) * inc
                        node.SetValue(v)
                        print(f"  {label}: {node.GetValue()}")
                # Center offsets
                sensor_w = ox.GetMax() + nw.GetValue()
                sensor_h = oy.GetMax() + nh.GetValue()
                for node, sensor, actual in [(ox, sensor_w, nw.GetValue()),
                                              (oy, sensor_h, nh.GetValue())]:
                    if PySpin.IsAvailable(node) and PySpin.IsWritable(node):
                        inc = node.GetInc()
                        v   = (((sensor - actual) // 2) // inc) * inc
                        v   = max(node.GetMin(), min(node.GetMax(), v))
                        node.SetValue(v)

            if self.trigger_enabled:
                self._configure_trigger(nodemap, cam_name)

            acq = PySpin.CEnumerationPtr(nodemap.GetNode("AcquisitionMode"))
            if PySpin.IsAvailable(acq) and PySpin.IsWritable(acq):
                cont = acq.GetEntryByName("Continuous")
                if PySpin.IsAvailable(cont) and PySpin.IsReadable(cont):
                    acq.SetIntValue(cont.GetValue())
                    print("  AcquisitionMode: Continuous")

            cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)
            cam_cfg = self.cam_configs[cam_name]
            cam.ExposureTime.SetValue(min(15000, cam_cfg.get("exposure_us", 14000)))
            print(f"  Exposure: {cam.ExposureTime.GetValue()} µs")

            cam.GainAuto.SetValue(PySpin.GainAuto_Off)
            cam.Gain.SetValue(min(cam.Gain.GetMax(), cam_cfg.get("gain_db", 10)))
            print(f"  Gain: {cam.Gain.GetValue():.1f} dB")

            for node_name, val, lbl in [("IspEnable", False, "ISP disabled"),
                                         ("GammaEnable", False, "Gamma disabled")]:
                n = PySpin.CBooleanPtr(nodemap.GetNode(node_name))
                if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                    n.SetValue(val); print(f"  {lbl}")

            n = PySpin.CFloatPtr(nodemap.GetNode("BlackLevel"))
            if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                n.SetValue(cam_cfg.get("black_level", 2.0))

            n = PySpin.CIntegerPtr(nodemap.GetNode("DeviceLinkThroughputLimit"))
            if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                tl = max(n.GetMin(), min(n.GetMax(), cam_cfg.get("throughput_limit", 90_000_000)))
                n.SetValue(tl)

            return True
        except PySpin.SpinnakerException as ex:
            print(f"  {cam_name} configure error: {ex}")
            return False

    def _find_cameras(self) -> bool:
        cam_list       = self.system.GetCameras()
        serials_wanted = {cfg["serial"]: name
                          for name, cfg in self.cam_configs.items()}
        for cam in cam_list:
            node = PySpin.CStringPtr(
                cam.GetTLDeviceNodeMap().GetNode("DeviceSerialNumber"))
            if PySpin.IsAvailable(node) and PySpin.IsReadable(node):
                s = node.GetValue()
                if s in serials_wanted:
                    self.cameras[serials_wanted[s]] = cam
        cam_list.Clear()
        missing = [n for n in self.cam_names if n not in self.cameras]
        if missing:
            print(f"Could not find cameras: {missing}")
            return False
        return True

    def _init_cameras(self):
        if not self._find_cameras():
            raise RuntimeError("Not all configured cameras were found.")
        for name, cam in self.cameras.items():
            cam.Init()
            self._configure_camera(cam, name)
            cam.BeginAcquisition()
            print(f"{name}: streaming (preview only)")

    # ------------------------------------------------------------------
    # Capture thread — always running; routes to writer only if recording
    # ------------------------------------------------------------------

    def _capture_frame(self, cam_name: str, cam):
        downsample = self.config.get("preview", {}).get("downsample", 1)
        frame_idx  = 0

        while not self._stop_event.is_set():
            try:
                image = cam.GetNextImage(self.trigger_timeout)
                if image.IsIncomplete():
                    image.Release()
                    continue

                framecount = image.GetFrameID()
                timestamp  = image.GetTimeStamp() * 1e-9
                sestime    = time.perf_counter() - self.session_start
                cputime    = time.time()
                frame      = np.array(image.GetNDArray(), copy=True)
                image.Release()

                frame_idx += 1
                self._update_stats(cam_name, frame_idx)

                if (frame_idx - 1) % downsample == 0:
                    with self.preview_locks[cam_name]:
                        self.preview_frames[cam_name] = frame

                # Only push to writer when this camera is recording
                if self._recording.get(cam_name):
                    writer = self._writers.get(cam_name)
                    if writer:
                        writer.push_frame(frame, framecount, timestamp,
                                          sestime, cputime)

            except PySpin.SpinnakerException as ex:
                if not self._stop_event.is_set():
                    print(f"{cam_name} capture error: {ex}")

    def _start_threads(self):
        for name, cam in self.cameras.items():
            ct = threading.Thread(target=self._capture_frame,
                                  args=(name, cam), daemon=True,
                                  name=f"capture-{name}")
            ct.start()
            self._capture_threads.append(ct)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def get_preview(self, cam_name: str):
        with self.preview_locks[cam_name]:
            return self.preview_frames[cam_name]

    def stop(self):
        print("\nStopping...")
        self.stop_all_recording()
        self._stop_event.set()
        for cam in self.cameras.values():
            try:
                cam.EndAcquisition()
                cam.DeInit()
            except Exception:
                pass
        self.cameras.clear()


# ---------------------------------------------------------------------------
# Preview overlay
# ---------------------------------------------------------------------------

def draw_overlay(frame: np.ndarray, stats: dict, cam_label: str,
                 recording: bool, chamber: str, auto_start: bool) -> np.ndarray:
    if frame.ndim == 2:
        display = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    else:
        display = frame.copy()

    fps   = stats.get("fps", 0.0)
    total = stats.get("total_frames", 0)

    rec_text  = "● REC" if recording else "○ PREVIEW"
    mode_text = f"MODE: {'AUTO' if auto_start else 'MANUAL/TTL'}"
    lines = [
        cam_label,
        rec_text,
        mode_text,
        f"Chamber : {chamber}",
        f"FPS     : {fps:6.2f}",
        f"Frames  : {total:>8,}",
    ]

    rec_color  = (0, 0, 255) if recording else (180, 180, 180)
    text_color = (0, 255, 255)
    font       = cv2.FONT_HERSHEY_SIMPLEX
    scale      = 0.52
    thick      = 1
    line_h     = 20
    pad        = 8

    max_w = max(cv2.getTextSize(l, font, scale, thick)[0][0] for l in lines)
    box_h = line_h * len(lines) + pad
    box_w = max_w + pad * 2

    ov = display.copy()
    cv2.rectangle(ov, (0, 0), (box_w, box_h), (0, 0, 0), -1)
    cv2.addWeighted(ov, 0.55, display, 0.45, 0, display)

    for i, line in enumerate(lines):
        y = pad + (i + 1) * line_h - 4
        color = (255, 255, 255) if i == 0 else (rec_color if i == 1 else text_color)
        cv2.putText(display, line, (pad, y), font, scale, color, thick, cv2.LINE_AA)

    h, w = display.shape[:2]
    hint = "R: start/stop rec  |  ESC: quit"
    cv2.putText(display, hint, (pad, h - 8), font, 0.40,
                (100, 100, 100), 1, cv2.LINE_AA)

    return display


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def print_device_info(nodemap, cam_name: str):
    print(f"\nDevice info for {cam_name}:")
    try:
        di = PySpin.CCategoryPtr(nodemap.GetNode("DeviceInformation"))
        if PySpin.IsAvailable(di) and PySpin.IsReadable(di):
            for feat in di.GetFeatures():
                nf = PySpin.CValuePtr(feat)
                val = nf.ToString() if PySpin.IsReadable(nf) else "n/a"
                print(f"  {nf.GetName()}: {val}")
    except PySpin.SpinnakerException as ex:
        print(f"  Error: {ex}")


def get_connected_serials(system: "PySpin.SystemPtr") -> list[dict]:
    found    = []
    cam_list = system.GetCameras()
    for cam in cam_list:
        tlmap = cam.GetTLDeviceNodeMap()
        def _r(n):
            node = PySpin.CStringPtr(tlmap.GetNode(n))
            return node.GetValue() if PySpin.IsAvailable(node) and PySpin.IsReadable(node) else "unknown"
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

    print(f"\nFound {len(devices)} camera(s):\n")
    for i, d in enumerate(devices):
        print(f"  [{i}]  Serial: {d['serial']}   Model: {d['model']}")

    raw      = input("\nCamera indices to include (Enter = all): ").strip()
    selected = list(range(len(devices))) if not raw else [int(x) for x in raw.split(",")]

    cameras_cfg  = {}
    chambers_cfg = {}

    for i in selected:
        d    = devices[i]
        name = input(f"\nCam {i} ({d['serial']}) friendly name [cam{i}]: ").strip() or f"cam{i}"
        ch   = input(f"  Chamber label for cam{i} (e.g. A1): ").strip() or f"chamber_{i}"

        cameras_cfg[f"cam{i}"] = {
            "serial":           d["serial"],
            "name":             name,
            "chamber":          ch,
            "enabled":          True,
            "exposure_us":      14000,
            "gain_db":          10,
            "black_level":      2.0,
            "throughput_limit": 90_000_000,
        }
        chambers_cfg[ch] = {
            "camera": f"cam{i}",
            "ttl": {
                "port":            input(f"  Serial port for {ch} TTL [COM1]: ").strip() or "COM1",
                "baud":            115200,
                "command":         105,
                "pin":             0,
                "polarity":        1,
                "poll_interval_ms": 10,
                "label":           ch,
            }
        }

    save_dir = input("\nSave directory [./recordings]: ").strip() or "./recordings"

    config = {
        "save_dir": save_dir,
        "cameras":  cameras_cfg,
        "chambers": chambers_cfg,
        "acquisition": {
            "auto_start": False,
        },
        "recording": {
            "fps":           59.99,
            "jpeg_quality":  90,
            "split_size_mb": None,
        },
        "roi": {
            "width": 1020, "height": 1020,
            "offset_x": 0, "offset_y": 0,
        },
        "trigger": {
            "enabled": False, "line": "Line0",
            "activation": "RisingEdge",
            "selector": "AcquisitionStart",
            "timeout_ms": 5000,
        },
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
    print("Run:  python multiAcquisition.py -c config.yaml")
    print("GUI:  python config.py -c config.yaml")


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

    cam_list = system.GetCameras()
    print(f"\nCameras detected: {cam_list.GetSize()}")
    for i, cam in enumerate(cam_list):
        print_device_info(cam.GetTLDeviceNodeMap(), f"cam{i}")
    cam_list.Clear()

    streamer = CameraStreamer(config, system)
    streamer._init_cameras()
    streamer._start_threads()

    # Build chamber → camera map and start one TTL listener per chamber
    chambers_cfg = config.get("chambers", {})
    # shared event queue between all listeners; main loop routes by chamber
    ttl_dispatch_queue: queue.Queue = queue.Queue()

    listeners: list[ChamberTTLListener] = []
    chamber_to_cam: dict[str, str] = {}

    for chamber_name, ch_cfg in chambers_cfg.items():
        cam_key = ch_cfg.get("camera")
        if not cam_key or cam_key not in streamer.cam_names:
            print(f"[Warning] Chamber {chamber_name} maps to unknown camera {cam_key!r} — skipped.")
            continue
        chamber_to_cam[chamber_name] = cam_key

        listener = ChamberTTLListener(
            chamber_name   = chamber_name,
            ttl_cfg        = ch_cfg.get("ttl", {}),
            event_queue    = ttl_dispatch_queue,
            session_start  = streamer.session_start,
        )
        listener.start()
        listeners.append(listener)

    # Auto-start toggle
    acq_cfg    = config.get("acquisition", {})
    auto_start = acq_cfg.get("auto_start", False)

    if auto_start:
        print("[Main] Auto-start enabled — beginning recording immediately.")
        streamer.start_all_recording()

    preview_enabled = config.get("preview", {}).get("enabled", True)
    print("\nRunning — preview active.")
    print("  R   : start / stop ALL cameras")
    print("  ESC : quit\n")

    try:
        while True:
            # --- Preview ---
            if preview_enabled:
                for name in streamer.cam_names:
                    frame = streamer.get_preview(name)
                    if frame is not None:
                        cam_cfg = config["cameras"][name]
                        label   = cam_cfg.get("name", name)
                        chamber = cam_cfg.get("chamber", "")
                        stats   = streamer.get_stats(name)
                        display = draw_overlay(
                            frame, stats, label,
                            streamer.is_recording(name),
                            chamber, auto_start,
                        )
                        cv2.imshow(label, display)

            key = cv2.waitKey(20) & 0xFF

            # R — manual toggle all cameras
            if key in (ord("r"), ord("R")):
                if streamer.any_recording():
                    streamer.stop_all_recording()
                else:
                    streamer.start_all_recording()

            if key == 27:
                break

            # --- Route TTL events from all chambers ---
            while not ttl_dispatch_queue.empty():
                try:
                    evt = ttl_dispatch_queue.get_nowait()
                    cam_key = chamber_to_cam.get(evt.chamber)
                    if cam_key:
                        streamer.route_ttl_event(evt, cam_key)
                except queue.Empty:
                    break

    finally:
        streamer.stop()
        for lst in listeners:
            lst.stop()
        if preview_enabled:
            cv2.destroyAllWindows()
        cam_list = system.GetCameras()
        cam_list.Clear()
        del cam_list
        system.ReleaseInstance()
        print("Done.")


if __name__ == "__main__":
    main()
