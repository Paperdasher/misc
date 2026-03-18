import os
import threading
import queue
import subprocess
import shutil
from datetime import datetime
import cv2
import numpy as np
import PySpin


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_W   = 1440
TARGET_H   = 1080
OFFSET_X   = 0
OFFSET_Y   = 0

WRITER_FPS    = 60.0               # CHANGE ACCORDINGLY
WRITER_FOURCC = "mp4v"
OUTPUT_DIR    = "recordings"       # video output folder name

TRIGGER_LINE       = "Line0"       # GPIO line TTL is wired to
TRIGGER_ACTIVATION = "RisingEdge"  # RisingEdge or FallingEdge
TRIGGER_SELECTOR   = "AcquisitionStart" # AcquisitionStart = one pulse starts streaming

GETNEXTIMAGE_TIMEOUT = 500        # ms — increase if TTL pulses are infrequent


# ---------------------------------------------------------------------------
# CameraStreamer
# ---------------------------------------------------------------------------

class CameraStreamer:
    """
    One capture thread + one writer thread per camera.

    Capture thread fans out to:
      - Preview slot  (latest frame only, overwritten unconditionally, non-blocking read)
      - Writer queue  (every frame, unbounded, blocking write to disk via ffmpeg)
    """

    def __init__(self, cam_list):
        self.cam_list     = cam_list
        self.camera_count = cam_list.GetSize()
        self._stop_event  = threading.Event() # Threading flag

        # Preview path, most recent frame
        self.preview_frames = [None] * self.camera_count
        self.preview_locks  = [threading.Lock() for _ in range(self.camera_count)]

        # Writer path, stores all frames
        self.writer_queues = [queue.Queue() for _ in range(self.camera_count)]

        self._capture_threads = []
        self._writer_threads  = []

        os.makedirs(OUTPUT_DIR, exist_ok=True) # make output directory

        # Configure all cameras and start threads
        self._init_cameras()
        self._start_threads()

    # ------------------------------------------------------------------
    # Spinnaker camera configuration
    # ------------------------------------------------------------------

    def _configure_trigger(self, nodemap, cam_index: int) -> bool:
        """
        Configure hardware TTL trigger on TRIGGER_LINE.
        Must be called before BeginAcquisition().
        """
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
                    node_trigger_selector.GetEntryByName(TRIGGER_SELECTOR).GetValue()
                )
                print(f"  Camera {cam_index}: TriggerSelector = {TRIGGER_SELECTOR}")

            # Trigger source — which GPIO line
            node_trigger_source = PySpin.CEnumerationPtr(
                nodemap.GetNode("TriggerSource")
            )
            if PySpin.IsAvailable(node_trigger_source) and PySpin.IsWritable(
                node_trigger_source
            ):
                node_trigger_source.SetIntValue(
                    node_trigger_source.GetEntryByName(TRIGGER_LINE).GetValue()
                )
                print(f"  Camera {cam_index}: TriggerSource = {TRIGGER_LINE}")

            # Trigger activation — rising or falling edge
            node_trigger_activation = PySpin.CEnumerationPtr(
                nodemap.GetNode("TriggerActivation")
            )
            if PySpin.IsAvailable(node_trigger_activation) and PySpin.IsWritable(
                node_trigger_activation
            ):
                node_trigger_activation.SetIntValue(
                    node_trigger_activation.GetEntryByName(
                        TRIGGER_ACTIVATION
                    ).GetValue()
                )
                print(f"  Camera {cam_index}: TriggerActivation = {TRIGGER_ACTIVATION}")

            # Turn trigger mode on — camera now waits for hardware signal
            if PySpin.IsAvailable(node_trigger_mode) and PySpin.IsWritable(
                node_trigger_mode
            ):
                node_trigger_mode.SetIntValue(
                    node_trigger_mode.GetEntryByName("On").GetValue()
                )
                print(f"  Camera {cam_index}: TriggerMode = On")

            return True

        except PySpin.SpinnakerException as ex:
            print(f"  Camera {cam_index} trigger config error: {ex}")
            return False

    def _configure_camera(self, cam, cam_index: int) -> bool:
        nodemap = cam.GetNodeMap()
        print(f"\nCamera {cam_index}: configuring...\n")

        try:
            # ----------------------------------------------------------------
            # Pixel format — BGR8 for colour output to ffmpeg (bgr24)
            # Falls back to BayerRG8 if BGR8 not available on-camera;
            # Convert() in _capture_frame handles demosaic either way.
            # ----------------------------------------------------------------
            node_pixel_format = PySpin.CEnumerationPtr(
                nodemap.GetNode("PixelFormat")
            )
            if PySpin.IsAvailable(node_pixel_format) and PySpin.IsWritable(
                node_pixel_format
            ):
                node_pixel_format_bgr8 = PySpin.CEnumEntryPtr(
                    node_pixel_format.GetEntryByName("BGR8")
                )
                if not (
                    PySpin.IsAvailable(node_pixel_format_bgr8)
                    and PySpin.IsReadable(node_pixel_format_bgr8)
                ):
                    node_pixel_format_bgr8 = PySpin.CEnumEntryPtr(
                        node_pixel_format.GetEntryByName("BayerRG8")
                    )
                node_pixel_format.SetIntValue(node_pixel_format_bgr8.GetValue())
                print(
                    f"  Pixel format: "
                    f"{node_pixel_format.GetCurrentEntry().GetSymbolic()}"
                )

            # ----------------------------------------------------------------
            # ROI — zero offsets first, then set dimensions, then re-apply offsets.
            # This order is mandatory: setting width/height while a non-zero offset
            # pushes the window out of sensor bounds will be rejected.
            # ----------------------------------------------------------------
            node_offset_x = PySpin.CIntegerPtr(nodemap.GetNode("OffsetX"))
            node_offset_y = PySpin.CIntegerPtr(nodemap.GetNode("OffsetY"))

            if PySpin.IsAvailable(node_offset_x) and PySpin.IsWritable(node_offset_x):
                node_offset_x.SetValue(node_offset_x.GetMin())
            if PySpin.IsAvailable(node_offset_y) and PySpin.IsWritable(node_offset_y):
                node_offset_y.SetValue(node_offset_y.GetMin())

            node_width = PySpin.CIntegerPtr(nodemap.GetNode("Width"))
            if PySpin.IsAvailable(node_width) and PySpin.IsWritable(node_width):
                w_min = node_width.GetMin()
                w_inc = node_width.GetInc()
                w_set = min(TARGET_W, node_width.GetMax())
                w_set = w_min + ((w_set - w_min) // w_inc) * w_inc
                node_width.SetValue(w_set)
                print(f"  Width: {node_width.GetValue()}")

            node_height = PySpin.CIntegerPtr(nodemap.GetNode("Height"))
            if PySpin.IsAvailable(node_height) and PySpin.IsWritable(node_height):
                h_min = node_height.GetMin()
                h_inc = node_height.GetInc()
                h_set = min(TARGET_H, node_height.GetMax())
                h_set = h_min + ((h_set - h_min) // h_inc) * h_inc
                node_height.SetValue(h_set)
                print(f"  Height: {node_height.GetValue()}")

            if PySpin.IsAvailable(node_offset_x) and PySpin.IsWritable(node_offset_x):
                x_inc = node_offset_x.GetInc()
                x_set = (min(OFFSET_X, node_offset_x.GetMax()) // x_inc) * x_inc
                node_offset_x.SetValue(x_set)
                print(f"  OffsetX: {x_set}")

            if PySpin.IsAvailable(node_offset_y) and PySpin.IsWritable(node_offset_y):
                y_inc = node_offset_y.GetInc()
                y_set = (min(OFFSET_Y, node_offset_y.GetMax()) // y_inc) * y_inc
                node_offset_y.SetValue(y_set)
                print(f"  OffsetY: {y_set}")

            # ----------------------------------------------------------------
            # TTL trigger
            # ----------------------------------------------------------------
            self._configure_trigger(nodemap, cam_index)

            # ----------------------------------------------------------------
            # Acquisition mode — Continuous
            # ----------------------------------------------------------------
            node_acquisition_mode = PySpin.CEnumerationPtr(
                nodemap.GetNode("AcquisitionMode")
            )
            if PySpin.IsAvailable(node_acquisition_mode) and PySpin.IsWritable(
                node_acquisition_mode
            ):
                node_continuous = node_acquisition_mode.GetEntryByName("Continuous")
                if PySpin.IsAvailable(node_continuous) and PySpin.IsReadable(
                    node_continuous
                ):
                    node_acquisition_mode.SetIntValue(node_continuous.GetValue())
                    print(f"  AcquisitionMode: Continuous")
                else:
                    print(f"  Camera {cam_index}: Continuous mode not available.")
                    return False
                
            if cam.ExposureAuto.GetAccessMode() != PySpin.RW:
                print('Unable to disable automatic exposure. Aborting...')
                return False

            cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)
            print('Automatic exposure disabled...')

            # Ensure desired exposure time does not exceed the maximum
            exposure_time_to_set = 5000 # 5ms or 5000 microseconds
            exposure_time_to_set = min(cam.ExposureTime.GetMax(), exposure_time_to_set)
            cam.ExposureTime.SetValue(exposure_time_to_set)
            print('Shutter time set to %s us...\n' % exposure_time_to_set)

            return True

        except PySpin.SpinnakerException as ex:
            print(f"  Camera {cam_index} configure error: {ex}")
            return False

    def _init_cameras(self):
        for i, cam in enumerate(self.cam_list):
            cam.Init()
            self._configure_camera(cam, i)
            cam.BeginAcquisition()
            print(f"Camera {i}: armed, waiting for TTL on {TRIGGER_LINE}.")

    # ------------------------------------------------------------------
    # Capture thread — one per camera
    # ------------------------------------------------------------------

    def _capture_frame(self, index: int, cam):
        while not self._stop_event.is_set():
            try: # Until timeout
                image_result = cam.GetNextImage(GETNEXTIMAGE_TIMEOUT)

                if image_result.IsIncomplete():
                    print(
                        f"Camera {index}: incomplete image "
                        f"(status {image_result.GetImageStatus()})"
                    )
                    image_result.Release() # release corrupt incomplete frame
                    continue

                # Convert to BGR8 — consistent pixel format for both preview
                # (cv2.imshow) and writer (ffmpeg bgr24). If the camera is already
                # BGR8 this is a no-op internally.
                converted = image_result.Convert(
                    PySpin.PixelFormat_BGR8, PySpin.HQ_LINEAR
                )
                # One copy owns the data; Release() is then safe immediately.
                frame = np.array(converted.GetNDArray(), copy=True)
                image_result.Release()

                # Preview path — overwrite slot, never blocks
                with self.preview_locks[index]:
                    self.preview_frames[index] = frame

                # Writer path — same array, writer never mutates it
                self.writer_queues[index].put(frame)

            except PySpin.SpinnakerException as ex:
                if not self._stop_event.is_set():
                    print(f"Camera {index} capture error: {ex}")

    # ------------------------------------------------------------------
    # Writer thread — one per camera
    # ------------------------------------------------------------------

    def _make_ffmpeg_writer(
        self, output_path: str, width: int, height: int
    ) -> subprocess.Popen:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found on PATH")
        
        """
        libx264 is H.264 software encoding. 
        preset fast is a speed/compression tradeoff — slower presets produce smaller files at the same quality. 
        crf 18 is the quality level — 0 is lossless, 51 is worst, 18 is near-lossless visually. 
        yuv420p is required for broad player compatibility
        """
        cmd = [
            "ffmpeg",
            "-y",
            "-f",       "rawvideo",
            "-vcodec",  "rawvideo",
            "-pix_fmt", "bgr24",
            "-s",       f"{width}x{height}",
            "-r",       str(WRITER_FPS),
            "-i",       "pipe:0",
            "-vcodec",  "libx264",
            "-preset",  "fast",
            "-crf",     "18",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            output_path,
        ]
        
        return subprocess.Popen(cmd, stdin=subprocess.PIPE)

    def _write_frames(self, index: int, serial: str):
        ts          = datetime.now().strftime("%Y%m%d_%H%M%S") # timestamp at start of thread
        output_path = os.path.join(OUTPUT_DIR, f"camera_{serial}_{ts}.mp4")
        proc        = None

        while not self._stop_event.is_set():
            try:
                frame = self.writer_queues[index].get(timeout=1.0)
            except queue.Empty:
                continue

            # Lazily open ffmpeg on the first frame so dimensions are known
            if proc is None:
                h, w = frame.shape[:2]
                proc = self._make_ffmpeg_writer(output_path, w, h)
                print(f"Camera {index}: writing to {output_path} at {w}x{h}")

            try:
                proc.stdin.write(frame.tobytes()) # converts numpy array to flat bytes object
            except BrokenPipeError:
                print(f"Camera {index}: ffmpeg pipe broken, restarting writer...")
                proc = None

        # Drain remaining frames on shutdown so nothing is lost
        while not self.writer_queues[index].empty():
            try:
                frame = self.writer_queues[index].get_nowait()
                if proc:
                    proc.stdin.write(frame.tobytes())
            except (queue.Empty, BrokenPipeError):
                break

        if proc:
            proc.stdin.close()
            proc.wait()
            print(f"Camera {index}: writer finished.")

    # ------------------------------------------------------------------
    # Thread startup
    # ------------------------------------------------------------------

    def _start_threads(self):
        for i, cam in enumerate(self.cam_list):
            serial = _get_serial(cam, i) # Camera's serial number

            # Create capture thread
            ct = threading.Thread(
                target=self._capture_frame, 
                args=(i, cam), # arguments passed to above func(i = know what queue, cam = know what cam to call GetNextImage)
                daemon=True, # marks as background thread
                name=f"capture-{i}", # for debugging
            )
            ct.start()
            self._capture_threads.append(ct)

            # Creates write thread
            wt = threading.Thread( 
                target=self._write_frames,
                args=(i, serial), # Doesn't touch camera, reads from self.writer_queues[i]
                daemon=True,
                name=f"writer-{i}",
            )
            wt.start()
            self._writer_threads.append(wt)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_preview(self, index: int):
        """Non-blocking. Returns latest BGR numpy frame or None."""
        with self.preview_locks[index]:
            return self.preview_frames[index]

    def stop(self):
        """
        Sets the stop event so all threads exit their loops. 
        Joins writer threads with a 5 second timeout to let them finish flushing — 
        capture threads are daemons so they don't need joining, they'll die when the main process exits.
        """
        print("\nStopping acquisition...")
        self._stop_event.set()

        for wt in self._writer_threads:
            wt.join(timeout=5.0)

        for cam in self.cam_list:
            cam.EndAcquisition()
            cam.DeInit()

        del cam


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_serial(cam, cam_index: int) -> str:
    try:
        node = PySpin.CStringPtr(
            cam.GetTLDeviceNodeMap().GetNode("DeviceSerialNumber")
        )
        if PySpin.IsAvailable(node) and PySpin.IsReadable(node):
            return node.GetValue()
    except PySpin.SpinnakerException:
        pass
    return str(cam_index)

# Mostly kept from example
def print_device_info(nodemap, cam_index: int) -> bool:
    print(f"Printing device information for camera {cam_index}...\n")
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
        else:
            print("  Device control information not available.")
        print()
        return True
    except PySpin.SpinnakerException as ex:
        print(f"  Error: {ex}")
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> bool:
    try:
        test_file = open("test.txt", "w+")
        test_file.close()
        os.remove(test_file.name)
    except IOError:
        print("Unable to write to current directory. Please check permissions.")
        return False

    system  = PySpin.System.GetInstance()
    version = system.GetLibraryVersion()
    print(
        f"Spinnaker library version: "
        f"{version.major}.{version.minor}.{version.type}.{version.build}"
    )

    cam_list    = system.GetCameras()
    num_cameras = cam_list.GetSize()
    print(f"Cameras detected: {num_cameras}")

    if num_cameras == 0:
        cam_list.Clear()
        system.ReleaseInstance()
        print("No cameras found.")
        return False

    print("\n*** DEVICE INFORMATION ***\n")
    for i, cam in enumerate(cam_list):
        print_device_info(cam.GetTLDeviceNodeMap(), i)

    streamer = CameraStreamer(cam_list)
    labels   = [f"Camera {_get_serial(cam, i)}" for i, cam in enumerate(cam_list)]

    print("\nStreaming — press ESC to stop.\n")
    try:
        while True:
            for i in range(streamer.camera_count):
                frame = streamer.get_preview(i)
                if frame is not None:
                    cv2.imshow(labels[i], frame)

            if cv2.waitKey(20) == 27: # ESC
                break
    finally:
        streamer.stop()
        cv2.destroyAllWindows()
        cam_list.Clear()
        system.ReleaseInstance()
        print("Done.")

    return True


if __name__ == "__main__":
    main()