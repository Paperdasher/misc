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

        TTL:<number>:<state>\n

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
          port: COM3
          baud: 115200
          start_ttl: 1
          stop_ttl: 2
          vref: 5.0

Required
--------
    pip install numpy pyyaml opencv-python pyserial
    PySpin via FLIR Spinnaker SDK
"""

# ---------------------------------------------------------------------------
# Standard library imports
# ---------------------------------------------------------------------------
# os / threading / queue: file paths, background threads, inter-thread comms.
# subprocess: spawns the ffmpeg child process that encodes video.
# shutil: checks whether ffmpeg is on the system PATH before trying to use it.
# time: wall-clock timestamps and perf_counter for high-resolution elapsed time.
# argparse: parses -c / --setup command-line flags.
# csv: writes per-frame timestamp and TTL event rows to .csv files.
# math: ceil/sqrt for the preview tile layout calculation.
# deque: fixed-length ring buffer used to compute rolling FPS.
# datetime: human-readable timestamps in filenames and session CSVs.
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

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
# numpy: holds raw pixel data from the camera as an ndarray.
# yaml: reads and writes the config.yaml file.
# cv2 (OpenCV): draws the stats popup and preview tile window.
# PySpin: FLIR Spinnaker SDK — the Python binding for FLIR machine-vision cameras.
import numpy as np
import yaml
import cv2
import PySpin


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    # Open the YAML file and parse it into a plain Python dict.
    # yaml.safe_load rejects arbitrary Python objects, which prevents
    # accidental code execution if the file is shared or version-controlled.
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Inline preview helpers
# ---------------------------------------------------------------------------
# These two functions replace the external tiling.py dependency.
# They are used only for the live preview window; they do not affect recording.

def draw_tile_overlay(
    bgr: np.ndarray,
    label: str,
    chamber: str,
    fps: float,
    total: int,
    recording: bool = False,
) -> np.ndarray:
    """
    Burn a translucent status bar onto the top of a BGR camera tile.
    Returns a new array; the input is not modified.
    """
    out  = bgr.copy()           # work on a copy so the original cached frame is untouched
    h, w = out.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX

    # Draw a semi-transparent black banner across the top 28 pixels.
    # addWeighted blends the solid-black overlay (55%) with the frame (45%).
    banner_h = 28
    overlay  = out.copy()
    cv2.rectangle(overlay, (0, 0), (w, banner_h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.55, out, 0.45, 0, out)

    # Recording indicator dot: filled red circle when recording, hollow when idle.
    rec_dot = "● " if recording else "○ "
    dot_col = (0, 60, 220) if recording else (120, 120, 120)
    cv2.putText(out, rec_dot, (6, 19), font, 0.52, dot_col, 1, cv2.LINE_AA)

    # Camera label and chamber name, left-aligned after the dot.
    title = f"{label}" + (f"  [{chamber}]" if chamber else "")
    cv2.putText(out, title, (26, 19), font, 0.50, (220, 220, 220), 1, cv2.LINE_AA)

    # FPS and total-frame counter, right-aligned.
    # getTextSize measures the pixel width so we can right-justify it.
    stats_str = f"FPS {fps:5.1f}   #{total:,}"
    (tw, _), _ = cv2.getTextSize(stats_str, font, 0.45, 1)
    cv2.putText(out, stats_str, (w - tw - 8, 18),
                font, 0.45, (0, 210, 210), 1, cv2.LINE_AA)

    return out


def tile_frames(frames: list, canvas_w: int, canvas_h: int) -> np.ndarray:
    """
    Arrange up to 4 BGR frames into a uniform grid that fills canvas_w × canvas_h.

    Column count is ceil(sqrt(n)) so 1 cam → 1×1, 2 cams → 2×1,
    3-4 cams → 2×2.  Each slot is exactly canvas_w/cols × canvas_h/rows pixels;
    frames are resized to fit with bilinear interpolation.
    """
    n = len(frames)
    if n == 0:
        # Nothing to show — return a black canvas of the right size.
        return np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)

    # Calculate grid dimensions that keep tiles as square as possible.
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    tw   = canvas_w // cols   # tile width
    th   = canvas_h // rows   # tile height

    canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
    for i, frame in enumerate(frames):
        # divmod gives (row_index, col_index) from a flat frame index.
        r, c = divmod(i, cols)
        x0, y0 = c * tw, r * th
        # Grayscale frames need to be promoted to 3-channel BGR for display.
        tile = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR) if frame.ndim == 2 else frame
        canvas[y0:y0 + th, x0:x0 + tw] = cv2.resize(
            tile, (tw, th), interpolation=cv2.INTER_LINEAR)
    return canvas


# ---------------------------------------------------------------------------
# TTLEvent — plain data container
# ---------------------------------------------------------------------------
# A TTLEvent is created by AnalogTTLListener when a channel is confirmed active
# and is passed through ttl_dispatch_q to the TTL router thread, which resolves
# it to a chamber + action and calls route_ttl_event on CameraStreamer.
# It is also stored in each camera's per-writer ttl_queue so the writer thread
# can log the event as a row in the timestamps CSV.

class TTLEvent:
    def __init__(self, chamber: str, kind: str,
                 wall: float, sestime: float, label: str,
                 ttl_number: int = None):
        self.chamber    = chamber     # chamber key (e.g. "chamber_A"); filled in by router
        self.kind       = kind        # initially "ttl_active"; overwritten with the action name
        self.wall       = wall        # Unix timestamp of the confirmed trigger
        self.sestime    = sestime     # seconds since session start (perf_counter-based)
        self.label      = label       # human label from ttl_map (e.g. "chamber_A_start")
        self.ttl_number = ttl_number  # 1-based channel index from the Arduino sketch


# ---------------------------------------------------------------------------
# AnalogTTLListener — one instance per unique Arduino serial port
# ---------------------------------------------------------------------------

class AnalogTTLListener:
    """
    Reads TTL channel states from one Arduino over USB serial in a background
    thread and puts confirmed TTLEvents into a shared dispatch queue.

    The Arduino sends one line per pin per loop:
        TTL:<number>:<state>\n
    where <number> is the 1-based channel index and <state> is 1 (high) or 0.

    Debounce / confirmation logic
    ------------------------------
    A single "state=1" reading is not trusted because analog voltages near
    the threshold can bounce.  This class requires CONFIRM_COUNT (3)
    consecutive state=1 readings of the same channel before treating it as
    a real trigger.  Once it fires it sets _fired[ttl_number]=True so it
    won't fire again until the voltage drops back to 0, clearing _fired.
    Each channel is tracked completely independently.
    """

    CONFIRM_COUNT = 3   # how many consecutive state=1 reads are needed to confirm

    def __init__(self, port: str, baud: int,
                 event_queue: queue.Queue, session_start: float):
        self.port           = port          # e.g. "COM3" or "/dev/ttyUSB0"
        self.baud           = baud          # must match BAUD_RATE in the .ino sketch
        self._eq            = event_queue   # shared queue → TTL router thread
        self._session_start = session_start # perf_counter reference for sestime
        self._stop          = threading.Event()
        self._thread        = None
        # Per-channel counters.  Keys are ttl_number ints; values are set on first read.
        self._consec: dict  = {}   # consecutive state=1 count for each channel
        self._fired:  dict  = {}   # True once fired; reset when state drops to 0

    def start(self):
        # Spawn a daemon thread so it is killed automatically if the main
        # process exits without calling stop().
        self._thread = threading.Thread(
            target=self._read_loop, daemon=True,
            name=f"analogttl-{self.port}"
        )
        self._thread.start()
        print(f"[AnalogTTL] Listening on {self.port} @ {self.baud} baud "
              f"(confirm after {self.CONFIRM_COUNT} consecutive reads)")

    def stop(self):
        # Signal the loop to exit, then wait up to 2 s for the thread to finish.
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _read_loop(self):
        # Import pyserial here so a missing install only fails when actually
        # trying to open a port, not at module load time.
        try:
            import serial
        except ImportError:
            print(f"[AnalogTTL:{self.port}] pyserial not installed — pip install pyserial")
            return

        # Open the serial port.  timeout=0.5 means readline() returns after
        # 0.5 s even if no newline has arrived, so the stop check runs regularly.
        try:
            ser = serial.Serial(port=self.port, baudrate=self.baud, timeout=0.5)
        except Exception as e:
            print(f"[AnalogTTL:{self.port}] Cannot open port: {e}")
            return

        print(f"[AnalogTTL:{self.port}] Port open — reading channels.")
        with ser:
            while not self._stop.is_set():
                # readline() blocks until a '\n' arrives or the 0.5 s timeout fires.
                try:
                    raw = ser.readline()
                except Exception:
                    break   # serial error — exit cleanly

                if not raw:
                    continue    # timeout with no data; loop and check stop flag

                # Decode bytes to a string, stripping trailing whitespace / CR-LF.
                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                except Exception:
                    continue

                # Skip the "READY" startup message and blank lines.
                if not line or line == "READY":
                    continue

                # Parse "TTL:<number>:<state>" — silently drop anything else.
                parts = line.split(":")
                if len(parts) != 3 or parts[0] != "TTL":
                    continue

                try:
                    ttl_number = int(parts[1])
                    state      = int(parts[2])
                except ValueError:
                    continue

                if state == 1:
                    # Increment the consecutive-high counter for this channel.
                    count = self._consec.get(ttl_number, 0) + 1
                    self._consec[ttl_number] = count

                    # Fire only when the threshold is met AND we haven't fired
                    # yet for this sustained high period.
                    if count >= self.CONFIRM_COUNT and not self._fired.get(ttl_number, False):
                        wall    = time.time()
                        sestime = time.perf_counter() - self._session_start
                        ts_str  = datetime.fromtimestamp(wall).strftime("%H:%M:%S.%f")
                        print(f"[AnalogTTL:{self.port}] TTL{ttl_number} confirmed "
                              f"active ({count} reads) at {ts_str}")
                        # Mark fired so we don't re-fire on every subsequent high read.
                        self._fired[ttl_number] = True
                        # Push into the shared dispatch queue.
                        # chamber is left blank here — the router resolves it via ttl_map.
                        self._eq.put(TTLEvent(
                            chamber    = "",
                            kind       = "ttl_active",
                            wall       = wall,
                            sestime    = sestime,
                            label      = f"TTL{ttl_number}",
                            ttl_number = ttl_number,
                        ))
                else:
                    # Voltage fell back to 0 — reset both counters so the next
                    # sustained high can fire again independently.
                    self._consec[ttl_number] = 0
                    self._fired[ttl_number]  = False

        print(f"[AnalogTTL:{self.port}] Read loop exited.")


# ---------------------------------------------------------------------------
# CameraWriter — per-camera, per-recording-session
# ---------------------------------------------------------------------------

class CameraWriter:
    """
    Owns a single recording session for one camera:
        • Receives frames via push_frame() from the capture thread.
        • Runs a background writer thread that pipes raw pixels into an
          ffmpeg child process which MJPEG-encodes them to an .avi file.
        • Interleaves TTL events from its per-camera ttl_queue as extra
          rows in the per-frame timestamps .csv file.

    Buffer cap
    ----------
    MAX_BUFFER_FRAMES limits how deep the frame queue (_fq) can grow.
    If the disk can't keep up with the camera frame rate, the pipe to
    ffmpeg fills and proc.stdin.write() blocks, stalling the writer thread.
    Without a cap the queue would grow unbounded in RAM; when stop() was
    finally called the 15-second join timeout would expire before all frames
    were written, ffmpeg's stdin would never be closed, and the .avi file
    would be incomplete/corrupt.  With the cap, excess frames are dropped
    (with a console warning) instead of crashing the recording.
    """

    # Cap at 300 frames (~5 s at 60 fps).
    # Increase on machines with fast NVMe; decrease on slow HDDs.
    MAX_BUFFER_FRAMES = 300

    def __init__(self, cam_name: str, cam_cfg: dict,
                 chamber_name: str, chamber_dir: str,
                 fps: float, jpeg_quality: int,
                 metadata_cfg: dict, ttl_queue: queue.Queue):
        # Store references needed by the writer thread and session summary.
        self.cam_name     = cam_name
        self.chamber_name = chamber_name
        self.fps          = fps             # used to calculate stop() timeout
        self.jpeg_quality = jpeg_quality    # 0–100; converted to ffmpeg -q:v scale below
        self.metadata_cfg = metadata_cfg    # controls which CSV columns are written
        self._ttl_queue   = ttl_queue       # per-camera queue fed by route_ttl_event

        # Create the output directory (e.g. recordings/20250610_143022/chamber_A/).
        os.makedirs(chamber_dir, exist_ok=True)

        # Build a unique filename base: "<chamber>_<cam_label>_<YYYYMMDD_HHMMSS>".
        cam_label  = cam_cfg.get("name", cam_name)
        ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
        base            = f"{chamber_name}_{cam_label}_{ts}"
        self.video_path = os.path.join(chamber_dir, f"{base}.avi")
        self.ts_path    = os.path.join(chamber_dir, f"{base}_timestamps.csv")
        self.label      = cam_label

        # Frame queue: capture thread puts tuples in, writer thread takes them out.
        # The Queue itself is unbounded; push_frame enforces MAX_BUFFER_FRAMES.
        self._fq             = queue.Queue()
        self._stop_evt       = threading.Event()
        self._thread         = None
        self.frame_count     = 0     # frames successfully written to ffmpeg
        self._dropped_frames = 0     # frames rejected by the buffer cap
        self.start_wall      = None  # set in _run() just before the write loop starts
        self.end_wall        = None  # set in _run() after ffmpeg exits cleanly

    @property
    def buffered_frames(self) -> int:
        # Expose queue depth so the stats popup can display it without
        # importing queue internals.
        return self._fq.qsize()

    def start(self):
        # Start the writer background thread.  daemon=True means it won't
        # prevent the process from exiting if stop() is never called.
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"writer-{self.cam_name}"
        )
        self._thread.start()

    def push_frame(self, frame, framecount, timestamp, sestime, cputime):
        """
        Called by the capture thread for every frame when recording is active.

        If the queue is at the cap, the frame is dropped rather than queued.
        This prevents the queue from growing without bound when the disk is
        slower than the camera, which would eventually cause proc.stdin.write()
        to block and freeze the writer thread entirely.
        """
        if self._fq.qsize() >= self.MAX_BUFFER_FRAMES:
            self._dropped_frames += 1
            # Print a warning on the first drop and every 60 drops after
            # (roughly once per second at 60 fps) to avoid log spam.
            if self._dropped_frames == 1 or self._dropped_frames % 60 == 0:
                print(
                    f"[Writer:{self.cam_name}] *** Buffer full "
                    f"({self._fq.qsize()} frames queued). "
                    f"Dropping frame — {self._dropped_frames} total dropped. "
                    f"Reduce FPS/quality or use a faster drive. ***"
                )
            return   # discard the frame; capture thread continues unblocked
        self._fq.put(("frame", frame, framecount, timestamp, sestime, cputime))

    def stop(self):
        """
        Tell the writer loop to finish, then wait for it to drain the queue
        and close ffmpeg cleanly.

        Timeout is at least 60 s, or long enough to write the remaining
        buffered frames at the configured FPS with 2× headroom.  This replaces
        the old fixed 15 s timeout, which was too short when a full buffer
        (~300 frames) needed to be flushed to disk.
        """
        remaining = self._fq.qsize()
        # "stop" sentinel causes the writer loop to break after draining frames.
        self._fq.put(("stop", None, None, None, None, None))
        if self._thread:
            timeout = max(60.0, (remaining / max(self.fps, 1.0)) * 2.0 + 10.0)
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                # Thread is still running — ffmpeg may have hung or the disk
                # is extremely slow.  The file is likely incomplete.
                print(
                    f"[Writer:{self.cam_name}] WARNING — writer thread still alive "
                    f"after {timeout:.0f} s. Output file may be incomplete. "
                    f"({self._dropped_frames} frames were dropped during recording.)"
                )

    def _run(self):
        """
        Background writer thread body.

        Loop structure (each iteration):
          1. Drain any pending TTL events into the CSV (timestamped rows).
          2. Block up to 0.5 s waiting for the next frame on _fq.
          3. On the very first frame, spawn ffmpeg with the correct resolution.
          4. Write the raw pixel bytes into ffmpeg's stdin pipe.
          5. Append a "frame" row to the CSV with all requested timestamp columns.
          6. On a "stop" sentinel, break out of the loop.
        After the loop: close ffmpeg's stdin (triggers file finalization), wait
        for ffmpeg to exit, close the CSV file.
        """
        proc     = None   # ffmpeg subprocess; None until first frame arrives
        csv_file = None
        csvw     = None
        self.start_wall = time.time()

        # Open the CSV and write the header row if metadata is enabled.
        if self.metadata_cfg.get("enabled", True):
            csv_file = open(self.ts_path, "w", newline="")
            csvw     = csv.writer(csv_file)
            # Build the header dynamically based on which columns are enabled.
            hdr = ["row_type"]
            if self.metadata_cfg.get("save_framecount", True): hdr.append("framecount")
            if self.metadata_cfg.get("save_timestamp",  True): hdr.append("camera_hw_ts_s")
            if self.metadata_cfg.get("save_sestime",    True): hdr.append("sestime_s")
            if self.metadata_cfg.get("save_cputime",    True): hdr.append("cpu_wall_s")
            # TTL columns are always appended but left empty on "frame" rows.
            hdr += ["ttl_chamber", "ttl_kind", "ttl_label", "ttl_number"]
            csvw.writerow(hdr)
            csv_file.flush()

        # Convert the 0-100 quality percentage to ffmpeg's -q:v scale (2=best, 31=worst).
        # The formula maps 100% quality → q=2 and 0% quality → q=31.
        q_val = max(2, min(31, int(2 + (100 - self.jpeg_quality) * 29 / 100)))

        while True:
            # --- Step 1: flush any TTL events into the CSV ---
            # TTL events arrive via _ttl_queue, which is fed by route_ttl_event
            # in the TTL router thread.  We drain before pulling video frames so
            # that event timestamps appear in approximately the right position
            # in the CSV relative to the surrounding frames.
            while not self._ttl_queue.empty():
                try:
                    evt = self._ttl_queue.get_nowait()
                    if csvw:
                        row = ["ttl_event"]
                        cfg = self.metadata_cfg
                        # Use the event's wall time for both timestamp columns
                        # (there is no "frame ID" for a TTL event).
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

            # --- Step 2: wait for the next frame (or stop sentinel) ---
            try:
                item = self._fq.get(timeout=0.5)
            except queue.Empty:
                continue   # no frame yet; loop back and check TTL queue again

            if item[0] == "stop":
                break   # sentinel received — finish up after the loop

            _, frame, framecount, timestamp, sestime, cputime = item

            # --- Step 3: spawn ffmpeg on the very first frame ---
            # We wait for the first frame so we know the actual pixel dimensions.
            # No sleep is needed after Popen — ffmpeg is ready to accept stdin
            # bytes essentially immediately.  The old time.sleep(0.05) here was
            # causing 3+ frames to accumulate before ffmpeg started, contributing
            # to the initial buffer spike.
            if proc is None:
                h, w = frame.shape[:2]
                if not shutil.which("ffmpeg"):
                    print(f"[Writer:{self.cam_name}] ffmpeg not on PATH.")
                    break
                cmd = [
                    "ffmpeg", "-y",
                    "-f",       "rawvideo", "-vcodec", "rawvideo",
                    "-pix_fmt", "gray",          # input is 8-bit grayscale
                    "-s",       f"{w}x{h}",      # frame dimensions
                    "-r",       str(self.fps),   # input frame rate (sets PTS in output)
                    "-i",       "pipe:0",         # read from stdin
                    "-vcodec",  "mjpeg",          # Motion JPEG — intra-frame, fast to encode
                    "-q:v",     str(q_val),       # JPEG quality (2=best, 31=worst)
                    "-pix_fmt", "yuvj420p",       # required for MJPEG in AVI containers
                    self.video_path,
                ]
                # stderr=DEVNULL silences ffmpeg's progress output in the terminal.
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL)
                print(f"[Writer:{self.cam_name}] → {self.video_path} ({w}×{h})")

            # --- Step 4: pipe raw pixels into ffmpeg ---
            # tobytes() gives a flat bytes object; ffmpeg reads exactly w*h bytes
            # per frame because we told it the dimensions and pixel format above.
            try:
                proc.stdin.write(frame.tobytes())
                self.frame_count += 1
            except BrokenPipeError:
                # ffmpeg exited unexpectedly (e.g. ran out of disk space).
                print(f"[Writer:{self.cam_name}] ffmpeg pipe broken.")
                break

            # --- Step 5: write the frame row to the CSV ---
            if csvw:
                row = ["frame"]
                cfg = self.metadata_cfg
                if cfg.get("save_framecount", True): row.append(framecount)
                if cfg.get("save_timestamp",  True): row.append(f"{timestamp:.6f}")
                if cfg.get("save_sestime",    True): row.append(f"{sestime:.6f}")
                if cfg.get("save_cputime",    True): row.append(f"{cputime:.6f}")
                # TTL columns are empty on frame rows.
                row += ["", "", "", ""]
                csvw.writerow(row)

        # --- Post-loop cleanup ---
        self.end_wall = time.time()
        if proc:
            # Closing stdin signals end-of-stream to ffmpeg; proc.wait() blocks
            # until ffmpeg has flushed all internal buffers and written the index.
            proc.stdin.close()
            proc.wait()
            if self._dropped_frames:
                print(f"[Writer:{self.cam_name}] done — "
                      f"{self.frame_count} frames written, "
                      f"{self._dropped_frames} dropped (disk too slow).")
            else:
                print(f"[Writer:{self.cam_name}] done — {self.frame_count} frames.")
        if csv_file:
            csv_file.close()


# ---------------------------------------------------------------------------
# CameraStreamer — central coordinator
# ---------------------------------------------------------------------------

class CameraStreamer:
    """
    Owns the Spinnaker camera handles, per-camera capture threads,
    CameraWriter instances, and TTL action routing.

    Lifecycle
    ---------
    1. __init__  — parse config, build lookup tables, create session directory.
    2. _init_cameras — find cameras by serial, configure them, begin acquisition.
    3. _start_threads — start one capture thread per camera.
    4. start_recording / stop_recording — called from the TTL router or UI.
    5. stop — stop all recordings, end acquisition, release cameras.
    """

    def __init__(self, config: dict, system: "PySpin.SystemPtr"):
        self.config        = config
        self.system        = system
        self._stop_event   = threading.Event()  # set in stop() to kill capture threads
        self.session_start = time.perf_counter() # reference for sestime calculations
        self.session_wall  = time.time()          # reference for wall-clock logging

        # Build the set of enabled cameras from the config "cameras" block.
        self.cam_configs = {
            name: cfg
            for name, cfg in config["cameras"].items()
            if cfg.get("enabled", True)
        }
        self.cam_names = list(self.cam_configs.keys())
        self.cameras: dict = {}   # populated by _find_cameras(); cam_name → PySpin.Camera

        # --- Recording state ---
        # _recording: True when a CameraWriter is active for that camera.
        # _writers: holds the live CameraWriter for each camera that is recording.
        # _rec_lock: protects both dicts.  IMPORTANT: writer.stop() is called
        #   OUTSIDE this lock (see stop_recording) because it can block for tens
        #   of seconds while flushing frames to disk.  Holding the lock that long
        #   would freeze all other threads that need to check recording state.
        self._recording: dict = {n: False for n in self.cam_names}
        self._writers:   dict = {}
        self._rec_lock = threading.Lock()

        # Per-camera TTL event queues.
        # route_ttl_event puts events here; each CameraWriter drains its own queue
        # and writes the events as "ttl_event" rows in the timestamps CSV.
        self._ttl_queues: dict = {n: queue.Queue() for n in self.cam_names}

        # Rolling stats (FPS, total frame count) per camera.
        # _stats_locks prevents a race between the capture thread updating stats
        # and the UI loop reading them for the popup.
        self._stats_locks = {n: threading.Lock() for n in self.cam_names}
        self._stats = {
            n: {"fps": 0.0, "total": 0, "_ring": deque(maxlen=30)}
            for n in self.cam_names
        }
        self._rec_start_times: dict = {}  # cam_name → perf_counter time recording started

        # Preview frame cache.
        # Each capture thread writes here at the per-chamber downsample rate.
        # The UI loop reads from here without affecting the capture thread.
        self._preview_frames = {n: None             for n in self.cam_names}
        self._preview_locks  = {n: threading.Lock() for n in self.cam_names}

        self._capture_threads: list = []

        # Pull commonly referenced settings out of config for convenience.
        rec = config["recording"]
        self.fps          = rec["fps"]
        self.jpeg_quality = rec.get("jpeg_quality", 90)

        roi = config.get("roi", {})
        self.target_w = roi.get("width",  None)   # None means use full sensor width
        self.target_h = roi.get("height", None)

        trig = config.get("trigger", {})
        self.hw_trigger_enabled = trig.get("enabled", False)
        # trigger_timeout: how long GetNextImage() waits before raising an exception.
        self.trigger_timeout    = trig.get("timeout_ms", 5000)

        self.metadata_cfg = config.get("metadata", {})

        # Create the session output directory: <save_dir>/<YYYYMMDD_HHMMSS>/
        save_dir   = config["save_dir"]
        experiment = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = os.path.join(save_dir, experiment)
        os.makedirs(self.session_dir, exist_ok=True)

        # Copy the config file into the session directory for reproducibility.
        with open(os.path.join(self.session_dir, "config.yaml"), "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        # Build bidirectional chamber ↔ camera lookup tables from the "chambers" block.
        self.chambers_cfg = config.get("chambers", {})
        self.chamber_to_cam: dict = {}   # "chamber_A" → "cam0"
        self.cam_to_chamber: dict = {}   # "cam0" → "chamber_A"
        for ch_name, ch_cfg in self.chambers_cfg.items():
            cam_key = ch_cfg.get("camera", "")
            if cam_key in self.cam_names:
                self.chamber_to_cam[ch_name] = cam_key
                self.cam_to_chamber[cam_key] = ch_name

        # Build the TTL action map from the "ttl_map" list in config.
        # Each list entry becomes TTL channel (index + 1).
        # The map is: ttl_number (int) → {chamber, action, label, pin}
        # This is the single source of truth for which Arduino pin does what.
        self.ttl_action_map: dict = {}
        for idx, entry in enumerate(config.get("ttl_map", [])):
            ttl_num = idx + 1
            self.ttl_action_map[ttl_num] = {
                "chamber": entry.get("chamber", ""),
                "action":  entry.get("action",  "log_event"),
                "label":   entry.get("ttl_label", f"TTL{ttl_num}"),
                "pin":     entry.get("pin", f"A{idx}"),
            }

        if not self.ttl_action_map:
            print("[TTL] No ttl_map entries found — TTL triggering inactive.")

    # ------------------------------------------------------------------
    # Recording control
    # ------------------------------------------------------------------

    def start_recording(self, cam_name: str):
        """
        Create and start a CameraWriter for cam_name.
        Protected by _rec_lock so concurrent calls (e.g. two TTL events
        arriving simultaneously) don't create duplicate writers.
        """
        with self._rec_lock:
            # Guard: do nothing if already recording.
            if self._recording.get(cam_name):
                return
            ch_name     = self.cam_to_chamber.get(cam_name, cam_name)
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
            # Register the writer and mark this camera as recording.
            self._writers[cam_name]         = writer
            self._recording[cam_name]       = True
            self._rec_start_times[cam_name] = time.perf_counter()
            print(f"[Recorder] {cam_name} ({ch_name}) started → {chamber_dir}")

    def stop_recording(self, cam_name: str):
        """
        Stop recording for cam_name.

        The state update (marking cam_name as not recording and removing the
        writer from the dict) happens inside _rec_lock so other threads see
        the change immediately.  The actual writer flush (writer.stop()) and
        session summary write happen OUTSIDE the lock because writer.stop()
        may block for tens of seconds while draining buffered frames to disk.
        Keeping that inside the lock would prevent TTL events for other
        chambers from being processed during the flush.
        """
        with self._rec_lock:
            if not self._recording.get(cam_name):
                return
            self._recording[cam_name] = False
            writer = self._writers.pop(cam_name, None)
            self._rec_start_times.pop(cam_name, None)

        # Outside lock: flush remaining frames and finalize the file.
        print(f"[Recorder] {cam_name} stopping — flushing remaining frames…")
        if writer:
            writer.stop()                           # blocks until ffmpeg closes
            self._write_session_summary(cam_name, writer)
        print(f"[Recorder] {cam_name} done.")

    def start_all_recording(self):
        # Start every chamber whose "record" flag is True in the config.
        for ch_name, ch_cfg in self.chambers_cfg.items():
            if not ch_cfg.get("record", True):
                continue
            cam_key = ch_cfg.get("camera")
            if cam_key and cam_key in self.cam_names:
                self.start_recording(cam_key)

    def stop_all_recording(self):
        # Copy the keys first because stop_recording modifies _writers.
        for name in list(self._writers.keys()):
            self.stop_recording(name)

    def is_recording(self, cam_name: str) -> bool:
        return self._recording.get(cam_name, False)

    def any_recording(self) -> bool:
        return any(self._recording.values())

    def elapsed_recording(self, cam_name: str) -> float:
        # Return seconds since recording started, or 0 if not recording.
        t = self._rec_start_times.get(cam_name)
        return (time.perf_counter() - t) if t else 0.0

    def buffered_frames(self, cam_name: str) -> int:
        # Ask the writer how many frames are waiting in its queue.
        w = self._writers.get(cam_name)
        return w.buffered_frames if w else 0

    # ------------------------------------------------------------------
    # TTL routing
    # ------------------------------------------------------------------

    def route_ttl_event(self, evt: TTLEvent):
        """
        Called by the dedicated TTL router thread for every confirmed
        "ttl_active" event.

        Steps:
          1. Look up the ttl_number in ttl_action_map to get chamber + action.
          2. Resolve the chamber name to a camera name via chamber_to_cam.
          3. Stamp the resolved fields onto the event object (so the writer
             can log the correct chamber/label into the CSV).
          4. Act: start_recording, stop_recording, or log_event.
          5. Put the event into the camera's ttl_queue so it is written to CSV.

        The event is enqueued AFTER starting (for start_recording) so the
        writer already exists when the event arrives, and BEFORE stopping
        (for stop_recording) so the event is captured before the writer closes.
        """
        if evt.kind != "ttl_active" or evt.ttl_number is None:
            return

        # Resolve TTL number → chamber + action using the ttl_map config.
        mapping = self.ttl_action_map.get(evt.ttl_number)
        if not mapping:
            print(f"[TTL] TTL{evt.ttl_number} confirmed but not in ttl_map — ignored.")
            return

        ch_name  = mapping["chamber"]
        action   = mapping["action"]
        label    = mapping["label"]
        pin      = mapping["pin"]

        # Overwrite the placeholder fields set by AnalogTTLListener with
        # the resolved values so the CSV row has the correct chamber/label.
        evt.chamber = ch_name
        evt.kind    = action
        evt.label   = label

        # Resolve chamber → camera.
        cam_name = self.chamber_to_cam.get(ch_name)
        if not cam_name:
            print(f"[TTL] TTL{evt.ttl_number}: chamber '{ch_name}' has no "
                  f"mapped camera — ignored. Check ttl_map in config.yaml.")
            return

        ts = datetime.fromtimestamp(evt.wall).strftime("%H:%M:%S.%f")

        if action == "start_recording":
            if not self.is_recording(cam_name):
                print(f"[TTL] {label} → START {ch_name} at {ts}")
                self.start_recording(cam_name)
                # Enqueue AFTER starting so the CameraWriter thread exists
                # and will receive the event in its ttl_queue drain loop.
                self._ttl_queues[cam_name].put(evt)
            else:
                # Camera is already recording (e.g. manually started).
                # Still log the pulse to the CSV.
                print(f"[TTL] {label} → {ch_name} already recording — logged.")
                self._ttl_queues[cam_name].put(evt)

        elif action == "stop_recording":
            if self.is_recording(cam_name):
                print(f"[TTL] {label} → STOP {ch_name} at {ts}")
                # Enqueue BEFORE stopping so the event row appears in the CSV
                # before the writer closes the file.
                self._ttl_queues[cam_name].put(evt)
                self.stop_recording(cam_name)
            else:
                print(f"[TTL] {label} → {ch_name} not recording, nothing to stop.")

        elif action == "log_event":
            # No recording state change — just log the pulse timestamp.
            print(f"[TTL] {label} (TTL{evt.ttl_number}, pin {pin}) → "
                  f"logged to {ch_name} at {ts}")
            if self.is_recording(cam_name):
                self._ttl_queues[cam_name].put(evt)
            else:
                print(f"       ({ch_name} not recording — event not written to CSV)")

        else:
            print(f"[TTL] Unknown action '{action}' for TTL{evt.ttl_number} — ignored.")

    # ------------------------------------------------------------------
    # Session summary
    # ------------------------------------------------------------------

    def _write_session_summary(self, cam_name: str, writer: CameraWriter):
        """
        Write a one-row CSV summarising the just-finished recording session.
        Called by stop_recording after writer.stop() returns (so end_wall is set).
        The file is named to match the video and timestamps files for easy linking.
        """
        if not self.metadata_cfg.get("enabled", True):
            return

        import socket

        label    = self.cam_configs[cam_name].get("name", cam_name)
        ch_name  = self.cam_to_chamber.get(cam_name, cam_name)
        base     = os.path.splitext(os.path.basename(writer.video_path))[0]
        path     = os.path.join(self.session_dir, ch_name, f"{base}_session.csv")
        duration = (writer.end_wall - writer.start_wall) if writer.end_wall else 0
        avg_fps  = writer.frame_count / duration if duration > 0 else 0
        start_dt = datetime.fromtimestamp(writer.start_wall)
        end_dt   = datetime.fromtimestamp(writer.end_wall or writer.start_wall)
        exp_meta = self.config.get("experiment_metadata", {})
        # Expected frames = configured FPS × actual duration; difference is dropped frames.
        expected = int(round(self.fps * duration))

        # PC name: prefer explicit config override, then auto-detect hostname.
        pc_name = (
            exp_meta.get("pc_name", "").strip()
            or self.config.get("pc_name", "").strip()
            or socket.gethostname()
        )

        # Extract a numeric chamber number from the key (e.g. "chamber_2" → "2").
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
            # frames_dropped: frames the camera delivered but ffmpeg never received
            # (gap between expected count and actual written count).
            "frames_dropped":            max(0, expected - writer.frame_count),
            # frames_dropped_buffer_full: frames explicitly discarded by push_frame
            # because the queue was at MAX_BUFFER_FRAMES.
            "frames_dropped_buffer_full": writer._dropped_frames,
            "configured_fps":            f"{self.fps:.4f}",
            "avg_actual_fps":            f"{avg_fps:.4f}",
            "video_path":                os.path.abspath(writer.video_path),
            "timestamp_file_path":       os.path.abspath(writer.ts_path),
            "eeg_fiber_photometry_path": exp_meta.get("eeg_fiber_photometry_path", ""),
            "notes":                     exp_meta.get("notes", ""),
        }
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(fields.keys()))
            w.writeheader()
            w.writerow(fields)
        print(f"[Summary] → {path}")

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self, cam_name: str) -> dict:
        # Thread-safe read of fps and total frame count for the popup.
        with self._stats_locks[cam_name]:
            s = self._stats[cam_name]
            return {"fps": s["fps"], "total": s["total"]}

    def _update_stats(self, cam_name: str, total: int):
        """
        Called by the capture thread on every frame.
        Stores the current timestamp in a 30-element ring buffer and computes
        FPS as (number of intervals) / (time span of the ring buffer).
        Using a ring buffer gives a smooth rolling average rather than a
        bursty instantaneous reading.
        """
        now = time.perf_counter()
        with self._stats_locks[cam_name]:
            s = self._stats[cam_name]
            s["total"] = total
            s["_ring"].append(now)
            if len(s["_ring"]) >= 2:
                el = s["_ring"][-1] - s["_ring"][0]
                s["fps"] = (len(s["_ring"]) - 1) / el if el > 0 else 0.0

    # ------------------------------------------------------------------
    # Camera configuration (Spinnaker / PySpin)
    # ------------------------------------------------------------------

    def _configure_camera(self, cam, cam_name: str) -> bool:
        """
        Apply all per-camera settings via the Spinnaker node map.

        Node map access follows a consistent pattern:
          1. Get a typed pointer to the node (CEnumerationPtr, CIntegerPtr, etc.)
          2. Check IsAvailable() and IsWritable() before touching it — the
             camera may not expose every node, and writing to a non-writable
             node raises a SpinnakerException.
          3. Set the value.
        """
        nodemap = cam.GetNodeMap()
        print(f"\n[Config] {cam_name}...")
        try:
            # Disable the Spinnaker hardware trigger.
            # TTL is read by this Python script over serial; the camera runs
            # free in continuous mode.  If TriggerMode is left On the camera
            # waits for a GPIO pulse before each frame and acquisition stalls.
            tm = PySpin.CEnumerationPtr(nodemap.GetNode("TriggerMode"))
            if PySpin.IsAvailable(tm) and PySpin.IsWritable(tm):
                off_entry = tm.GetEntryByName("Off")
                if PySpin.IsAvailable(off_entry) and PySpin.IsReadable(off_entry):
                    tm.SetIntValue(off_entry.GetValue())
                    print(f"  TriggerMode: Off")

            # Set pixel format to the first supported monochrome mode.
            # Mono8 is preferred (1 byte/pixel, fast pipe); fall back to
            # Mono16, BayerRG8, or BGR8 if the sensor doesn't support it.
            pf = PySpin.CEnumerationPtr(nodemap.GetNode("PixelFormat"))
            if PySpin.IsAvailable(pf) and PySpin.IsWritable(pf):
                for fmt in ["Mono8", "Mono16", "BayerRG8", "BGR8"]:
                    e = PySpin.CEnumEntryPtr(pf.GetEntryByName(fmt))
                    if PySpin.IsAvailable(e) and PySpin.IsReadable(e):
                        pf.SetIntValue(e.GetValue())
                        print(f"  PixelFormat: {fmt}")
                        break

            # Configure the sensor ROI if target dimensions are specified.
            if self.target_w and self.target_h:
                ox = PySpin.CIntegerPtr(nodemap.GetNode("OffsetX"))
                oy = PySpin.CIntegerPtr(nodemap.GetNode("OffsetY"))
                nw = PySpin.CIntegerPtr(nodemap.GetNode("Width"))
                nh = PySpin.CIntegerPtr(nodemap.GetNode("Height"))
                # Reset offsets to 0 first so we have the full range for Width/Height.
                for n in [ox, oy]:
                    if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                        n.SetValue(n.GetMin())
                # Set Width and Height, snapping to the nearest valid increment.
                for node, target, lbl in [(nw, self.target_w, "Width"),
                                           (nh, self.target_h, "Height")]:
                    if PySpin.IsAvailable(node) and PySpin.IsWritable(node):
                        mn, inc = node.GetMin(), node.GetInc()
                        v = mn + ((min(target, node.GetMax()) - mn) // inc) * inc
                        node.SetValue(v)
                        print(f"  {lbl}: {node.GetValue()}")
                # Centre the ROI on the sensor by adjusting OffsetX/Y.
                for node, sensor, actual in [
                    (ox, ox.GetMax() + nw.GetValue(), nw.GetValue()),
                    (oy, oy.GetMax() + nh.GetValue(), nh.GetValue()),
                ]:
                    if PySpin.IsAvailable(node) and PySpin.IsWritable(node):
                        inc = node.GetInc()
                        v   = (((sensor - actual) // 2) // inc) * inc
                        node.SetValue(max(node.GetMin(), min(node.GetMax(), v)))

            # Set continuous acquisition mode so the camera streams frames
            # indefinitely without needing a software trigger per frame.
            acq = PySpin.CEnumerationPtr(nodemap.GetNode("AcquisitionMode"))
            if PySpin.IsAvailable(acq) and PySpin.IsWritable(acq):
                cont = acq.GetEntryByName("Continuous")
                if PySpin.IsAvailable(cont) and PySpin.IsReadable(cont):
                    acq.SetIntValue(cont.GetValue())
                    print("  AcquisitionMode: Continuous")

            # Exposure: disable auto-exposure and set a fixed value from config.
            # Capped at 15000 µs to avoid motion blur at typical rodent speeds.
            cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)
            cfg = self.cam_configs[cam_name]
            cam.ExposureTime.SetValue(min(15000, cfg.get("exposure_us", 14000)))
            print(f"  Exposure: {cam.ExposureTime.GetValue():.0f} µs")

            # Gain: disable auto-gain and set a fixed dB value from config.
            cam.GainAuto.SetValue(PySpin.GainAuto_Off)
            cam.Gain.SetValue(min(cam.Gain.GetMax(), cfg.get("gain_db", 10)))
            print(f"  Gain: {cam.Gain.GetValue():.1f} dB")

            # Disable ISP (image signal processing) and gamma correction.
            # These apply non-linear transforms that make pixel values harder
            # to interpret in downstream analysis.
            for node_name, val, lbl in [("IspEnable",   False, "ISP disabled"),
                                         ("GammaEnable", False, "Gamma disabled")]:
                n = PySpin.CBooleanPtr(nodemap.GetNode(node_name))
                if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                    n.SetValue(val)
                    print(f"  {lbl}")

            # Set the black level offset (raises the floor slightly above zero
            # to distinguish true black from dead pixels).
            n = PySpin.CFloatPtr(nodemap.GetNode("BlackLevel"))
            if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                n.SetValue(cfg.get("black_level", 2.0))

            # Throughput limit: cap the USB/GigE bandwidth to prevent packet
            # collisions when multiple cameras share the same host controller.
            n = PySpin.CIntegerPtr(nodemap.GetNode("DeviceLinkThroughputLimit"))
            if PySpin.IsAvailable(n) and PySpin.IsWritable(n):
                tl = max(n.GetMin(), min(n.GetMax(), cfg.get("throughput_limit", 90_000_000)))
                n.SetValue(tl)

            return True
        except PySpin.SpinnakerException as ex:
            print(f"  {cam_name} configure error: {ex}")
            return False

    def _find_cameras(self) -> bool:
        """
        Enumerate all cameras visible to Spinnaker and match them to the
        config entries by serial number.
        Populates self.cameras: {cam_name: PySpin.Camera}.
        Returns False if any configured camera is not found.
        """
        cam_list = self.system.GetCameras()
        # Build a reverse map: serial_string → cam_name from config.
        wanted = {cfg["serial"]: name for name, cfg in self.cam_configs.items()}
        for cam in cam_list:
            node = PySpin.CStringPtr(
                cam.GetTLDeviceNodeMap().GetNode("DeviceSerialNumber"))
            if PySpin.IsAvailable(node) and PySpin.IsReadable(node):
                s = node.GetValue()
                if s in wanted:
                    self.cameras[wanted[s]] = cam
        cam_list.Clear()   # release the Spinnaker camera list reference
        missing = [n for n in self.cam_names if n not in self.cameras]
        if missing:
            print(f"Cameras not found: {missing}")
            return False
        return True

    def _init_cameras(self):
        """
        Find, initialise, configure, and begin streaming for all cameras.
        Raises RuntimeError if any camera is missing.
        """
        if not self._find_cameras():
            raise RuntimeError("Not all cameras found.")
        for name, cam in self.cameras.items():
            cam.Init()                      # allocates the Spinnaker camera object
            self._configure_camera(cam, name)
            cam.BeginAcquisition()          # camera starts delivering frames
            print(f"  {name}: streaming.")

    # ------------------------------------------------------------------
    # Capture thread — one per camera, runs for the entire session
    # ------------------------------------------------------------------

    def _capture_frame(self, cam_name: str, cam):
        """
        Runs in a background thread, continuously pulling frames from the
        camera's internal buffer and either caching them for preview or
        forwarding them to the CameraWriter.

        The thread always runs; it only forwards to the writer when
        self._recording[cam_name] is True.  This means the camera is
        always streaming at full frame rate, keeping the Spinnaker buffer
        flushed, even between recording sessions.

        Preview downsample
        ------------------
        To reduce CPU load on the UI thread, only every Nth frame is
        copied into _preview_frames.  N comes from the per-chamber
        "preview_downsample" setting in config (default 1 = every frame).
        """
        ch_name    = self.cam_to_chamber.get(cam_name, cam_name)
        ch_cfg     = self.chambers_cfg.get(ch_name, {})
        # Fall back to global preview.downsample if no per-chamber setting.
        downsample = ch_cfg.get("preview_downsample",
                        self.config.get("preview", {}).get("downsample", 1))
        idx = 0   # local frame counter used for downsample modulo

        while not self._stop_event.is_set():
            try:
                # GetNextImage blocks until a frame is ready or timeout expires.
                # trigger_timeout is in milliseconds.
                img = cam.GetNextImage(self.trigger_timeout)
                if img.IsIncomplete():
                    # The camera delivered a partial frame — discard it.
                    img.Release()
                    continue

                # Extract metadata from the Spinnaker image object.
                framecount = img.GetFrameID()              # hardware frame counter
                timestamp  = img.GetTimeStamp() * 1e-9    # nanoseconds → seconds
                sestime    = time.perf_counter() - self.session_start
                cputime    = time.time()                   # Python wall-clock time

                # Copy pixel data into a numpy array and release the Spinnaker
                # buffer immediately so the camera can reuse it.
                frame = np.array(img.GetNDArray(), copy=True)
                img.Release()

                idx += 1
                self._update_stats(cam_name, idx)

                # Update the preview cache at the downsampled rate.
                if (idx - 1) % max(1, downsample) == 0:
                    with self._preview_locks[cam_name]:
                        self._preview_frames[cam_name] = frame

                # Forward to the writer only when actively recording.
                if self._recording.get(cam_name):
                    w = self._writers.get(cam_name)
                    if w:
                        w.push_frame(frame, framecount, timestamp, sestime, cputime)

            except PySpin.SpinnakerException as ex:
                # Log unexpected Spinnaker errors but keep the thread alive
                # unless the stop event has been set.
                if not self._stop_event.is_set():
                    print(f"[Capture:{cam_name}] {ex}")

    def get_preview_frame(self, cam_name: str):
        """Thread-safe read of the latest cached preview frame (or None)."""
        with self._preview_locks[cam_name]:
            return self._preview_frames[cam_name]

    def _start_threads(self):
        """Spawn one capture thread per camera."""
        for name, cam in self.cameras.items():
            t = threading.Thread(target=self._capture_frame,
                                 args=(name, cam), daemon=True,
                                 name=f"capture-{name}")
            t.start()
            self._capture_threads.append(t)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def stop(self):
        """
        Gracefully shut down:
          1. Stop all active recordings (flushes frames and finalizes files).
          2. Signal capture threads to exit via _stop_event.
          3. End acquisition and deinitialise each camera.
        """
        self.stop_all_recording()
        self._stop_event.set()
        for cam in self.cameras.values():
            try:
                cam.EndAcquisition()   # stops the frame stream
                cam.DeInit()           # releases Spinnaker resources for this camera
            except Exception:
                pass
        self.cameras.clear()


# ---------------------------------------------------------------------------
# Stats popup window (drawn with OpenCV)
# ---------------------------------------------------------------------------
# The popup is a numpy image redrawn every 100 ms in the main loop.
# It shows one column per chamber with live stats and clickable buttons.

POPUP_W = 320   # pixel width of each chamber column
POPUP_H = 320   # total window height
BTN_H   = 28    # height of the START / STOP button row at the bottom

# Module-level list of button hit-zones, rebuilt every frame by build_stats_popup.
# _popup_mouse_cb reads this to decide which button was clicked.
_POPUP_BTN_ZONES: list = []


def build_stats_popup(streamer: "CameraStreamer", chamber_to_cam: dict) -> np.ndarray:
    """
    Render the stats popup image.

    One column is drawn per chamber.  Each column contains:
      - Chamber name and camera name
      - Recording status dot (● REC / ○ IDLE)
      - Live FPS
      - Buffer depth (with colour: teal → orange → red as it fills)
      - Elapsed recording time
      - Time remaining (if timer is enabled)
      - START and STOP buttons

    The hit-zones for the buttons are stored in _POPUP_BTN_ZONES so the
    mouse callback can match click coordinates to the right chamber.
    """
    global _POPUP_BTN_ZONES
    _POPUP_BTN_ZONES = []

    chambers = list(chamber_to_cam.keys())
    n_ch     = max(len(chambers), 1)
    # Canvas: height × total_width (one POPUP_W slot per chamber).
    img      = np.zeros((POPUP_H, POPUP_W * n_ch, 3), dtype=np.uint8)
    img[:]   = (18, 20, 22)   # near-black background

    font = cv2.FONT_HERSHEY_SIMPLEX
    sc, th, lh, pad = 0.50, 1, 22, 10   # scale, thickness, line-height, padding

    for col, ch_name in enumerate(chambers):
        x0       = col * POPUP_W   # left edge of this column in pixels
        cam_name = chamber_to_cam.get(ch_name, "")
        ch_cfg   = streamer.chambers_cfg.get(ch_name, {})

        # Gather live data for this chamber.
        rec    = streamer.is_recording(cam_name) if cam_name else False
        stats  = streamer.get_stats(cam_name) if cam_name else {"fps": 0.0, "total": 0}
        fps    = stats["fps"]
        buf    = streamer.buffered_frames(cam_name) if cam_name else 0
        elapsed = streamer.elapsed_recording(cam_name) if cam_name else 0.0

        # Compute time-left string if a timer is configured and running.
        timer_on = ch_cfg.get("timer_enabled", False)
        duration = float(ch_cfg.get("duration_s", 1800))
        if timer_on and rec and duration > 0:
            left     = max(0.0, duration - elapsed)
            lm, ls   = int(left) // 60, int(left) % 60
            left_str = f"{lm:02d}:{ls:02d}"
        else:
            left_str = "--:--"   # timer not active

        em, es      = int(elapsed) // 60, int(elapsed) % 60
        elapsed_str = f"{em:02d}:{es:02d}"

        rec_color = (0, 60, 220) if rec else (50, 50, 50)    # blue vs dark grey
        rec_text  = "● REC" if rec else "○ IDLE"

        # Buffer colour: teal (OK) → orange (getting full) → red (nearly full).
        max_buf  = CameraWriter.MAX_BUFFER_FRAMES
        buf_frac = buf / max_buf
        if buf_frac < 0.5:
            buf_color = (0, 200, 200)
        elif buf_frac < 0.85:
            buf_color = (0, 165, 255)
        else:
            buf_color = (0, 60, 255)

        # Vertical separator between columns.
        if col > 0:
            cv2.line(img, (x0, 0), (x0, POPUP_H), (40, 44, 50), 1)

        # Subtle blue tint behind the text area when recording to give a visual
        # at-a-glance cue even when the window is small.
        if rec:
            roi = img[4:POPUP_H - BTN_H - 8, x0 + 4:x0 + POPUP_W - 4]
            roi[:] = np.clip(roi.astype(int) + [0, 0, 22], 0, 255).astype(np.uint8)

        # Draw text lines, advancing y by lh (line height) each time.
        lines = [
            (ch_name,                      (220, 220, 220)),
            (cam_name or "no cam",         (100, 120, 120)),
            (rec_text,                     rec_color),
            (f"FPS      {fps:6.2f}",       (0, 220, 220)),
            (f"Buf {buf:>4}/{max_buf}",    buf_color),
            (f"Elapsed  {elapsed_str}",    (180, 180, 100)),
            (f"Time left {left_str}",      (120, 200, 120)),
        ]

        y = pad + lh
        for text, color in lines:
            cv2.putText(img, text, (x0 + pad, y), font, sc, color, th, cv2.LINE_AA)
            y += lh

        # Draw START button (left half of bottom row).
        btn_y  = POPUP_H - BTN_H - 4
        half_w = (POPUP_W - 3 * pad) // 2

        sx0, sx1 = x0 + pad,    x0 + pad + half_w
        sy0, sy1 = btn_y,        btn_y + BTN_H
        s_col    = (30, 140, 30) if not rec else (30, 60, 30)   # bright green / muted
        cv2.rectangle(img, (sx0, sy0), (sx1, sy1), s_col, -1)
        cv2.rectangle(img, (sx0, sy0), (sx1, sy1), (60, 160, 60), 1)
        cv2.putText(img, "START", (sx0 + 6, sy0 + 18),
                    font, 0.45, (180, 255, 180), 1, cv2.LINE_AA)

        # Draw STOP button (right half of bottom row).
        ex0, ex1 = sx1 + pad,   x0 + POPUP_W - pad
        e_col    = (140, 30, 30) if rec else (60, 30, 30)   # bright red / muted
        cv2.rectangle(img, (ex0, sy0), (ex1, sy1), e_col, -1)
        cv2.rectangle(img, (ex0, sy0), (ex1, sy1), (160, 60, 60), 1)
        cv2.putText(img, "STOP", (ex0 + 8, sy0 + 18),
                    font, 0.45, (255, 180, 180), 1, cv2.LINE_AA)

        # Store the pixel rectangles for hit-testing in _popup_mouse_cb.
        _POPUP_BTN_ZONES.append(((sx0, sy0, sx1, sy1), (ex0, sy0, ex1, sy1)))

    # Global keyboard hint at the bottom of the window.
    cv2.putText(img, "S: start all   X: stop all   ESC: quit",
                (pad, POPUP_H - 6), font, 0.36, (60, 60, 60), 1, cv2.LINE_AA)

    return img


def _popup_mouse_cb(event, x, y, flags, param):
    """
    OpenCV mouse callback registered on the stats popup window.
    On left-click, checks whether (x, y) falls inside a START or STOP button
    for any chamber and calls start_recording / stop_recording accordingly.
    """
    if event != cv2.EVENT_LBUTTONDOWN:
        return
    streamer: CameraStreamer = param["streamer"]
    chambers: list           = param["chambers"]
    chamber_to_cam: dict     = param["chamber_to_cam"]

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
# Helper: enumerate connected cameras (used by setup wizard)
# ---------------------------------------------------------------------------

def get_connected_serials(system) -> list:
    """Return a list of dicts with serial, model, and vendor for each connected camera."""
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
# Entry point
# ---------------------------------------------------------------------------

def main():
    # Parse command-line arguments.
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default="config.yaml",
                        help="Path to config.yaml (default: config.yaml in cwd)")
    parser.add_argument("--setup", action="store_true",
                        help="Run the interactive setup wizard to generate config.yaml")
    args = parser.parse_args()

    # Obtain the global Spinnaker system object — this enumerates all cameras.
    system  = PySpin.System.GetInstance()
    version = system.GetLibraryVersion()
    print(f"Spinnaker {version.major}.{version.minor}.{version.type}.{version.build}")

    if args.setup:
        # Run the wizard, write the config, then exit.
        run_setup_wizard(system, args.config)
        system.ReleaseInstance()
        return

    if not os.path.isfile(args.config):
        print(f"Config not found: {args.config}")
        system.ReleaseInstance()
        return

    # Load config and build the streamer.
    config   = load_config(args.config)
    streamer = CameraStreamer(config, system)

    # Find, configure, and start all cameras.
    try:
        streamer._init_cameras()
    except RuntimeError as e:
        print(f"\n[Acquisition] Failed to start: {e}")
        system.ReleaseInstance()
        return

    # Start per-camera capture threads (they run for the whole session).
    streamer._start_threads()

    # ------------------------------------------------------------------
    # Build AnalogTTLListener instances — one per unique Arduino port
    # ------------------------------------------------------------------
    # Walk the ttl_map entries to discover which ports are needed.
    # Multiple ttl_map entries with the same port share one listener
    # because that one listener reads all lines from the Arduino and
    # dispatches by TTL channel number.
    ttl_map          = config.get("ttl_map", [])
    ttl_dispatch_q   = queue.Queue()   # shared queue between all listeners → router thread
    listeners: list  = []
    port_to_listener = {}   # prevents creating duplicate listeners for the same port

    global_ard   = config.get("arduino", {})
    default_port = global_ard.get("port", "")
    default_baud = global_ard.get("baud", 115200)

    for entry in ttl_map:
        # Per-entry arduino block overrides the global defaults.
        ard  = entry.get("arduino", {})
        port = ard.get("port", default_port)
        baud = ard.get("baud", default_baud)

        if not port:
            print(f"[Warning] ttl_map entry for pin {entry.get('pin','?')} "
                  f"has no port — set arduino.port in the entry or in the "
                  f"top-level arduino: block in config.yaml.")
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
        print("[Warning] No Arduino listeners started — TTL triggering inactive.")

    # ------------------------------------------------------------------
    # Dedicated TTL router thread
    # ------------------------------------------------------------------
    # Previously, TTL events were drained from ttl_dispatch_q inside the
    # main UI loop after cv2.waitKey(100).  This meant events could sit
    # unprocessed for up to 100 ms (longer if the UI was slow), which caused
    # the second chamber's TTL start to appear unreliable when both fired
    # close together.
    #
    # Now the router runs in its own thread and processes events within
    # ~50 ms of the listener confirming them, entirely independent of UI pace.
    ttl_router_stop = threading.Event()

    def _ttl_router():
        # Block up to 50 ms waiting for an event; if none arrives, loop and
        # check the stop flag so the thread exits cleanly on shutdown.
        while not ttl_router_stop.is_set():
            try:
                evt = ttl_dispatch_q.get(timeout=0.05)
                streamer.route_ttl_event(evt)
            except queue.Empty:
                continue

    ttl_router_thread = threading.Thread(
        target=_ttl_router, daemon=True, name="ttl-router"
    )
    ttl_router_thread.start()

    # ------------------------------------------------------------------
    # Auto-start (optional)
    # ------------------------------------------------------------------
    acq_cfg    = config.get("acquisition", {})
    auto_start = acq_cfg.get("auto_start", False)
    if auto_start:
        print("[Main] Auto-start — recording all eligible chambers.")
        streamer.start_all_recording()

    # Print startup info so the operator knows the buffer headroom.
    print(f"\nBuffer cap: {CameraWriter.MAX_BUFFER_FRAMES} frames per camera "
          f"(~{CameraWriter.MAX_BUFFER_FRAMES / max(streamer.fps, 1):.1f} s at "
          f"{streamer.fps:.1f} fps).")
    print("Stats popup open.")
    print("  Click START / STOP buttons per chamber")
    print("  S = start all   X = stop all   ESC = quit\n")

    # ------------------------------------------------------------------
    # Create OpenCV windows
    # ------------------------------------------------------------------
    POPUP_WIN = "Acquisition Stats"
    cv2.namedWindow(POPUP_WIN, cv2.WINDOW_NORMAL)

    chambers_list = list(streamer.chamber_to_cam.keys())
    # Register the mouse callback, passing references needed to handle clicks.
    cv2.setMouseCallback(POPUP_WIN, _popup_mouse_cb, {
        "streamer":       streamer,
        "chambers":       chambers_list,
        "chamber_to_cam": streamer.chamber_to_cam,
    })

    preview_enabled      = config.get("preview", {}).get("enabled", True)
    PREVIEW_WIN          = "Acquisition Preview"
    default_w, default_h = 1280, 720

    if preview_enabled:
        cv2.namedWindow(PREVIEW_WIN, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(PREVIEW_WIN, default_w, default_h)

    # ------------------------------------------------------------------
    # Main UI loop — runs at ~10 Hz (100 ms per iteration)
    # ------------------------------------------------------------------
    # TTL routing is handled by the router thread, not here.
    # This loop only handles: stats popup refresh, preview render, keyboard,
    # and per-chamber timer checks.
    try:
        while True:
            # Redraw and display the stats popup every iteration.
            popup = build_stats_popup(streamer, streamer.chamber_to_cam)
            cv2.imshow(POPUP_WIN, popup)

            if preview_enabled:
                tiles = []
                for name in streamer.cam_names:
                    frame = streamer.get_preview_frame(name)
                    if frame is None:
                        # Camera hasn't delivered a frame yet — show black.
                        tiles.append(np.zeros((480, 640, 3), dtype=np.uint8))
                    else:
                        # Convert grayscale to BGR for display if needed.
                        bgr = (cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
                               if frame.ndim == 2 else frame.copy())
                        cam_cfg = config["cameras"][name]
                        label   = cam_cfg.get("name", name)
                        chamber = streamer.cam_to_chamber.get(name, "")
                        stats   = streamer.get_stats(name)
                        # Burn the status overlay onto the tile.
                        bgr = draw_tile_overlay(
                            bgr, label, chamber, stats["fps"], stats["total"],
                            recording=streamer.is_recording(name),
                        )
                        tiles.append(bgr)

                # Query the current preview window size for correct tiling.
                try:
                    _, _, pw, ph = cv2.getWindowImageRect(PREVIEW_WIN)
                    if pw < 64 or ph < 64:
                        pw, ph = default_w, default_h
                except Exception:
                    pw, ph = default_w, default_h

                cv2.imshow(PREVIEW_WIN, tile_frames(tiles[:4], pw, ph))

            # waitKey(100) renders pending OpenCV events and waits up to 100 ms.
            # The return value is the ASCII code of any key pressed, or -1.
            key = cv2.waitKey(100) & 0xFF

            if key in (ord("s"), ord("S")):
                streamer.start_all_recording()
            elif key in (ord("x"), ord("X")):
                streamer.stop_all_recording()
            elif key == 27:    # ESC
                break

            # Per-chamber timer: stop recording automatically after duration_s.
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
        # ------------------------------------------------------------------
        # Cleanup — runs on ESC, exception, or Ctrl-C
        # ------------------------------------------------------------------
        # Stop the TTL router first so no new recording actions are triggered
        # while we are in the process of shutting down.
        ttl_router_stop.set()
        ttl_router_thread.join(timeout=1.0)

        # Stop all recordings and release cameras.
        streamer.stop()

        # Stop all Arduino serial listeners.
        for lst in listeners:
            lst.stop()

        # Destroy OpenCV windows.
        cv2.destroyAllWindows()

        # Release the Spinnaker camera list and system object.
        # These must be released in the correct order to avoid SDK errors.
        cam_list = system.GetCameras()
        cam_list.Clear()
        del cam_list
        system.ReleaseInstance()
        print("Done.")


if __name__ == "__main__":
    main()