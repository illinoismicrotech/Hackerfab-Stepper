"""Camera discovery and advertised capture modes; no settings are changed here."""
from dataclasses import dataclass
from pathlib import Path
import platform
import re
import subprocess


@dataclass(frozen=True)
class CaptureMode:
    fourcc: str
    width: int
    height: int
    fps: float

    def label(self):
        return f"{self.width} × {self.height} · {self.fps:g} fps · {self.fourcc}"


@dataclass(frozen=True)
class CameraDevice:
    device: str
    name: str
    usb_speed: str = ""

    def connection_hint(self):
        if 'B0477' in self.name and self.usb_speed:
            try:
                if float(self.usb_speed) <= 480:
                    return ('Arducam B0477 is connected at USB 2 speed. Its supported fallback is '
                            '1280 × 720, YUYV, 10 fps. For full modes, reconnect directly with a USB 3 '
                            'data cable and USB 3 port, then refresh devices.')
            except ValueError:
                pass
        return ''

    def label(self):
        speed = f" · USB {self.usb_speed} Mb/s" if self.usb_speed else ""
        return f"{self.name} ({self.device}){speed}"


def v4l_info(device, option):
    try:
        result = subprocess.run(["v4l2-ctl", "--device", str(device), option],
                                capture_output=True, text=True, timeout=3)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def parse_modes(output):
    modes = []
    fourcc = None
    size = None
    for line in output.splitlines():
        fmt = re.search(r"\[\d+\]: '(.{4})'", line)
        resolution = re.search(r"Size: Discrete (\d+)x(\d+)", line)
        fps = re.search(r"\(([\d.]+) fps\)", line)
        if fmt:
            fourcc, size = fmt[1], None
        elif resolution:
            size = (int(resolution[1]), int(resolution[2]))
        elif fps and fourcc and size:
            modes.append(CaptureMode(fourcc, *size, float(fps[1])))
    return list(dict.fromkeys(modes))


def discover_devices():
    if platform.system() != "Linux":
        # OpenCV has no portable named-device enumeration. These are candidates,
        # validated in an isolated capture process when selected.
        return [CameraDevice(str(i), f"Camera index {i} (test on connect)") for i in range(8)]
    aliases = {}
    for path in sorted(Path("/dev/v4l/by-id").glob("*")):
        aliases.setdefault(str(path.resolve()), str(path))
    devices = []
    for node in sorted(Path("/sys/class/video4linux").glob("video*")):
        device = f"/dev/{node.name}"
        info = v4l_info(device, "--all")
        # Metadata-only nodes cannot deliver images. If v4l2-ctl is unavailable,
        # retain the candidate and let capture validate it.
        caps = info.split("Device Caps", 1)[-1]
        if info and "Video Capture" not in caps:
            continue
        try:
            name = (node / "name").read_text().strip()
        except OSError:
            name = node.name
        speed = ""
        for parent in (node / "device").resolve().parents:
            try:
                speed = (parent / "speed").read_text().strip()
                break
            except OSError:
                pass
        devices.append(CameraDevice(aliases.get(device, device), name, speed))
    return sorted(devices, key=lambda d: ("arducam" not in d.name.lower(), d.device))


def candidate_modes(device, settings):
    if settings.get("mode", "auto") == "manual":
        return [CaptureMode(settings.get("fourcc", "MJPG"), int(settings.get("width", 1280)),
                            int(settings.get("height", 720)), float(settings.get("fps", 30)))]
    modes = parse_modes(v4l_info(device, "--list-formats-ext")) if platform.system() == "Linux" else []
    # Prefer compressed USB transport, moderate image size and <=30 fps. Higher
    # resolutions remain available explicitly for calibrated imaging workflows.
    if modes:
        return sorted(modes, key=lambda m: (m.fourcc not in ("MJPG", "JPEG"),
                      m.width * m.height > 1280 * 960, m.fps > 30,
                      abs(m.width * m.height - 1280 * 720), abs(m.fps - 30)))[:16]
    return [CaptureMode(fmt, w, h, fps) for fmt in ("MJPG", "YUYV")
            for w, h, fps in ((1280, 720, 30), (640, 480, 30), (640, 480, 15))] + [None]
