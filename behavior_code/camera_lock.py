"""
camera_lock.py — Cross-process exclusivity lock for Spinnaker cameras
=======================================================================
Spinnaker (FLIR/Blackfly) cameras can only be opened — Init() + 
BeginAcquisition() — by ONE process at a time. If two processes (e.g.
preview.py and multiAcquisition.py) both try to open the same camera
serial, the SDK's internal frame buffer gets corrupted across the two
driver handles. This typically surfaces as a SpinnakerException mentioning
the buffer, and the losing process's capture thread silently freezes —
the window stays open but never receives a new frame again.

This module provides a simple file-based lock so either script can detect
the conflict BEFORE calling Init(), refuse cleanly, and tell the user
exactly which process is holding the camera.

Usage
-----
    from camera_lock import acquire_camera_lock, release_camera_lock, who_holds_lock

    if not acquire_camera_lock(serial, owner="preview.py:cam0"):
        print(f"Camera in use by {who_holds_lock(serial)}")
        return

    ... open and use camera ...

    release_camera_lock(serial)
"""

import os
import json
from datetime import datetime

LOCKDIR = os.path.join(
    os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp",
    "camera_acquisition_locks"
)


def _lock_path(serial: str) -> str:
    return os.path.join(LOCKDIR, f"{serial}.lock")


def _pid_is_alive(pid: int) -> bool:
    """Cross-platform check for whether a PID still corresponds to a running process."""
    try:
        if os.name == "nt":
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def acquire_camera_lock(serial: str, owner: str) -> bool:
    """
    Try to claim exclusive use of a camera serial for this process.

    Returns True if claimed (safe to call cam.Init() now).
    Returns False if another live process already holds it.

    Stale locks (the owning PID is no longer running, e.g. the other
    script crashed without cleanup) are detected and overwritten
    automatically — they do not require manual deletion.
    """
    os.makedirs(LOCKDIR, exist_ok=True)
    path = _lock_path(serial)

    if os.path.exists(path):
        try:
            with open(path) as f:
                info = json.load(f)
            if _pid_is_alive(info.get("pid", -1)):
                return False   # genuinely held by a live process
        except Exception:
            pass   # corrupt/unreadable lock file — treat as stale, overwrite

    with open(path, "w") as f:
        json.dump({
            "pid":     os.getpid(),
            "owner":   owner,
            "started": datetime.now().isoformat(),
        }, f)
    return True


def release_camera_lock(serial: str):
    """Release a lock this process holds. No-op if not held by us."""
    path = _lock_path(serial)
    try:
        with open(path) as f:
            info = json.load(f)
        if info.get("pid") == os.getpid():
            os.remove(path)
    except Exception:
        pass


def who_holds_lock(serial: str) -> str:
    """Human-readable description of who currently holds a camera lock."""
    path = _lock_path(serial)
    try:
        with open(path) as f:
            info = json.load(f)
        return f"{info.get('owner', 'unknown')} (PID {info.get('pid', '?')})"
    except Exception:
        return "unknown process"
