from tk_runtime import enable_font_support
enable_font_support()

import json
import os
import queue
import time
import toml

import tkinter
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from functools import partial
from pathlib import Path
from tkinter import BooleanVar, IntVar, StringVar, Tk, filedialog, messagebox
from typing import Callable, List, Optional

import ttkbootstrap as ttk
from ttkbootstrap.constants import *

import cv2
import numpy as np
import serial
import math
from PIL import Image, ImageOps, ImageTk
from ultralytics import YOLO
from camera.camera_module import CameraModule
from camera.webcam import Webcam
from settings import SettingsPage
from ui_theme import configure_theme, prepare_dropdowns
from hardware import ImageProcessSettings, Lithographer, ProcessedImage
from lib.gui import IntEntry, Thumbnail, FloatEntry
from lib.img import image_to_tk_image
from projector import TkProjector
from fullscreen_preview import FullscreenPreview
from stage_control.grbl_stage import GrblStage
from stage_control.stage_controller import StageController



# TODO: Don't hardcode
THUMBNAIL_SIZE: tuple[int, int] = (160, 90)
#The values set here are not used and instead come from the config file
DEFAULT_RED_EXPOSURE: float = 4167.0
DEFAULT_UV_EXPOSURE: float = 25000.0
DEFAULT_UI_SCALE: float = 1.6


def configure_ui_scale(root: tkinter.Tk) -> None:
    """Keep the desktop UI readable on high-resolution displays.

    Set HACKERFAB_UI_SCALE (for example, to 1.6) to request a larger scale.
    Existing system scaling is never reduced.
    """
    try:
        requested_scale = float(os.environ.get("HACKERFAB_UI_SCALE", DEFAULT_UI_SCALE))
    except ValueError:
        requested_scale = DEFAULT_UI_SCALE

    current_scale = float(root.tk.call("tk", "scaling"))
    root.tk.call("tk", "scaling", max(current_scale, requested_scale))

def compute_focus_score(camera_image, blue_only, save=False):
    camera_image = camera_image.copy()
    camera_image[:, :, 1] = 0  # green should never be used for focus
    if blue_only:
      camera_image[:, :, 0] = 0  # disable red
    img = cv2.cvtColor(camera_image, cv2.COLOR_RGB2GRAY)
    img = cv2.resize(img, (0, 0), fx=0.5, fy=0.5)
    mean = float(np.mean(img))
    if mean <= 0:
        return 0.0
    img_lapl = (np.abs(cv2.Sobel(img, cv2.CV_64F, 1, 0, ksize=1)) + np.abs(cv2.Sobel(img, cv2.CV_64F, 0, 1, ksize=1))) / mean
    if save:
        print('saved focus: ', np.min(img_lapl), np.max(img_lapl))
        cv2.imwrite(save, img_lapl * 255.0 / 5.0)
    return img_lapl.var() / mean


def detect_alignment_markers(model, image, draw_rectangle=False):
    detections = []
    display_image = image.copy()
    try:
        image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        original_height, original_width = image_rgb.shape[:2]
        resized = cv2.resize(image_rgb, (640, 640))
        results = model(resized)
        boxes = results[0].boxes
        for box in boxes:
            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
            x1 = int(x1 * original_width / 640)
            x2 = int(x2 * original_width / 640)
            y1 = int(y1 * original_height / 640)
            y2 = int(y2 * original_height / 640)
            detections.append(((x1, y1), (x2, y2)))
            print('mark at ', (x1 + x2) / 2, (y1 + y2) / 2)
            if draw_rectangle:
                cv2.rectangle(display_image, (x1, y1), (x2, y2), (0, 255, 0), 5)
    except Exception as e:
        print(f"Detection failed: {e}")

    return detections, display_image 


class StrAutoEnum(str, Enum):
    """Base class for string-valued enums that use auto()"""

    def _generate_next_value_(name, *_):
        return name.lower()


class ShownImage(StrAutoEnum):
    """The type of image currently being displayed by the projector"""

    CLEAR = auto()
    PATTERN = auto()
    FLATFIELD = auto()
    RED_FOCUS = auto()
    UV_FOCUS = auto()


class PatterningStatus(StrAutoEnum):
    """The current state of the patterning process"""

    IDLE = auto()
    PATTERNING = auto()
    ABORTING = auto()


class Event(StrAutoEnum):
    """Events that can be dispatched to listeners"""

    SNAPSHOT = auto()
    SHOWN_IMAGE_CHANGED = auto()
    STAGE_POSITION_CHANGED = auto()
    IMAGE_ADJUST_CHANGED = auto()
    PATTERN_IMAGE_CHANGED = auto()
    MOVEMENT_LOCK_CHANGED = auto()
    EXPOSURE_PATTERN_PROGRESS_CHANGED = auto()
    PATTERNING_BUSY_CHANGED = auto()
    PATTERNING_FINISHED = auto()
    CHIP_CHANGED = auto()


class MovementLock(StrAutoEnum):
    """Controls whether stage position can be manually adjusted"""

    UNLOCKED = auto()  # X, Y, and Z are free to move
    XY_LOCKED = auto() # Only Z (focus) is free to move to avoid smearing UV focus pattern
    LOCKED = auto()  # No positions can move to avoid disrupting patterning


class RedFocusSource(StrAutoEnum):
    """The source image to use for red focus mode"""

    IMAGE = auto()  # Uses the dedicated red focus image
    SOLID = auto()  # Shows a solid red screen
    PATTERN = auto()  # Uses the blue channel from the pattern image
    INV_PATTERN = auto()  # Uses the inverse of the blue channel from the pattern image


@dataclass
class AlignmentConfig:
    enabled: bool
    model_path: str
    right_marker_x: float
    left_marker_x: float
    top_marker_y: float
    bottom_marker_y: float
    x_scale_factor: float
    y_scale_factor: float


@dataclass
class LithographerConfig:
    stage: StageController
    camera: CameraModule
    camera_scale: float
    red_exposure: float
    uv_exposure: float
    alignment: AlignmentConfig


@dataclass
class ExposureLog:
    time: datetime
    path: str
    coords: tuple[float, float, float]
    duration: float # ms
    aborted: bool

    def to_disk(self):
        return {
            "time": str(self.time),
            "path": self.path,
            "coords": self.coords,
            "duration": self.duration,
            "aborted": self.aborted,
        }

    @classmethod
    def from_disk(cls, d):
        return cls(
            datetime.fromisoformat(d["time"]),
            d["path"],
            d["coords"],
            d["duration"],
            d["aborted"],
        )


@dataclass
class ChipLayer:
    exposures: List[ExposureLog]

    def to_disk(self):
        return {"exposures": [ex.to_disk() for ex in self.exposures]}

    @classmethod
    def from_disk(cls, d):
        return cls([ExposureLog.from_disk(ex) for ex in d["exposures"]])


@dataclass
class Chip:
    layers: List[ChipLayer]

    def to_disk(self):
        return {"layers": [layer.to_disk() for layer in self.layers]}

    @classmethod
    def from_disk(cls, d):
        return cls([ChipLayer.from_disk(layer) for layer in d["layers"]])


class EventDispatcher:
    hardware: Lithographer
    root: Tk
    model: Optional[YOLO]
    camera: Optional[CameraModule]
    red_focus: ProcessedImage
    uv_focus: ProcessedImage
    pattern: ProcessedImage
    pattern_image: Image.Image
    red_focus_image: Image.Image
    uv_focus_image: Image.Image
    solid_red_image: Image.Image
    image_adjust_position: tuple[float, float, float]
    border_size: float
    posterize_strength: Optional[int]
    red_focus_source: RedFocusSource
    stage_setpoint: tuple[float, float, float]
    shown_image: ShownImage
    autofocus_busy: bool
    patterning_busy: bool
    autofocus_on_mode_switch: bool
    realtime_detection: bool
    first_autofocus: bool
    should_abort: bool
    exposure_time: int
    patterning_progress: float # ranges from 0.0 to 1.0
    red_exposure_time: float
    uv_exposure_time: float
    exposure_history: List[ExposureLog]
    chip: Chip
    auto_snapshot_on_uv: bool
    snapshot_directory: Path
    listeners: dict[Event, List[Callable]]

    def __init__(
        self, 
        stage: StageController,
        proj: TkProjector,
        root: Tk,
        camera: Optional[CameraModule],
        red_exposure: float,
        uv_exposure: float,
    ):
        # Hardware components
        self.hardware = Lithographer(stage, proj)
        self.camera = camera
        self.camera_image = None
        self.root = root

        # Detection model
        self.model = None

        # Image processing objects
        self.red_focus = ProcessedImage()
        self.uv_focus = ProcessedImage()
        self.pattern = ProcessedImage()

        # Source images
        self.pattern_image = Image.new("RGB", (1, 1), "black")
        self.pattern_image_path = ""
        self.red_focus_image = Image.new("RGB", (1, 1), "black")
        self.uv_focus_image = Image.new("RGB", (1, 1), "black")
        self.solid_red_image = Image.new("RGB", (1, 1), "red")

        # Image settings
        self.image_adjust_position = (0.0, 0.0, 0.0)
        self.border_size = 0.0
        self.posterize_strength = None
        self.red_focus_source = RedFocusSource.IMAGE

        # Stage control
        self.stage_setpoint = (0.0, 0.0, 0.0)

        # Status flags
        self.shown_image = ShownImage.CLEAR
        self.autofocus_busy = False
        self.patterning_busy = False
        self.autofocus_on_mode_switch = False
        self.realtime_detection = False
        self.first_autofocus = True
        self.should_abort = False

        # Exposure settings and progress
        self.exposure_time = 8000
        self.patterning_progress = 0.0
        self.red_exposure_time = red_exposure
        self.uv_exposure_time = uv_exposure

        # History and logging
        self.exposure_history = []
        self.chip = Chip([ChipLayer([])])

        # Snapshot settings
        self.auto_snapshot_on_uv = True
        self.snapshot_directory = Path("stepper_captures")
        self.snapshot_directory.mkdir(exist_ok=True)

        # Event handling
        self.listeners = dict()
        self.add_event_listener(Event.SHOWN_IMAGE_CHANGED, lambda: self._update_projector())

    def load_chip(self, path: str):
        print(f"Loading chip at {path!r}")
        with open(path, "r") as f:
            d = json.load(f)
        self.chip = Chip.from_disk(d)
        self.on_event(Event.CHIP_CHANGED)

    def new_chip(self):
        # TODO: Prompt user to save old chip??
        self.chip = Chip([ChipLayer([])])
        self.on_event(Event.CHIP_CHANGED)

    def add_chip_layer(self):
        self.chip.layers.append(ChipLayer([]))
        self.on_event(Event.CHIP_CHANGED)

    def save_chip(self, path: str):
        with open(path, "w") as f:
            json.dump(self.chip.to_disk(), f)

    def delete_chip_exposure(self, layer: int, ex: int):
        self.chip.layers[layer].exposures.pop(ex)
        print(f"Deleted exposure {layer} {ex}")
        self.on_event(Event.CHIP_CHANGED)

    @property
    def current_image(self) -> Optional[Image.Image]:
        match self.shown_image:
            case ShownImage.CLEAR:
                return None
            case ShownImage.RED_FOCUS:
                return self.red_focus.processed()
            case ShownImage.UV_FOCUS:
                return self.uv_focus.processed()
            case ShownImage.PATTERN:
                return self.pattern.processed()

    def _update_projector(self):
        img = self.current_image
        if img is None:
            self.hardware.projector.clear()
        else:
            self.hardware.projector.show(img)

    def _refresh_pattern(self):
        self.pattern.update(
            image=self.pattern_image,
            settings=ImageProcessSettings(
                posterization=self.posterize_strength,
                color_channels=(False, False, True),
                flatfield=None,
                size=self.hardware.projector.size(),
                image_adjust=self.image_adjust_position,
                border_size=self.border_size,
            ),
        )

        if self.red_focus_source in (RedFocusSource.PATTERN, RedFocusSource.INV_PATTERN):
            self._refresh_red_focus()

        # TODO:
        # Image adjust, resizing, and flatfield correction are performed *AFTER SLICING*

        self.on_event(Event.PATTERN_IMAGE_CHANGED)

    def set_red_focus_source(self, source: RedFocusSource):
        self.red_focus_source = source
        self._refresh_red_focus()

    def _red_focus_source(self) -> Image.Image:
        match self.red_focus_source:
            case RedFocusSource.IMAGE:
                return self.red_focus_image
            case RedFocusSource.SOLID:
                return self.solid_red_image
            case RedFocusSource.PATTERN:
                return self.pattern_image.getchannel("B").convert("RGBA")
            case RedFocusSource.INV_PATTERN:
                return ImageOps.invert(self.pattern_image.getchannel("B")).convert("RGBA")

    def _refresh_red_focus(self):
        if self.hardware.projector.size() != self.solid_red_image.size:
            self.solid_red_image = Image.new("RGB", self.hardware.projector.size(), "red")

        img = self._red_focus_source()

        self.red_focus.update(
            image=img,
            settings=ImageProcessSettings(
                posterization=self.posterize_strength,
                flatfield=None,
                color_channels=(True, False, False),
                size=self.hardware.projector.size(),
                image_adjust=self.image_adjust_position,
                border_size=self.border_size,
            ),
        )

        if self.shown_image == ShownImage.RED_FOCUS:
            self.on_event(Event.SHOWN_IMAGE_CHANGED)

    def _refresh_uv_focus(self):
        self.uv_focus.update(
            image=self.uv_focus_image,
            settings=ImageProcessSettings(
                posterization=self.posterize_strength,
                flatfield=None,
                color_channels=(False, False, True),
                size=self.hardware.projector.size(),
                image_adjust=self.image_adjust_position,
                border_size=0.0,
            ),
        )

        if self.shown_image == ShownImage.UV_FOCUS:
            self.on_event(Event.SHOWN_IMAGE_CHANGED)

    def set_posterize_strength(self, strength: Optional[int]):
        self.posterize_strength = strength
        self._refresh_red_focus()
        self._refresh_uv_focus()
        self._refresh_pattern()

    def set_border_size(self, border_size: float):
        self.border_size = border_size
        self._refresh_red_focus()
        self._refresh_uv_focus()
        self._refresh_pattern()

    def set_shown_image(self, shown_image: ShownImage):
        print(f"set_shown_image({shown_image})")
        self.shown_image = shown_image
        self.on_event(Event.SHOWN_IMAGE_CHANGED)

    def move_absolute(self, coords: dict[str, float]):
        self.hardware.stage.move_to(coords)
        x = coords.get("x", self.stage_setpoint[0])
        y = coords.get("y", self.stage_setpoint[1])
        z = coords.get("z", self.stage_setpoint[2])
        self.stage_setpoint = (x, y, z)
        self.on_event(Event.STAGE_POSITION_CHANGED)

    def move_relative(self, coords: dict[str, float]):
        x = coords.get("x", 0) + self.stage_setpoint[0]
        y = coords.get("y", 0) + self.stage_setpoint[1]
        z = coords.get("z", 0) + self.stage_setpoint[2]
        self.stage_setpoint = (x, y, z)
        self.hardware.stage.move_to({k: self.stage_setpoint[i] for k, i in (("x", 0), ("y", 1), ("z", 2))})
        self.on_event(Event.STAGE_POSITION_CHANGED)

    def set_use_solid_red(self, use: bool):
        self.use_solid_red = use
        self.set_shown_image(ShownImage.RED_FOCUS)
        self._refresh_red_focus()

    def set_pattern_image(self, img: Image.Image, path: str):
        self.pattern_image = img
        self.pattern_image_path = path
        self._refresh_pattern()

    def set_red_focus_image(self, img: Image.Image):
        self.red_focus_image = img
        self._refresh_red_focus()

    def set_uv_focus_image(self, img: Image.Image):
        self.uv_focus_image = img
        self._refresh_uv_focus()

    def set_patterning_busy(self, busy: bool):
        self.patterning_busy = busy
        self.on_event(Event.MOVEMENT_LOCK_CHANGED)
        self.on_event(Event.PATTERNING_BUSY_CHANGED)

    def set_progress(self, pattern_progress: float, exposure_progress: float):
        self.patterning_progress = pattern_progress
        self.exposure_progress = exposure_progress
        self.on_event(Event.EXPOSURE_PATTERN_PROGRESS_CHANGED)

    def set_latest_image(self, camera_image):
        self.camera_image = camera_image

    def set_autofocus_busy(self, busy):
        self.autofocus_busy = busy
        self.on_event(Event.MOVEMENT_LOCK_CHANGED)

    def abort_patterning(self):
        self.should_abort = True
        print("Aborting patterning")

    def in_uv(self):
        return self.shown_image in (ShownImage.PATTERN, ShownImage.UV_FOCUS)

    def home_stage(self):
        self.hardware.stage.home()
        self.non_blocking_delay(1.0)
        while True:
            self.non_blocking_delay(1.0)
            idle, pos = self.hardware.stage._query_state()
            if idle:
                break

        self.stage_setpoint = (pos[0] * 1000.0, pos[1] * 1000.0, pos[2] * 1000.0)
        print(f"Homed stage to {self.stage_setpoint}")
        self.on_event(Event.STAGE_POSITION_CHANGED)

    def set_image_position(self, x, y, t):
        self.image_adjust_position = (x, y, t)
        self._refresh_red_focus()
        self._refresh_uv_focus()
        self._refresh_pattern()
        self.on_event(Event.IMAGE_ADJUST_CHANGED)

    @property
    def image_position(self):
        return self.image_adjust_position

    @property
    def movement_lock(self):
        if self.patterning_busy or self.autofocus_busy:
            return MovementLock.LOCKED
        # elif (self.shown_image == ShownImage.UV_FOCUS or self.shown_image == ShownImage.PATTERN):
        #     return MovementLock.XY_LOCKED
        else:
            return MovementLock.UNLOCKED

    def on_event(self, event: Event, *args, **kwargs):
        if event not in self.listeners:
            return

        for listener in self.listeners[event]:
            listener(*args, **kwargs)

    def on_event_cb(self, event: Event, *args, **kwargs):
        return lambda: self.on_event(event, *args, **kwargs)

    def add_event_listener(self, event: Event, listener: Callable):
        if event not in self.listeners:
            self.listeners[event] = []
        self.listeners[event].append(listener)

    def begin_patterning(self):
        if self.patterning_busy or self.autofocus_busy:
            return
        if not self.pattern_image_path:
            messagebox.showinfo("Choose a pattern", "Select a pattern image before starting an exposure.")
            return
        if not isinstance(self.exposure_time, (int, float)) or not math.isfinite(self.exposure_time) or self.exposure_time <= 0:
            messagebox.showerror("Exposure time", "Enter a positive exposure time in milliseconds.")
            return
        # TODO: Update patterning preview

        print("Patterning at ", self.stage_setpoint)
        duration = self.exposure_time
        print(f"Patterning 1 tiles for {duration}ms\nTotal time: {str(round((duration) / 1000))}s")

        # TODO: Image slicing.
        # Note that flatfield correction and image adjustment should be applied *after* slicing
        img = self.pattern.processed()

        self.set_patterning_busy(True)
        self.hardware.projector.show(img)
        end_time = time.time() + duration / 1000.0
        while time.time() < end_time:
            progress = 1.0 - ((end_time - time.time()) * 1000 / duration)
            self.set_progress(0.0, progress)
            self.root.update()
            if self.should_abort:
                break
        self.set_shown_image(ShownImage.CLEAR)
        self.root.update()  # Force image to stop being displayed ASAP
        self.set_progress(1.0, 1.0)

        log = ExposureLog(
            datetime.now(),
            self.pattern_image_path,
            self.stage_setpoint,
            duration,
            self.should_abort,
        )
        self.exposure_history.append(log)
        self.chip.layers[-1].exposures.append(log)

        self.on_event(Event.CHIP_CHANGED)
        self.set_patterning_busy(False)

        if self.should_abort:
            print("Patterning aborted")
            self.should_abort = False

    def non_blocking_delay(self, t: float):
        start = time.time()
        while time.time() - start < t:
            self.root.update()

    def enter_red_mode(self, mode_switch_autofocus=True):
        print("enter_red_mode")
        self.set_shown_image(ShownImage.RED_FOCUS)
        if self.camera:
            self.camera.setExposureTime(self.red_exposure_time)
        if mode_switch_autofocus and self.autofocus_on_mode_switch:
            self.autofocus(blue_only=False)
        self.on_event(Event.MOVEMENT_LOCK_CHANGED)

    def enter_uv_mode(self, mode_switch_autofocus=True):
        if self.auto_snapshot_on_uv:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            filename = self.snapshot_directory / f"uv_mode_entry_{timestamp}.png"
            self.on_event(Event.SNAPSHOT, str(filename))

        if self.camera:
            self.camera.setExposureTime(self.uv_exposure_time)
        if (
            mode_switch_autofocus
            and not self.autofocus_busy
            and self.autofocus_on_mode_switch
        ):
            # UV mode usually needs about -70 to be in focus compared to red mode
            #self.move_relative({"z": -85.0})
            pass

        # self.set_shown_image(ShownImage.UV_FOCUS)
        self.set_shown_image(ShownImage.CLEAR) # enter uv mode: don't project uv

        if mode_switch_autofocus and self.autofocus_on_mode_switch:
            self.non_blocking_delay(2.0)
            self.autofocus(blue_only=True)

        self.on_event(Event.MOVEMENT_LOCK_CHANGED)

    def autofocus(self, blue_only, log=False):
        if not self.camera or self.camera_image is None:
            print("No live camera connected, skipping autofocus")
            return

        if self.first_autofocus:
            # TODO: Fix this spuriously triggering
            self.first_autofocus = False
            return

        if self.autofocus_busy:
            print("Skipping nested autofocus!")
            return

        if log:
            try:
                os.mkdir('aftest')
            except FileExistsError:
                pass
            log_file = open('aftest/log.csv', 'w')

        counter = 0
        def sample_focus():
            def do_thing():
                self.non_blocking_delay(0.1)
                if self.camera_image is None:
                    raise RuntimeError("Camera feed lost during autofocus")
                return compute_focus_score(self.camera_image, blue_only=blue_only)
            focus_score = sorted([do_thing() for _ in range(3)])[1]
            nonlocal counter
            if log:
                log_file.write(f'{counter},{focus_score}\n')
                cv2.imwrite(f'aftest/img{counter}.png', self.camera_image)
            counter += 1
            return focus_score


        print("Starting autofocus")

        self.set_autofocus_busy(True)
        try:
            self.non_blocking_delay(1.0)
            mid_score = sample_focus()
            self.move_relative({"z": -20.0})
            self.non_blocking_delay(1.0)
            neg_score = sample_focus()
            self.move_relative({"z": 40.0})
            self.non_blocking_delay(1.0)
            pos_score = sample_focus()
            self.move_relative({"z": -20.0})
            self.non_blocking_delay(1.0)

            last_focus = mid_score

            if neg_score < mid_score < pos_score:
                # Improved focus is in the +Z direction
                for i in range(30):
                    self.move_relative({"z": 10.0})
                    self.non_blocking_delay(0.5)
                    new_score = sample_focus()
                    if last_focus > new_score:
                        print(f"Successful +Z coarse autofocus {i}")
                        last_focus = new_score
                        break
                    last_focus = new_score

                for i in range(10):
                    self.move_relative({"z": -2.0})
                    self.non_blocking_delay(0.5)
                    new_score = sample_focus()
                    if last_focus > new_score:
                        print(f"Successful -Z fine autofocus {i}")
                        break
                    last_focus = new_score
            elif neg_score > mid_score > pos_score:
                # Improved focus is in the -Z direction
                for i in range(30):
                    self.move_relative({"z": -10.0})
                    self.non_blocking_delay(0.5)
                    new_score = sample_focus()
                    if last_focus > new_score:
                        print(f"Successful -Z coarse autofocus {i}")
                        break
                    last_focus = new_score

                for i in range(10):
                    self.move_relative({"z": 2.0})
                    self.non_blocking_delay(0.5)
                    new_score = sample_focus()
                    if last_focus > new_score:
                        print(f"Successful +Z fine autofocus {i}")
                        break
                    last_focus = new_score
            elif neg_score < mid_score and pos_score < mid_score:
                # We are very close to already being in focus
                print(f"Almost in focus! (neg {neg_score} mid {mid_score} pos {pos_score})")
                self.move_relative({"z": -20.0})
                self.non_blocking_delay(0.5)

                for i in range(30):
                    self.move_relative({"z": 2.0})
                    self.non_blocking_delay(0.5)
                    new_score = sample_focus()
                    if last_focus > new_score:
                        print(f"Successful +Z fine autofocus {i}")
                        break
                    last_focus = new_score
            else:
                print("Autofocus is confused!")


        except RuntimeError as exc:
            print(f"Autofocus stopped: {exc}")
        finally:
            self.set_autofocus_busy(False)
            if log:
                log_file.close()

        print("Finished autofocus")

    def initialize_alignment(self, config: LithographerConfig):
        self.config = config
        self.realtime_detection = config.alignment.enabled
        # Attempt loading the model even if detection is off by default
        try:
            print("loading model")
            model_path = config.alignment.model_path
            self.model = YOLO(model_path)
            print("loaded model")
        except Exception as e:
            print(f"Failed to load YOLO model: {e}")

    def set_snapshot_directory(self, directory: Path):
        self.snapshot_directory = directory
        self.snapshot_directory.mkdir(exist_ok=True)


class SnapshotFrame:
    """
    Presents a frame with a filename entry and a button to save screenshots of the current camera view.
    """

    def __init__(self, parent, enable, event_dispatcher: EventDispatcher):
        self.frame = ttk.Frame(parent)
        self.frame.grid(row=1, column=0)

        state = "normal" if enable else "disable"

        # TODO: Allow %X, %Y, %Z formats to save position on chip
        self.name_var = StringVar(value="output_%T.png")
        self.name_var.trace_add("write", lambda _a, _b, _c: self._refresh_name_preview())

        self.counter = 0

        self.name_entry = ttk.Entry(self.frame, textvariable=self.name_var, state=state)
        self.name_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.frame.columnconfigure(0, weight=1)

        self.name_preview = ttk.Label(self.frame, wraplength=480, bootstyle="secondary")
        self.name_preview.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        def on_snapshot_button():
            event_dispatcher.on_event(Event.SNAPSHOT, self._next_filename())
            self.counter += 1
            self._refresh_name_preview()

        self.button = ttk.Button(self.frame, text="Save snapshot", command=on_snapshot_button, state=state)
        self.button.grid(row=0, column=1)

        self._refresh_name_preview()

    def _next_filename(self):
        counter_str = str(self.counter)
        time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        name = self.name_var.get()
        name = name.replace("%C", counter_str).replace("%c", counter_str)
        name = name.replace("%T", time_str).replace("%t", time_str)
        return name

    def _refresh_name_preview(self):
        self.name_preview.configure(text=f"Output File: {self._next_filename()}")


class CameraFrame:
    def __init__(self, parent, event_dispatcher, c, camera_scale):
        self.frame = ttk.Labelframe(parent, text="Camera", padding=16)
        self.frame.columnconfigure(0, weight=1)
        self.label = ttk.Label(self.frame, text="Connect your microscope camera\n\nChoose a device in Settings to see a live preview.", anchor="center")
        self.label.grid(row=0, column=0, sticky="nsew", ipady=70)
        self.status = ttk.Label(self.frame, text="Not connected", wraplength=620, bootstyle="secondary")
        self.status.grid(row=1, column=0, sticky="w", pady=(12, 4))
        self.focus_score_label = ttk.Label(self.frame, text="Focus · —", bootstyle="secondary")
        self.focus_score_label.grid(row=2, column=0, sticky="w")
        self.snapshot = SnapshotFrame(self.frame, True, event_dispatcher)
        self.snapshot.frame.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        self.event_dispatcher = event_dispatcher
        self.snapshots_pending = queue.Queue()
        event_dispatcher.add_event_listener(Event.SNAPSHOT, lambda filename: self.snapshots_pending.put(filename))
        self.gui_img = None
        self.camera = c
        self.pending_frame = None
        self.poll_id = None
        self.generation = 0
        self.last_received = 0

    def _on_new_frame(self):
        self.status.configure(text=getattr(self.camera, "status", "Camera disabled" if not self.camera else "Vendor camera connected"))
        packet, self.pending_frame = self.pending_frame, None
        if getattr(self.camera, "state", "") == "error":
            packet = None
            self.event_dispatcher.camera_image = None
            self.label.configure(image="", text="Camera connection failed · see details below")
            self.focus_score_label.configure(text="Focus · unavailable")
        if getattr(self.camera, "state", "") != "error" and self.last_received and time.monotonic() - self.last_received > 2:
            self.event_dispatcher.camera_image = None
            self.label.configure(image="", text="Camera feed interrupted · reconnect in Settings")
            self.focus_score_label.configure(text="Focus · unavailable")
        if packet is not None:
            image, dimensions, format = packet
            self.last_received = time.monotonic()
            red_score = compute_focus_score(image, blue_only=False)
            blue_score = compute_focus_score(image, blue_only=True)
            self.focus_score_label.configure(text=f"Focus · Red {red_score:.2f}   UV {blue_score:.2f}")
            try:
                filename = self.snapshots_pending.get_nowait()
                if not cv2.imwrite(filename, cv2.cvtColor(image, cv2.COLOR_RGB2BGR)):
                    self.status.configure(text=f"Could not save snapshot: {filename}")
            except queue.Empty:
                pass
            except (OSError, cv2.error) as exc:
                self.status.configure(text=f"Snapshot failed: {exc}")
            self.gui_camera_preview(image, dimensions)
        elif self.event_dispatcher.camera_image is None:
            while not self.snapshots_pending.empty():
                self.snapshots_pending.get_nowait()
        self.snapshot.button.configure(state="normal" if self.event_dispatcher.camera_image is not None else "disabled")
        self.poll_id = self.frame.after(66, self._on_new_frame)

    def start(self):
        if self.camera:
            generation = self.generation
            def callback(image, dimensions, format):
                if generation == self.generation:
                    self.pending_frame = (image, dimensions, format)
            self.camera.setStreamCaptureCallback(callback)
            try:
                if not self.camera.open() or not self.camera.startStreamCapture():
                    self.status.configure(text="Camera failed to start. Open Settings to reconnect.")
            except Exception as exc:
                self.status.configure(text=f"Camera failed: {exc}")
        if self.poll_id is None:
            self._on_new_frame()

    def replace(self, camera):
        self.generation += 1
        if self.camera:
            self.camera.close()
        self.camera = camera
        self.event_dispatcher.camera = camera
        self.event_dispatcher.config.camera = camera
        self.event_dispatcher.camera_image = None
        self.pending_frame = None
        self.last_received = 0
        self.label.configure(image="", text="Connecting…" if camera else "Camera disabled")
        self.focus_score_label.configure(text="Focus · —")
        self.start()

    def cleanup(self):
        self.generation += 1
        if self.poll_id:
            self.frame.after_cancel(self.poll_id)
            self.poll_id = None
        if self.camera:
            self.camera.close()

    def gui_camera_preview(self, camera_image, dimensions):
        # Measurement and snapshots always use the original unannotated image.
        self.event_dispatcher.set_latest_image(camera_image)
        model = self.event_dispatcher.model
        if model and self.event_dispatcher.realtime_detection:
            _, camera_image = detect_alignment_markers(model, camera_image, draw_rectangle=True)
        width = max(320, min(760, self.frame.winfo_width() - 36))
        image = Image.fromarray(camera_image)
        image.thumbnail((width, 360), Image.Resampling.LANCZOS)
        self.gui_img = image_to_tk_image(image)
        self.label.configure(image=self.gui_img, text="")

class StagePositionFrame:
    """Explicit movement distance and direction, with the same stage interlocks."""
    def __init__(self, parent, event_dispatcher, uvmode):
        self.frame = ttk.Labelframe(parent, text="Stage & focus", padding=20)
        self.event_dispatcher = event_dispatcher
        self.xy_widgets, self.z_widgets = [], []
        self.position_intputs = []
        self.position_frame = ttk.Frame(self.frame)
        self.position_frame.grid(row=0, column=0, sticky="ew")
        for i, axis in enumerate(('X', 'Y', 'Z')):
            ttk.Label(self.position_frame, text=f"{axis} · µm", bootstyle="secondary").grid(row=0, column=i, sticky="w", padx=4)
            field = FloatEntry(self.position_frame, default=0)
            field.widget.configure(width=8)
            field.widget.grid(row=1, column=i, padx=4, pady=8, sticky="ew")
            self.position_intputs.append(field)
            (self.xy_widgets if i < 2 else self.z_widgets).append(field.widget)
        def move_absolute():
            try:
                values = [field.get() for field in self.position_intputs]
                if not all(math.isfinite(v) for v in values):
                    raise ValueError()
                event_dispatcher.move_absolute(dict(zip(('x', 'y', 'z'), values)))
            except (ValueError, tkinter.TclError):
                messagebox.showerror("Position", "Enter a valid number for each position.")
        self.set_position_button = ttk.Button(self.frame, text="Move to position", command=move_absolute, bootstyle="secondary-outline")
        self.set_position_button.grid(row=1, column=0, sticky="ew", pady=(0, 20))
        ttk.Separator(self.frame).grid(row=2, column=0, sticky="ew", pady=(0, 16))
        step = ttk.Frame(self.frame)
        step.grid(row=3, column=0, sticky="ew")
        ttk.Label(step, text="Distance per click · µm").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.step_size = StringVar(value="10")
        ttk.Entry(step, textvariable=self.step_size, width=7).grid(row=0, column=1)
        presets = ttk.Frame(self.frame)
        presets.grid(row=4, column=0, sticky="ew", pady=12)
        for i, amount in enumerate((1, 10, 50, 250)):
            ttk.Radiobutton(presets, text=str(amount), variable=self.step_size, value=str(amount), bootstyle="toolbutton").grid(row=0, column=i, padx=3, sticky="ew")
            presets.columnconfigure(i, weight=1)
        def jog(axis, direction):
            try:
                distance = float(self.step_size.get())
                if not math.isfinite(distance) or distance <= 0:
                    raise ValueError()
                event_dispatcher.move_relative({axis: direction * distance})
            except ValueError:
                messagebox.showerror("Movement distance", "Enter a positive distance in micrometers.")
        controls = ttk.Frame(self.frame)
        controls.grid(row=5, column=0, pady=(8, 16))
        if not uvmode:
            for text, axis, direction, row, column in [('↑ Y', 'y', 1, 0, 1), ('X ←', 'x', -1, 1, 0), ('X →', 'x', 1, 1, 2), ('↓ Y', 'y', -1, 2, 1)]:
                button = ttk.Button(controls, text=text, width=5, command=lambda a=axis, d=direction: jog(a, d), bootstyle="secondary-outline")
                button.grid(row=row, column=column, padx=3, pady=3)
                self.xy_widgets.append(button)
        focus = ttk.Frame(self.frame)
        focus.grid(row=6, column=0, sticky="ew")
        ttk.Label(focus, text="Focus", bootstyle="secondary").pack(side="left", padx=(0, 12))
        for label, direction in [('− Z', -1), ('+ Z', 1)]:
            button = ttk.Button(focus, text=label, command=lambda d=direction: jog('z', d), bootstyle="secondary-outline")
            button.pack(side="left", fill="x", expand=True, padx=4)
            self.z_widgets.append(button)
        shortcuts = ttk.Frame(self.frame)
        shortcuts.grid(row=7, column=0, sticky="ew", pady=(20, 0))
        self.shortcuts = []
        for label, coords in [('Chip origin', {'x': -14500., 'y': -13500., 'z': -13844.}), ('Load / unload', {'x': -14500., 'y': -14500., 'z': -14500.})]:
            button = ttk.Button(shortcuts, text=label, bootstyle="secondary-link", command=lambda c=coords: event_dispatcher.move_absolute(c))
            button.pack(side="left", padx=3)
            self.shortcuts.append(button)
        def on_lock_change():
            lock = event_dispatcher.movement_lock
            for widget in self.xy_widgets + [self.set_position_button]:
                widget.configure(state="normal" if lock == MovementLock.UNLOCKED else "disabled")
            for widget in self.z_widgets:
                widget.configure(state="disabled" if lock == MovementLock.LOCKED else "normal")
            for widget in self.shortcuts:
                widget.configure(state="normal" if lock == MovementLock.UNLOCKED and event_dispatcher.hardware.stage.has_homing() else "disabled")
        def on_position_change():
            for field, value in zip(self.position_intputs, event_dispatcher.stage_setpoint):
                field.set(value)
        event_dispatcher.add_event_listener(Event.MOVEMENT_LOCK_CHANGED, on_lock_change)
        event_dispatcher.add_event_listener(Event.STAGE_POSITION_CHANGED, on_position_change)
        on_lock_change()

class ImageAdjustFrame:
    def __init__(self, parent, event_dispatcher: EventDispatcher):
        self.frame = ttk.Frame(parent)

        self.position_intputs = []
        self.step_size_intputs = []

        self.lockable_widgets = []

        # Absolute

        self.absolute_frame = ttk.Labelframe(self.frame, text="Image Adjustment")
        self.absolute_frame.grid(row=0, column=0)

        for i, coord in ((0, "x"), (1, "y"), (2, "ϴ")):
            if i == 0 or i == 1:
                self.position_intputs.append(
                    IntEntry(parent=self.absolute_frame, default=0)
                )
            else:
                self.position_intputs.append(
                    FloatEntry(parent=self.absolute_frame, default=0)
                )
            self.position_intputs[-1].widget.grid(row=0, column=i)

        def callback_set():
            x, y, t = self._position()
            event_dispatcher.set_image_position(x, y, t)

        self.set_position_button = ttk.Button(self.absolute_frame, text="Set Image Position",
                                               command=callback_set, bootstyle="primary-outline")
        self.set_position_button.grid(row=1, column=0, columnspan=3, sticky="ew", padx=4, pady=(2, 6))

        # Relative
        self.relative_frame = ttk.Labelframe(self.frame, text="Adjustment")
        self.relative_frame.grid(row=1, column=0)

        for i, coord in ((0, "x"), (1, "y"), (2, "ϴ")):
            if i == 0 or i == 1:
                self.step_size_intputs.append(
                    IntEntry(
                        parent=self.relative_frame,
                        default=10,
                        min_value=-1000,
                        max_value=1000,
                    )
                )
            else:
                self.step_size_intputs.append(
                    FloatEntry(
                        parent=self.relative_frame,
                        default=10.0,
                        min_value=0.0,
                        max_value=360.0
                    )
                )
            self.step_size_intputs[-1].widget.grid(row=3, column=i)

            def callback_pos(index, c):
                pos = list(event_dispatcher.image_position)
                pos[index] += self.step_sizes()[index]
                event_dispatcher.set_image_position(*pos)

            def callback_neg(index, c):
                pos = list(event_dispatcher.image_position)
                pos[index] -= self.step_sizes()[index]
                event_dispatcher.set_image_position(*pos)

            coord_inc_button = ttk.Button(
                self.relative_frame,
                text=f"+{coord.upper()}",
                command=partial(callback_pos, i, coord),
                bootstyle="info-outline",
                width=6,
            )
            coord_dec_button = ttk.Button(
                self.relative_frame,
                text=f"-{coord.upper()}",
                command=partial(callback_neg, i, coord),
                bootstyle="info-outline",
                width=6,
            )

            coord_inc_button.grid(row=0, column=i, padx=4, pady=(6, 2))
            coord_dec_button.grid(row=1, column=i, padx=4, pady=(2, 4))

            self.lockable_widgets.append(coord_inc_button)
            self.lockable_widgets.append(coord_dec_button)
            self.lockable_widgets.append(self.position_intputs[i].widget)
            self.lockable_widgets.append(self.step_size_intputs[i].widget)
        self.lockable_widgets.append(self.set_position_button)

        ttk.Label(
            self.relative_frame,
            text="Step Size (pixels, pixels, degrees)",
            anchor="center",
        ).grid(row=2, column=0, columnspan=3, sticky="ew")

        def on_position_change():
            pos = event_dispatcher.image_adjust_position
            for i in range(3):
                self.position_intputs[i].set(pos[i])

        event_dispatcher.add_event_listener(Event.IMAGE_ADJUST_CHANGED, on_position_change)

        def on_lock_change():
            if event_dispatcher.movement_lock == MovementLock.UNLOCKED:
                for w in self.lockable_widgets:
                    w.configure(state="normal")
            else:
                for w in self.lockable_widgets:
                    w.configure(state="disabled")

        event_dispatcher.add_event_listener(Event.MOVEMENT_LOCK_CHANGED, on_lock_change)

    def _position(self) -> tuple[int, int, int]:
        return tuple(intput.get() for intput in self.position_intputs)

    def _set_position(self, pos: tuple[int, int, int]):
        for i in range(3):
            self.position_intputs[i].set(pos[i])

    def step_sizes(self) -> tuple[int, int, int]:
        return tuple(intput.get() for intput in self.step_size_intputs)

class PredefinedImageSelector:
    """A widget that shows a selection of predefined images instead of file dialog"""

    def __init__(self, parent, size, predefined_images, on_select=None):
        self.parent = parent
        self.size = size
        self.predefined_images = predefined_images  # List of (name, path) tuples
        self.on_select = on_select
        self.current_image = None
        self.current_path = ""

        # Create main frame
        self.widget = ttk.Frame(parent)

        # Create thumbnail display
        placeholder = Image.new("RGB", size, "gray")
        self.photo = image_to_tk_image(placeholder)
        self.label = ttk.Label(self.widget, image=self.photo, relief="solid", borderwidth=2)
        self.label.grid(row=0, column=0, columnspan=2, pady=5)

        # Create dropdown for image selection
        self.image_var = StringVar()
        self.image_dropdown = ttk.Combobox(
            self.widget, 
            textvariable=self.image_var,
            values=[name for name, _ in predefined_images],
            state="readonly"
        )
        self.image_dropdown.grid(row=1, column=0, columnspan=2, sticky="ew", pady=2)
        self.image_dropdown.bind("<<ComboboxSelected>>", self._on_selection_change)

        # Add a button to Load custom alignment marks
        self.upload_button = ttk.Button(self.widget, text="Upload Marks", command= self._upload_marks)
        self.upload_button.grid(row=3, column=0, columnspan=2, sticky="ew", pady=2)

        # Add a button to load the selected image
        self.load_button = ttk.Button(self.widget, text="Load Selected", command=self._load_selected)
        self.load_button.grid(row=2, column=0, columnspan=2, sticky="ew", pady=2)

        # Set default selection if images are available
        if predefined_images:
            self.image_dropdown.set(predefined_images[0][0])
            self._load_image(predefined_images[0][1])

    def _on_selection_change(self, event=None):
        """Called when dropdown selection changes"""
        selected_name = self.image_var.get()
        for name, path in self.predefined_images:
            if name == selected_name:
                self._load_image(path)
                break

    def _upload_marks(self):
        """Called when Upload Marks button is clicked"""
        current_directory = StringVar(value=str("~"));
        # checks need to be made before we allow user to do this
        dir_path = filedialog.askopenfilename(
            initialdir=current_directory,
            filetypes=[("All Files", "*.*")],
            title="Select Custom Alignment Marks",
        )
        filename = f"Plus Mark {len(self.predefined_images)}"
        print(f'uploading custom alignment marker file: {filename}')

        if dir_path:  # User didn't cancel            
            self.predefined_images.append((filename, dir_path))
            self.image_dropdown['values'] = [name for name, _ in self.predefined_images]

    def _load_selected(self):
        """Called when Load Selected button is clicked"""
        if self.on_select and self.current_image:
            self.on_select(None)  # Call the callback

    def _load_image(self, path):
        """Load and display an image from the given path"""
        try:
            img = Image.open(path)
            self.current_image = img
            self.current_path = path

            # Create thumbnail for display
            thumb = img.copy()
            thumb.thumbnail(self.size, Image.Resampling.LANCZOS)
            self.photo = image_to_tk_image(thumb)
            self.label.configure(image=self.photo)
        except Exception as e:
            print(f"Failed to load image {path}: {e}")
            # Show placeholder on error
            placeholder = Image.new("RGB", self.size, "red")
            self.photo = image_to_tk_image(placeholder)
            self.label.configure(image=self.photo)

    @property
    def image(self):
        """Return the current image"""
        return self.current_image

    @property
    def path(self):
        """Return the current image path"""
        return self.current_path

class ImageSelectFrame:
    def __init__(self, parent, button_text, import_command, predefined_images=None):
        self.frame = ttk.Frame(parent)

        if predefined_images:
            # Use predefined image selector
            self.selector = PredefinedImageSelector(
                self.frame, 
                THUMBNAIL_SIZE, 
                predefined_images,
                on_select=import_command
            )
            self.selector.widget.grid(row=0, column=0)

            # For compatibility with existing code
            self.thumb = self.selector
        else:
            # Use original thumbnail selector (file dialog)
            self.thumb = Thumbnail(self.frame, THUMBNAIL_SIZE, on_import=import_command)
            self.thumb.widget.grid(row=0, column=0)

        self.label = ttk.Label(self.frame, text=button_text)
        self.label.grid(row=1, column=0)


class PatternDisplayFrame: # read only pattern display in red and uv focusing mode
    def __init__(self, parent, event_dispatcher: EventDispatcher):
        self.frame = ttk.Frame(parent)
        self.event_dispatcher = event_dispatcher
        # instead of pattern_frame = ImageSelectFrame
        # use pattern_display_frame (read-only)
        self.pattern_display_frame = ttk.Labelframe(self.frame, text="Current Pattern")
        self.pattern_display_frame.grid(row=0, column=0, padx=5, pady=5)

        placeholder = Image.new("RGB", THUMBNAIL_SIZE, "gray")
        self.pattern_photo = image_to_tk_image(placeholder)
        self.pattern_label = ttk.Label(self.pattern_display_frame, image=self.pattern_photo)
        self.pattern_label.grid(row=0, column=0, padx=5, pady=5)

        ttk.Label(self.pattern_display_frame, text="(Upload in Pattern Upload tab)", 
                 font=("TkDefaultFont", 8), foreground="gray").grid(row=1, column=0)

        event_dispatcher.add_event_listener(Event.PATTERN_IMAGE_CHANGED, self._update_pattern_display)
        event_dispatcher.add_event_listener(Event.SHOWN_IMAGE_CHANGED, self._on_shown_image_changed)

    def _update_pattern_display(self):
        """Update the read-only pattern display when pattern changes"""
        if hasattr(self.event_dispatcher, 'pattern_image') and self.event_dispatcher.pattern_image:
            # Create thumbnail for display
            thumb = self.event_dispatcher.pattern_image.copy()
            thumb.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
            self.pattern_photo = image_to_tk_image(thumb)
            self.pattern_label.configure(image=self.pattern_photo)

    def _on_shown_image_changed(self):
        """Handle visual highlighting when shown image changes"""
        # TODO: Add highlighting logic if needed
        # For example, highlight the pattern preview when pattern is being shown
        shown_image = self.event_dispatcher.shown_image
        if shown_image == ShownImage.PATTERN:
            # Could add border or background color change here
            pass

class UvFocusFrame: # crosses
    def __init__(self, parent, event_dispatcher: EventDispatcher, show_uv_focus=False):
        self.frame = ttk.Frame(parent)
        self.event_dispatcher = event_dispatcher
        # UV focus frame - use predefined images - only shown in UV mode
        uv_focus_predefined = [("Plus Mark", "src/uvFocusImage/plus.png")]
        self.uv_focus_frame = ImageSelectFrame(
            self.frame,
            "UV Focus",
            self._on_uv_focus_change,
            # lambda t: event_dispatcher.set_uv_focus_image(self.uv_focus_image),
            # import_command in ImageSelectFrame --> on_select in PredefinedImageSelector
            predefined_images=uv_focus_predefined
        )
        self.uv_focus_frame.frame.grid(row=1, column=0, padx=5, pady=5)

        # event_dispatcher.add_event_listener(Event.PATTERN_IMAGE_CHANGED, self._update_pattern_display)
        event_dispatcher.add_event_listener(Event.SHOWN_IMAGE_CHANGED, self._on_shown_image_changed)

    def _on_uv_focus_change(self, _):
        """Handle UV focus image selection and projection"""
        if self.uv_focus_frame.thumb.image:
            self.event_dispatcher.set_uv_focus_image(self.uv_focus_frame.thumb.image)
            self.event_dispatcher.set_shown_image(ShownImage.UV_FOCUS)

    def _on_shown_image_changed(self):
        """Handle visual highlighting when shown image changes"""
        # TODO: Add highlighting logic if needed
        # For example, highlight the UV focus selector when UV focus is being shown
        shown_image = self.event_dispatcher.shown_image
        if shown_image == ShownImage.UV_FOCUS:
            # Could add border or background color change here
            pass

    @property
    def uv_focus_image(self):
        """Get the currently selected UV focus image"""
        return self.uv_focus_frame.thumb.image if hasattr(self.uv_focus_frame, 'thumb') else None

class ChipFrame:
    def __init__(self, parent, event_dispatcher: EventDispatcher):
        self.frame = ttk.Frame(parent)
        self.model = event_dispatcher
        self.path = StringVar()

        self.image_cache = dict()

        self.chip_select_frame = ttk.Frame(self.frame)
        self.chip_select_frame.grid(row=0, column=0)
        ttk.Label(self.chip_select_frame, text="Current Chip: ").grid(row=0, column=0)
        ttk.Label(self.chip_select_frame, textvariable=self.path).grid(row=0, column=1)

        def on_open():
            path = filedialog.askopenfilename(title="Open Chip")
            self.path.set(path)
            self.model.load_chip(path)

        def on_new():
            path = filedialog.asksaveasfilename(title="Create Chip As")
            self.path.set(path)
            self.model.new_chip()

        def on_save():
            path = filedialog.asksaveasfilename(title="Save As")
            if path != "":
                self.path.set(path)
                self.model.save_chip(self.path.get())

        def on_finish_layer():
            print("Layer finished!")
            self.model.add_chip_layer()
            self.model.save_chip(self.path.get())

        def on_delete_exposure():
            pair = self._selected_exposure()
            assert pair is not None
            yes = messagebox.askyesno(title="Delete Exposure", message="Are you sure you want to delete the selected exposure?")
            if yes:
                self.model.delete_chip_exposure(pair[0], pair[1])
                if self.path.get() != "":
                    self.model.save_chip(self.path.get())

        def on_select(e, cur):
            if cur and len(self.cur_layer_view.selection()) > 0:
                self.prev_layer_view.selection_set([])
                self.delete_exposure_button["state"] = "normal"
            elif not cur and len(self.prev_layer_view.selection()) > 0:
                self.cur_layer_view.selection_set([])
                self.delete_exposure_button["state"] = "normal"
            else:
                self.delete_exposure_button["state"] = "disabled"

        def on_double_click(cur):
            pair = self._selected_exposure()
            assert pair is not None
            x, y, z = self.model.chip.layers[pair[0]].exposures[pair[1]].coords
            self.model.move_absolute({"x": x, "y": y, "z": z})

        def on_chip_changed():
            if len(self.model.chip.layers) < 2:
                self.prev_layer_select.configure(state="disabled")
                self.prev_layer_select.configure(to=0)
            else:
                self.prev_layer_select.configure(state="readonly")
                self.prev_layer_select.configure(to=len(self.model.chip.layers) - 2)
                self.prev_layer_select_var.set(str(len(self.model.chip.layers) - 2))
            self.refresh_prev_layer()
            self.refresh_cur_layer()
            if self.path.get() != "":
                self.model.save_chip(self.path.get())
            if len(self.model.chip.layers[-1].exposures) > 0:
                self.finish_layer_button.configure(state="normal")
            else:
                self.finish_layer_button.configure(state="disabled")

        def prev_layer_index_changed(a, b, c):
            self.refresh_prev_layer()

        self.model.add_event_listener(Event.CHIP_CHANGED, on_chip_changed)

        _cbtn_grid = dict(padx=4, pady=4)
        _cbtn_w = 14
        self.open_chip_button = ttk.Button(self.chip_select_frame, text="Open",
                                           command=on_open, bootstyle="secondary-outline", width=_cbtn_w)
        self.open_chip_button.grid(row=0, column=2, **_cbtn_grid)
        self.new_chip_button = ttk.Button(self.chip_select_frame, text="New",
                                          command=on_new, bootstyle="secondary-outline", width=_cbtn_w)
        self.new_chip_button.grid(row=0, column=3, **_cbtn_grid)
        self.save_chip_button = ttk.Button(self.chip_select_frame, text="Save As",
                                           command=on_save, bootstyle="secondary-outline", width=_cbtn_w)
        self.save_chip_button.grid(row=0, column=4, **_cbtn_grid)
        self.finish_layer_button = ttk.Button(self.chip_select_frame, text="Finish Layer",
                                              command=on_finish_layer, bootstyle="primary-outline", width=_cbtn_w)
        self.finish_layer_button.grid(row=0, column=5, **_cbtn_grid)
        self.finish_layer_button.configure(state="disabled")
        self.delete_exposure_button = ttk.Button(
            self.chip_select_frame,
            text="Delete Exposure",
            command=on_delete_exposure,
            bootstyle="danger-outline",
            state="disabled",
            width=_cbtn_w,
        )
        self.delete_exposure_button.grid(row=0, column=6, **_cbtn_grid)

        self.layer_frame = ttk.Frame(self.frame)
        self.layer_frame.grid(row=1, column=0)

        self.prev_layer_frame = ttk.Labelframe(self.layer_frame, text="Previous Layer")
        self.prev_layer_frame.grid(row=0, column=0, sticky="ns")
        self.cur_layer_frame = ttk.Labelframe(self.layer_frame, text="Current Layer")
        self.cur_layer_frame.grid(row=0, column=1, sticky="ns")

        self.tree_view_style = ttk.Style()
        self.tree_view_style.configure("Treeview", rowheight=50)

        self.prev_layer_view = ttk.Treeview(self.prev_layer_frame, selectmode="browse", columns=("XYZ",), height=5)
        self.prev_layer_view.grid(row=0, column=0)
        self.prev_layer_view.bind("<<TreeviewSelect>>", lambda e: on_select(e, cur=False))
        self.prev_layer_view.bind("<Double-1>", lambda e: on_double_click(cur=False))
        self.cur_layer_view = ttk.Treeview(self.cur_layer_frame, selectmode="browse", columns=("XYZ",), height=5)
        self.cur_layer_view.grid(row=0, column=0)
        self.cur_layer_view.bind("<<TreeviewSelect>>", lambda e: on_select(e, cur=True))
        self.cur_layer_view.bind("<Double-1>", lambda e: on_double_click(cur=True))

        select_frame = ttk.Frame(self.prev_layer_frame)
        select_frame.grid(row=1, column=0)

        prev_layer_select_label = ttk.Label(select_frame, text="Select previous layer:")
        prev_layer_select_label.grid(row=0, column=0)

        self.prev_layer_select_var = StringVar()
        self.prev_layer_select_var.trace_add("write", prev_layer_index_changed)
        self.prev_layer_select = ttk.Spinbox(select_frame, from_=0, to=0, textvariable=self.prev_layer_select_var)
        self.prev_layer_select.configure(state="disabled")
        self.prev_layer_select.grid(row=0, column=1)

    def _selected_exposure(self):
        cur_sel = self.cur_layer_view.selection()
        if len(cur_sel) > 0:
            layer_idx, ex_idx = cur_sel[0].split("_")
            return int(layer_idx), int(ex_idx)
        prev_sel = self.prev_layer_view.selection()
        if len(prev_sel) > 0:
            layer_idx, ex_idx = prev_sel[0].split("_")
            return int(layer_idx), int(ex_idx)
        return None

    def _get_thumbnail(self, path: str):
        try:
            return self.image_cache[path][1]
        except KeyError:
            img = Image.open(path).resize((80, 45))
            photo = image_to_tk_image(img)
            self.image_cache[path] = (img, photo)
            return photo

    def refresh_prev_layer(self):
        chip = self.model.chip

        for item in self.prev_layer_view.get_children():
            self.prev_layer_view.delete(item)

        try:
            idx = int(self.prev_layer_select_var.get())
        except ValueError:
            print(f"Leaving previous layer empty because select var is {self.prev_layer_select_var.get()!r}")
            return

        if len(chip.layers) < 2:
            return

        for i, ex in enumerate(chip.layers[idx].exposures):
            ex_id = f"{idx}_{i}"
            pos = f"{ex.coords[0]},{ex.coords[1]},{ex.coords[2]}"
            self.prev_layer_view.insert("", "end", ex_id, image=self._get_thumbnail(ex.path), values=(pos,))

    def refresh_cur_layer(self):
        chip = self.model.chip

        for item in self.cur_layer_view.get_children():
            self.cur_layer_view.delete(item)

        for i, ex in enumerate(chip.layers[-1].exposures):
            ex_id = f"{len(chip.layers) - 1}_{i}"
            pos = f"{ex.coords[0]},{ex.coords[1]},{ex.coords[2]}"
            self.cur_layer_view.insert("", "end", ex_id, image=self._get_thumbnail(ex.path), values=(pos,))


class ExposureFrame:
    def __init__(self, parent, event_dispatcher: EventDispatcher):
        self.frame = ttk.Frame(parent)
        self.frame.columnconfigure(0, weight=1)
        ttk.Label(self.frame, text="Exposure Time (ms)", anchor="w").grid(row=0, column=0)
        self.exposure_time_entry = IntEntry(self.frame, default=8000, min_value=0)
        self.exposure_time_entry.widget.grid(row=0, column=1, columnspan=2, sticky="nesw")

        def on_exposure_time_change(_a, _b, _c):
            try:
                event_dispatcher.exposure_time = self.exposure_time_entry._var.get()
            except tkinter.TclError:
                event_dispatcher.exposure_time = None

        self.exposure_time_entry._var.trace_add("write", on_exposure_time_change)

        # Posterization
        def on_posterize_change(*args):
            event_dispatcher.set_posterize_strength(self._posterize_strength())

        def on_posterize_check():
            if self.posterize_enable_var.get():
                self.posterize_scale["state"] = "normal"
                self.posterize_cutoff_entry.widget["state"] = "normal"
            else:
                self.posterize_scale["state"] = "disabled"
                self.posterize_cutoff_entry.widget["state"] = "disabled"
            on_posterize_change()

        self.posterize_enable_var = BooleanVar()
        self.posterize_checkbutton = ttk.Checkbutton(
            self.frame,
            text="Posterize Cutoff (%)",
            command=on_posterize_check,
            variable=self.posterize_enable_var,
            onvalue=True,
            offvalue=False,
        )
        self.posterize_checkbutton.grid(row=2, column=0)
        self.posterize_strength_var = IntVar()
        self.posterize_strength_var.trace_add("write", on_posterize_change)
        self.posterize_scale = ttk.Scale(self.frame, variable=self.posterize_strength_var, from_=0.0, to=100.0)
        self.posterize_scale.grid(row=2, column=1, sticky="nesw")
        self.posterize_scale["state"] = "disabled"
        self.posterize_cutoff_entry = IntEntry(
            self.frame,
            var=self.posterize_strength_var,
            default=50,
            min_value=0,
            max_value=100,
        )
        self.posterize_cutoff_entry.widget.grid(row=2, column=2, sticky="nesw")
        self.posterize_cutoff_entry.widget["state"] = "disabled"

    # returns threshold percentage if posterizing is enabled, else None
    def _posterize_strength(self) -> Optional[int]:
        if self.posterize_enable_var.get():
            return self.posterize_cutoff_entry.get()
        else:
            return None


class PatterningFrame:
    def __init__(self, parent, event_dispatcher: EventDispatcher):
        self.frame = ttk.Frame(parent)

        self.preview_tile = ttk.Label(self.frame, text="Next Pattern Tile", compound="top")  # type:ignore
        self.preview_tile.grid(row=0, column=0)

        self.begin_patterning_button = ttk.Button(
            self.frame,
            text="Start exposure",
            command=lambda: event_dispatcher.begin_patterning(),
            bootstyle="success",
            state="normal",
        )
        self.begin_patterning_button.grid(row=1, column=0, sticky="ew", padx=6, pady=(4, 2))

        self.abort_patterning_button = ttk.Button(
            self.frame,
            text="Stop exposure",
            command=lambda: event_dispatcher.abort_patterning(),
            bootstyle="danger",
            state="disabled",
        )
        self.abort_patterning_button.grid(row=2, column=0, sticky="ew", padx=6, pady=(2, 4))

        ttk.Label(self.frame, text="Exposure Progress", anchor="s").grid(row=3, column=0)
        self.exposure_progress = ttk.Progressbar(self.frame, orient="horizontal", mode="determinate", maximum=1000, bootstyle="success")
        self.exposure_progress.grid(row=4, column=0, sticky="ew")

        self.set_image(Image.new("RGB", (1, 1)))

        def on_change_patterning_status():
            if event_dispatcher.patterning_busy:
                self.begin_patterning_button["state"] = "disabled"
                self.abort_patterning_button["state"] = "normal"
            else:
                self.begin_patterning_button["state"] = "normal"
                self.abort_patterning_button["state"] = "disabled"

        event_dispatcher.add_event_listener(Event.PATTERNING_BUSY_CHANGED, on_change_patterning_status)
        event_dispatcher.add_event_listener(Event.PATTERN_IMAGE_CHANGED, lambda: self.set_image(event_dispatcher.pattern.processed()))

        def on_progress_changed():
            self.exposure_progress["value"] = (event_dispatcher.exposure_progress * 1000.0)

        event_dispatcher.add_event_listener(Event.EXPOSURE_PATTERN_PROGRESS_CHANGED, on_progress_changed)

    def set_image(self, img: Image.Image):
        # TODO: What is the correct size?
        self.thumb_image = image_to_tk_image(img.resize(THUMBNAIL_SIZE))
        self.preview_tile.configure(image=self.thumb_image)  # type:ignore


class RedModeFrame:
    def __init__(self, parent, event_dispatcher: EventDispatcher):
        self.frame = ttk.Frame(parent, name="redmodeframe", padding=20)
        self.event_dispatcher = event_dispatcher

        # Create left middle right sections
        self.left_frame = ttk.Frame(self.frame)
        self.left_frame.grid(row=0, column=0)

        self.middle_frame = ttk.Frame(self.frame)
        self.middle_frame.grid(row=0, column=1)

        self.right_frame = ttk.Frame(self.frame)
        self.right_frame.grid(row=0, column=2)

        # center the frames
        self.frame.grid_columnconfigure(0, weight=1)
        self.frame.grid_columnconfigure(1, weight=0)
        self.frame.grid_columnconfigure(2, weight=1)
        self.frame.grid_rowconfigure(0, weight=1)

        # Stage position controls (middle)
        self.stage_position_frame = StagePositionFrame(self.middle_frame, event_dispatcher, False)
        self.stage_position_frame.frame.grid(row=0, column=0)

        # test tiling check button & preview
        # self.tiling_check_frame  = TilingCheckFrame(self.middle_frame, event_dispatcher)
        # self.tiling_check_frame.frame.grid(row=0, column = 1)

        # Pattern preview display (right side)
        self.pattern_display = PatternDisplayFrame(self.right_frame, event_dispatcher)
        self.pattern_display.frame.grid(row=0, column=0)

        # Red focus image selection (left)
        self.red_select_var = StringVar(value="focus")
        self.red_focus_rb = ttk.Radiobutton(
            self.left_frame,
            variable=self.red_select_var,
            text="Red Focus",
            value=RedFocusSource.IMAGE.value,
        )
        self.solid_red_rb = ttk.Radiobutton(
            self.left_frame,
            variable=self.red_select_var,
            text="Solid Red",
            value=RedFocusSource.SOLID.value,
        )
        self.pattern_rb = ttk.Radiobutton(
            self.left_frame,
            variable=self.red_select_var,
            text="Same as Pattern",
            value=RedFocusSource.PATTERN.value,
        )
        self.inv_pattern_rb = ttk.Radiobutton(
            self.left_frame,
            variable=self.red_select_var,
            text="Inverse of Pattern",
            value=RedFocusSource.INV_PATTERN.value,
        )

        self.solid_red_rb.grid(row=0, column=0)
        self.pattern_rb.grid(row=1, column=0)
        self.inv_pattern_rb.grid(row=2, column=0)
        self.red_focus_rb.grid(row=3, column=0)

        self.red_focus_frame = ImageSelectFrame(
            self.left_frame,
            "Red Focus",
            lambda t: event_dispatcher.set_red_focus_image(self.red_focus_image()),
        )
        self.red_focus_frame.frame.grid(row=5, column=0)

        def on_radiobutton(*_):
            print(f"red select var {self.red_select_var.get()}")
            for s in RedFocusSource:
                if s.value == self.red_select_var.get():
                    event_dispatcher.set_red_focus_source(s)
                    break
            else:
                raise Exception()

        self.red_select_var.trace_add("write", on_radiobutton)

    def red_focus_image(self):
        return self.red_focus_frame.thumb.image


class UvModeFrame:
    def __init__(self, parent, event_dispatcher):
        self.frame = ttk.Frame(parent, name="uvmodeframe", padding=20)
        # Create left and right sections
        self.left_frame = ttk.Frame(self.frame)
        self.left_frame.grid(row=0, column=0, sticky="ns", padx=(0,10))

        self.middle_frame = ttk.Frame(self.frame)
        self.middle_frame.grid(row=0, column=1, sticky="ns")

        self.right_frame = ttk.Frame(self.frame)
        self.right_frame.grid(row=0, column=2, sticky="ns")

        # center the frames
        self.frame.grid_columnconfigure(0, weight=1)
        self.frame.grid_columnconfigure(1, weight=0)
        self.frame.grid_columnconfigure(2, weight=1)
        self.frame.grid_rowconfigure(0, weight=1)

        # Predefined UV focus image selection
        self.uv_focus_frame = UvFocusFrame(self.left_frame, event_dispatcher)
        self.uv_focus_frame.frame.grid(row=0, column=0, pady=(10,0))

        # Stage position controls (middle)
        self.stage_position_frame = StagePositionFrame(self.middle_frame, event_dispatcher, True)
        self.stage_position_frame.frame.grid(row=0, column=0, sticky="n")

        # Pattern preview and UV focus selector (right side)
        # self.pattern_display = PatternDisplayFrame(self.right_frame, event_dispatcher)
        # self.pattern_display.frame.grid(row=0, column=0)

        # Exposure and patterning controls (right side, below images)
        self.exposure_frame = ExposureFrame(self.right_frame, event_dispatcher)
        self.exposure_frame.frame.grid(row=0, column=0)
        self.patterning_frame = PatterningFrame(self.right_frame, event_dispatcher)
        self.patterning_frame.frame.grid(row=1, column=0)

class PatternUploadFrame:
    def __init__(self, parent, event_dispatcher: EventDispatcher):
        self.frame = ttk.Frame(parent, padding=24)
        self.event_dispatcher = event_dispatcher
        details = ttk.Frame(self.frame)
        details.grid(row=0, column=0, sticky="nw", padx=(0, 32))
        ttk.Label(details, text="Pattern image", font="StepperSection").pack(anchor="w", pady=(0, 8))
        ttk.Label(details, text="Choose the image you want to project. You can check its alignment before starting an exposure.", wraplength=340, bootstyle="secondary").pack(anchor="w", pady=(0, 20))
        self.choose_button = ttk.Button(details, text="Choose image…", command=self.choose_image)
        self.choose_button.pack(anchor="w")
        self.pattern_path_var = StringVar(value="No image selected")
        ttk.Label(details, textvariable=self.pattern_path_var, wraplength=340, bootstyle="secondary").pack(anchor="w", pady=(16, 0))
        self.preview_photo = image_to_tk_image(Image.new('RGB', (380, 220), '#e7ece8'))
        self.preview_label = ttk.Label(self.frame, image=self.preview_photo, text="Your pattern preview", compound="center", anchor="center")
        self.preview_label.grid(row=0, column=1, sticky="n")
        self.frame.columnconfigure(1, weight=1)
        event_dispatcher.add_event_listener(Event.PATTERN_IMAGE_CHANGED, self.refresh)
        event_dispatcher.add_event_listener(Event.PATTERNING_BUSY_CHANGED, lambda: self.choose_button.configure(state="disabled" if event_dispatcher.patterning_busy else "normal"))

    def choose_image(self):
        if self.event_dispatcher.patterning_busy:
            return
        path = filedialog.askopenfilename(title="Choose a pattern image", filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff"), ("All files", "*.*")])
        if not path:
            return
        try:
            with Image.open(path) as source:
                image = ImageOps.exif_transpose(source).convert('RGB')
            self.event_dispatcher.set_pattern_image(image, path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Could not open image", str(exc))

    def refresh(self):
        image = self.event_dispatcher.pattern_image
        self.pattern_path_var.set(f"{Path(self.event_dispatcher.pattern_image_path).name}\n{image.width} × {image.height} pixels")
        preview = image.copy()
        preview.thumbnail((380, 260), Image.Resampling.LANCZOS)
        self.preview_photo = image_to_tk_image(preview)
        self.preview_label.configure(image=self.preview_photo, text="")

class ModeSelectFrame:
    def __init__(self, parent, event_dispatcher: EventDispatcher):
        self.notebook = ttk.Notebook(parent)

        # Add Pattern Upload tab first
        self.pattern_upload_frame = PatternUploadFrame(self.notebook, event_dispatcher)
        self.notebook.add(self.pattern_upload_frame.frame, text="Pattern")
        self.red_mode_frame = RedModeFrame(self.notebook, event_dispatcher)
        self.notebook.add(self.red_mode_frame.frame, text="Focus & align")
        self.uv_mode_frame = UvModeFrame(self.notebook, event_dispatcher)
        self.notebook.add(self.uv_mode_frame.frame, text="Expose")

        def on_tab_change():
            if event_dispatcher.patterning_busy or event_dispatcher.autofocus_busy:
                return
            current_tab = self._current_tab()
            if current_tab == "uv":
                event_dispatcher.enter_uv_mode()
            elif current_tab == "red":
                event_dispatcher.enter_red_mode()

        self.notebook.bind("<<NotebookTabChanged>>", lambda _: on_tab_change())
        def update_tab_lock():
            locked = event_dispatcher.patterning_busy or event_dispatcher.autofocus_busy
            selected = self.notebook.select()
            for tab in self.notebook.tabs():
                self.notebook.tab(tab, state="disabled" if locked and tab != selected else "normal")
        event_dispatcher.add_event_listener(Event.MOVEMENT_LOCK_CHANGED, update_tab_lock)

        # def on_tab_event(evt):
        #  self.notebook.select(1 if evt == Event.EnterUvMode else 0)

        # event_dispatcher.add_event_listener(Event.EnterRedMode, lambda: on_tab_event(Event.EnterRedMode))
        # event_dispatcher.add_event_listener(Event.EnterUvMode, lambda: on_tab_event(Event.EnterUvMode))

    def _current_tab(self):
        selected = self.notebook.select()
        if not selected:
            return None
        if "patternupload" in selected.lower() or self.notebook.index("current") == 0:
            return "pattern"
        elif "redmode" in selected or self.notebook.index("current") == 1:
            return "red" 
        else:
            return "uv"

class GlobalSettingsFrame:
    def __init__(self, parent, event_dispatcher: EventDispatcher, enable_detection: bool = False):
        self.frame = ttk.Labelframe(parent, text="Global Settings")

        def set_autofocus_on_mode_switch(*_):
            event_dispatcher.autofocus_on_mode_switch = self.autofocus_on_mode_switch_var.get()

        self.autofocus_on_mode_switch_var = BooleanVar(value=False)
        self.autofocus_on_mode_switch_check = ttk.Checkbutton(
            self.frame,
            text="Autofocus on Mode Change",
            variable=self.autofocus_on_mode_switch_var,
        )
        self.autofocus_on_mode_switch_check.grid(row=0, column=0, columnspan=2)
        self.autofocus_on_mode_switch_var.trace_add("write", set_autofocus_on_mode_switch)

        def set_realtime_detection(*_):
            event_dispatcher.realtime_detection = self.realtime_detection_var.get()

        self.realtime_detection_var = BooleanVar(value=enable_detection)
        self.realtime_detection_check = ttk.Checkbutton(
            self.frame,
            text="Detect alignment markers in real time",
            variable=self.realtime_detection_var,
            state="disabled" if event_dispatcher.model is None else "normal",
        )
        self.realtime_detection_check.grid(row=1, column=0, columnspan=2)
        self.realtime_detection_var.trace_add("write", set_realtime_detection)

        def do_align():
            if event_dispatcher.camera_image is None or event_dispatcher.model is None:
                messagebox.showinfo("Alignment unavailable", "Connect a live camera and load an alignment model first.")
                return
            h, w, _ = event_dispatcher.camera_image.shape
            markers, _ = detect_alignment_markers(event_dispatcher.model, event_dispatcher.camera_image)
            dx, dy = 0, 0
            if len(markers) == 0:
                return

            # Get alignment parameters from config
            alignment = event_dispatcher.config.alignment

            for m in markers:
                xy0, xy1 = m
                x0, y0 = xy0
                x1, y1 = xy1
                # compute normalized centers of the bounding box
                x = (x0 + x1) / 2 / w
                y = (y0 + y1) / 2 / h

                if x > 0.5:
                    dx += alignment.x_scale_factor * (alignment.right_marker_x / w - x)
                else:
                    dx += alignment.x_scale_factor * (alignment.left_marker_x / w - x)
                if y > 0.5:
                    dy += alignment.y_scale_factor * (alignment.bottom_marker_y / h - y)
                else:
                    dy += alignment.y_scale_factor * (alignment.top_marker_y / h - y)

            dx /= len(markers)
            dy /= len(markers)
            event_dispatcher.move_relative({ 'x': dx, 'y': dy })

            print(markers)

        self.alignbutton = ttk.Button(
            self.frame,
            text="Auto-Align",
            command=do_align,
            state="disabled" if event_dispatcher.model is None else "normal",
            bootstyle="primary-outline",
            width=12,
        )
        self.alignbutton.grid(row=2, column=1, padx=4, pady=4, sticky="ew")

        self.autofocus_button = ttk.Button(
            self.frame, text="Autofocus",
            command=lambda: event_dispatcher.autofocus(blue_only=event_dispatcher.in_uv()),
            bootstyle="secondary-outline",
            width=12,
        )
        self.autofocus_button.grid(row=2, column=0, padx=4, pady=4, sticky="ew")

        # Maybe this should have a scale?
        # Or, even further, maybe this should just be the same as the interface for posterization strength?
        self.border_size_var = IntVar()
        self.border_label = ttk.Label(self.frame, text="Border Size (%)")
        self.border_label.grid(row=3, column=0)
        self.border_entry = IntEntry(self.frame, var=self.border_size_var, default=0, min_value=0, max_value=100)
        self.border_entry.widget.grid(row=3, column=1, sticky="nesw")

        def on_border_size_change(*_):
            event_dispatcher.set_border_size(self.border_size_var.get())

        self.border_size_var.trace_add("write", on_border_size_change)

        self.placeholder_photo = image_to_tk_image(Image.new("RGB", THUMBNAIL_SIZE, "black"))
        self.photo = None

        ttk.Label(self.frame, text="Current Pattern", anchor="center").grid(
            row=4, column=0, columnspan=2, pady=(6, 2))
        self.current_image = ttk.Label(self.frame, image=self.placeholder_photo)  # type:ignore
        self.current_image.grid(row=5, column=0, columnspan=2, pady=(0, 4))

        # Disable the autofocus button if autofocus is already running
        def movement_lock_changed():
            if event_dispatcher.movement_lock == MovementLock.LOCKED:
                self.autofocus_button.configure(state="disabled")
            else:
                self.autofocus_button.configure(state="normal")

        event_dispatcher.add_event_listener(Event.MOVEMENT_LOCK_CHANGED, movement_lock_changed)

        def shown_image_changed():
            img = event_dispatcher.current_image
            if img is None:
                self.current_image.configure(image=self.placeholder_photo)  # type:ignore
            else:
                photo = image_to_tk_image(img.resize(THUMBNAIL_SIZE, Image.Resampling.NEAREST))
                self.current_image.configure(image=photo)  # type:ignore
                self.photo = photo

        event_dispatcher.add_event_listener(Event.SHOWN_IMAGE_CHANGED, shown_image_changed)

        self.snapshot_frame = ttk.Labelframe(self.frame, text="Snapshot Settings")
        self.snapshot_frame.grid(row=6, column=0, columnspan=2, sticky="ew", pady=5)

        self.auto_snapshot_var = BooleanVar(value=event_dispatcher.auto_snapshot_on_uv)
        self.auto_snapshot_check = ttk.Checkbutton(
            self.snapshot_frame,
            text="Auto-save snapshot on UV mode entry",
            variable=self.auto_snapshot_var,
        )
        self.auto_snapshot_check.grid(row=0, column=0, columnspan=2)

        def on_auto_snapshot_change(*_):
            event_dispatcher.auto_snapshot_on_uv = self.auto_snapshot_var.get()

        self.auto_snapshot_var.trace_add("write", on_auto_snapshot_change)

        # Directory selection
        ttk.Label(self.snapshot_frame, text="Save Directory:").grid(row=1, column=0)
        self.directory_var = StringVar(value=str(event_dispatcher.snapshot_directory))
        self.directory_entry = ttk.Entry(self.snapshot_frame, textvariable=self.directory_var, state="readonly")
        self.directory_entry.grid(row=1, column=1, sticky="ew")

        def choose_directory():
            dir_path = filedialog.askdirectory(
                initialdir=self.directory_var.get(),
                title="Select Snapshot Save Directory",
            )
            if dir_path:  # User didn't cancel
                event_dispatcher.set_snapshot_directory(Path(dir_path))
                self.directory_var.set(dir_path)

        self.choose_dir_button = ttk.Button(self.snapshot_frame, text="Choose Directory", command=choose_directory)
        self.choose_dir_button.grid(row=2, column=0, columnspan=2, sticky="ew")

        # Configure grid weights for proper expansion
        self.snapshot_frame.columnconfigure(1, weight=1)


class ExposureHistoryFrame:
    def __init__(self, parent, event_dispatcher: EventDispatcher):
        self.frame = ttk.Labelframe(parent, text="Exposure History")
        self.text = tkinter.Text(self.frame, width=80, height=10, wrap="none", state="disabled")
        self.text.grid(row=0, column=0)
        self.event_dispatcher = event_dispatcher
        event_dispatcher.add_event_listener(Event.PATTERNING_BUSY_CHANGED, lambda: self._refresh())

    def _refresh(self):
        self.text["state"] = "normal"
        self.text.delete("1.0", "end")
        for exp_log in self.event_dispatcher.exposure_history[-10:]:
            t = exp_log.time.strftime("%H:%M:%S")
            line = f"{t} {exp_log.path} {int(exp_log.duration)}ms X:{exp_log.coords[0]} Y:{exp_log.coords[1]} Z:{exp_log.coords[2]}\n"

            if self.text.index("end-1c") != 1.0:
                self.text.insert("end", "\n")
            self.text.insert("end", line)
        self.text["state"] = "disabled"


class OffsetAmountFrame:
    def __init__(self, parent, label, default_offset):
        self.frame = ttk.Labelframe(parent, text=label)

        offset_label = ttk.Label(self.frame, text="Offset (µm)")
        offset_label.grid(row=0, column=0)
        self.offset_var = StringVar(value=str(default_offset))
        self.offset_entry = ttk.Entry(self.frame, textvariable=self.offset_var, width=5)
        self.offset_entry.grid(row=0, column=1)
        amount_label = ttk.Label(self.frame, text="Amount")
        amount_label.grid(row=0, column=2)
        self.amount_var = StringVar(value="1")
        self.amount_spinbox = ttk.Spinbox(self.frame, from_=-20, to=20, textvariable=self.amount_var, width=3)
        self.amount_spinbox.grid(row=0, column=3)

class TilingFrame:
    def __init__(self, parent, model: EventDispatcher):
        self.frame = ttk.Labelframe(parent, text="Tiling")
        self.model = model

        self.red_to_uv_offset = -40

        self.overall_pattern_size_w = 0
        self.overall_pattern_size_h = 0

        #Defaults set based on DLP471TP and a 10x objective
        #5.4 um Pixel Pitch
        #Width 10.368 mm
        #Height 5.832 mm
        # Move in X = 10.368mm / 10 = 1037um
        # Move in Y = 5.832 mm / 10 = 538.2 um ~ 539 um
        #Subtraction amounts are there to tune the offset for the alignment markers
        self.x_settings = OffsetAmountFrame(self.frame, "X", 1037-54) #Move amount between exposures in X
        self.y_settings = OffsetAmountFrame(self.frame, "Y", 539-27)  #Move amount between exposures in y

        #Tiling verisons of alignment
        def detect_alignment_markers_tiling(yolo_model, image, draw_rectangle=False, edge=None, edge_fraction=0.25):
            #Detects alignment markers and optionally filters detections by image edge(s).
            #yolo_model: YOLO model
            #image: image to detect on 
            #draw_rectangle: If True, draw rectangles
            #edge: 'left', 'right', 'top', or a list like ['left', 'right'] where markers are expected
                                                    #none means that markers are expect on all edges
            #edge_fraction: Fraction of width/height considered as edge region

            detections = []
            display_image = image.copy()
            try:
                image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
                original_height, original_width = image_rgb.shape[:2]
                resized = cv2.resize(image_rgb, (640, 640))
                results = yolo_model(resized)
                boxes = results[0].boxes

                if isinstance(edge, str):
                    edge = [edge]  # allow single string or list

                for box in boxes:
                    x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                    x1 = int(x1 * original_width / 640)
                    x2 = int(x2 * original_width / 640)
                    y1 = int(y1 * original_height / 640)
                    y2 = int(y2 * original_height / 640)
                    x_center = (x1 + x2) / 2
                    y_center = (y1 + y2) / 2

                    # If edge filtering is enabled
                    if edge is not None:
                        if 'left' in edge and x_center > original_width * edge_fraction:
                            continue
                        if 'right' in edge and x_center < original_width * (1 - edge_fraction):
                            continue
                        if 'top' in edge and y_center > original_height * edge_fraction:
                            continue

                    detections.append(((x1, y1), (x2, y2)))
                    if draw_rectangle:
                        cv2.rectangle(display_image, (x1, y1), (x2, y2), (0, 255, 0), 3)

                print(f"Detected {len(detections)} marker(s)")
            except Exception as e:
                print(f"Detection failed: {e}")

            return detections, display_image

        def do_align_tiling(edge):
            if model.camera_image is None or model.model is None:
                messagebox.showinfo("Alignment unavailable", "Connect a live camera and load an alignment model first.")
                return
            #edge = ['left', 'right', 'top']
            h, w, _ = model.camera_image.shape

            # Detect markers on the left, right, and top edges
            markers, _ = detect_alignment_markers_tiling(model.model, model.camera_image, edge)
            if len(markers) == 0:
                print("No markers detected.")
                return

            alignment = model.config.alignment
            dx, dy = 0.0, 0.0
            count_x, count_y = 0, 0

            for m in markers:
                xy0, xy1 = m
                x0, y0 = xy0
                x1, y1 = xy1
                x = (x0 + x1) / 2 / w
                y = (y0 + y1) / 2 / h

                # Horizontal alignment (left/right markers)
                if x < 0.5:
                    dx += alignment.x_scale_factor * (alignment.left_marker_x / w - x)
                    count_x += 1
                elif x > 0.5:
                    dx += alignment.x_scale_factor * (alignment.right_marker_x / w - x)
                    count_x += 1

                # Vertical alignment (top markers only)
                if y < 0.3:  # top region
                    dy += alignment.y_scale_factor * (alignment.top_marker_y / h - y)
                    count_y += 1

            # Average corrections based on detected edges
            if count_x > 0:
                dx /= count_x
            if count_y > 0:
                dy /= count_y

            # Move accordingly (if no top markers, dy=0)
            #If a small amount of alignment is needed move the image otherwise move the stage since we have far more percision in moving the image than the stage
            #The con of this is that large movements of the image result in cropping of the image
            #TODO calibrate the stage move threshold
            if(dx or dy < 10):
                #move the image instead of the stage
                model.set_image_position(dx, dy, t=0)
            else:
              model.move_relative({'x': dx, 'y': dy})
              print(f"Alignment correction: dx={dx:.5f}, dy={dy:.5f} using {len(markers)} markers.")

        #function that takes in an arbitrary sized image composed of 3840x2160 tiles
        #with shared alignment marks that are 200 pixels from the edge
        def split_image_with_overlap(image_path, 
                                          tile_width=3840, 
                                          tile_height=2160, 
                                          overlap_x=200, 
                                          overlap_y=200, 
                                          output_dir="tiles"):
            img = Image.open(image_path)
            img_w, img_h = img.size
            self.overall_pattern_size_w = img_w
            self.overall_pattern_size_h = img_h
            os.makedirs(output_dir, exist_ok=True)

            stride_x = tile_width - overlap_x
            stride_y = tile_height - overlap_y

            # Compute all top-left coordinates
            x_positions = []
            y_positions = []

            # Horizontal positions
            x = 0
            while True:
                if x + tile_width >= img_w:
                    x = max(0, img_w - tile_width)
                    x_positions.append(x)
                    break
                x_positions.append(x)
                x += stride_x

            # Vertical positions
            y = 0
            while True:
                if y + tile_height >= img_h:
                    y = max(0, img_h - tile_height)
                    y_positions.append(y)
                    break
                y_positions.append(y)
                y += stride_y

            tile_count = 0
            #Set amount of tiles for later use when exposing
            self.x_settings.amount_var = len(x_positions)
            self.y_settings.amount_var = len(y_positions)
            #Crop and Save the tile images
            for tile_id_y, top in enumerate(y_positions):
                for tile_id_x, left in enumerate(x_positions):
                    right = left + tile_width
                    bottom = top + tile_height

                    box = (left, top, right, bottom)
                    tile = img.crop(box)
                    tile.save(os.path.join(output_dir, f"tile_{tile_id_y},{tile_id_x}.png"))
                    tile_count += 1

            print("X amount = "+str(self.x_settings.amount_var))
            print("Y amount = "+str(self.y_settings.amount_var))
            print(f"Saved {tile_count} tiles to {output_dir}")

        #function that patterns a single tile
        def pattern_for_tile(self, model, x_start, x_dir, x_idx, x_offset, y_start, y_dir, y_idx, y_offset, y_idx_max, x_idx_max, tile_dir="tiles"):
            #change image
            image_path = tile_dir+"/tile_"+str(y_idx)+","+str(x_idx)+".png"
            current_tile = Image.open(image_path)
            model.set_pattern_image(current_tile, image_path)
            #move to the next position if not the first tile
            #the first tile is exposed where the operator(user of the stepper) places it
            if(~(x_idx == 0 & y_idx == 0)):
                self.model.move_absolute(
                    {
                        "x": x_start + x_dir * x_idx * x_offset,
                        "y": y_start + y_dir * y_idx * y_offset,
                    }
                )
            #Red autofocus
            self.model.autofocus(blue_only=False)

            #align to previous alignment marks if not first tile
            if(~(x_idx == 0 & y_idx == 0)):
                if(x_idx !=0 & x_idx!=x_idx_max):
                    if(y_idx % 2 == 0):
                        do_align_tiling('left')
                    else:
                        do_align_tiling('right')
                else:
                    do_align_tiling('top')



            #Do automatic offset for UV then autofocus
            self.model.move_relative({"z": self.red_to_uv_offset})
            self.model.non_blocking_delay(0.5)
            self.model.enter_uv_mode(mode_switch_autofocus=False)
            self.model.autofocus(blue_only=True)

            #expose the image
            self.model.begin_patterning()

            #TODO Add second exposure of the alignment markers
            # I tried doing this with a non blocking delay but didnt have success
            # I think that loading a pattern of the alignment marks that is hardcoded into the software might be the best bet

            #Offset back to red mode
            self.model.enter_red_mode(mode_switch_autofocus=False)
            self.model.move_relative({"z": -1 * self.red_to_uv_offset})



        def segment():
            #create tile directory and segment images
            split_image_with_overlap(model.pattern_image_path)
            #load the first tile for operator placement
            model.set_red_focus_source(RedFocusSource.PATTERN)
            image_path = "tiles/tile_"+str(0)+","+str(0)+".png"
            current_tile = Image.open(image_path)
            model.set_pattern_image(current_tile, image_path)


        def on_begin():
            model.set_red_focus_source(RedFocusSource.PATTERN)

            x_amount = self.x_settings.amount_var
            x_offset = int(self.x_settings.offset_var.get())
            x_dir = 1 if x_amount > 0 else -1
            x_amount = abs(x_amount)

            y_amount = self.y_settings.amount_var
            y_offset = int(self.y_settings.offset_var.get())
            y_dir = 1 if y_amount > 0 else -1
            y_amount = abs(y_amount)

            x_start, y_start = self.model.stage_setpoint[0], self.model.stage_setpoint[1]

            #Move in Snake pattern with left to right on even rows and right to left on odd rows
            for y_idx in range(y_amount):
                if(y_idx %2 == 0):
                  for x_idx in range(x_amount):
                      pattern_for_tile(self, model, x_start, -x_dir, x_idx, x_offset, y_start, -y_dir, y_idx, y_offset, y_idx_max=y_amount, x_idx_max=x_amount)
                      print("Patterned x_idx:" + str(x_idx) + " y_idx: "+str(y_idx))
                else:
                    for x_idx in range(x_amount - 1, -1, -1):
                      pattern_for_tile(self, model, x_start, -x_dir, x_idx, x_offset, y_start, -y_dir, y_idx, y_offset, y_idx_max=y_amount, x_idx_max=x_amount)
                      print("Patterned x_idx:" + str(x_idx) + " y_idx: "+str(y_idx))

        #TODO IMPLEMENT ABORT
        def on_abort():
            pass

        #Segment Images must be done before begining tiling
        #TODO enforce above
        #Tiling check must be done before segment images if needed
        self.tiling_check_button = TilingCheckFrame(self.frame, model)
        self.tiling_check_button.frame.grid(row=0, column = 0)
        _tbtn = dict(sticky="ew", padx=6, pady=3)
        self.segment_images_button = ttk.Button(self.frame, text="Segment Images",
                                                command=segment, bootstyle="secondary-outline", width=16)
        self.segment_images_button.grid(row=1, column=0, **_tbtn)
        self.begin_tiling_button = ttk.Button(self.frame, text="▶  Begin Tiling",
                                              command=on_begin, bootstyle="success", width=16)
        self.begin_tiling_button.grid(row=2, column=0, **_tbtn)
        self.abort_tiling_button = ttk.Button(self.frame, text="■  Abort Tiling",
                                              command=on_abort, bootstyle="danger", width=16, state="disabled")
        self.abort_tiling_button.grid(row=3, column=0, **_tbtn)


class ProjectorDisplayFrame:
    """Frame to display what the projector is currently showing"""

    def __init__(self, parent, event_dispatcher: EventDispatcher):
        self.frame = ttk.Frame(parent)
        self.event_dispatcher = event_dispatcher

        # Main label frame
        self.display_frame = ttk.Labelframe(self.frame, text="Projector Output")
        self.display_frame.grid(row=0, column=0)

        # Create placeholder image
        # Using a similar size to camera preview for consistency
        self.display_size = (320, 180)
        placeholder = Image.new("RGB", self.display_size, "black")
        self.photo = image_to_tk_image(placeholder)

        # Display label
        self.label = ttk.Label(self.display_frame, image=self.photo, relief="solid", borderwidth=2)
        self.label.grid(row=0, column=0, padx=5, pady=5)

        # Status label showing current mode
        self.status_var = StringVar(value="Clear")
        self.status_label = ttk.Label(self.display_frame, textvariable=self.status_var,
                                      bootstyle="secondary")
        self.status_label.grid(row=1, column=0, padx=5, pady=(0, 4))

        self.label.configure(cursor="hand2")
        ttk.Label(self.display_frame, text="Click preview to open fullscreen", bootstyle="secondary").grid(row=2, column=0, pady=(0, 8))

        # Listen for projector changes
        event_dispatcher.add_event_listener(Event.SHOWN_IMAGE_CHANGED, self._update_display)
        event_dispatcher.add_event_listener(Event.PATTERN_IMAGE_CHANGED, self._update_display)
        event_dispatcher.add_event_listener(Event.IMAGE_ADJUST_CHANGED, self._update_display)
        event_dispatcher.add_event_listener(Event.PATTERNING_BUSY_CHANGED, self._update_display)

        # Force initial update
        # self.event_dispatcher.root.after(100, self._update_display)

    def _update_display(self):
        """Update the display when projector content changes"""
        shown_image = self.event_dispatcher.shown_image

        # Update status text
        status_map = {
            ShownImage.CLEAR: "Status: Clear (No Output)",
            ShownImage.PATTERN: "Status: Pattern (UV Exposure)",
            ShownImage.FLATFIELD: "Status: Flatfield Correction",
            ShownImage.RED_FOCUS: "Status: Red Focus Mode",
            ShownImage.UV_FOCUS: "Status: UV Focus Pattern",
        }
        self.status_var.set(status_map.get(shown_image, "Status: Unknown"))

        # Get the appropriate processed image based on mode
        # Note: When patterning, we check patterning_busy flag as well
        img = None
        if self.event_dispatcher.patterning_busy:
            img = self.event_dispatcher.pattern.processed()
        elif shown_image == ShownImage.RED_FOCUS:
            img = self.event_dispatcher.red_focus.processed()
        # pattern case above uv focus case: when set_patterning_busy(True) is called,
        # shown_image is never changed to PATTERN during patterning - it stays as UV_FOCUS
        elif shown_image == ShownImage.PATTERN or self.event_dispatcher.patterning_busy:
            img = self.event_dispatcher.pattern.processed()
        elif shown_image == ShownImage.UV_FOCUS:
            img = self.event_dispatcher.uv_focus.processed()
        elif shown_image == ShownImage.FLATFIELD:
            # Flatfield might not be implemented, use pattern as fallback
            img = self.event_dispatcher.pattern.processed()

        # Update image
        if img is None or (shown_image == ShownImage.CLEAR and not self.event_dispatcher.patterning_busy):
            # Show black placeholder when clear
            placeholder = Image.new("RGB", self.display_size, "black")
            self.photo = image_to_tk_image(placeholder)
        else:
            display_img = img.copy()
            display_img.thumbnail(self.display_size, Image.Resampling.LANCZOS)
            self.photo = image_to_tk_image(display_img)

        self.label.configure(image=self.photo)

class TilingCheckFrame:
    def __init__(self, parent, event_dispatcher: EventDispatcher):
        self.frame = ttk.Frame(parent)
        self.event_dispatcher = event_dispatcher

        self.capture_button = ttk.Button(
            self.frame, 
            text="Capture & Stitch Chip Imges", 
            command=self.capture_and_stitch
        )
        self.capture_button.grid(row=0, column=0)

        self.preview_label = ttk.Label(self.frame)
        self.preview_label.grid(row=1, column=0, padx=5, pady=5, sticky="nsew")

        self.frame.rowconfigure(1, weight=1)
        self.frame.columnconfigure(0, weight=1)

    def capture_and_stitch(self):
        self.img = self.event_dispatcher.pattern_image
        self.img_w, self.img_h = self.img.size # 3840*2, 2160*2 # in pixels 
        print("self.img_w, self.img_h: ", self.img_w, self.img_h)

        self.tile_width, self.tile_height = 3840, 2160 # in pixels, defined in TilingFrame

        # the distance that the stage will move in um
        stride_x, stride_y = 798, 448 # 1037 / 1.3 and 583 / 1.3
        # crop image pixels
        # each snapshot captured is 1920 x 1080 pixels
        crop_x, crop_y = 1477, 831 # 1920 / 1.3 and 1080 / 1.3
        # Disable button during capture
        self.capture_button.config(state='disabled', text="Capturing...")
        self.frame.update()

        stitched_image = self.takeAndStitchMapImages(stride_x, stride_y, crop_x, crop_y)
        if stitched_image:
            self.display_image(stitched_image, crop_x, crop_y)
            print("Stitching complete!")
        else:
            print("Failed to stitch images")

        self.capture_button.config(state='normal', text="Capture & Stitch Chip Imges")

    def display_image(self, pil_image, crop_x, crop_y):
        display_img = pil_image.copy()
        display_img = display_img.resize((crop_x//4, crop_y//4), Image.Resampling.LANCZOS)
        photo = ImageTk.PhotoImage(display_img)
        self.preview_label.config(image=photo)
        self.preview_label.image = photo

    def capture_current_image(self):
        # Get the camera view from the event dispatcher
        if hasattr(self.event_dispatcher, 'camera_image') and self.event_dispatcher.camera_image is not None:
            camera_image = self.event_dispatcher.camera_image
            pil_image = Image.fromarray(camera_image)
            return pil_image
        else:
            print("No camera image available")
            return None

    def takeAndStitchMapImages(self, stride_x, stride_y, crop_x, crop_y):
        """
        Take snapshots num_cols * num_rows times, move in snake pattern
        Move in stride_x and y um in distance
        Crop all snapshots to crop_x, crop_y pixels in the center
        Paste them to the blank canvas
        Overlay the pattern image in the center
        """
        # calculate how many times the stage will move: num cols & rows
        total_x_um = self.img_w * 1037 / 3840
        total_y_um = self.img_h * 583 / 2160
        num_cols = int(total_x_um // stride_x)
        num_rows = int(total_y_um // stride_y)
        if num_cols * stride_x < total_x_um:
            num_cols += 1
        if num_rows * stride_y < total_y_um:
            num_rows += 1
        print("num_cols, num_rows: ", num_cols, num_rows)

        # large blank canvas
        stitched_width = num_cols * crop_x
        stitched_height = num_rows * crop_y
        stitched_image = Image.new('RGB', (stitched_width, stitched_height), color='black')

        # get starting position: center the chip
        orig_x, orig_y, orig_z = self.event_dispatcher.stage_setpoint
        delta_x_um = (num_cols * stride_x - total_x_um) / 2
        delta_y_um = (num_rows * stride_y - total_y_um) / 2
        start_x = orig_x - delta_x_um
        start_y = orig_y - delta_y_um

        # move in snake pattern with left to right on even rows
        # and right to left on odd rows
        for row in range(num_rows):
            current_y = start_y + row * stride_y

            if row % 2 == 0:
                col_range = range(num_cols)
                first_x = start_x
            else:
                col_range = range(num_cols - 1, -1, -1)
                first_x = start_x + (num_cols - 1) * stride_x

            # move to the next row
            self.event_dispatcher.move_absolute({
                "x": first_x,
                "y": current_y,
                "z": orig_z
            })
            self.event_dispatcher.non_blocking_delay(2)

            # columns in this row
            for idx, col in enumerate(col_range):
                current_x = start_x + col * stride_x
                if idx > 0: # idx = 0 first one don't need to move
                    self.event_dispatcher.move_absolute({
                        "x": current_x,
                        "y": current_y,
                        "z": orig_z
                    })
                    self.event_dispatcher.non_blocking_delay(2.5)

                captured_image = self.capture_current_image()
                # crop
                orig_width, orig_height = captured_image.size
                # each snapshot captured is 1920 x 1080 pixels
                left = (orig_width - crop_x) // 2
                top = (orig_height - crop_y) // 2
                right = left + crop_x
                bottom = top + crop_y
                cropped_img = captured_image.crop((left, top, right, bottom))
                # paste
                x_pos = col * crop_x
                y_pos = (num_rows - 1 - row) * crop_y
                stitched_image.paste(cropped_img, (x_pos, y_pos))

                self.event_dispatcher.non_blocking_delay(0.5)
                self.frame.update()

        # Return to starting position
        self.event_dispatcher.move_absolute({
            "x": orig_x,
            "y": orig_y,
            "z": orig_z
        })

        # Overlay the pattern image at center with 50% transparency
        pattern_img = self.img.copy().convert('RGBA') # pattern image
        # resize pattern image to match the snapshot's pixel
        # each snapshot has half of each tile's w and h
        pattern_img = pattern_img.resize((self.img_w//2, self.img_h//2), Image.Resampling.LANCZOS)
        pattern_img.putalpha(int(255 * 0.5))
        center_x = (stitched_width - self.img_w//2) // 2
        center_y = (stitched_height - self.img_h//2) // 2
        stitched_image = stitched_image.convert('RGBA')
        stitched_image.paste(pattern_img, (center_x, center_y), pattern_img)
        stitched_image = stitched_image.convert('RGB')

        return stitched_image

class MapFrame:
    def __init__(self, parent, event_dispatcher: EventDispatcher):
        self.frame = ttk.Labelframe(parent)
        self.event_dispatcher = event_dispatcher

        # Map dimensions in micrometers
        self.map_size_um = 10000.0  # 1 cm * 1 cm

        # Canvas size in pixels
        self.canvas_size = 350

        # Pattern dimensions: from DLP projector datasheet
        self.pattern_w = 1037
        self.pattern_h = 583

        # canvas with plain background
        self.canvas = tkinter.Canvas(
            self.frame, 
            width=self.canvas_size, 
            height=self.canvas_size,
            bg='#182632',
        )
        self.canvas.grid(row=0, column=0, padx=5, pady=5)

        # coordinates of exposed patterns (list of tuples: (x, y))
        self.pattern_markers = []

        event_dispatcher.add_event_listener(Event.STAGE_POSITION_CHANGED, self._on_position_changed)
        event_dispatcher.add_event_listener(Event.PATTERNING_FINISHED, self._on_pattern_exposed)
        event_dispatcher.add_event_listener(Event.CHIP_CHANGED, self._on_chip_changed)

        self._redraw_all()

    def _um_to_pixels(self, um_x, um_y):
        """
        Convert micrometer coordinates to canvas pixel coordinates.
        (0, 0) in micrometers is at the center of the canvas.
        """
        scale = self.canvas_size / self.map_size_um

        # Add half map size to shift origin to center
        pixel_x = (um_x + self.map_size_um / 2) * scale
        pixel_y = (um_y + self.map_size_um / 2) * scale

        return pixel_x, pixel_y

    def _um_size_to_pixels(self, um_width, um_height):
        """Convert micrometer dimensions to pixel dimensions"""
        scale = self.canvas_size / self.map_size_um
        return um_width * scale, um_height * scale

    def _draw_pattern_marker(self, x_um, y_um):
        """ Draw a blue rectangle given x and y """
        x1_px, y1_px = self._um_to_pixels(x_um, y_um)
        w_px, h_px = self._um_size_to_pixels(self.pattern_w, self.pattern_h)

        x2_px = x1_px + w_px
        y2_px = y1_px + h_px

        # shift from bottom down to bottom up
        y1_px = self.canvas_size - y1_px
        y2_px = self.canvas_size - y2_px

        marker = self.canvas.create_rectangle(
            x1_px, y1_px, x2_px, y2_px,
            fill='#7BB7B7',
            width=0
        )

        return marker

    def _draw_current_position(self):
        """ Draw the red rectangle for current position. """
        x_um, y_um, z_um = self.event_dispatcher.stage_setpoint

        # Convert top-left corner to pixel coordinates
        x1_px, y1_px = self._um_to_pixels(x_um, y_um)

        # Get pattern dimensions in pixels
        w_px, h_px = self._um_size_to_pixels(self.pattern_w, self.pattern_h)

        # Calculate bottom-right corner
        x2_px = x1_px + w_px
        y2_px = y1_px + h_px

        y1_px = self.canvas_size - y1_px
        y2_px = self.canvas_size - y2_px        

        marker = self.canvas.create_rectangle(
                x1_px, y1_px, x2_px, y2_px,
                fill='',  # No fill
                outline='#E86E7F',  # Red outline
                width=2
            )

        return marker # marker ID

    def _load_patterns_from_chip(self):
        """ Load all exposed pattern coordinates from the chip
        into the self.pattern_markers list """

        self.pattern_markers.clear()

        chip = self.event_dispatcher.chip
        for layer in chip.layers:
            for exposure in layer.exposures:
                if not exposure.aborted:  # Only include successful exposures
                    x, y, z = exposure.coords
                    self.pattern_markers.append((x, y))

    def _redraw_all(self):
        """Redraw all exposed patterns from the chip"""
        # Clear existing pattern markers
        self.canvas.delete("all")

        # Draw all exposures from all layers
        for x_um, y_um in self.pattern_markers:
            self._draw_pattern_marker(x_um, y_um)

        self._draw_current_position()

    def _on_position_changed(self):
        self._redraw_all() # TODO: only update current_position?

    def _on_pattern_exposed(self):
        """ Get the most recent exposure from current layer """
        chip = self.event_dispatcher.chip
        if chip.layers and chip.layers[-1].exposures:
            latest_exposure = chip.layers[-1].exposures[-1]
            if not latest_exposure.aborted:
                x, y, z = latest_exposure.coords
                self.pattern_markers.append((x, y))
                self._redraw_all()

    def _on_chip_changed(self):
        """Handle chip changes (load, new chip, etc.) - reload and redraw"""
        self._load_patterns_from_chip()
        self._redraw_all()

class ScrollPage:
    """Scrollable workspace that stays usable on smaller laptop displays."""
    def __init__(self, parent):
        self.frame = ttk.Frame(parent)
        self.canvas = tkinter.Canvas(self.frame, highlightthickness=0, background=ttk.Style().colors.bg)
        vertical = ttk.Scrollbar(self.frame, orient="vertical", command=self.canvas.yview)
        horizontal = ttk.Scrollbar(self.frame, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(0, weight=1)
        self.body = ttk.Frame(self.canvas, padding=(28, 28))
        self.item = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        def fit(event):
            width = max(self.body.winfo_reqwidth(), min(event.width, 1240))
            self.canvas.itemconfigure(self.item, width=width)
            self.canvas.coords(self.item, max(0, (event.width - width) / 2), 0)
        self.canvas.bind("<Configure>", fit)


class LithographerGui:
    def __init__(self, config: LithographerConfig, root: tkinter.Tk, settings=None, config_path="config.toml"):
        self.root = root
        root.title("Stepper — HackerFab")
        root.geometry("1440x1000")
        root.minsize(900, 650)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)
        style = configure_theme(root, (settings or {}).get("ui", {}).get("theme", "studio-dark"), (settings or {}).get("ui", {}).get("text-scale", 1.0))
        style.configure("TButton", padding=(14, 9))
        style.configure("TNotebook.Tab", padding=(20, 12))
        style.configure("TLabelframe", padding=12)
        self.event_dispatcher = EventDispatcher(config.stage, TkProjector(root), root,
                                               config.camera, config.red_exposure, config.uv_exposure)
        self.event_dispatcher.initialize_alignment(config)
        self.shown_image = ShownImage.CLEAR
        sidebar = ttk.Frame(root, padding=(20, 28))
        sidebar.grid(row=0, column=0, sticky="ns")
        ttk.Label(sidebar, text="HackerFab", font="StepperSection", bootstyle="info").pack(anchor="w")
        ttk.Label(sidebar, text="Stepper", font="StepperBrand").pack(anchor="w", pady=(6, 4))
        ttk.Label(sidebar, text="Photolithography", bootstyle="secondary").pack(anchor="w", pady=(0, 32))
        host = ttk.Frame(root)
        host.grid(row=0, column=1, sticky="nsew")
        host.columnconfigure(0, weight=1)
        host.rowconfigure(0, weight=1)
        self.pages = {}
        self.nav_buttons = {}
        for name in ("Operate", "Alignment", "Wafer & tiling", "Settings"):
            page = ScrollPage(host)
            page.frame.grid(row=0, column=0, sticky="nsew")
            self.pages[name] = page
            button = ttk.Button(sidebar, text=name, command=lambda n=name: self.show_page(n), bootstyle="secondary-link", width=20)
            button.pack(fill="x", pady=5)
            self.nav_buttons[name] = button
        ttk.Label(sidebar, text="Connected equipment", bootstyle="secondary").pack(anchor="w", pady=(36, 12))
        self.device_status = StringVar(value="Camera · connecting")
        ttk.Label(sidebar, textvariable=self.device_status, wraplength=195).pack(anchor="w")
        ttk.Label(sidebar, text="Stage · " + ("connected" if isinstance(config.stage, GrblStage) else "simulation"), bootstyle="secondary").pack(anchor="w", pady=8)
        def clear_projector():
            if self.event_dispatcher.patterning_busy:
                self.event_dispatcher.abort_patterning()
            self.event_dispatcher.set_shown_image(ShownImage.CLEAR)
        ttk.Button(sidebar, text="Clear projector", command=clear_projector, bootstyle="secondary-outline").pack(side="bottom", fill="x", pady=8)
        self.abort_button = ttk.Button(sidebar, text="Stop exposure", command=self.event_dispatcher.abort_patterning, bootstyle="danger", state="disabled")
        self.abort_button.pack(side="bottom", fill="x")
        self.event_dispatcher.add_event_listener(Event.PATTERNING_BUSY_CHANGED, lambda: self.abort_button.configure(state="normal" if self.event_dispatcher.patterning_busy else "disabled"))
        operation = self.pages["Operate"].body
        operation.columnconfigure(0, weight=1)
        ttk.Label(operation, text="Workspace", font="StepperTitle").grid(row=0, column=0, sticky="w")
        ttk.Label(operation, text="Set up your pattern, bring it into focus, then expose.", bootstyle="secondary").grid(row=1, column=0, sticky="w", pady=(4, 20))
        self.top_panel = ttk.Frame(operation)
        self.top_panel.grid(row=2, column=0, sticky="ew")
        self.top_panel.columnconfigure(0, weight=1)
        self.camera = CameraFrame(self.top_panel, self.event_dispatcher, config.camera, config.camera_scale)
        self.camera.frame.grid(row=0, column=0, sticky="nsew", padx=(0, 16))
        self.projector_display = ProjectorDisplayFrame(self.top_panel, self.event_dispatcher)
        self.projector_display.frame.grid(row=0, column=1, sticky="n")
        def close_fullscreen(kind):
            if kind == "projector":
                if self.event_dispatcher.patterning_busy:
                    self.event_dispatcher.abort_patterning()
                self.event_dispatcher.set_shown_image(ShownImage.CLEAR)
        self.fullscreen = FullscreenPreview(root, on_close=close_fullscreen)
        def camera_picture():
            image = self.event_dispatcher.camera_image
            return Image.fromarray(image) if image is not None else None
        def projector_picture():
            return getattr(self.event_dispatcher.hardware.projector, "current_image", self.event_dispatcher.current_image)
        self.camera.label.configure(cursor="hand2")
        self.camera.label.bind("<Button-1>", lambda _: self.fullscreen.open("camera", camera_picture))
        self.projector_display.label.bind("<Button-1>", lambda _: self.fullscreen.open("projector", projector_picture))
        def projector_updated():
            # Actual exposures use the same fullscreen view, never a hidden
            # output window. The exposure timer starts after show() returns.
            if self.event_dispatcher.patterning_busy and not self.event_dispatcher.should_abort and self.fullscreen.kind != "projector":
                self.fullscreen.open("projector", projector_picture)
            if self.fullscreen.kind == "projector":
                self.fullscreen.refresh()
        self.event_dispatcher.hardware.projector.on_show = projector_updated
        def arrange_preview(event):
            if event.width < 1080:
                self.projector_display.frame.grid(row=1, column=0, sticky="w", pady=(16, 0))
            else:
                self.projector_display.frame.grid(row=0, column=1, sticky="n", pady=0)
        self.top_panel.bind("<Configure>", arrange_preview)
        ttk.Label(operation, text="Click either preview for fullscreen. Press Esc or × to return. For projection, place this app on the DLP display.", bootstyle="warning", wraplength=900).grid(row=3, column=0, sticky="w", pady=16)
        self.pattern_progress = ttk.Progressbar(operation, bootstyle="info")
        self.pattern_progress.grid(row=4, column=0, sticky="ew", pady=(0, 16))
        self.event_dispatcher.add_event_listener(Event.EXPOSURE_PATTERN_PROGRESS_CHANGED, lambda: self.pattern_progress.configure(value=self.event_dispatcher.patterning_progress * 100))
        self.mode_select_frame = ModeSelectFrame(operation, self.event_dispatcher)
        self.mode_select_frame.notebook.grid(row=5, column=0, sticky="nsew")
        alignment = self.pages["Alignment"].body
        ttk.Label(alignment, text="Alignment & image", font="StepperTitle").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 20))
        self.image_adjust_frame = ImageAdjustFrame(alignment, self.event_dispatcher)
        self.image_adjust_frame.frame.grid(row=1, column=0, sticky="nw", padx=(0, 20))
        self.global_settings_frame = GlobalSettingsFrame(alignment, self.event_dispatcher, config.alignment.enabled)
        self.global_settings_frame.frame.grid(row=1, column=1, sticky="nw")
        wafer = self.pages["Wafer & tiling"].body
        ttk.Label(wafer, text="Wafer & tiling", font="StepperTitle").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 20))
        self.map = MapFrame(wafer, self.event_dispatcher)
        self.map.frame.grid(row=1, column=0, sticky="nw", padx=(0, 20))
        self.chip_frame = ChipFrame(wafer, self.event_dispatcher)
        self.chip_frame.frame.grid(row=1, column=1, sticky="nw")
        self.tiling_frame = TilingFrame(wafer, self.event_dispatcher)
        self.tiling_frame.frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=20)
        self.settings_page = SettingsPage(self.pages["Settings"].body, self, settings or {}, config_path)
        self.settings_page.frame.pack(fill="both", expand=True)
        self.exposure_frame = self.mode_select_frame.uv_mode_frame.exposure_frame
        self.patterning_frame = self.mode_select_frame.uv_mode_frame.patterning_frame
        prepare_dropdowns(root)
        def scroll_workspace(event):
            if event.widget.winfo_class() in ('Listbox', 'Treeview', 'Text', 'TCombobox'):
                return
            widget = event.widget
            while widget is not None:
                for page in self.pages.values():
                    if widget == page.frame:
                        direction = -1 if getattr(event, 'num', 0) == 4 or getattr(event, 'delta', 0) > 0 else 1
                        page.canvas.yview_scroll(direction * 3, 'units')
                        return 'break'
                widget = getattr(widget, 'master', None)
        for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            root.bind_all(sequence, scroll_workspace)
        root.protocol("WM_DELETE_WINDOW", self.cleanup)
        self.show_page("Operate")
        def on_start():
            self.camera.start()
            self.event_dispatcher.enter_red_mode(mode_switch_autofocus=False)
            if self.event_dispatcher.hardware.stage.has_homing():
                self.event_dispatcher.home_stage()
            self.update_status()
        root.after(0, on_start)

    def show_page(self, name):
        self.pages[name].frame.tkraise()
        for label, button in self.nav_buttons.items():
            button.configure(bootstyle="primary" if label == name else "secondary-link")

    def update_status(self):
        camera = self.camera.camera
        text = "disabled" if camera is None else ("live" if self.event_dispatcher.camera_image is not None else "offline / connecting")
        self.device_status.set(f"Camera · {text}")
        self.root.after(500, self.update_status)

    def cleanup(self):
        self.fullscreen.cleanup()
        self.camera.cleanup()
        self.event_dispatcher.hardware.stage.close()
        self.root.destroy()


class _StartupDialog:
    """Pre-launch dialog: config file selection + GRBL board detection.

    Runs its own Tk() event loop.  On a successful launch, self.config and
    self.selected_port are populated before the window is destroyed.
    """

    _GRBL_VIDS = {
        0x2341,  # Arduino LLC
        0x2A03,  # Arduino.org
        0x239A,  # Adafruit
        0x1B4F,  # SparkFun
        0x1A86,  # CH340 (ubiquitous clone chip)
        0x0403,  # FTDI
        0x10C4,  # Silicon Labs CP210x
        0x067B,  # Prolific PL2303
        0x2E8A,  # Raspberry Pi RP2040 / BTT SKR Pico
    }

    def __init__(self, master: tkinter.Tk):
        self.win = tkinter.Toplevel(master)
        self.win.title("HackerFab Stepper – Setup")
        self.win.resizable(False, False)
        self.win.grab_set()  # modal: blocks interaction with the hidden root window

        self.config: dict = {}
        self.selected_port: Optional[str] = None   # None → no stage
        self.launched: bool = False
        self._port_devices: list[str] = []

        self._build_ui()
        prepare_dropdowns(self.win)
        self._center()
        self.win.after(50, self._scan_ports)        # scan after window renders

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        f = ttk.Frame(self.win, padding=16)
        f.grid(sticky="nsew")

        ttk.Label(f, text="HackerFab Stepper",
                  font=("Arial", 20, "bold"), bootstyle="info").grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="Photolithography stepper control",
                  bootstyle="secondary").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))
        ttk.Separator(f, orient="horizontal").grid(
            row=2, column=0, columnspan=3, sticky="ew", pady=(0, 10))

        # Config file
        ttk.Label(f, text="Config file:").grid(
            row=3, column=0, sticky="e", padx=(0, 8), pady=4)
        self._config_var = StringVar(value="config.toml" if Path("config.toml").exists() else "default.toml")
        ttk.Entry(f, textvariable=self._config_var, width=40).grid(
            row=3, column=1, sticky="ew", pady=4)
        ttk.Button(f, text="Browse…", command=self._browse,
                   bootstyle="secondary-outline").grid(
            row=3, column=2, padx=(8, 0), pady=4)

        ttk.Separator(f, orient="horizontal").grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=10)

        # Stage port
        ttk.Label(f, text="Stage port:").grid(
            row=5, column=0, sticky="e", padx=(0, 8), pady=4)
        self._port_var = StringVar(value="Scanning…")
        self._combo = ttk.Combobox(f, textvariable=self._port_var,
                                   state="readonly", width=40)
        self._combo.grid(row=5, column=1, sticky="ew", pady=4)
        ttk.Button(f, text="↺", width=3, command=self._scan_ports,
                   bootstyle="info-outline").grid(
            row=5, column=2, padx=(8, 0), pady=4)

        self._status_var = StringVar()
        self._status_lbl = ttk.Label(f, textvariable=self._status_var, foreground="gray")
        self._status_lbl.grid(row=6, column=0, columnspan=3, sticky="w", pady=(2, 0))

        ttk.Separator(f, orient="horizontal").grid(
            row=7, column=0, columnspan=3, sticky="ew", pady=10)

        btns = ttk.Frame(f)
        btns.grid(row=8, column=0, columnspan=3, sticky="e")
        ttk.Button(btns, text="Cancel", command=self.win.destroy,
                   bootstyle="secondary").pack(side="left", padx=4)
        ttk.Button(btns, text="Launch  →", command=self._launch,
                   bootstyle="success").pack(side="left", padx=4)

        f.columnconfigure(1, weight=1)

    def _center(self) -> None:
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    # ── Actions ──────────────────────────────────────────────────────────

    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.win,
            title="Select config file",
            filetypes=[("TOML files", "*.toml"), ("All files", "*.*")],
        )
        if path:
            self._config_var.set(path)
            self._scan_ports()

    def _scan_ports(self) -> None:
        from serial.tools import list_ports
        ports = list_ports.comports()
        grbl  = [p for p in ports if (p.vid or 0) in self._GRBL_VIDS]
        other = [p for p in ports if (p.vid or 0) not in self._GRBL_VIDS]

        entries: list[tuple[str, str]] = []
        for p in grbl:
            entries.append((p.device,
                            f"{p.device}  –  {p.description}  [Arduino / GRBL]"))
        for p in other:
            entries.append((p.device, f"{p.device}  –  {p.description}"))
        entries.append(("none", "No stage  (run without hardware)"))

        self._port_devices    = [e[0] for e in entries]
        self._combo["values"] = [e[1] for e in entries]

        try:
            stage_settings = toml.load(self._config_var.get()).get("stage", {})
        except (OSError, ValueError):
            stage_settings = {}
        saved_port = str(stage_settings.get("port", "auto"))
        if not stage_settings.get("enabled", True):
            saved_port = "none"
        if saved_port != "auto":
            if saved_port not in self._port_devices:
                self._port_devices.insert(0, saved_port)
                self._combo["values"] = [f"{saved_port} (saved; unavailable)"] + [e[1] for e in entries]
            self._combo.current(self._port_devices.index(saved_port))
            self._status(f"Saved stage selection: {saved_port}")
            return

        if grbl:
            self._combo.current(0)
            if len(grbl) == 1:
                self._status(f"✓  Auto-selected: {grbl[0].description}", "green")
            else:
                self._status(
                    f"{len(grbl)} GRBL boards found – please select one", "dark orange")
        elif ports:
            if len(other) == 1:
                self._combo.current(0)
                self._status(
                    f"Unrecognized serial device auto-selected: {other[0].description}",
                    "dark orange",
                )
            else:
                self._combo.current(len(entries) - 1)
                self._status(
                    "No Arduino / GRBL board detected – select the controller from the list", "#cc5500")
        else:
            self._combo.current(len(entries) - 1)
            self._status(
                "No serial ports found – connect the board then press  ↺", "#cc5500")

    def _status(self, msg: str, color: str = "gray") -> None:
        self._status_var.set(msg)
        self._status_lbl.configure(foreground=color)

    def _launch(self) -> None:
        path = self._config_var.get().strip()
        try:
            with open(path, "r") as fh:
                self.config = toml.load(fh)
        except FileNotFoundError:
            messagebox.showerror(
                "Config not found", f"File not found:\n{path}", parent=self.win)
            return
        except Exception as e:
            messagebox.showerror("Config error", str(e), parent=self.win)
            return

        idx = self._combo.current()
        self.selected_port = self._port_devices[idx] if idx >= 0 else None
        self.launched = True
        self.win.destroy()

    # ── Entry point ──────────────────────────────────────────────────────

    def run(self) -> bool:
        """Block until the dialog is closed; return True if the user clicked Launch."""
        self.win.wait_window()
        return self.launched


def main():
    # One root window for the entire app lifetime — ttkbootstrap Style binds to it
    # and must never be destroyed and recreated.
    root = ttk.Window(themename="darkly")
    configure_ui_scale(root)
    configure_theme(root)
    root.withdraw()  # stay hidden until the main UI is ready

    dialog = _StartupDialog(root)
    if not dialog.run():
        root.destroy()
        return

    config = dialog.config
    theme = config.get("ui", {}).get("theme", "studio-dark")
    if theme in ttk.Style().theme_names():
        ttk.Style().theme_use(theme)

    # ── Stage ────────────────────────────────────────────────────────────
    stage_cfg = config.get("stage", {})
    port = dialog.selected_port
    if stage_cfg.get("enabled", True) and port and port != "none":
        try:
            sp    = serial.Serial(port, stage_cfg.get("baud-rate", 115200))
            stage = GrblStage(sp, stage_cfg.get("homing", False), invert_z=stage_cfg.get("invert-z", True))
        except Exception as e:
            print(f"Stage connection failed ({port}): {e}")
            stage = StageController()
    else:
        stage = StageController()

    # ── Camera ───────────────────────────────────────────────────────────
    cam_cfg  = config.get("camera", {})
    cam_type = cam_cfg.get("type", "none")
    try:
        if cam_type == "webcam":
            camera = Webcam(cam_cfg.get("device", cam_cfg.get("index", "auto")), settings=cam_cfg)
        elif cam_type == "flir":
            import camera.flir.flir_camera as flir
            camera = flir.FlirCamera()
        elif cam_type in ("basler", "pylon"):
            from camera.pylon import BaslerPylon
            camera = BaslerPylon(int(cam_cfg.get("index", 0)))
        elif cam_type == "none":
            camera = None
        else:
            print(f"Unknown camera type '{cam_type}' – camera disabled")
            camera = None
    except Exception as exc:
        print(f"Camera initialization failed: {exc}. Choose a camera in Settings.")
        camera = None

    camera_scale = float(cam_cfg.get("gui-scale",    1.0))
    red_exposure = float(cam_cfg.get("red-exposure",  DEFAULT_RED_EXPOSURE))
    uv_exposure  = float(cam_cfg.get("uv-exposure",   DEFAULT_UV_EXPOSURE))

    # ── Alignment ────────────────────────────────────────────────────────
    ac = config.get("alignment", {})
    alignment_config = AlignmentConfig(
        enabled         = ac.get("enabled",         False),
        model_path      = ac.get("model_path",      "ckpts/best.pt"),
        right_marker_x  = float(ac.get("right_marker_x",  1820.0)),
        left_marker_x   = float(ac.get("left_marker_x",    280.0)),
        top_marker_y    = float(ac.get("top_marker_y",     269.0)),
        bottom_marker_y = float(ac.get("bottom_marker_y", 1075.0)),
        x_scale_factor  = float(ac.get("x_scale_factor",  -1100)),
        y_scale_factor  = float(ac.get("y_scale_factor",    800)),
    )

    lithographer = LithographerGui(LithographerConfig(
        stage, camera, camera_scale, red_exposure, uv_exposure, alignment_config,
    ), root, config, "config.toml" if Path(dialog._config_var.get()).resolve() == Path("default.toml").resolve() else dialog._config_var.get())
    root.deiconify()  # show the main window now that it's fully built
    root.mainloop()


if __name__ == "__main__":
    main()
