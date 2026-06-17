# Video Acquisition with Multiple Chambers

## Introduction

This application uses the Spinnaker PySpin to continuously acquire videos of multiple mouse behavior chambers simultaneously. Many applications and scripts exist for single camera acquisition, but this program addresses the lack of applications allowing flexibility in video acquisition for multi-chamber setups. 

Some feature highlights are listed below:

- Select to record certain but not all chambers
- Preview all chambers without recording
- Recording video stats(FPS, buffer, duration left for timed recordings)
- Acquisition configuration GUI
- Logging frame, computer time, TTL event for each chamber
- TTL trigger to start and stop recording(using Arduino)

## Installation

Refer to the following for installing the spinnaker pyspin wheel on your computer: [Teledyne Guide](https://www.teledynevisionsolutions.com/support/support-center/technical-guidance/iis/installing-pyspin-for-the-spinnaker-sdk/)

This application uses <b>Python 3.10</b>, therefore the PySpin installed from Teledyne must be of the same version for compatibility. Make sure your installed Python is also 3.10. 

Download this repo by clicking the green code button and clicking 'Download ZIP.'

Unzip the folder and open up a terminal. Change directories to where the folder is located.

Ex. Folder located in `Downloads/code/multiAcq`

```bash
cd Downloads/code/multiAcq
```

Install other required pip installs via the following command:

```bash
pip install -r requirements.txt
```

---

## Using the program


### Configuring cameras and chamber mapping

Make sure your cameras and Arduino are plugged in properly to the computer running the script prior to the following steps. 

First run the `config.py` file to configure your cameras and map them to specific chambers. The GUI should automatically show cameras found plugged into your computer. 

Fill out the experiment info and confirm the settings/serial number for each camera in the Cameras tab. 

To make a new chamber, go to the Chamber_TTL tab and name the chamber. Click "+ Add Chamber" and make sure to map the correct camera and Arduino connection port to the chamber. 

This application allows for recording individual chambers. To record a specific chamber, turn the "Record this chamber" toggle on. There is also an option to stop recording after a certain duration. If you want the recording to be a set duration and automatically stop at that point, turn the "Stop recording after" toggle on and set the duration(in seconds) to the desired length. By default the toggle is off. Check the baud rate for the Arduino and select the corresponding rate in the dropdown selection. 

For automatically starting recording once the acqusition script is run, turn on the "Auto-start recording when script 
launches" toggle in the Recordings/ROI tab.


Once finished with the configuration, make sure to save the configuration file. We recommend you first to save a general configuration file that maps the cameras to chambers, then using the browse feature to amend components specific to your experiment and save a new config file. Make sure to save the config file in the same folder as the scripts.


### Chamber live feed

To have a preview window showing a live feed of all chambers, run the `multiAcquisition.py` script using the following command:

```
python multiAcquisition.py -c <config file name>.yaml
```

To downsample the preview feed quality, use the following command:

```

```

This preview window is independent of recording. This preview can be manually terminated by pressing ESC or Q when on the preview window(make sure screen is not on the recording stats window).


### Recording chambers

Recordings will be acquired in mjpg format and converted to an avi file at the end. 

The timestamp CSV will include the following details:
- Frame count: which corresponding frame in the recording
- Computer/CPU time: timestamp of the computer running the script
- Camera time: timestamp from the Blackfly camera
- TTL: any TTL input detected at that frame
- EDIT FOR MORE HEREEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE
They are all indexed by the frame count.

The metadata CSV will include the experiment information from the inputs provided in the configuration file(experiment tab of GUI). Additionally, it will give the start and end time of the experiment(from the computer timestamp), total TTL pulses received, the path of the video recording, path of the timestamp CSV, number of frames dropped. 

All files will be in its own experiment folder named by the datetime_computerName_chamber#

### Manual start/stop

Along with TTL triggered start/stopping of recordings and duration stopping, you may start/stop recordings manually. Starting a recording manually assumes recordings have not been set to automatically start when the script is run, and that no TTL start trigger has been sent yet from the specific chamber.

Recordings for all chambers selected in the configuration can be started by pressing the S key. The individual buttons in the stats popup window can be used for manually starting recording for specific chambers.

You may end the recording at any time manually by pressing the X key if stopping all recordings or individual buttons in the stats popup window for individual chambers.

---

## Notes

This application was used for the BFS-U3-16S2M-CS USB 3.1 Blackfly® S, Monochrome Camera by Teledyne FLIR and was also tested with the ______ camera as well. <b>Spinnaker 3.2.0.62 (64bit)</b> was installed. The [DB15 Female 15-Pin to Screw Terminal Breakout Board Adapter](https://www.amazon.com/Oiyagai-Terminal-Connector-Signal-Module/dp/B07DCM5FDC?th=1) as the breakout board for the TTLs along with the [Arduino Mega 2560 Rev3](https://store-usa.arduino.cc/products/arduino-mega-2560-rev3?utm_source=google&utm_medium=cpc&utm_campaign=US-Pmax&gad_source=1&gad_campaignid=21317508903&gbraid=0AAAAACbEa86z88u0HLKYAbVZZ1xwL9vR2&gclid=Cj0KCQjwlqTRBhCBARIsANrkrxgfqTqLVhNS9Avx6jZU1In4YGmlsE1hOzoSp5ggkfSmArEuNoXtYBcaAqA8EALw_wcB) for multiple chambers and the [Arduino Uno Rev3 SMD](https://store-usa.arduino.cc/collections/uno/products/arduino-uno-rev3-smd) for one chamber.

We recommend ___ GB of RAM per camera connected to the computer. We found around 2GB of CPU, ____ RAM, and 1.2GB of GPU used when recording using two cameras through a ______ chip computer.


Last edited June 2026. 