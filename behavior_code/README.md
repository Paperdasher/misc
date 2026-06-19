# Video Acquisition with Multiple Chambers

## Introduction

This application uses Spinnaker PySpin to continuously acquire video from multiple mouse behavior chambers simultaneously. Many applications and scripts exist for single-camera acquisition, but this program addresses the lack of tools allowing flexibility in video acquisition for multi-chamber setups.

Feature highlights:

- Select to record certain but not all chambers
- Live preview of all chambers, tiled into a single resizable window, shown both before and during recording
- Recording stats popup (FPS, buffered frames, elapsed time, time remaining for timed recordings) with per-chamber manual start/stop buttons
- Acquisition configuration GUI
- Per-frame logging of frame count, camera timestamp, computer timestamp, and any TTL event received
- TTL-triggered start/stop of recording via Arduino (analog voltage threshold detection)
- Automatic protection against two scripts trying to open the same camera at once

## Installation

Refer to the following guide for installing the Spinnaker PySpin wheel on your computer: [Teledyne Guide](https://www.teledynevisionsolutions.com/support/support-center/technical-guidance/iis/installing-pyspin-for-the-spinnaker-sdk/)

This application uses **Python 3.10**. The PySpin wheel installed from Teledyne must match your installed Python version exactly, or it will fail to import. We tested against **Spinnaker 3.2.0.62 (64-bit)**.

Download this repo by clicking the green Code button and selecting "Download ZIP."

Unzip the folder and open a terminal. Change directories to where the folder is located.

```bash
cd Downloads/code/multiAcq
```

Install the remaining dependencies via:

```bash
pip install -r requirements.txt
```

`requirements.txt` installs numpy, PyYAML, opencv-python, pyserial, and PyQt5. PySpin is **not** in this file — it must be installed separately following the Teledyne guide above, matching your Python version.

You will also need the Arduino IDE installed to flash `chamber_ttl_analog.ino` onto your Arduino. Get it from [arduino.cc](https://www.arduino.cc/en/software).

---

## Files in this repo

| File | Purpose |
|---|---|
| `config.py` | GUI for creating and editing `.yaml` configuration files |
| `multiAcquisition.py` | Main acquisition script — preview, recording, TTL handling, stats popup |
| `chamber_ttl_analog.ino` | Arduino sketch — reads analog TTL voltages and reports them over serial |
| `requirements.txt` | Python package dependencies (excludes PySpin — see Installation) |

---

## Using the program

### Configuring cameras and chamber mapping

Make sure your cameras and Arduino are plugged into the computer running the script before proceeding.

Run `config.py` to configure your cameras and map them to chambers:

```bash
python config.py
```

The GUI scans for connected cameras automatically. In the **Experiment** tab, fill out experimenter, animal, and session info — this is optional but recommended, since it's written into every session's metadata CSV. The **PC / Station Name** field can be set manually if you run this script on multiple machines and want a label other than the auto-detected hostname.

In the **Cameras** tab, confirm the serial number, exposure, gain, and other settings detected for each camera.

In the **Chambers & TTL** tab, click "+ Add Chamber" and give it a name (e.g. `chamber_A`). For each chamber:
- **Mapped camera** — select which camera this chamber's TTL controls
- **Record this chamber** — toggle on for chambers you want to record; chambers left off are skipped even if a TTL pulse arrives or auto-start is enabled
- **Stop recording after** — optional. When enabled, set the duration in seconds; recording for that chamber stops automatically once reached. Off by default, meaning recording continues until a TTL stop pulse or manual stop
- **Arduino connection** — set the serial port (use the Scan button to list available ports), baud rate (must match the value set in the Arduino sketch — default 115200), board voltage (5V for Uno/Mega/Nano, 3.3V for Due/Zero/MKR — this is informational only; the actual threshold is set inside the `.ino` sketch and must be changed there to match), and the **Start TTL #** / **Stop TTL #** channel numbers (see the TTL section below for what these mean)

In the **Recording / ROI** tab:
- **Auto-start recording when script launches** — if on, recording begins immediately for every chamber with "Record this chamber" enabled, the moment `multiAcquisition.py` starts. If off, recording for each chamber only starts via its TTL start pulse or a manual start (S key / stats popup button)
- **Show live preview window during acquisition** — toggle the tiled camera window on/off during recording; the stats popup always shows regardless of this setting
- FPS, JPEG quality, and ROI (width/height/offset) settings apply to all enabled cameras

Once finished, save the configuration file (Save or Save As, top right). We recommend saving a general configuration file first that maps cameras to chambers and Arduino connections — this part rarely changes — then using Save As to create experiment-specific config files that vary the experiment metadata, recording duration, or which chambers are active. **Save the config file in the same folder as the scripts.**

---

### Running acquisition

```bash
python multiAcquisition.py -c <config file name>.yaml
```

This does the following:
- Opens all enabled cameras and begins streaming immediately (continuous mode, no camera-side hardware trigger — TTL is read by this script over serial, not by the camera's GPIO)
- Opens the **Acquisition Stats** popup — one column per chamber showing recording status, FPS, buffered frame count, elapsed time, and time remaining (if a duration timer is set). Each column has clickable START and STOP buttons for that chamber alone
- If "Show live preview window" is enabled in the config, also opens a tiled camera window identical in layout to `preview.py`, except each tile shows `REC` once that chamber starts recording
- Starts an `AnalogTTLListener` thread per unique Arduino serial port, listening for confirmed TTL channel activity
- If auto-start is enabled, immediately begins recording for every chamber with "Record this chamber" on

To stop the script entirely, press **ESC** while either window is focused, or Ctrl+C in the terminal. This stops all active recordings cleanly (closing video files and writing final CSVs) before exiting.

---

### TTL triggering (Arduino, analog)

TTL detection uses the Arduino sketch `chamber_ttl_analog.ino`, which reads several analog input pins and reports their state over serial — it does **not** use digital pins or edge interrupts.

**Channel numbering.** UPDATE LATER

**Threshold.** The sketch compares each pin's analog reading against a threshold derived from `BOARD_VREF` (the board's logic voltage — 5.0 for Uno/Mega/Nano, 3.3 for Due/Zero/MKR) and `THRESHOLD_FRACTION` (default 0.5, i.e. half of VREF). Both constants are set directly in `chamber_ttl_analog.ino` — open the file in the Arduino IDE, edit `BOARD_VREF` to match your board, and re-upload. The Board Voltage dropdown in `config.py` is informational only and does not change the sketch.

**Confirmation.** A single high reading is not trusted, since analog signals can have noise near the threshold. `multiAcquisition.py` requires **3 consecutive** reads reporting a channel as active before it acts — this confirmation count is fixed in the script and is not configurable through the GUI. Once 3 consecutive start reads are confirmed for a chamber's Start TTL channel, recording begins. The same applies to the Stop TTL channel ending it. A channel must drop back to inactive before it can fire again, so a sustained high voltage triggers only once.

**Wiring.** Each chamber's TTL source connects to its assigned analog pins on the Arduino, with grounds tied together. If your TTL source outputs 5V into a 3.3V board's analog pin, use a voltage divider — 3.3V analog pins can be damaged by overvoltage.

---

### Recording output

Recordings are captured as MJPEG and written into an `.avi` container as they're acquired (not converted after the fact).

Each acquisition session creates a folder named by the **session start timestamp** (`YYYYMMDD_HHMMSS`) inside the configured save directory. Inside that, every chamber gets its own subfolder, and every recording within that chamber is named with the chamber, camera, and recording start time:

```
<save_dir>/
  20250610_143000/                              <- session folder (script start time)
    config.yaml                                  <- copy of the config used for this session
    chamber_A/
      chamber_A_BoxA_20250610_143022.avi
      chamber_A_BoxA_20250610_143022_timestamps.csv
      chamber_A_BoxA_20250610_143022_session.csv
    chamber_B/
      chamber_B_BoxB_20250610_143105.avi
      chamber_B_BoxB_20250610_143105_timestamps.csv
      chamber_B_BoxB_20250610_143105_session.csv
```

Note that each chamber's recording start time can differ from the others (and from the session folder's timestamp) if chambers are started at different times via separate TTL pulses or manual buttons.

**Timestamps CSV** (`..._timestamps.csv`) is indexed by row, with one row per captured frame plus interleaved rows for any TTL event received during that recording. Columns:
- `row_type` — `frame` for a captured video frame, `ttl_event` for a TTL pulse
- `framecount` — the camera's internal frame ID (frame rows only)
- `camera_hw_ts_s` — hardware timestamp reported by the camera itself, in seconds
- `sestime_s` — seconds elapsed since the acquisition script started (`time.perf_counter()`-based, monotonic)
- `cpu_wall_s` — computer wall-clock time (`time.time()`, Unix epoch seconds)
- `ttl_chamber`, `ttl_kind`, `ttl_label`, `ttl_number` — populated only on `ttl_event` rows: which chamber the pulse belonged to, whether it was a `start` or `stop` event, a human-readable label, and the raw TTL channel number that fired

**Session CSV** (`..._session.csv`) is a single-row summary written once a chamber's recording stops, containing:
- Experimenter name, experiment name, PC/station name, camera name, chamber name and chamber number
- Animal ID, genotype, group, schedule name (if filled in during config)
- Date, acquisition start and end time, total duration
- Total frames recorded, estimated frames dropped (based on configured FPS x duration vs. actual frame count)
- Configured FPS (what was set in `config.py`) and average actual FPS achieved
- Full paths to the video file and the timestamps CSV
- EEG/fiber photometry co-recording path, and any notes (if filled in during config)

---

### Manual start/stop

Alongside TTL-triggered and timer-based stopping, you can start or stop recordings manually at any time:

- **S** — start recording for every chamber that has "Record this chamber" enabled (only while the stats popup or preview window is focused)
- **X** — stop recording for every currently recording chamber
- Per-chamber **START** / **STOP** buttons in the stats popup — click directly on a chamber's column to start or stop just that one chamber, independent of the others

Starting manually works regardless of whether auto-start or TTL triggering is configured — if a chamber is already recording (started by auto-start, a TTL pulse, or another manual start), pressing its START button again has no effect, and an incoming TTL start pulse for that chamber will be logged to its CSV but will not open a second recording.

---

## Notes

This application was developed and tested using the BFS-U3-16S2M-CS USB 3.1 Blackfly(R) S Monochrome Camera by Teledyne FLIR. Spinnaker 3.2.0.62 (64-bit) was used throughout development.

For TTL breakout, we used a [DB15 Female 15-Pin to Screw Terminal Breakout Board Adapter](https://www.amazon.com/Oiyagai-Terminal-Connector-Signal-Module/dp/B07DCM5FDC?th=1), paired with an [Arduino Mega 2560 Rev3](https://store-usa.arduino.cc/products/arduino-mega-2560-rev3) for setups with multiple chambers (more analog pins available), or an [Arduino Uno Rev3 SMD](https://store-usa.arduino.cc/collections/uno/products/arduino-uno-rev3-smd) for single-chamber setups.

**ffmpeg required.** Video writing pipes raw frames into `ffmpeg` as a subprocess. Make sure `ffmpeg` is installed and available on your system PATH, or recording will fail with an error noting it could not be found.

**Hardware camera trigger left off intentionally.** The "Hardware TTL Trigger (GPIO)" section in the config GUI's Trigger/CSV tab controls the camera's own Spinnaker `TriggerMode`, which is unrelated to the Arduino TTL system used for starting/stopping recording. This should be left disabled for the standard setup described in this README, since TTL is read by the script over serial rather than wired into the camera itself. It's only relevant if you're separately gating individual frame capture via a physical signal wired directly into the camera's GPIO port — leaving it enabled without that wiring will cause a Spinnaker error when the camera tries to configure its trigger source.

We have not yet benchmarked RAM, CPU, or GPU usage figures for this application across different numbers of cameras and computer specs. If you run this on your own setup, consider documenting your camera count, computer specs, and observed resource usage to expand this section.

---

*Last edited June 2026.*