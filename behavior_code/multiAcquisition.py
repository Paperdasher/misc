import os
import threading
import queue
import numpy as np
import cv2
import PySpin


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WRITER_FPS = 60.0
WRITER_FOURCC = "mp4v"
OUTPUT_DIR = "recordings"


# ---------------------------------------------------------------------------
# CameraStreamer
# ---------------------------------------------------------------------------

class CameraStreamer:
    """
    One capture thread per camera. Each thread fans out to:
      - A preview slot  (latest frame only, disposable, non-blocking read)
      - A writer queue  (every frame, unbounded, blocking write to disk)
    """

    def __init__(self, cam_list):
        self.cam_list = cam_list
        self.camera_count = cam_list.GetSize()
        self._stop_event = threading.Event()

        # Preview path: one slot + one lock per camera
        self.preview_frames = [None] * self.camera_count
        self.preview_locks = [threading.Lock() for _ in range(self.camera_count)]

        # Writer path: one unbounded queue per camera
        self.writer_queues = [queue.Queue() for _ in range(self.camera_count)]

        self._capture_threads = []
        self._writer_threads = []

        os.makedirs(OUTPUT_DIR, exist_ok=True)

        self._init_cameras()
        self._start_threads()

    # ------------------------------------------------------------------
    # Spinnaker camera setup  (kept from example)
    # ------------------------------------------------------------------

    def _configure_camera(self, cam, cam_index):
        """Set AcquisitionMode to Continuous. Extend here for exposure, gain, etc."""
        nodemap = cam.GetNodeMap()

        node_acquisition_mode = PySpin.CEnumerationPtr(
            nodemap.GetNode("AcquisitionMode")
        )
        if not PySpin.IsAvailable(node_acquisition_mode) or not PySpin.IsWritable(
            node_acquisition_mode
        ):
            print(
                f"Camera {cam_index}: unable to set acquisition mode. Aborting."
            )
            return False

        node_continuous = node_acquisition_mode.GetEntryByName("Continuous")
        if not PySpin.IsAvailable(node_continuous) or not PySpin.IsReadable(
            node_continuous
        ):
            print(
                f"Camera {cam_index}: 'Continuous' entry not available. Aborting."
            )
            return False

        node_acquisition_mode.SetIntValue(node_continuous.GetValue())
        print(f"Camera {cam_index}: acquisition mode set to Continuous.")

        if cam.ExposureAuto.GetAccessMode() != PySpin.RW:
            print('Unable to disable automatic exposure. Aborting...')
            return False

        cam.ExposureAuto.SetValue(PySpin.ExposureAuto_Off)
        print('Automatic exposure disabled...')

        # Ensure desired exposure time does not exceed the maximum
        exposure_time_to_set = 5000 # 5 miliseconds / 5000 microseconds 
        exposure_time_to_set = min(cam.ExposureTime.GetMax(), exposure_time_to_set)
        cam.ExposureTime.SetValue(exposure_time_to_set)
        print('Shutter time set to %s us...\n' % exposure_time_to_set)

        return True

    def _init_cameras(self):
        for i, cam in enumerate(self.cam_list):
            cam.Init()
            self._configure_camera(cam, i)
            cam.BeginAcquisition()
            print(f"Camera {i}: acquisition started.")

    # ------------------------------------------------------------------
    # Capture thread  (one per camera)
    # ------------------------------------------------------------------

    def _capture_frame(self, index: int, cam):
        """
        Runs in its own thread. Grabs frames from Spinnaker, releases the
        image buffer immediately, then fans out to preview slot and writer queue.
        """
        while not self._stop_event.is_set():
            try:
                # 1000 ms timeout so the thread can check _stop_event periodically
                image_result = cam.GetNextImage(1000)

                if image_result.IsIncomplete():
                    print(
                        f"Camera {index}: incomplete image "
                        f"(status {image_result.GetImageStatus()})"
                    )
                    image_result.Release()
                    continue

                # Convert to BGR8 so OpenCV/VideoWriter can consume it directly
                converted = image_result.Convert(
                    PySpin.PixelFormat_BGR8, PySpin.HQ_LINEAR
                )
                frame = converted.GetNDArray()          # numpy array, still Spinnaker memory
                frame = np.array(frame, copy=True)      # own copy before Release()

                # Release Spinnaker buffer ASAP — fixed internal buffer, don't starve it
                image_result.Release()

                # --- Preview path: overwrite slot, never blocks ---
                with self.preview_locks[index]:
                    self.preview_frames[index] = frame

                # --- Writer path: enqueue a second copy so both paths are independent ---
                self.writer_queues[index].put(frame.copy())

            except PySpin.SpinnakerException as ex:
                if not self._stop_event.is_set():
                    print(f"Camera {index} capture error: {ex}")

    # ------------------------------------------------------------------
    # Writer thread  (one per camera)
    # ------------------------------------------------------------------

    import subprocess
    import shutil

    def _make_ffmpeg_writer(self, output_path: str, width: int, height: int) -> subprocess.Popen:
        """
        Open an ffmpeg process that reads raw BGR frames from stdin and encodes
        to H.264 in an MP4 container with faststart.
        """
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found on PATH")

        cmd = [
            "ffmpeg",
            "-y",                           # overwrite output if exists
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-pix_fmt", "bgr24",            # matches OpenCV/Spinnaker BGR8 numpy layout
            "-s", f"{width}x{height}",
            "-r", str(WRITER_FPS),
            "-i", "pipe:0",                 # read from stdin
            "-vcodec", "libx264",
            "-preset", "fast",              # fast/medium/slow — tradeoff encode speed vs filesize
            "-crf", "18",                   # quality: 0=lossless, 51=worst; 18 is near-lossless
            "-pix_fmt", "yuv420p",          # broadest player compatibility
            "-movflags", "+faststart",      # moov atom at front — file is playable while recording
            output_path,
        ]
        return subprocess.Popen(cmd, stdin=subprocess.PIPE)


    def _write_frames(self, index: int, serial: str):
        output_path = os.path.join(OUTPUT_DIR, f"camera_{serial}.mp4")
        proc = None

        while not self._stop_event.is_set():
            try:
                frame = self.writer_queues[index].get(timeout=1.0)
            except queue.Empty:
                continue

            if proc is None:
                h, w = frame.shape[:2]
                proc = self._make_ffmpeg_writer(output_path, w, h)
                print(f"Camera {index}: writing to {output_path} at {w}x{h}")

            # prevent breaking if ffmpeg exits unexpectedly
            try:
                proc.stdin.write(frame.tobytes())
            except BrokenPipeError:
                print(f"Camera {index}: ffmpeg pipe broken, restarting writer...")
                proc = None   # will reinitialize on next frame

        # Drain remaining frames
        while not self.writer_queues[index].empty():
            try:
                frame = self.writer_queues[index].get_nowait()
                if proc:
                    proc.stdin.write(frame.tobytes())
            except queue.Empty:
                break

        # Close stdin so ffmpeg knows the stream is done, then wait for it to finish muxing
        if proc:
            proc.stdin.close()
            proc.wait()
            print(f"Camera {index}: ffmpeg writer finished.")


    # ------------------------------------------------------------------
    # Thread startup
    # ------------------------------------------------------------------

    def _start_threads(self):
        for i, cam in enumerate(self.cam_list):
            serial = _get_serial(cam, i)

            ct = threading.Thread(
                target=self._capture_frame,
                args=(i, cam),
                daemon=True,
                name=f"capture-{i}",
            )
            ct.start()
            self._capture_threads.append(ct)

            wt = threading.Thread(
                target=self._write_frames,
                args=(i, serial),
                daemon=True,
                name=f"writer-{i}",
            )
            wt.start()
            self._writer_threads.append(wt)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_preview(self, index: int):
        """Non-blocking. Returns the latest frame (numpy BGR) or None."""
        with self.preview_locks[index]:
            return self.preview_frames[index]

    def stop(self):
        """Signal all threads to stop, then clean up Spinnaker resources."""
        print("Stopping acquisition...")
        self._stop_event.set()

        # Wait for writer threads to flush (capture threads are daemons, skip join)
        for wt in self._writer_threads:
            wt.join(timeout=5.0)

        for cam in self.cam_list:
            cam.EndAcquisition()
            cam.DeInit()

        del cam  # Spinnaker: explicit del, not just None assignment


# ---------------------------------------------------------------------------
# Helpers  (kept from example)
# ---------------------------------------------------------------------------

def _get_serial(cam, cam_index: int) -> str:
    """Read device serial number from TL nodemap."""
    try:
        node = PySpin.CStringPtr(
            cam.GetTLDeviceNodeMap().GetNode("DeviceSerialNumber")
        )
        if PySpin.IsAvailable(node) and PySpin.IsReadable(node):
            return node.GetValue()
    except PySpin.SpinnakerException:
        pass
    return str(cam_index)


def print_device_info(nodemap, cam_index: int) -> bool:
    """Print transport-layer device info. Kept verbatim from example."""
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
    # Write-permission check (kept from example)
    try:
        test_file = open("test.txt", "w+")
        test_file.close()
        os.remove(test_file.name)
    except IOError:
        print("Unable to write to current directory. Please check permissions.")
        return False

    # Spinnaker system init (kept from example)
    system = PySpin.System.GetInstance()
    version = system.GetLibraryVersion()
    print(
        f"Spinnaker library version: "
        f"{version.major}.{version.minor}.{version.type}.{version.build}"
    )

    cam_list = system.GetCameras()
    num_cameras = cam_list.GetSize()
    print(f"Cameras detected: {num_cameras}")

    if num_cameras == 0:
        cam_list.Clear()
        system.ReleaseInstance()
        print("No cameras found.")
        return False

    # Print device info for each camera (kept from example)
    print("*** DEVICE INFORMATION ***\n")
    for i, cam in enumerate(cam_list):
        print_device_info(cam.GetTLDeviceNodeMap(), i)

    # Start streamer
    streamer = CameraStreamer(cam_list)
    labels = [f"Camera {_get_serial(cam, i)}" for i, cam in enumerate(cam_list)]

    print("Streaming — press ESC to stop.\n")
    try:
        while True:
            for i in range(streamer.camera_count):
                frame = streamer.get_preview(i)
                if frame is not None:
                    cv2.imshow(labels[i], frame)

            if cv2.waitKey(20) == 27:  # ESC
                break
    finally:
        streamer.stop()
        cv2.destroyAllWindows()

        # Spinnaker cleanup (kept from example)
        cam_list.Clear()
        system.ReleaseInstance()
        print("Done.")

    return True


if __name__ == "__main__":
    main()