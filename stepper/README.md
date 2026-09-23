# Hacker Fab - Stepper V2

This repository contains the source code for The Hacker Fab's open source stepper software. It contains a user interface for pattern selection, stage alignment, timed ultraviolet exposure, and optionally a live camera preview.

![stepper-gui](https://github.com/user-attachments/assets/3687a777-6f2b-4d9b-b7dc-8fc08cd7d4bf) ![stepper-assembly](https://github.com/user-attachments/assets/6211e7e7-3368-4a26-bbe2-425e88622b5c)

For more information about software setup, hardware assembly, or the Hacker Fab in general, please visit our [Gitbook](https://hacker-fab.gitbook.io/hacker-fab-space/fab-toolkit/patterning/lithography-stepper-v2-build-work-in-progress) or our [website](https://hackerfab.ece.cmu.edu/).

---

## Quick Start

### Requirements

- **Python 3.10–3.13** (Python 3.13 recommended). Install it from
  [python.org](https://www.python.org/downloads/). Python 3.14 is not yet
  supported because its Pillow wheel is unavailable.
- **Linux / macOS:** Bash (pre-installed on most systems)
- **Windows:** PowerShell 5.1+ (built in to Windows 10/11)
- Internet connection for the first run

The launchers create a versioned project-local `.venv-*` environment and install dependencies with
standard `pip`; no separate package manager is needed.

### Windows

Double-click **`run.bat`**. It bypasses the per-user PowerShell execution policy for this launch only; you do not need to change that policy globally.

To validate installation without opening the GUI, open PowerShell in this directory and run:

```powershell
.\run.ps1 -SetupOnly
```

### Linux / macOS

```bash
./run.sh
```

If needed, make the launcher executable first:

```bash
chmod +x run.sh
./run.sh
```

---

# GRBL Setup

This GUI is designed to be used with an Arduino running [GRBL](https://github.com/gnea/grbl) to move the stage,
possibly coupled with homing sensors for absolute positioning.

## Axes

The X/Y/Z axes of the stage are labeled according to the **point of view of the projector/camera**.
If any of the axis directions below are incorrect, swap and/or reverse the appropriate stepper connections.

Movement on the X and Y axes should pan the camera's view,
while movement on the Z axis should adjust focus.

The polarity of the X/Y axes is not currently used by the GUI; any direction is acceptable.

Movement in +Z should bring the camera and the chip's surface closer together.
The Z axis should be used for focus adjustment (i.e. in/out).

## Build Configuration (Sensors Only)

If proximity sensors or limit switches are installed on each axis,
some GRBL settings must be changed before flashing to the Arduino.
Open your GRBL folder's `config.h` and make the following changes.

Remove any existing defines for `HOMING_CYCLE_0` and `HOMING_CYCLE_1` and replace them
with the defines listed below. This enables homing.

```c
#define HOMING_CYCLE_0 ((1 << X_AXIS)|(1<<Y_AXIS))
#define HOMING_CYCLE_1 (1 << Z_AXIS)
```

Comment the `VARIABLE_SPINDLE` define.
This allows the Z axis limit switch to be used.

```c
//#define VARIABLE_SPINDLE
```

## Runtime Configuration

Once GRBL is flashed to an Arduino, you can use the serial monitor in the Arduino IDE
to adjust its configuration.
More detail on these settings is available
[on GRBL's wiki](https://github.com/gnea/grbl/wiki/Grbl-v1.1-Configuration).

Enable homing (if you have sensors).

```
$22=1
```

Adjust the homing direction invert mask (if you have sensors).
Bits 0, 1, and 2 in this value correspond to the X, Y, and Z axes.
For each axis, check if the limit switch is reached by moving in the positive direction or the negative direction.
If the axis requires negative movement, set the corresponding bit.
On CMU's setup, the limit switches are all reached by traveling in the negative direction,
so we use a value of 7 (invert all axes).

```
$23=7
```

Adjust the homing feed and homing seek (if you have sensors).

```
$24=10
$25=50
```

Adjust the homing pull-off (may need to be increased depending on your sensors' hysteresis).

```
$27=0.5
```

Adjust the steps per mm for the X, Y, and Z axes.
For CMU's setup, there is 8x microstepping, 200 steps per revolution, and 0.5mm per revolution,
so this value is 3200.

```
$100=3200
$101=3200
$102=3200
```

Adjust the max rate in mm per minute.
These numbers are empirical; you may need lower (or may be able to use higher) ones on your stage.

```
$110=120.000
$111=120.000
$112=120.000
```

Adjust the maximum acceleration in mm per sec^2.
Same caveats apply as for the max rate.

```
$120=5.000
$121=5.000
$122=5.000
```

Adjust the maximum travel of each axis in mm.
You should measure the travel and then back it off by 0.1mm,
or you could just approximate it as 15mm and hope for the best.

```
$130=15.000
$131=15.000
$132=15.000
```

Once these steps are complete, you should be able to home your stage using the `$H` command (if you have sensors)
and you are ready to use the GUI.

**Set `homing=true` under `[stage]` in your config file to enable the use of sensors.**

# Software Setup

## Python Setup with venv

The launchers perform these steps automatically. To run them yourself, use a
[virtual environment](https://docs.python.org/3/library/venv.html) with Python
3.10–3.13:

```bash
python -m venv venv       # name may change depending on system python
source venv/bin/activate  # depending on your shell, see venv docs
python --version          # ensure it is Python 3.10–3.13
pip install -r requirements.txt
```

### Using a Basler (Pylon) camera

To use a Basler camera with the GUI, you will need to install `pylon` from
[Basler's website](https://www.baslerweb.com/en-us/downloads/software/).

Select the "Software Suite" download under Pylon, as it contains useful tools for debugging the camera view.

After a few steps, the installer will prompt you to **restart the computer**.
Do not delay this restart as the Pylon installer has several more steps that are run only after restarting.

### Using a FLIR camera

If you are using the FLIR camera with your stage,
ask in the Hacker Fab Discord for an invitation to the FLIR repository.

Then, update the git submodules to add support for the FLIR:

```bash
git submodule init
git submodule update
```

If the submodule update doesn't work, clone the `flir-private` repo as follows:

```bash
cd src/camera
git clone git@github.com:hacker-fab/flir-private.git flir
```

In order to use the FLIR camera, you will need to use a version of Python **at or before 3.10.**
Specify a version when creating your venv to make this work:

```bash
python3.10 -m venv venv   # name may change depending on system python
```

This restriction does not apply to other camera brands.

> [!NOTE]
> This software is in active development, and features are subject to change. Though each change to the main branch has been tested, there remains a chance that some bugs are undetected. To report a bug or to suggest additional features, please create an issue on this repository.

## Configuration

The GUI uses a `config.toml` file (in the [TOML](https://toml.io/en/) format) for configuration.
A sample configuration file with an explanation of the settings is shown below.

```toml
# This section configures the camera used for the GUI's preview.
[camera]
# Available options are:
# "webcam" (for a generic USB camera),
# "basler" (for a Basler/Pylon camera)
# "flir" (for a FLIR camera)
# "none" (to disable camera)
type = "webcam"
# The index field is optional and is only used to select which of multiple webcams or basler/pylon cameras should be used,
# e.g. on a laptop where there may be a builtin webcam in addition to an external USB camera.
index = 1
# The output from the camera is typically too large to show at full resolution.
# This parameter adjusts the size of the camera feed before it is displayed in the GUI.
gui-scale = 0.25
# The following two values adjust the *camera* exposure in microseconds when viewing red or UV light.
# Note that using values that are not a multiple of 4167 can lead to flickering.
red-exposure = 4167.0
uv-exposure = 25000.0

# This section configures the motion stage
[stage]
# Set enabled to false to disable all motion.
enabled = true
# Set homing to false if your stage does not have limit sensors
homing = true
# Select the correct serial port for the device running GRBL.
# The correct serial port can be checked with Device Manager on Windows.
port = "COM6"
# GRBL's baud rate is almost always 115200, do not change this value
baud-rate = 115200

# This section configures alignment marker detection
[alignment]
# Enable or disable real-time detection of alignment markers
enabled = false
# Path to the YOLO model weights file
model_path = "best.pt"
# Alignment marker reference coordinates (in pixels)
right_marker_x = 1634.0  # x-coordinate for markers on the right side
top_marker_y = 117.5     # y-coordinate for markers on the top
bottom_marker_y = 1001.5 # y-coordinate for markers on the bottom
left_marker_x = 0.0      # x-coordinate for markers on the left side
# Scaling factors for converting normalized differences to stage movements (in µm)
x_scale_factor = -1040   # Scaling factor for x-axis movements
y_scale_factor = -580    # Scaling factor for y-axis movements
```

## Camera and device settings

The sidebar separates **Operate**, **Alignment**, **Wafer & tiling**, and
**Settings**. The workspaces scroll on smaller screens; camera previews fit their
panel while snapshots and focus measurements retain the original image.

On a fresh launch the camera uses `device = "auto"` and `mode = "auto"`. On Linux,
Stepper enumerates capture nodes, uses stable `/dev/v4l/by-id` paths where available,
and reads advertised resolution / format / frame-rate combinations with
`v4l2-ctl` (optional, provided by `v4l-utils`). It prefers MJPEG at moderate
resolution, tests multiple decoded frames, and rejects likely solid-green
corruption. Metadata nodes are excluded; monochrome auxiliary streams require
explicit selection in Auto device mode. Windows and macOS expose index candidates
0–7, validated when opened, and use platform capture backends. Existing numeric
`index` configurations still work.

In **Settings**:

1. Refresh devices and select the intended camera, or enter a device path/index.
2. Leave capture configuration on **auto**, or read the advertised modes and
   select one. Manual mode checks the driver's returned resolution, format and
   frame rate rather than silently claiming the requested settings worked.
3. **Connect camera** changes the active camera. The status shows its
   actual mode, errors, and measured capture rate. Reconnection clears stale
   images and is blocked during exposure or autofocus.
4. **Save settings** persists selections. Starting from `default.toml` saves to
   project-local `config.toml`, which the launcher dialog selects next time.
   A custom configuration is saved back to its selected path. Existing files get
   timestamped backups; unrelated configuration sections are preserved, although
   TOML formatting/comments are rewritten. Stage port/enabled changes take effect
   after saving and restarting. Appearance can be applied immediately.

Capture runs in an isolated process with a watchdog so a blocked driver cannot
freeze the UI or hang shutdown. A disconnected or stalled feed is cleared;
reconnect from Settings after fixing the connection. Basler and FLIR retain their
vendor backends; their SDKs must be installed separately. USB mode negotiation
and green-frame screening apply to the generic webcam backend. Webcam exposure
is left under driver control: the red/UV exposure values in microseconds are
vendor-camera settings, not portable UVC exposure values.

### Green or flickering video

A green preview can indicate a capture-format/decoder problem or an unstable USB
stream; the image alone cannot identify the cause. Close OBS and other camera
clients before connecting in Stepper. Try Auto, or an advertised MJPEG mode at
1280×720 / 30 fps or a lower supported resolution/frame rate. USB speed shown beside
the device is the negotiated physical link speed, not a software setting. A bad
cable, insufficient power, a hub, or a hardware/driver fault can still require a
physical fix. Software cannot guarantee a perfect stream for those conditions.
Disable green-frame rejection only when the actual specimen fills the view with
green. Review pixel-based alignment calibration whenever capture resolution changes.

Mode discovery follows the [V4L2 format enumeration API](https://docs.kernel.org/userspace-api/media/v4l/vidioc-enum-fmt.html).
Requested properties may differ from driver results, as documented by
[OpenCV's capture property API](https://docs.opencv.org/4.x/d4/d15/group__videoio__flags__base.html).

Run the camera/configuration regression checks with:

```bash
PYTHONPATH=src .venv-3.12/bin/python -m unittest discover -s tests -v
```

Use the Python executable from your launcher-created virtual environment if its
version differs.

### Readable text on Linux

The default dark theme uses a system sans-serif font, larger controls, and padded
camera dropdowns. **Settings → Appearance** offers Light / Dark and 100%, 125%,
or 150% text size. Changing appearance does not reconnect devices. Motion uses
explicit direction buttons with a single distance-per-click field.

Some standalone Python distributions ship Tk without Xft font rendering. On
those runtimes every requested font can collapse to the same tiny bitmap font;
changing the application's font size alone cannot fix it. This checkout has a
project-local Tcl/Tk 9.0.4 build with Xft under `.runtime/tk-9.0.4`. The application
loads it automatically for a compatible Linux Tcl 9 Python, without modifying
system Python, desktop settings, or other applications.

To reproduce that optional build on another Linux machine:

```bash
./tools/build_tk.sh
```

It requires a C compiler, make, curl, pkg-config, and the X11/Xft/fontconfig
development libraries. The script downloads checksum-pinned official Tcl/Tk
sources and keeps all build/install files under `.runtime/` (git-ignored).
Removing `.runtime/tk-9.0.4` restores the Python runtime's original Tk. Windows,
macOS, and Python builds using Tcl 8 continue to use their bundled/system Tk.

### Z-axis direction

This setup defaults to `invert-z = true` under `[stage]`. The app reverses Z
in both absolute and relative movement commands, and reverses Z position
readback to keep displayed coordinates consistent. X and Y are unchanged.
Use **Settings → Stage → Reverse Z direction**, then save and restart, to change
this mapping. Autofocus and other app-driven Z moves use the same mapping.
This is an application coordinate reversal; it does not change the controller's
stored direction-polarity or homing settings. Verify direction on the stepper
computer with a small jog before using saved absolute positions.

If connection fails, **Settings → Camera → Copy camera diagnostics** includes
the selected device, discovered cameras, and each failed capture attempt. The
preview clears its connecting state on failure and disables snapshots until
a live frame is available.

### Fullscreen previews

The app starts with one window. Click either the camera preview or projector
preview to view it fullscreen in that same window. **Esc** or the **×** button
returns to the workspace. Camera fullscreen remains live and preserves aspect
ratio. Projector output uses the same view during an exposure; exiting that
view stops the exposure and clears the pattern. To use the DLP, put the app on
the DLP display before opening projector fullscreen. The projector's pattern
canvas stays 1280×720 regardless of preview size.
