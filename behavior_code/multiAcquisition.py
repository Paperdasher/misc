## Imports

import os
import threading
import queue
import subprocess
import shutil
import time
import argparse
from datetime import datetime

import numpy as np
import yaml
import csv
import PySpin


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# CameraStreamer
# ---------------------------------------------------------------------------

class CameraStreamer:
    def __init__(self, config: dict, system: "PySpin.SystemPtr"):
        self.config       = config
        self.system       = system
        self._stop_event  = threading.Event()
        self.start_t      = time.perf_counter()

        self.cam_configs = {
            name: cfg
            for name, cfg in config["cameras"].items()
            if cfg.get("enabled", True)
        }

        # Camera properties
        self.camera_count = len(self.cam_configs)
        self.cam_names    = list(self.cam_configs.keys())
        self.cameras: dict[str, PySpin.Camera] = {}

        self.preview_frames = {name: None             for name in self.cam_names}
        self.preview_locks  = {name: threading.Lock() for name in self.cam_names}
        self.writer_queues  = {name: queue.Queue()    for name in self.cam_names}

        self._final_ttl_counts:   dict[str, int] = {}
        self._final_frame_counts: dict[str, int] = {}

        # Live stats for preview overlay
        self._stats_locks        = {name: threading.Lock() for name in self.cam_names}
        self._stats: dict[str, dict] = {
            name: {
                "buffered_frames": 0,
                "total_frames":    0,
                "fps":             0.0,
                # ring buffer for FPS rolling average (last N frame timestamps)
                "_ts_ring":        [],
                "_ring_size":      30,
            }
            for name in self.cam_names
        }


        self._capture_threads: list[threading.Thread] = []
        self._writer_threads:  list[threading.Thread] = []

        # Configurations for recording video
        rec = config["recording"]
        self.fps              = rec["fps"]
        self.jpeg_quality     = rec.get("jpeg_quality", 90)
        self.split_size_mb    = rec.get("split_size_mb", None)

        # Configurations for region of interest(roi)
        roi = config.get("roi", {})
        self.target_w = roi.get("width",    None)
        self.target_h = roi.get("height",   None)
        self.offset_x = roi.get("offset_x", 0)
        self.offset_y = roi.get("offset_y", 0)

        # Configuration for triggering start of recording(TTL vs manually)
        trig = config.get("trigger", {})
        self.trigger_enabled    = trig.get("enabled",    False)
        self.trigger_line       = trig.get("line",       "Line0")
        self.trigger_activation = trig.get("activation", "RisingEdge")
        self.trigger_selector   = trig.get("selector",   "AcquisitionStart")
        self.trigger_timeout    = trig.get("timeout_ms", 5000)

        self.metadata_config = config.get("metadata", {})

        # Creating new experiment folder named the experiment start datetime inside initially chosen folder
        save_dir   = config["save_dir"]
        experiment = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_dir = os.path.join(save_dir, experiment)
        os.makedirs(self.output_dir, exist_ok=True)

        # Saves copy of config file in new experiment folder
        with open(os.path.join(self.output_dir, "config.yaml"), "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    # ------------------------------------------------------------------
    # Live stats helpers
    # ------------------------------------------------------------------

    def get_stats(self, cam_name: str) -> dict:
        # Return a snapshot of live capture stats for the given camera
        with self._stats_locks[cam_name]:
            s = self._stats[cam_name]
            return {
                "buffered_frames": self.writer_queues[cam_name].qsize(),
                "total_frames":    s["total_frames"],
                "fps":             s["fps"],
            }

    def _update_stats(self, cam_name: str, total_frames: int):
        # Update rolling FPS and total frame count (called from capture thread)
        now = time.perf_counter()
        with self._stats_locks[cam_name]:
            s = self._stats[cam_name]
            s["total_frames"] = total_frames
            ring = s["_ts_ring"]
            ring.append(now)
            if len(ring) > s["_ring_size"]:
                ring.pop(0)
            if len(ring) >= 2:
                elapsed = ring[-1] - ring[0]
                s["fps"] = (len(ring) - 1) / elapsed if elapsed > 0 else 0.0
            s["buffered_frames"] = self.writer_queues[cam_name].qsize()

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def _init_metadata(self, cam_name: str):
        """
        Opens the per-frame timestamp CSV and writes its header.
        Returns (file_handle, csv_writer) or (None, None) if metadata disabled.
        """
        if not self.metadata_config.get("enabled", False):
            return None, None

        cfg   = self.metadata_config
        label = self.cam_configs[cam_name].get("name", cam_name)
        path  = os.path.join(self.output_dir, f"{label}_timestamps.csv") # timestamp by frame csv

        f      = open(path, "w", newline="")
        writer = csv.writer(f)

        header = []
        if cfg.get("save_framecount", True): header.append("framecount")
        if cfg.get("save_timestamp",  True): header.append("camera_hardware_timestamp_s") # camera timestamp
        if cfg.get("save_sestime",    True): header.append("sestime_s") # time elapsed 
        if cfg.get("save_cputime",    True): header.append("cpu_wall_clock_s") # computer timestamp

        writer.writerow(header)
        f.flush()

        print(f"{cam_name}: timestamps → {path}")
        return f, writer

    def _append_metadata(self, writer, framecount, timestamp, sestime, cputime):
        # Append one per-frame row to the timestamp CSV
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
        self,
        cam_name: str,
        video_path: str,
        timestamp_path: str,
        start_wall: float,
        end_wall: float,
        total_frames: int,
        ttl_count: int,
    ):
        """
        Write a one-row session summary CSV once acquisition is complete.
        Includes all metadata fields from config (experimenter, animal info, etc.)
        """
        if not self.metadata_config.get("enabled", False):
            return

        label    = self.cam_configs[cam_name].get("name", cam_name)
        path     = os.path.join(self.output_dir, f"{label}_session.csv")
        duration = end_wall - start_wall
        avg_fps  = total_frames / duration if duration > 0 else 0.0

        start_dt = datetime.fromtimestamp(start_wall)
        end_dt   = datetime.fromtimestamp(end_wall)

        # Grab optional extended metadata from config
        exp_meta = self.config.get("experiment_metadata", {})
        cam_cfg  = self.cam_configs[cam_name]

        # Estimate dropped frames: expected - actual
        expected_frames = int(round(self.fps * duration))
        frames_dropped  = max(0, expected_frames - total_frames)

        fields = {
            # --- Identity ---
            "experimenter_name":       exp_meta.get("experimenter_name", ""),
            "experiment_name":         exp_meta.get("experiment_name", ""),
            "camera_name":             label,
            "chamber":                 cam_cfg.get("chamber", exp_meta.get("chamber", "")),
            # --- Animal ---
            "animal_id":               exp_meta.get("animal_id", ""),
            "genotype":                exp_meta.get("genotype", ""),
            "group":                   exp_meta.get("group", ""),
            "schedule_name":           exp_meta.get("schedule_name", ""),
            # --- Timing ---
            "date":                    start_dt.strftime("%Y-%m-%d"),
            "acquisition_start":       start_dt.strftime("%H:%M:%S.%f"),
            "acquisition_end":         end_dt.strftime("%H:%M:%S.%f"),
            "duration_s":              f"{duration:.3f}",
            # --- Frame stats ---
            "total_frames":            total_frames,
            "frames_dropped":          frames_dropped,
            "ttl_pulses_received":     ttl_count,
            "avg_sampling_rate_hz":    f"{avg_fps:.4f}",
            # --- Paths ---
            "video_path":              os.path.abspath(video_path),
            "timestamp_file_path":     os.path.abspath(timestamp_path) if timestamp_path else "",
            "computer_timestamps_from": (
                "time.time() — Unix epoch seconds (UTC), synced to system clock"
            ),
            # --- Co-recordings ---
            "eeg_fiber_photometry_path": exp_meta.get("eeg_fiber_photometry_path", ""),
            # --- Notes ---
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

        # Go through PySpin node map to obtain serial num of cameras
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
    # Spinnaker configuration
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

            print(
                f"  {cam_name}: trigger ON — {self.trigger_line} "
                f"{self.trigger_activation} {self.trigger_selector}"
            )
            return True

        except PySpin.SpinnakerException as ex:
            print(f"  {cam_name} trigger config error: {ex}")
            return False

    def _configure_camera(self, cam, cam_name: str) -> bool:
        nodemap = cam.GetNodeMap()
        print(f"\n{cam_name}: configuring...")

        # Pixel format chosen out of Mono8, Mono16, BayerRG8, BGR8
        try:
            node_pixel_format = PySpin.CEnumerationPtr(nodemap.GetNode("PixelFormat"))
            if PySpin.IsAvailable(node_pixel_format) and PySpin.IsWritable(node_pixel_format):
                for fmt_name in ["Mono8", "Mono16", "BayerRG8", "BGR8"]:
                    node_entry = PySpin.CEnumEntryPtr(node_pixel_format.GetEntryByName(fmt_name))
                    if PySpin.IsAvailable(node_entry) and PySpin.IsReadable(node_entry):
                        node_pixel_format.SetIntValue(node_entry.GetValue())
                        print(f"  Pixel format: {fmt_name}")
                        break
            else:
                print("  Warning: could not set any preferred pixel format")

            # Offset X and Y for ROI
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
                    w_set = min(self.target_w, node_width.GetMax())
                    w_set = w_min + ((w_set - w_min) // w_inc) * w_inc
                    node_width.SetValue(w_set)
                    actual_w = node_width.GetValue()
                    print(f"  Width: {actual_w}")

                actual_h = node_height.GetMax()
                if PySpin.IsAvailable(node_height) and PySpin.IsWritable(node_height):
                    h_min = node_height.GetMin()
                    h_inc = node_height.GetInc()
                    h_set = min(self.target_h, node_height.GetMax())
                    h_set = h_min + ((h_set - h_min) // h_inc) * h_inc
                    node_height.SetValue(h_set)
                    actual_h = node_height.GetValue()
                    print(f"  Height: {actual_h}")

                sensor_w = node_offset_x.GetMax() + actual_w
                sensor_h = node_offset_y.GetMax() + actual_h

                # Calculate X and Y center based on offset
                if PySpin.IsAvailable(node_offset_x) and PySpin.IsWritable(node_offset_x):
                    x_inc    = node_offset_x.GetInc()
                    x_center = (sensor_w - actual_w) // 2
                    x_set    = (x_center // x_inc) * x_inc
                    x_set    = max(node_offset_x.GetMin(), min(node_offset_x.GetMax(), x_set))
                    node_offset_x.SetValue(x_set)
                    print(f"  OffsetX: {x_set}  (centered on {sensor_w}px sensor)")

                if PySpin.IsAvailable(node_offset_y) and PySpin.IsWritable(node_offset_y):
                    y_inc    = node_offset_y.GetInc()
                    y_center = (sensor_h - actual_h) // 2
                    y_set    = (y_center // y_inc) * y_inc
                    y_set    = max(node_offset_y.GetMin(), min(node_offset_y.GetMax(), y_set))
                    node_offset_y.SetValue(y_set)
                    print(f"  OffsetY: {y_set}  (centered on {sensor_h}px sensor)")

            if self.trigger_enabled:
                self._configure_trigger(nodemap, cam_name)

            # Always choose continuous acquisition
            node_acq_mode = PySpin.CEnumerationPtr(nodemap.GetNode("AcquisitionMode"))
            if PySpin.IsAvailable(node_acq_mode) and PySpin.IsWritable(node_acq_mode):
                node_continuous = node_acq_mode.GetEntryByName("Continuous")
                if PySpin.IsAvailable(node_continuous) and PySpin.IsReadable(node_continuous):
                    node_acq_mode.SetIntValue(node_continuous.GetValue())
                    print(f"  AcquisitionMode: Continuous")
                else:
                    print(f"  {cam_name}: Continuous mode not available.")
                    return False

            if cam.ExposureAuto.GetAccessMode() != PySpin.RW:
                print('Unable to disable automatic exposure. Aborting...')
                return False

            cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)
            print('Automatic exposure disabled...')

            # Exposure time no more than 15000us
            cam_cfg = self.cam_configs[cam_name]
            exposure_time_to_set = cam_cfg.get("exposure_us", 14000)
            exposure_time_to_set = min(15000, exposure_time_to_set)
            cam.ExposureTime.SetValue(exposure_time_to_set)
            print('Shutter time set to %s us...\n' % exposure_time_to_set)

            
            cam.GainAuto.SetValue(PySpin.GainAuto_Off)
            gain_to_set = cam_cfg.get("gain_db", 10)
            gain_to_set = min(cam.Gain.GetMax(), gain_to_set)
            cam.Gain.SetValue(gain_to_set)
            print(f'Gain set to {gain_to_set} dB')

            node_isp = PySpin.CBooleanPtr(nodemap.GetNode("IspEnable"))
            if PySpin.IsAvailable(node_isp) and PySpin.IsWritable(node_isp):
                node_isp.SetValue(False)
                print("ISP disabled")

            node_gamma_en = PySpin.CBooleanPtr(nodemap.GetNode("GammaEnable"))
            if PySpin.IsAvailable(node_gamma_en) and PySpin.IsWritable(node_gamma_en):
                node_gamma_en.SetValue(False)
                print("Gamma disabled")

            node_gamma = PySpin.CFloatPtr(nodemap.GetNode("Gamma"))
            if PySpin.IsAvailable(node_gamma) and PySpin.IsWritable(node_gamma):
                node_gamma.SetValue(1.0)
                print("Gamma set to 1.0")

            node_bl = PySpin.CFloatPtr(nodemap.GetNode("BlackLevel"))
            if PySpin.IsAvailable(node_bl) and PySpin.IsWritable(node_bl):
                bl = cam_cfg.get("black_level", 2.0)
                node_bl.SetValue(bl)
                print(f"Black level set to {bl}")

            node_dlt = PySpin.CIntegerPtr(nodemap.GetNode("DeviceLinkThroughputLimit"))
            if PySpin.IsAvailable(node_dlt) and PySpin.IsWritable(node_dlt):
                throughput_limit = cam_cfg.get("throughput_limit", 90_000_000)
                throughput_limit = max(node_dlt.GetMin(), min(node_dlt.GetMax(), throughput_limit))
                node_dlt.SetValue(throughput_limit)
                print(f"Device link throughput limit set to: {throughput_limit}")

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
            status = "armed, waiting for TTL" if self.trigger_enabled else "acquiring"
            print(f"{name}: {status}")

    # ------------------------------------------------------------------
    # Capture thread
    # ------------------------------------------------------------------

    def _capture_frame(self, cam_name: str, cam):
        preview_cfg = self.config.get("preview", {})
        downsample  = preview_cfg.get("downsample", 1)
        frame_idx   = 0
        ttl_count   = 0

        while not self._stop_event.is_set():
            try:
                image_result = cam.GetNextImage(self.trigger_timeout)

                if image_result.IsIncomplete():
                    print(
                        f"{cam_name}: incomplete image "
                        f"(status {image_result.GetImageStatus()})"
                    )
                    image_result.Release()
                    continue

                framecount = image_result.GetFrameID()
                timestamp  = image_result.GetTimeStamp() * 1e-9
                sestime    = time.perf_counter() - self.start_t
                cputime    = time.time()

                frame = np.array(image_result.GetNDArray(), copy=True)
                image_result.Release()

                ttl_count += 1
                frame_idx += 1

                # Update live stats
                self._update_stats(cam_name, frame_idx)

                if (frame_idx - 1) % downsample == 0:
                    with self.preview_locks[cam_name]:
                        self.preview_frames[cam_name] = frame

                self.writer_queues[cam_name].put(
                    (frame, framecount, timestamp, sestime, cputime)
                )

            except PySpin.SpinnakerException as ex:
                if not self._stop_event.is_set():
                    print(f"{cam_name} capture error: {ex}")

        self._final_ttl_counts[cam_name]   = ttl_count
        self._final_frame_counts[cam_name] = frame_idx

    # ------------------------------------------------------------------
    # Writer thread  (MJPEG → AVI)
    # ------------------------------------------------------------------

    def _make_ffmpeg_mjpeg_writer(
        self, output_path: str, width: int, height: int
    ) -> subprocess.Popen:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found on PATH")

        q_val = max(2, min(31, int(2 + (100 - self.jpeg_quality) * 29 / 100)))

        cmd = [
            "ffmpeg",
            "-y",
            "-f",        "rawvideo",
            "-vcodec",   "rawvideo",
            "-pix_fmt",  "gray",
            "-s",        f"{width}x{height}",
            "-r",        str(self.fps),
            "-i",        "pipe:0",
            "-vcodec",   "mjpeg",
            "-q:v",      str(q_val),
            "-pix_fmt",  "yuvj420p",
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

        def _write_one(item):
            nonlocal proc, frame_count
            frame, framecount, timestamp, sestime, cputime = item

            if proc is None:
                h, w = frame.shape[:2]
                proc = self._make_ffmpeg_mjpeg_writer(output_path, w, h)
                print(
                    f"{cam_name}: writing MJPEG/AVI → {output_path} "
                    f"at {w}x{h}  quality={self.jpeg_quality}"
                )
                time.sleep(0.1)

            try:
                proc.stdin.write(frame.tobytes())
                frame_count += 1
            except BrokenPipeError:
                print(f"{cam_name}: ffmpeg pipe broken, restarting writer...")
                proc = None

            self._append_metadata(csv_writer, framecount, timestamp, sestime, cputime)

        while not self._stop_event.is_set():
            try:
                item = self.writer_queues[cam_name].get(timeout=1.0)
                _write_one(item)
            except queue.Empty:
                continue

        while not self.writer_queues[cam_name].empty():
            try:
                _write_one(self.writer_queues[cam_name].get_nowait())
            except (queue.Empty, BrokenPipeError):
                break

        end_wall = time.time()

        if proc:
            proc.stdin.close()
            proc.wait()
            print(f"{cam_name}: ffmpeg writer finished.")

        if metadata_file is not None:
            metadata_file.close()
            print(f"{cam_name}: timestamp file closed.")

        ttl_count = self._final_ttl_counts.get(cam_name, frame_count)

        self._write_session_summary(
            cam_name       = cam_name,
            video_path     = output_path,
            timestamp_path = timestamp_path,
            start_wall     = start_wall,
            end_wall       = end_wall,
            total_frames   = frame_count,
            ttl_count      = ttl_count,
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

            wt = threading.Thread(
                target=self._write_frames,
                args=(name,),
                daemon=True,
                name=f"writer-{name}",
            )
            wt.start()
            self._writer_threads.append(wt)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_preview(self, cam_name: str):
        """Non-blocking. Returns latest grayscale numpy frame or None."""
        with self.preview_locks[cam_name]:
            return self.preview_frames[cam_name]

    def stop(self):
        print("\nStopping acquisition...")
        self._stop_event.set()

        for wt in self._writer_threads:
            wt.join(timeout=5.0)

        for cam in self.cameras.values():
            cam.EndAcquisition()
            cam.DeInit()

        self.cameras.clear()


# ---------------------------------------------------------------------------
# Preview overlay helper
# ---------------------------------------------------------------------------

def draw_stats_overlay(frame: np.ndarray, stats: dict, cam_label: str) -> np.ndarray:
    """
    Burn live acquisition stats into the preview frame as a semi-transparent
    HUD in the top-left corner.

    Parameters
    ----------
    frame      : grayscale or BGR uint8 numpy array from the camera
    stats      : dict with keys buffered_frames, total_frames, fps
    cam_label  : friendly name shown in the overlay title

    Returns a BGR frame (always, so cv2.imshow renders colour text).
    """
    import cv2

    # Convert to BGR for colored text indicating stats
    if frame.ndim == 2:
        display = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    else:
        display = frame.copy()

    buffered = stats["buffered_frames"]
    total    = stats["total_frames"]
    fps      = stats["fps"]

    lines = [
        cam_label,
        f"FPS      : {fps:6.2f}",
        f"Frames   : {total:>8,}",
        f"Buffered : {buffered:>8,}",
    ]

    # Yellow normally, red if buffer is growing large
    text_color = (0, 255, 255) if buffered < 50 else (0, 80, 255)

    font       = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 0.55
    thickness  = 1
    line_h     = 22
    pad        = 8

    # Measure widest line for background rectangle
    max_w = max(cv2.getTextSize(l, font, font_scale, thickness)[0][0] for l in lines)
    box_h = line_h * len(lines) + pad
    box_w = max_w + pad * 2

    # Semi-transparent dark background
    overlay = display.copy()
    cv2.rectangle(overlay, (0, 0), (box_w, box_h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, display, 0.45, 0, display)

    for i, line in enumerate(lines):
        y = pad + (i + 1) * line_h - 4
        # First line (camera name) in brighter white
        color = (255, 255, 255) if i == 0 else text_color
        cv2.putText(display, line, (pad, y), font, font_scale, color, thickness, cv2.LINE_AA)

    return display


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def print_device_info(nodemap, cam_name: str) -> bool:
    # Info such as serial num extracted from node of camera
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
# Setup wizard — auto-discover cameras and write config.yaml interactively
# ---------------------------------------------------------------------------

def run_setup_wizard(system: "PySpin.SystemPtr", output_path: str = "config.yaml"):
    print("\n" + "="*60)
    print("  Camera Acquisition — Setup Wizard")
    print("="*60)

    # Find cameras from node map
    devices = get_connected_serials(system)
    if not devices:
        print("No cameras detected. Check USB/GigE connections and drivers.")
        return

    # Print cameras found from node map
    print(f"\nFound {len(devices)} camera(s):\n")
    for i, d in enumerate(devices):
        print(f"  [{i}]  Serial: {d['serial']}   Model: {d['model']}   Vendor: {d['vendor']}")

    print("\nEnter camera indices to include (comma-separated, e.g. 0,1) or press Enter for all:")
    raw = input("  > ").strip()
    if raw == "":
        selected = list(range(len(devices)))
    else:
        try:
            selected = [int(x.strip()) for x in raw.split(",")]
        except ValueError:
            print("Invalid input — including all cameras.")
            selected = list(range(len(devices)))

    cameras_cfg = {}
    for i in selected:
        d = devices[i]
        default_name = f"cam{i}"
        print(f"\nCamera {i}  (serial {d['serial']}, {d['model']})")
        name = input(f"  Friendly name [{default_name}]: ").strip() or default_name
        chamber = input(f"  Chamber/arena label (e.g. A1) []: ").strip()
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

        # Extended experiment / animal metadata — editable via the GUI
        "experiment_metadata": {
            "experimenter_name":        "",
            "experiment_name":          "",
            "animal_id":                "",
            "genotype":                 "",
            "group":                    "",
            "schedule_name":            "",
            "eeg_fiber_photometry_path": "",
            "notes":                    "",
        },
    }

    with open(output_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"\nConfig written to: {os.path.abspath(output_path)}")
    print("Run acquisition with:  python multiAcquisition.py -c config.yaml\n")
    print("Open the GUI with:     python config.py -c config.yaml\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Multi-camera MJPEG/AVI acquisition.")
    parser.add_argument(
        "-c", "--config",
        type=str,
        default="config.yaml",
        help="Path to configuration YAML file.",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help=(
            "Run the interactive setup wizard to auto-detect cameras and "
            "generate a config.yaml without manually looking up serial numbers."
        ),
    )
    args = parser.parse_args()

    system  = PySpin.System.GetInstance()
    version = system.GetLibraryVersion()
    print(
        f"Spinnaker {version.major}.{version.minor}."
        f"{version.type}.{version.build}"
    )

    if args.setup:
        run_setup_wizard(system, output_path=args.config)
        system.ReleaseInstance()
        return

    if not os.path.isfile(args.config):
        print(f"Config file not found: {args.config}")
        print("Tip: run  python camera_acquisition.py --setup  to generate one.")
        system.ReleaseInstance()
        return

    config = load_config(args.config)

    try:
        test = open("test.txt", "w+")
        test.close()
        os.remove(test.name)
    except IOError:
        print("Cannot write to current directory. Check permissions.")
        system.ReleaseInstance()
        return

    cam_list = system.GetCameras()
    print(f"\nCameras detected: {cam_list.GetSize()}")
    for i, cam in enumerate(cam_list):
        print_device_info(cam.GetTLDeviceNodeMap(), f"cam{i}")
    cam_list.Clear()

    streamer = CameraStreamer(config, system)
    streamer._init_cameras()
    streamer._start_threads()

    preview_enabled = config.get("preview", {}).get("enabled", True)
    print("\nRunning — press ESC to stop.\n")

    try:
        import cv2
        while True:
            if preview_enabled:
                for name in streamer.cam_names:
                    frame = streamer.get_preview(name)
                    if frame is not None:
                        label   = config["cameras"][name].get("name", name)
                        stats   = streamer.get_stats(name)
                        display = draw_stats_overlay(frame, stats, label)
                        cv2.imshow(label, display)

            if cv2.waitKey(20) == 27:  # ESC
                break

    finally:
        streamer.stop()
        if preview_enabled:
            import cv2
            cv2.destroyAllWindows()
        cam_list = system.GetCameras()
        cam_list.Clear()
        del cam_list
        system.ReleaseInstance()
        print("Done.")


if __name__ == "__main__":
    main()