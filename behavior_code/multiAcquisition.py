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
        """Append one per-frame row to the timestamp CSV."""
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
        This is the file you'll merge with your neurorecording session log.
        """
        if not self.metadata_config.get("enabled", False):
            return

        label = self.cam_configs[cam_name].get("name", cam_name)
        path  = os.path.join(self.output_dir, f"{label}_session.csv")

        start_dt = datetime.fromtimestamp(start_wall)
        end_dt   = datetime.fromtimestamp(end_wall)

        fields = {
            "camera_name":          label,
            "date":                 start_dt.strftime("%Y-%m-%d"),
            "acquisition_start":    start_dt.strftime("%H:%M:%S.%f"),
            "acquisition_end":      end_dt.strftime("%H:%M:%S.%f"),
            "duration_s":           f"{end_wall - start_wall:.3f}",
            "total_frames":         total_frames,
            "ttl_pulses_received":  ttl_count,
            "video_path":           os.path.abspath(video_path),
            "timestamp_file_path":  os.path.abspath(timestamp_path)
                if timestamp_path else "",
            "computer_timestamps_from": (
                "time.time() — Unix epoch seconds (UTC), "
                "synced to system clock"
            ),
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
        """Match Spinnaker camera objects to serial numbers in config."""
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
    # Spinnaker configuration
    # ------------------------------------------------------------------

    def _configure_trigger(self, nodemap, cam_name: str) -> bool:
        try:
            # Turn trigger mode off first so all trigger nodes become writable
            node_trigger_mode = PySpin.CEnumerationPtr(
                nodemap.GetNode("TriggerMode")
            )
            if PySpin.IsAvailable(node_trigger_mode) and PySpin.IsWritable(
                node_trigger_mode
            ):
                node_trigger_mode.SetIntValue(
                    node_trigger_mode.GetEntryByName("Off").GetValue()
                )

            # Trigger selector — what does the trigger act on
            node_trigger_selector = PySpin.CEnumerationPtr(
                nodemap.GetNode("TriggerSelector")
            )
            if PySpin.IsAvailable(node_trigger_selector) and PySpin.IsWritable(
                node_trigger_selector
            ):
                # Tells camera which GPIO pin to listen to, TTL cable wired to this
                node_trigger_selector.SetIntValue(
                    node_trigger_selector.GetEntryByName(
                        self.trigger_selector
                    ).GetValue()
                )

            # Trigger source — which GPIO line
            node_trigger_source = PySpin.CEnumerationPtr(
                nodemap.GetNode("TriggerSource")
            )
            if PySpin.IsAvailable(node_trigger_source) and PySpin.IsWritable(
                node_trigger_source
            ):
                node_trigger_source.SetIntValue(
                    node_trigger_source.GetEntryByName(self.trigger_line).GetValue()
                )

            # Trigger activation
            node_trigger_activation = PySpin.CEnumerationPtr(
                nodemap.GetNode("TriggerActivation")
            )
            if PySpin.IsAvailable(node_trigger_activation) and PySpin.IsWritable(
                node_trigger_activation
            ):
                node_trigger_activation.SetIntValue(
                    node_trigger_activation.GetEntryByName(
                        self.trigger_activation
                    ).GetValue()
                )

            # Turn trigger mode on — camera now waits for hardware signal
            if PySpin.IsAvailable(node_trigger_mode) and PySpin.IsWritable(
                node_trigger_mode
            ):
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

        try:
            # ----------------------------------------------------------------
            # Pixel format
            # ----------------------------------------------------------------
            node_pixel_format = PySpin.CEnumerationPtr(
                nodemap.GetNode("PixelFormat")
            )
            if PySpin.IsAvailable(node_pixel_format) and PySpin.IsWritable(
                node_pixel_format
            ):
                node_bgr8 = PySpin.CEnumEntryPtr(
                    node_pixel_format.GetEntryByName("BGR8")
                )
                if not (
                    PySpin.IsAvailable(node_bgr8) and PySpin.IsReadable(node_bgr8)
                ):
                    node_bgr8 = PySpin.CEnumEntryPtr(
                        node_pixel_format.GetEntryByName("BayerRG8")
                    )
                node_pixel_format.SetIntValue(node_bgr8.GetValue())
                print(
                    f"  Pixel format: "
                    f"{node_pixel_format.GetCurrentEntry().GetSymbolic()}"
                )

            # ----------------------------------------------------------------
            # ROI — only apply if specified in config
            # ----------------------------------------------------------------
            if self.target_w is not None and self.target_h is not None:
                node_offset_x = PySpin.CIntegerPtr(nodemap.GetNode("OffsetX"))
                node_offset_y = PySpin.CIntegerPtr(nodemap.GetNode("OffsetY"))

                # Zero offsets first
                if PySpin.IsAvailable(node_offset_x) and PySpin.IsWritable(node_offset_x):
                    node_offset_x.SetValue(node_offset_x.GetMin())
                if PySpin.IsAvailable(node_offset_y) and PySpin.IsWritable(node_offset_y):
                    node_offset_y.SetValue(node_offset_y.GetMin())

                node_width = PySpin.CIntegerPtr(nodemap.GetNode("Width"))
                if PySpin.IsAvailable(node_width) and PySpin.IsWritable(node_width):
                    w_min = node_width.GetMin()
                    w_inc = node_width.GetInc()
                    w_set = min(self.target_w, node_width.GetMax())
                    w_set = w_min + ((w_set - w_min) // w_inc) * w_inc
                    node_width.SetValue(w_set)
                    print(f"  Width: {node_width.GetValue()}")

                node_height = PySpin.CIntegerPtr(nodemap.GetNode("Height"))
                if PySpin.IsAvailable(node_height) and PySpin.IsWritable(node_height):
                    h_min = node_height.GetMin()
                    h_inc = node_height.GetInc()
                    h_set = min(self.target_h, node_height.GetMax())
                    h_set = h_min + ((h_set - h_min) // h_inc) * h_inc
                    node_height.SetValue(h_set)
                    print(f"  Height: {node_height.GetValue()}")

                # Re-apply offsets
                if PySpin.IsAvailable(node_offset_x) and PySpin.IsWritable(node_offset_x):
                    x_inc = node_offset_x.GetInc()
                    x_set = (
                        min(self.offset_x, node_offset_x.GetMax()) // x_inc
                    ) * x_inc
                    node_offset_x.SetValue(x_set)
                    print(f"  OffsetX: {x_set}")

                if PySpin.IsAvailable(node_offset_y) and PySpin.IsWritable(node_offset_y):
                    y_inc = node_offset_y.GetInc()
                    y_set = (
                        min(self.offset_y, node_offset_y.GetMax()) // y_inc
                    ) * y_inc
                    node_offset_y.SetValue(y_set)
                    print(f"  OffsetY: {y_set}")

            # ----------------------------------------------------------------
            # TTL Trigger
            # ----------------------------------------------------------------
            if self.trigger_enabled:
                self._configure_trigger(nodemap, cam_name)

            # ----------------------------------------------------------------
            # Acquisition mode
            # ----------------------------------------------------------------
            node_acq_mode = PySpin.CEnumerationPtr(
                nodemap.GetNode("AcquisitionMode")
            )
            if PySpin.IsAvailable(node_acq_mode) and PySpin.IsWritable(node_acq_mode):
                node_continuous = node_acq_mode.GetEntryByName("Continuous")
                if PySpin.IsAvailable(node_continuous) and PySpin.IsReadable(
                    node_continuous
                ):
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

            # Ensure desired exposure time does not exceed the maximum
            cam_cfg = self.cam_configs[cam_name]
            exposure_time_to_set = cam_cfg.get("exposure_us", 5000)
            exposure_time_to_set = min(cam.ExposureTime.GetMax(), exposure_time_to_set)
            cam.ExposureTime.SetValue(exposure_time_to_set)
            print('Shutter time set to %s us...\n' % exposure_time_to_set)

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
    # Metadata
    # ------------------------------------------------------------------

    def _append_metadata(self, writer, framecount, timestamp, sestime, cputime):
        """Append one row. Only writes columns that are enabled in config."""
        if writer is None:
            return

        cfg = self.metadata_config
        row = []
        if cfg.get("save_framecount", True): row.append(framecount)
        if cfg.get("save_timestamp",  True): row.append(f"{timestamp:.9f}")
        if cfg.get("save_sestime",    True): row.append(f"{sestime:.6f}")
        if cfg.get("save_cputime",    True): row.append(f"{cputime:.6f}")

        writer.writerow(row)

    # ------------------------------------------------------------------
    # Capture thread
    # ------------------------------------------------------------------

    def _capture_frame(self, cam_name: str, cam):
        preview_cfg = self.config.get("preview", {})
        downsample  = preview_cfg.get("downsample", 1)
        frame_idx   = 0
        ttl_count   = 0   # counts successful frame acquisitions triggered by TTL

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

                converted = image_result.Convert(
                    PySpin.PixelFormat_BGR8, PySpin.HQ_LINEAR
                )
                frame = np.array(converted.GetNDArray(), copy=True)
                image_result.Release()

                # After image_result.Release(), inside the while loop
                ttl_count += 1

                if frame_idx % downsample == 0:
                    with self.preview_locks[cam_name]:
                        self.preview_frames[cam_name] = frame

                self.writer_queues[cam_name].put(
                    (frame, framecount, timestamp, sestime, cputime)
                )

                frame_idx += 1

            except PySpin.SpinnakerException as ex:
                if not self._stop_event.is_set():
                    print(f"{cam_name} capture error: {ex}")

        # Store final counts so the writer thread can access them for the summary
        self._final_ttl_counts[cam_name]   = ttl_count
        self._final_frame_counts[cam_name] = frame_idx

    # ------------------------------------------------------------------
    # Writer thread
    # ------------------------------------------------------------------

    def _make_ffmpeg_writer(
        self, output_path: str, width: int, height: int
        ) -> subprocess.Popen:
            if not shutil.which("ffmpeg"):
                raise RuntimeError("ffmpeg not found on PATH")

            cmd = [
                "ffmpeg",
                "-y",
                "-f",        "rawvideo",
                "-vcodec",   "rawvideo",
                "-pix_fmt",  "bgr24",
                "-s",        f"{width}x{height}",
                "-r",        str(self.fps),
                "-i",        "pipe:0",
                "-vcodec",   self.codec,
                "-preset",   self.preset,
                "-crf",      self.crf,
                "-pix_fmt",  self.pix_fmt,
                "-movflags", "+faststart",
                output_path,
            ]
            return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def _write_frames(self, cam_name: str):
        cam_cfg     = self.cam_configs[cam_name]
        label       = cam_cfg.get("name", cam_name)
        ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(self.output_dir, f"{label}_{ts}.mp4")

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
                proc = self._make_ffmpeg_writer(output_path, w, h)
                print(f"{cam_name}: writing to {output_path} at {w}x{h}")

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

        # Drain remaining frames
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

        # TTL count comes from the capture thread which has already exited by now
        ttl_count = self._final_ttl_counts.get(cam_name, frame_count)

        self._write_session_summary(
            cam_name      = cam_name,
            video_path    = output_path,
            timestamp_path= timestamp_path,
            start_wall    = start_wall,
            end_wall      = end_wall,
            total_frames  = frame_count,
            ttl_count     = ttl_count,
        )
    # ------------------------------------------------------------------
    # Thread startup
    # ------------------------------------------------------------------

    def _start_threads(self):
        for name, cam in self.cameras.items():
            # Preview thread
            ct = threading.Thread(
                target=self._capture_frame,
                args=(name, cam),
                daemon=True,
                name=f"capture-{name}",
            )
            ct.start()
            self._capture_threads.append(ct)

            # Write thread
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
        """Non-blocking. Returns latest BGR numpy frame or None."""
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

        del cam


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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

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

    self.camera_count = len(self.cam_configs)
    self.cam_names    = list(self.cam_configs.keys())
    self.cameras: dict[str, PySpin.Camera] = {}

    self.preview_frames = {name: None             for name in self.cam_names}
    self.preview_locks  = {name: threading.Lock() for name in self.cam_names}
    self.writer_queues  = {name: queue.Queue()    for name in self.cam_names}

    self._final_ttl_counts:   dict[str, int] = {}
    self._final_frame_counts: dict[str, int] = {}

    self._capture_threads: list[threading.Thread] = []
    self._writer_threads:  list[threading.Thread] = []

    rec = config["recording"]
    self.fps     = rec["fps"]
    self.codec   = rec["codec"]
    self.crf     = str(rec["crf"])
    self.preset  = rec["preset"]
    self.pix_fmt = rec["pixel_format"]

    roi = config.get("roi", {})
    self.target_w = roi.get("width",    None)
    self.target_h = roi.get("height",   None)
    self.offset_x = roi.get("offset_x", 0)
    self.offset_y = roi.get("offset_y", 0)

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

    self._init_cameras()
    self._start_threads()

def main():
    parser = argparse.ArgumentParser(description="Multi-camera acquisition.")
    parser.add_argument(
        "-c", "--config",
        type=str,
        default="config.yaml",
        help="Path to configuration YAML file.",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.config):
        print(f"Config file not found: {args.config}")
        return

    config = load_config(args.config)

    # Write permission check
    try:
        test = open("test.txt", "w+")
        test.close()
        os.remove(test.name)
    except IOError:
        print("Cannot write to current directory. Check permissions.")
        return

    system  = PySpin.System.GetInstance()
    version = system.GetLibraryVersion()
    print(
        f"Spinnaker {version.major}.{version.minor}."
        f"{version.type}.{version.build}"
    )

    # Print device info for all connected cameras
    cam_list = system.GetCameras()
    print(f"\nCameras detected: {cam_list.GetSize()}")
    for i, cam in enumerate(cam_list):
        print_device_info(cam.GetTLDeviceNodeMap(), f"cam{i}")
    cam_list.Clear()

    streamer = CameraStreamer(config, system)

    preview_enabled = config.get("preview", {}).get("enabled", True)
    print("\nRunning — press ESC to stop.\n")

    try:
        import cv2
        while True:
            if preview_enabled:
                for name in streamer.cam_names:
                    frame = streamer.get_preview(name)
                    if frame is not None:
                        label = config["cameras"][name].get("name", name)
                        cv2.imshow(label, frame)

            if cv2.waitKey(20) == 27:
                break

    finally:
        streamer.stop()
        if preview_enabled:
            import cv2
            cv2.destroyAllWindows()
        cam_list = system.GetCameras()
        cam_list.Clear()
        system.ReleaseInstance()
        print("Done.")


if __name__ == "__main__":
    main()