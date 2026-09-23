"""Isolated USB capture: a blocked driver never blocks Tk or application exit."""
from collections import deque
import multiprocessing as mp
import platform
import queue
import threading
import time

import cv2
import numpy as np

from camera.camera_module import CameraModule
from camera.discovery import candidate_modes, discover_devices


def frame_problem(frame, reject_green=True):
    if frame is None or frame.ndim != 3 or frame.shape[2] != 3 or not frame.size:
        return "No decoded color frame"
    # Conservative heuristic for the solid green corruption seen on USB streams.
    # Users can disable this for specimens that really fill the view with green.
    sample = frame[::8, ::8].astype(np.int16)
    green = (sample[:, :, 1] > 70) & (sample[:, :, 0] < 25) & (sample[:, :, 2] < 25)
    if reject_green and green.mean() > .65:
        return "Suspected green-frame corruption"
    return ""


def _send(channel, kind, value):
    try:
        channel.put((kind, value), timeout=0 if kind == "frame" else 1)
    except queue.Full:
        pass


def _capture(settings, channel, stop):
    backend = {"Linux": cv2.CAP_V4L2, "Windows": cv2.CAP_DSHOW,
               "Darwin": cv2.CAP_AVFOUNDATION}.get(platform.system(), cv2.CAP_ANY)
    requested = str(settings.get("device", settings.get("index", "auto")))
    devices = [d.device for d in discover_devices()] if requested == "auto" else [requested]
    if not devices:
        _send(channel, "error", "No camera found. Connect a camera, then Refresh devices and Reconnect.")
        return
    failures_by_mode = []
    for device in devices:
        modes = candidate_modes(device, settings)
        # Do not silently select an IR auxiliary stream when a color camera is busy.
        if requested == "auto" and modes and all(m and m.fourcc in ("GREY", "Y16 ", "Y10 ") for m in modes):
            _send(channel, "status", f"Skipping monochrome auxiliary stream {device}; select it explicitly if needed.")
            continue
        for mode in modes:
            if stop.is_set():
                return
            _send(channel, "status", f"Testing {device}: {mode.label() if mode else 'driver defaults'}")
            cap = cv2.VideoCapture(int(device) if device.isdecimal() else device, backend)
            try:
                if not cap.isOpened():
                    failures_by_mode.append(f"{device}: could not open device (possibly busy, unavailable, or inaccessible)")
                    _send(channel, "status", failures_by_mode[-1])
                    break
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                cap.set(cv2.CAP_PROP_CONVERT_RGB, 1)
                if mode:
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*mode.fourcc))
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, mode.width)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, mode.height)
                    cap.set(cv2.CAP_PROP_FPS, mode.fps)
                good = 0
                frame = None
                for _ in range(12):
                    if stop.is_set():
                        return
                    ok, frame = cap.read()
                    problem = frame_problem(frame, settings.get("reject-green", True)) if ok else "No frames received"
                    good = good + 1 if not problem else 0
                    if good >= 4:
                        break
                if good < 4:
                    reason = f"{device} · {mode.label() if mode else 'driver defaults'}: {problem}"
                    failures_by_mode.append(reason)
                    _send(channel, "status", f"Rejected {reason}")
                    continue
                fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
                actual = {"device": device, "width": frame.shape[1], "height": frame.shape[0],
                          "fps": cap.get(cv2.CAP_PROP_FPS),
                          "fourcc": ''.join(chr((fourcc >> (8*i)) & 255) for i in range(4)).strip('\x00')}
                if settings.get("mode") == "manual" and mode and (
                    actual["width"] != mode.width or actual["height"] != mode.height or
                    actual["fourcc"] != mode.fourcc or abs(actual["fps"] - mode.fps) > 1):
                    _send(channel, "error", f"Driver did not accept requested mode. Returned: {actual}")
                    return
                _send(channel, "ready", actual)
                failures = 0
                started, count = time.monotonic(), 0
                while not stop.is_set():
                    ok, frame = cap.read()
                    problem = frame_problem(frame, settings.get("reject-green", True)) if ok else "Camera disconnected or capture failed"
                    if problem:
                        failures += 1
                        if failures >= 8:
                            _send(channel, "error", problem + ". Reconnect or try a lower-bandwidth mode; check the USB cable and close OBS.")
                            return
                        continue
                    failures = 0
                    count += 1
                    _send(channel, "frame", cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    elapsed = time.monotonic() - started
                    if elapsed >= 2:
                        _send(channel, "fps", count / elapsed)
                        started, count = time.monotonic(), 0
                return
            finally:
                cap.release()
    detail = "\n".join(failures_by_mode[-3:]) or "Only auxiliary monochrome streams were found; select the intended device explicitly."
    _send(channel, "error", "No usable camera mode.\n" + detail + "\nClose OBS/other camera apps before retrying. See Settings for camera diagnostics.")


def _capture_worker(settings, channel, stop):
    try:
        _capture(settings, channel, stop)
    except Exception as exc:
        _send(channel, "error", f"Capture failed: {exc}. Check the device and reconnect in Settings.")


class Webcam(CameraModule):
    def __init__(self, index="auto", settings=None):
        self.settings = dict(settings or {})
        self.settings.setdefault("device", str(index))
        self.status = "Not connected"
        self.state = "disconnected"
        self.diagnostics = deque(maxlen=80)
        self.actual = {}
        self.measured_fps = 0.0
        self.capture_thread = None
        self.process = None
        self.should_stop = threading.Event()
        self.last_frame_at = 0.0
        self.__active__ = False

    def setExposureTime(self, value):
        # Generic UVC exposure units vary by backend; never send microseconds
        # as raw OpenCV exposure values. Vendor backends implement this API.
        return False

    def open(self):
        self.status = "Searching for a usable camera…"
        self.state = "connecting"
        return True  # The worker performs and reports the actual device open.

    def startStreamCapture(self):
        if self.process is not None:
            return False
        self.should_stop.clear()
        context = mp.get_context("spawn")
        self.channel = context.Queue(maxsize=4)
        self.stop = context.Event()
        self.process = context.Process(target=_capture_worker, args=(self.settings, self.channel, self.stop), daemon=True)
        self.process.start()
        self.capture_thread = threading.Thread(target=self._monitor, daemon=True)
        self.capture_thread.start()
        return True

    def _monitor(self):
        last_message = time.monotonic()
        while not self.should_stop.is_set():
            try:
                kind, value = self.channel.get(timeout=.25)
            except queue.Empty:
                if not self.process.is_alive():
                    if self.__active__ or self.process.exitcode not in (None, 0):
                        self.status = "Camera stopped. Reconnect in Settings."
                    self.__active__ = False
                    self.state = "error"
                    return
                if time.monotonic() - last_message > 12:
                    self.status = "Camera timed out. Close other camera apps, check USB, then Reconnect."
                    self.__active__ = False
                    self.state = "error"
                    self.diagnostics.append(self.status)
                    self.process.terminate()
                    return
                continue
            last_message = time.monotonic()
            if kind == "frame":
                self.last_frame_at = time.monotonic()
                if self.__streamCaptureCallback__:
                    self.__streamCaptureCallback__(value, (value.shape[1], value.shape[0]), "RGB888")
            elif kind == "ready":
                self.actual = value
                self.state = "streaming"
                self.__active__ = True
                self.status = f"Connected · {value['width']} × {value['height']} · {value['fps']:g} fps · {value['fourcc']} · {value['device']}"
            elif kind == "fps":
                self.measured_fps = value
            else:
                self.status = value
                self.diagnostics.append(value)
                if kind == "error":
                    self.state = "error"
                    self.__active__ = False

    def close(self):
        self.should_stop.set()
        if self.process is not None:
            self.stop.set()
            self.process.join(.5)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(1)
            if self.capture_thread:
                self.capture_thread.join(1)
            self.channel.close()
            self.process = None
        self.capture_thread = None
        self.state = "disconnected"
        self.__active__ = False
        return True
