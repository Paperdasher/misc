"""
multiAcquisition.py — Multi-camera MJPEG/AVI acquisition
=========================================================
Usage
-----
    python multiAcquisition.py -c config.yaml
    python multiAcquisition.py --setup          # interactive wizard

Recording triggers
------------------
    • Press  R  in any preview window to start recording manually.
    • TTL pulse on the Morelia/Pinnacle POD device (polled via serial,
      configured under  morelia_ttl  in config.yaml).
    • Both triggers are independent; whichever fires first starts recording.

Preview runs immediately on startup. Recording starts only on trigger.
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
# Morelia TTL Listener  (GET TTL IN — Command 105 or configured)
# ---------------------------------------------------------------------------

class TTLListener:
    """
    Polls a Morelia/Pinnacle POD device over serial using the binary
    GET TTL IN packet (doc-1 methodology: [cmd U8][pin U8] → [cmd U8][state U8]).

    Fires self._triggered once the configured polarity is detected.
    Safe to construct even when ttl config is absent or disabled.
    """

    def __init__(self, ttl_cfg: dict):
        self.enabled     = ttl_cfg.get("enabled", False)
        self.port        = ttl_cfg.get("port",         "COM3")
        self.baud        = ttl_cfg.get("baud",         115200)
        self.cmd         = ttl_cfg.get("command",      105)
        self.pin         = ttl_cfg.get("pin",          0)
        self.polarity    = ttl_cfg.get("polarity",     1)   # 1=High/rising 0=Low/falling
        self.interval    = ttl_cfg.get("poll_interval_ms", 10) / 1000.0

        self._stop      = threading.Event()
        self._triggered = threading.Event()
        self._thread    = None

    # ---- public -----------------------------------------------------------

    def start(self):
        if not self.enabled:
            return
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="ttl-listener"
        )
        self._thread.start()
        edge = "RISING (High)" if self.polarity == 1 else "FALLING (Low)"
        print(f"[TTL] Polling pin {self.pin} via cmd {self.cmd} "
              f"on {self.port} @ {self.baud} baud — trigger on {edge}")

    def wait_for_trigger(self, timeout=0) -> bool:
        return self._triggered.wait(timeout=timeout)

    def triggered(self) -> bool:
        return self._triggered.is_set()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    # ---- internal ---------------------------------------------------------

    def _poll_loop(self):
        try:
            import serial
        except ImportError:
            print("[TTL] pyserial not installed. pip install pyserial")
            return

        try:
            ser = serial.Serial(port=self.port, baudrate=self.baud, timeout=0.1)
        except serial.SerialException as e:
            print(f"[TTL] Cannot open {self.port}: {e}")
            return

        print("[TTL] Serial open — waiting for pulse...")
        with ser:
            while not self._stop.is_set():
                # Build [cmd U8][pin U8]
                ser.reset_input_buffer()
                ser.write(struct.pack("BB", self.cmd, self.pin))
                resp = ser.read(2)

                if len(resp) == 2:
                    cmd_echo, state = struct.unpack("BB", resp)
                    if cmd_echo == self.cmd and state == self.polarity:
                        ts = datetime.now().strftime("%H:%M:%S.%f")
                        print(f"[TTL] Trigger detected (pin {self.pin}) at {ts}")
                        self._triggered.set()
                        break

                time.sleep(self.interval)

        print("[TTL] Poll loop exited.")


# ---------------------------------------------------------------------------
# CameraStreamer
# ---------------------------------------------------------------------------

class CameraStreamer:
    def __init__(self, config: dict, system: "PySpin.SystemPtr"):
        self.config      = config
        self.system      = system
        self._stop_event = threading.Event()
        self.start_t     = time.perf_counter()

        # Only include enabled cameras
        self.cam_configs = {
            name: cfg
            for name, cfg in config["cameras"].items()
            if cfg.get("enabled", True)
        }

        self.camera_count = len(self.cam_configs)
        self.cam_names    = list(self.cam_configs.keys())
        self.cameras: dict[str, PySpin.Camera] = {}

        self.preview_frames = {name: None             for name in self.cam_names}
        self.preview_locks  = {name: threading.Lock() for name in self.cam_names}

        # Per-camera writer queues — None means frame is dropped (not recording)
        self.writer_queues: dict[str, queue.Queue | None] = {
            name: None for name in self.cam_names
        }

        self._final_ttl_counts:   dict[str, int] = {}
        self._final_frame_counts: dict[str, int] = {}

        # ---- Recording state ----
        # Shared event: set = recording active, clear = preview-only
        self._recording = threading.Event()
        self._rec_lock  = threading.Lock()

        # Live stats
        self._stats_locks = {name: threading.Lock() for name in self.cam_names}
        self._stats: dict[str, dict] = {
            name: {
                "buffered_frames": 0,
                "total_frames":    0,
                "fps":             0.0,
                "_ts_ring":        [],
                "_ring_size":      30,
            }
            for name in self.cam_names
        }

        self._capture_threads: list[threading.Thread] = []
        self._writer_threads:  list[threading.Thread] = []

        rec = config["recording"]
        self.fps           = rec["fps"]
        self.jpeg_quality  = rec.get("jpeg_quality", 90)
        self.split_size_mb = rec.get("split_size_mb", None)

        roi = config.get("roi", {})
        self.target_w = roi.get("width",    None)
        self.target_h = roi.get("height",   None)
        self.offset_x = roi.get("offset_x", 0)
        self.offset_y = roi.get("offset_y", 0)

        # Camera hardware trigger settings (separate from Morelia TTL)
        trig = config.get("trigger", {})
        self.trigger_enabled    = trig.get("enabled",    False)
        self.trigger_line       = trig.get("line",       "Line0")
        self.trigger_activation = trig.get("activation", "RisingEdge")
        self.trigger_selector   = trig.get("selector",   "AcquisitionStart")
        self.trigger_timeout    = trig.get("timeout_ms", 5000)

        self.metadata_config = config.get("metadata", {})

        save_dir   = config["save_dir"]
        experiment = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = os.path.join(save_dir, experiment)
        os.makedirs(self.output_dir, exist_ok=True)

        with open(os.path.join(self.output_dir, "config.yaml"), "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    # ------------------------------------------------------------------
    # Recording control  (called from main thread or TTL listener)
    # ------------------------------------------------------------------

    def start_recording(self):
        """
        Activate recording for all cameras.
        Creates writer queues and launches writer threads if not yet running.
        Safe to call multiple times — only acts on first call.
        """
        with self._rec_lock:
            if self._recording.is_set():
                return  # already recording

            print("[Recorder] Starting recording...")

            for name in self.cam_names:
                # Create a fresh queue so the writer thread can consume frames
                self.writer_queues[name] = queue.Queue()

                wt = threading.Thread(
                    target=self._write_frames,
                    args=(name,),
                    daemon=True,
                    name=f"writer-{name}",
                )
                wt.start()
                self._writer_threads.append(wt)

            self._recording.set()
            print("[Recorder] Recording active — press R to stop, or Ctrl+C.")

    def stop_recording(self):
        """Stop recording for all cameras (flush queues, close files)."""
        with self._rec_lock:
            if not self._recording.is_set():
                return

            print("[Recorder] Stopping recording...")
            self._recording.clear()

            # Signal each writer to finish by pushing sentinel None
            for name in self.cam_names:
                q = self.writer_queues[name]
                if q is not None:
                    q.put(None)

            for wt in self._writer_threads:
                wt.join(timeout=10.0)
            self._writer_threads.clear()

            # Reset queues so a future start_recording() gets fresh ones
            for name in self.cam_names:
                self.writer_queues[name] = None

            print("[Recorder] Recording stopped.")

    @property
    def is_recording(self) -> bool:
        return self._recording.is_set()

    # ------------------------------------------------------------------
    # Live stats
    # ------------------------------------------------------------------

    def get_stats(self, cam_name: str) -> dict:
        with self._stats_locks[cam_name]:
            s = self._stats[cam_name]
            q = self.writer_queues.get(cam_name)
            return {
                "buffered_frames": q.qsize() if q is not None else 0,
                "total_frames":    s["total_frames"],
                "fps":             s["fps"],
            }

    def _update_stats(self, cam_name: str, total_frames: int):
        now = time.perf_counter()
        with self._stats_locks[cam_name]:
            s    = self._stats[cam_name]
            s["total_frames"] = total_frames
            ring = s["_ts_ring"]
            ring.append(now)
            if len(ring) > s["_ring_size"]:
                ring.pop(0)
            if len(ring) >= 2:
                elapsed = ring[-1] - ring[0]
                s["fps"] = (len(ring) - 1) / elapsed if elapsed > 0 else 0.0

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def _init_metadata(self, cam_name: str):
        if not self.metadata_config.get("enabled", False):
            return None, None

        cfg   = self.metadata_config
        label = self.cam_configs[cam_name].get("name", cam_name)
        path  = os.path.join(self.output_dir, f"{label}_timestamps.csv")

        f      = open(path, "w", newline="")
        writer = csv.writer(f)

        header = []
        if cfg.get("save_framecount", True): header.append("framecount")
        if cfg.get("save_timestamp",  True): header.append("camera_hardware_timestamp_s")
        if cfg.get("save_sestime",    True): header.append("sestime_s")
        if cfg.get("save_cputime",    True): header.append("cpu_wall_clock_s")

        writer.writerow(header)
        f.flush()
        print(f"{cam_name}: timestamps → {path}")
        return f, writer

    def _append_metadata(self, writer, framecount, timestamp, sestime, cputime):
        if writer is None:
            return
        cfg = self.metadata_config
        row = []
        if cfg.get("save_framecount", True): row.append(framecount)
        if cfg.get("save_timestamp",  True): row.append(f"{timestamp:.9f}")
        if cfg.get("save_sestime",    True): row.append(f"{sestime:.6f}")
        if cfg.get("save_cputime",    True): row.append(f"{cputime:.6f}")
        writer.writerow(row)

    def _write_session_summary(
        self, cam_name, video_path, timestamp_path,
        start_wall, end_wall, total_frames, ttl_count,
    ):
        if not self.metadata_config.get("enabled", False):
            return

        label    = self.cam_configs[cam_name].get("name", cam_name)
        path     = os.path.join(self.output_dir, f"{label}_session.csv")
        duration = end_wall - start_wall
        avg_fps  = total_frames / duration if duration > 0 else 0.0
        start_dt = datetime.fromtimestamp(start_wall)
        end_dt   = datetime.fromtimestamp(end_wall)
        exp_meta = self.config.get("experiment_metadata", {})
        cam_cfg  = self.cam_configs[cam_name]
        expected = int(round(self.fps * duration))

        fields = {
            "experimenter_name":       exp_meta.get("experimenter_name", ""),
            "experiment_name":         exp_meta.get("experiment_name", ""),
            "camera_name":             label,
            "chamber":                 cam_cfg.get("chamber", exp_meta.get("chamber", "")),
            "animal_id":               exp_meta.get("animal_id", ""),
            "genotype":                exp_meta.get("genotype", ""),
            "group":                   exp_meta.get("group", ""),
            "schedule_name":           exp_meta.get("schedule_name", ""),
            "date":                    start_dt.strftime("%Y-%m-%d"),
            "acquisition_start":       start_dt.strftime("%H:%M:%S.%f"),
            "acquisition_end":         end_dt.strftime("%H:%M:%S.%f"),
            "duration_s":              f"{duration:.3f}",
            "total_frames":            total_frames,
            "frames_dropped":          max(0, expected - total_frames),
            "ttl_pulses_received":     ttl_count,
            "avg_sampling_rate_hz":    f"{avg_fps:.4f}",
            "video_path":              os.path.abspath(video_path),
            "timestamp_file_path":     os.path.abspath(timestamp_path) if timestamp_path else "",
            "computer_timestamps_from":"time.time() — Unix epoch seconds (UTC)",
            "eeg_fiber_photometry_path": exp_meta.get("eeg_fiber_photometry_path", ""),
            "notes":                   exp_meta.get("notes", ""),
        }

        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(fields.keys()))
            writer.writeheader()
            writer.writerow(fields)
        print(f"{cam_name}: session summary → {path}")

    # ------------------------------------------------------------------
    # Camera lookup
    # ------------------------------------------------------------------

    def _find_cameras(self) -> bool:
        cam_list = self.system.GetCameras()
        serials_wanted = {
            cfg["serial"]: name
            for name, cfg in self.cam_configs.items()
        }
        for cam in cam_list:
            node = PySpin.CStringPtr(
                cam.GetTLDeviceNodeMap().GetNode("DeviceSerialNumber")
            )
            if PySpin.IsAvailable(node) and PySpin.IsReadable(node):
                serial = node.GetValue()
                if serial in serials_wanted:
                    self.cameras[serials_wanted[serial]] = cam
        cam_list.Clear()

        missing = [n for n in self.cam_names if n not in self.cameras]
        if missing:
            print(f"Could not find cameras: {missing}")
            return False
        return True

    # ------------------------------------------------------------------
    # Spinnaker configuration (unchanged from original)
    # ------------------------------------------------------------------

    def _configure_trigger(self, nodemap, cam_name: str) -> bool:
        try:
            node_trigger_mode = PySpin.CEnumerationPtr(nodemap.GetNode("TriggerMode"))
            if PySpin.IsAvailable(node_trigger_mode) and PySpin.IsWritable(node_trigger_mode):
                node_trigger_mode.SetIntValue(
                    node_trigger_mode.GetEntryByName("Off").GetValue()
                )
            node_trigger_selector = PySpin.CEnumerationPtr(nodemap.GetNode("TriggerSelector"))
            if PySpin.IsAvailable(node_trigger_selector) and PySpin.IsWritable(node_trigger_selector):
                node_trigger_selector.SetIntValue(
                    node_trigger_selector.GetEntryByName(self.trigger_selector).GetValue()
                )
            node_trigger_source = PySpin.CEnumerationPtr(nodemap.GetNode("TriggerSource"))
            if PySpin.IsAvailable(node_trigger_source) and PySpin.IsWritable(node_trigger_source):
                node_trigger_source.SetIntValue(
                    node_trigger_source.GetEntryByName(self.trigger_line).GetValue()
                )
            node_trigger_activation = PySpin.CEnumerationPtr(nodemap.GetNode("TriggerActivation"))
            if PySpin.IsAvailable(node_trigger_activation) and PySpin.IsWritable(node_trigger_activation):
                node_trigger_activation.SetIntValue(
                    node_trigger_activation.GetEntryByName(self.trigger_activation).GetValue()
                )
            if PySpin.IsAvailable(node_trigger_mode) and PySpin.IsWritable(node_trigger_mode):
                node_trigger_mode.SetIntValue(
                    node_trigger_mode.GetEntryByName("On").GetValue()
                )
            print(f"  {cam_name}: trigger ON — {self.trigger_line} "
                  f"{self.trigger_activation} {self.trigger_selector}")
            return True
        except PySpin.SpinnakerException as ex:
            print(f"  {cam_name} trigger config error: {ex}")
            return False

    def _configure_camera(self, cam, cam_name: str) -> bool:
        nodemap = cam.GetNodeMap()
        print(f"\n{cam_name}: configuring...")
        try:
            node_pixel_format = PySpin.CEnumerationPtr(nodemap.GetNode("PixelFormat"))
            if PySpin.IsAvailable(node_pixel_format) and PySpin.IsWritable(node_pixel_format):
                for fmt_name in ["Mono8", "Mono16", "BayerRG8", "BGR8"]:
                    node_entry = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName(fmt_name))
                    if PySpin.IsAvailable(node_entry) and PySpin.IsReadable(node_entry):
                        node_pixel_format.SetIntValue(node_entry.GetValue())
                        print(f"  Pixel format: {fmt_name}")
                        break

            if self.target_w is not None and self.target_h is not None:
                node_offset_x = PySpin.CIntegerPtr(nodemap.GetNode("OffsetX"))
                node_offset_y = PySpin.CIntegerPtr(nodemap.GetNode("OffsetY"))
                node_width    = PySpin.CIntegerPtr(nodemap.GetNode("Width"))
                node_height   = PySpin.CIntegerPtr(nodemap.GetNode("Height"))

                if PySpin.IsAvailable(node_offset_x) and PySpin.IsWritable(node_offset_x):
                    node_offset_x.SetValue(node_offset_x.GetMin())
                if PySpin.IsAvailable(node_offset_y) and PySpin.IsWritable(node_offset_y):
                    node_offset_y.SetValue(node_offset_y.GetMin())

                actual_w = node_width.GetMax()
                if PySpin.IsAvailable(node_width) and PySpin.IsWritable(node_width):
                    w_min = node_width.GetMin()
                    w_inc = node_width.GetInc()
                    w_set = w_min + ((min(self.target_w, node_width.GetMax()) - w_min) // w_inc) * w_inc
                    node_width.SetValue(w_set)
                    actual_w = node_width.GetValue()
                    print(f"  Width: {actual_w}")

                actual_h = node_height.GetMax()
                if PySpin.IsAvailable(node_height) and PySpin.IsWritable(node_height):
                    h_min = node_height.GetMin()
                    h_inc = node_height.GetInc()
                    h_set = h_min + ((min(self.target_h, node_height.GetMax()) - h_min) // h_inc) * h_inc
                    node_height.SetValue(h_set)
                    actual_h = node_height.GetValue()
                    print(f"  Height: {actual_h}")

                sensor_w = node_offset_x.GetMax() + actual_w
                sensor_h = node_offset_y.GetMax() + actual_h

                if PySpin.IsAvailable(node_offset_x) and PySpin.IsWritable(node_offset_x):
                    x_inc = node_offset_x.GetInc()
                    x_set = ((((sensor_w - actual_w) // 2)) // x_inc) * x_inc
                    x_set = max(node_offset_x.GetMin(), min(node_offset_x.GetMax(), x_set))
                    node_offset_x.SetValue(x_set)
                    print(f"  OffsetX: {x_set}")

                if PySpin.IsAvailable(node_offset_y) and PySpin.IsWritable(node_offset_y):
                    y_inc = node_offset_y.GetInc()
                    y_set = ((((sensor_h - actual_h) // 2)) // y_inc) * y_inc
                    y_set = max(node_offset_y.GetMin(), min(node_offset_y.GetMax(), y_set))
                    node_offset_y.SetValue(y_set)
                    print(f"  OffsetY: {y_set}")

            if self.trigger_enabled:
                self._configure_trigger(nodemap, cam_name)

            node_acq_mode = PySpin.CEnumerationPtr(nodemap.GetNode("AcquisitionMode"))
            if PySpin.IsAvailable(node_acq_mode) and PySpin.IsWritable(node_acq_mode):
                node_continuous = node_acq_mode.GetEntryByName("Continuous")
                if PySpin.IsAvailable(node_continuous) and PySpin.IsReadable(node_continuous):
                    node_acq_mode.SetIntValue(node_continuous.GetValue())
                    print("  AcquisitionMode: Continuous")
                else:
                    print(f"  {cam_name}: Continuous mode not available.")
                    return False

            if cam.ExposureAuto.GetAccessMode() != PySpin.RW:
                print("Unable to disable automatic exposure.")
                return False
            cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)

            cam_cfg = self.cam_configs[cam_name]
            cam.ExposureTime.SetValue(min(15000, cam_cfg.get("exposure_us", 14000)))
            print(f"  Exposure: {cam.ExposureTime.GetValue()} µs")

            cam.GainAuto.SetValue(PySpin.GainAuto_Off)
            cam.Gain.SetValue(min(cam.Gain.GetMax(), cam_cfg.get("gain_db", 10)))
            print(f"  Gain: {cam.Gain.GetValue():.1f} dB")

            for node_name, val, label in [
                ("IspEnable",    False, "ISP disabled"),
                ("GammaEnable",  False, "Gamma disabled"),
            ]:
                n = PySpin.CBooleanPtr(nodemap.GetNode(node_name))
                if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                    n.SetValue(val); print(f"  {label}")

            n = PySpin.CFloatPtr(nodemap.GetNode("Gamma"))
            if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                n.SetValue(1.0); print("  Gamma: 1.0")

            n = PySpin.CFloatPtr(nodemap.GetNode("BlackLevel"))
            if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                bl = cam_cfg.get("black_level", 2.0)
                n.SetValue(bl); print(f"  BlackLevel: {bl}")

            n = PySpin.CIntegerPtr(nodemap.GetNode("DeviceLinkThroughputLimit"))
            if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                tl = max(n.GetMin(), min(n.GetMax(), cam_cfg.get("throughput_limit", 90_000_000)))
                n.SetValue(tl); print(f"  ThroughputLimit: {tl}")

            return True

        except PySpin.SpinnakerException as ex:
            print(f"  {cam_name} configure error: {ex}")
            return False

    def _init_cameras(self):
        if not self._find_cameras():
            raise RuntimeError("Not all configured cameras were found.")
        for name, cam in self.cameras.items():
            cam.Init()
            self._configure_camera(cam, name)
            cam.BeginAcquisition()
            print(f"{name}: streaming (preview only — not recording yet)")

    # ------------------------------------------------------------------
    # Capture thread — always runs; frames only queued when recording
    # ------------------------------------------------------------------

    def _capture_frame(self, cam_name: str, cam):
        preview_cfg = self.config.get("preview", {})
        downsample  = preview_cfg.get("downsample", 1)
        frame_idx   = 0

        while not self._stop_event.is_set():
            try:
                image_result = cam.GetNextImage(self.trigger_timeout)

                if image_result.IsIncomplete():
                    image_result.Release()
                    continue

                framecount = image_result.GetFrameID()
                timestamp  = image_result.GetTimeStamp() * 1e-9
                sestime    = time.perf_counter() - self.start_t
                cputime    = time.time()

                frame = np.array(image_result.GetNDArray(), copy=True)
                image_result.Release()

                frame_idx += 1
                self._update_stats(cam_name, frame_idx)

                # Always update preview
                if (frame_idx - 1) % downsample == 0:
                    with self.preview_locks[cam_name]:
                        self.preview_frames[cam_name] = frame

                # Only push to writer queue when actively recording
                if self._recording.is_set():
                    q = self.writer_queues.get(cam_name)
                    if q is not None:
                        q.put((frame, framecount, timestamp, sestime, cputime))

            except PySpin.SpinnakerException as ex:
                if not self._stop_event.is_set():
                    print(f"{cam_name} capture error: {ex}")

        self._final_frame_counts[cam_name] = frame_idx

    # ------------------------------------------------------------------
    # Writer thread — MJPEG → AVI
    # ------------------------------------------------------------------

    def _make_ffmpeg_writer(self, output_path, width, height):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found on PATH")
        q_val = max(2, min(31, int(2 + (100 - self.jpeg_quality) * 29 / 100)))
        cmd = [
            "ffmpeg", "-y",
            "-f",       "rawvideo",
            "-vcodec",  "rawvideo",
            "-pix_fmt", "gray",
            "-s",       f"{width}x{height}",
            "-r",       str(self.fps),
            "-i",       "pipe:0",
            "-vcodec",  "mjpeg",
            "-q:v",     str(q_val),
            "-pix_fmt", "yuvj420p",
            output_path,
        ]
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def _write_frames(self, cam_name: str):
        cam_cfg     = self.cam_configs[cam_name]
        label       = cam_cfg.get("name", cam_name)
        ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(self.output_dir, f"{label}_{ts}.avi")

        proc                      = None
        metadata_file, csv_writer = self._init_metadata(cam_name)
        timestamp_path            = (
            os.path.join(self.output_dir, f"{label}_timestamps.csv")
            if metadata_file is not None else None
        )
        start_wall  = time.time()
        frame_count = 0

        q = self.writer_queues[cam_name]

        while True:
            try:
                item = q.get(timeout=1.0)
            except queue.Empty:
                # If recording was stopped via sentinel, queue will have None;
                # if not, keep draining
                if not self._recording.is_set():
                    break
                continue

            if item is None:
                break   # sentinel pushed by stop_recording()

            frame, framecount, timestamp, sestime, cputime = item

            if proc is None:
                h, w = frame.shape[:2]
                proc = self._make_ffmpeg_writer(output_path, w, h)
                print(f"{cam_name}: writing → {output_path}  ({w}×{h}  q={self.jpeg_quality})")
                time.sleep(0.1)

            try:
                proc.stdin.write(frame.tobytes())
                frame_count += 1
            except BrokenPipeError:
                print(f"{cam_name}: ffmpeg pipe broken.")
                break

            self._append_metadata(csv_writer, framecount, timestamp, sestime, cputime)

        end_wall = time.time()

        if proc:
            proc.stdin.close()
            proc.wait()
            print(f"{cam_name}: ffmpeg writer finished.")

        if metadata_file is not None:
            metadata_file.close()

        ttl_count = self._final_ttl_counts.get(cam_name, frame_count)
        self._write_session_summary(
            cam_name, output_path, timestamp_path,
            start_wall, end_wall, frame_count, ttl_count,
        )

    # ------------------------------------------------------------------
    # Thread startup
    # ------------------------------------------------------------------

    def _start_threads(self):
        for name, cam in self.cameras.items():
            ct = threading.Thread(
                target=self._capture_frame,
                args=(name, cam),
                daemon=True,
                name=f"capture-{name}",
            )
            ct.start()
            self._capture_threads.append(ct)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_preview(self, cam_name: str):
        with self.preview_locks[cam_name]:
            return self.preview_frames[cam_name]

    def stop(self):
        print("\nStopping acquisition...")
        if self.is_recording:
            self.stop_recording()
        self._stop_event.set()

        for cam in self.cameras.values():
            cam.EndAcquisition()
            cam.DeInit()
        self.cameras.clear()


# ---------------------------------------------------------------------------
# Preview overlay
# ---------------------------------------------------------------------------

def draw_stats_overlay(
    frame: np.ndarray,
    stats: dict,
    cam_label: str,
    recording: bool,
    ttl_enabled: bool,
) -> np.ndarray:
    if frame.ndim == 2:
        display = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    else:
        display = frame.copy()

    buffered = stats["buffered_frames"]
    total    = stats["total_frames"]
    fps      = stats["fps"]

    rec_text = "● REC" if recording else "○ PREVIEW"
    lines = [
        cam_label,
        rec_text,
        f"FPS      : {fps:6.2f}",
        f"Frames   : {total:>8,}",
        f"Buffered : {buffered:>8,}",
    ]

    text_color = (0, 255, 255) if buffered < 50 else (0, 80, 255)
    rec_color  = (0, 0, 255)   if recording      else (180, 180, 180)

    font       = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.55
    thickness  = 1
    line_h     = 22
    pad        = 8

    max_w = max(cv2.getTextSize(l, font, font_scale, thickness)[0][0] for l in lines)
    box_h = line_h * len(lines) + pad
    box_w = max_w + pad * 2

    overlay = display.copy()
    cv2.rectangle(overlay, (0, 0), (box_w, box_h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, display, 0.45, 0, display)

    for i, line in enumerate(lines):
        y = pad + (i + 1) * line_h - 4
        if   i == 0: color = (255, 255, 255)
        elif i == 1: color = rec_color
        else:        color = text_color
        cv2.putText(display, line, (pad, y), font, font_scale,
                    color, thickness, cv2.LINE_AA)

    # Bottom hint bar
    hint = "R: start/stop rec"
    if ttl_enabled:
        hint += "  |  TTL: armed"
    h, w = display.shape[:2]
    hint_y = h - 10
    cv2.putText(display, hint, (pad, hint_y), font, 0.45,
                (120, 120, 120), 1, cv2.LINE_AA)

    return display


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def print_device_info(nodemap, cam_name: str) -> bool:
    print(f"\nDevice information for {cam_name}:")
    try:
        node_device_information = PySpin.CCategoryPtr(
            nodemap.GetNode("DeviceInformation")
        )
        if PySpin.IsAvailable(node_device_information) and PySpin.IsReadable(
            node_device_information
        ):
            for feature in node_device_information.GetFeatures():
                node_feature = PySpin.CValuePtr(feature)
                print(
                    f"  {node_feature.GetName()}: "
                    + (
                        node_feature.ToString()
                        if PySpin.IsReadable(node_feature)
                        else "Node not readable"
                    )
                )
        return True
    except PySpin.SpinnakerException as ex:
        print(f"  Error: {ex}")
        return False


def get_connected_serials(system: "PySpin.SystemPtr") -> list[dict]:
    found = []
    cam_list = system.GetCameras()
    for cam in cam_list:
        tlmap = cam.GetTLDeviceNodeMap()
        def _read(node_name):
            n = PySpin.CStringPtr(tlmap.GetNode(node_name))
            return n.GetValue() if PySpin.IsAvailable(n) and PySpin.IsReadable(n) else "unknown"
        found.append({
            "serial": _read("DeviceSerialNumber"),
            "model":  _read("DeviceModelName"),
            "vendor": _read("DeviceVendorName"),
        })
    cam_list.Clear()
    return found


# ---------------------------------------------------------------------------
# Setup wizard
# ---------------------------------------------------------------------------

def run_setup_wizard(system: "PySpin.SystemPtr", output_path: str = "config.yaml"):
    print("\n" + "="*60)
    print("  Camera Acquisition — Setup Wizard")
    print("="*60)

    devices = get_connected_serials(system)
    if not devices:
        print("No cameras detected. Check USB/GigE connections and drivers.")
        return

    print(f"\nFound {len(devices)} camera(s):\n")
    for i, d in enumerate(devices):
        print(f"  [{i}]  Serial: {d['serial']}   Model: {d['model']}   Vendor: {d['vendor']}")

    print("\nEnter camera indices to include (comma-separated) or press Enter for all:")
    raw = input("  > ").strip()
    selected = list(range(len(devices))) if not raw else [int(x) for x in raw.split(",")]

    cameras_cfg = {}
    for i in selected:
        d = devices[i]
        default_name = f"cam{i}"
        print(f"\nCamera {i}  (serial {d['serial']}, {d['model']})")
        name    = input(f"  Friendly name [{default_name}]: ").strip() or default_name
        chamber = input(f"  Chamber/arena label []: ").strip()
        cameras_cfg[f"cam{i}"] = {
            "serial":           d["serial"],
            "name":             name,
            "chamber":          chamber,
            "enabled":          True,
            "exposure_us":      14000,
            "gain_db":          10,
            "black_level":      2.0,
            "throughput_limit": 90_000_000,
        }

    print("\nSave directory for recordings [./recordings]: ", end="")
    save_dir = input().strip() or "./recordings"

    config = {
        "save_dir": save_dir,
        "cameras":  cameras_cfg,
        "recording": {
            "fps":           59.99,
            "jpeg_quality":  90,
            "split_size_mb": None,
        },
        "roi": {
            "width":    1020,
            "height":   1020,
            "offset_x": 0,
            "offset_y": 0,
        },
        "trigger": {
            "enabled":    False,
            "line":       "Line0",
            "activation": "RisingEdge",
            "selector":   "AcquisitionStart",
            "timeout_ms": 5000,
        },
        "preview": {
            "enabled":    True,
            "downsample": 1,
        },
        "metadata": {
            "enabled":         True,
            "save_framecount": True,
            "save_timestamp":  True,
            "save_sestime":    True,
            "save_cputime":    True,
        },
        # Morelia TTL — PC-side polling (separate from camera hardware trigger)
        "morelia_ttl": {
            "enabled":           False,
            "port":              "COM3",
            "baud":              115200,
            "command":           105,
            "pin":               0,
            "polarity":          1,
            "poll_interval_ms":  10,
        },
        "experiment_metadata": {
            "experimenter_name":         "",
            "experiment_name":           "",
            "animal_id":                 "",
            "genotype":                  "",
            "group":                     "",
            "schedule_name":             "",
            "eeg_fiber_photometry_path": "",
            "notes":                     "",
        },
    }

    with open(output_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"\nConfig written to: {os.path.abspath(output_path)}")
    print("Run acquisition with:  python multiAcquisition.py -c config.yaml\n")
    print("Open the GUI with:     python config.py -c config.yaml\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Multi-camera MJPEG/AVI acquisition.")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--setup", action="store_true")
    args = parser.parse_args()

    system  = PySpin.System.GetInstance()
    version = system.GetLibraryVersion()
    print(f"Spinnaker {version.major}.{version.minor}.{version.type}.{version.build}")

    if args.setup:
        run_setup_wizard(system, output_path=args.config)
        system.ReleaseInstance()
        return

    if not os.path.isfile(args.config):
        print(f"Config file not found: {args.config}")
        print("Tip: run  python multiAcquisition.py --setup  to generate one.")
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

    # Start Morelia TTL listener (no-op if disabled in config)
    ttl_cfg      = config.get("morelia_ttl", {})
    ttl_listener = TTLListener(ttl_cfg)
    ttl_listener.start()
    ttl_enabled = ttl_cfg.get("enabled", False)

    preview_enabled = config.get("preview", {}).get("enabled", True)
    print("\nRunning — preview active.")
    print("  R       : start / stop recording")
    print("  ESC     : quit\n")

    try:
        while True:
            if preview_enabled:
                for name in streamer.cam_names:
                    frame = streamer.get_preview(name)
                    if frame is not None:
                        label   = config["cameras"][name].get("name", name)
                        stats   = streamer.get_stats(name)
                        display = draw_stats_overlay(
                            frame, stats, label,
                            streamer.is_recording, ttl_enabled
                        )
                        cv2.imshow(label, display)

            key = cv2.waitKey(20) & 0xFF

            # R — manual record toggle
            if key == ord("r") or key == ord("R"):
                if streamer.is_recording:
                    streamer.stop_recording()
                else:
                    streamer.start_recording()

            # ESC — quit
            if key == 27:
                break

            # TTL trigger check (non-blocking)
            if ttl_enabled and not streamer.is_recording:
                if ttl_listener.wait_for_trigger(timeout=0):
                    print("[Main] TTL trigger received — starting recording.")
                    streamer.start_recording()

    finally:
        streamer.stop()
        ttl_listener.stop()
        if preview_enabled:
            cv2.destroyAllWindows()
        cam_list = system.GetCameras()
        cam_list.Clear()
        del cam_list
        system.ReleaseInstance()
        print("Done.")


if __name__ == "__main__":
    main()