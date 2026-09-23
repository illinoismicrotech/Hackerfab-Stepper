"""Device settings UI and atomic project-local configuration persistence."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import math
import os
import queue
import shutil
import threading
import tkinter as tk
from tkinter import messagebox

import toml
import ttkbootstrap as ttk
from camera.discovery import discover_devices, parse_modes, v4l_info
from camera.webcam import Webcam
from ui_theme import configure_theme, prepare_dropdowns


def save_config(path, config):
    path = Path(path)
    content = toml.dumps(config)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + datetime.now().strftime('.%Y%m%d-%H%M%S-%f.bak')))
    temporary = path.with_name(path.name + '.tmp')
    try:
        temporary.write_text(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class SettingsPage:
    def __init__(self, parent, app, config, path):
        self.app, self.config, self.path = app, config, Path(path)
        self.frame = ttk.Frame(parent, padding=4)
        self.frame.columnconfigure(0, weight=1)
        self.results = queue.Queue()
        self.devices, self.modes, self.vars = [], [], {}
        ttk.Label(self.frame, text='Settings', font='StepperTitle').grid(row=0, column=0, sticky='w')
        ttk.Label(self.frame, text='Make this workspace work for your equipment.', bootstyle='secondary').grid(row=1, column=0, sticky='w', pady=(6, 24))
        notebook = ttk.Notebook(self.frame)
        notebook.grid(row=2, column=0, sticky='ew')
        camera_page = ttk.Frame(notebook, padding=24)
        stage_page = ttk.Frame(notebook, padding=24)
        appearance_page = ttk.Frame(notebook, padding=24)
        for title, page in [('Camera', camera_page), ('Stage', stage_page), ('Appearance', appearance_page)]:
            notebook.add(page, text=title)
            page.columnconfigure(1, weight=1)
        cam = config.get('camera', {})
        device_value = str(cam.get('device', cam.get('index', 'auto')))
        self.field(0, 'Camera', 'device', 'Automatic selection' if device_value == 'auto' else device_value, ['Automatic selection'], camera_page)
        self.device = self.widgets['device']
        self.device.bind('<<ComboboxSelected>>', lambda _: self.scan_modes())
        self.connection_hint = tk.StringVar(value='Connect the Arducam, then refresh this list. Choose its name to avoid the laptop camera.')
        ttk.Button(camera_page, text='Refresh', command=self.scan, bootstyle='secondary-outline').grid(row=0, column=2, padx=(12, 0))
        ttk.Label(camera_page, textvariable=self.connection_hint, wraplength=650, bootstyle='secondary').grid(row=1, column=1, columnspan=2, sticky='w', pady=(0, 20))
        self.field(2, 'Image quality', 'mode', cam.get('mode', 'auto'), ['auto', 'manual'], camera_page)
        ttk.Label(camera_page, text='Auto finds a working resolution and frame rate. Use Manual for a specific capture mode.', wraplength=650, bootstyle='secondary').grid(row=3, column=1, columnspan=2, sticky='w', pady=(0, 16))
        self.manual = ttk.Labelframe(camera_page, text='Manual capture', padding=20)
        self.manual.columnconfigure(1, weight=1)
        self.mode_choice = ttk.Combobox(self.manual, state='readonly')
        ttk.Label(self.manual, text='Supported modes').grid(row=0, column=0, sticky='w', padx=(0, 20), pady=8)
        self.mode_choice.grid(row=0, column=1, sticky='ew')
        self.mode_choice.bind('<<ComboboxSelected>>', self.choose_mode)
        ttk.Button(self.manual, text='Read modes', command=self.scan_modes, bootstyle='secondary-outline').grid(row=0, column=2, padx=12)
        for row, label, key, default in [(1, 'Width · pixels', 'width', 1280), (2, 'Height · pixels', 'height', 720), (3, 'Frame rate · fps', 'fps', 30)]:
            self.field(row, label, key, cam.get(key, default), parent=self.manual)
        self.field(4, 'Image format', 'fourcc', cam.get('fourcc', 'MJPG'), ['MJPG', 'YUYV', 'YUY2', 'RGB3'], self.manual)
        def show_manual(*_):
            if self.vars['mode'].get() == 'manual':
                self.manual.grid(row=4, column=0, columnspan=3, sticky='ew', pady=(0, 20))
            else:
                self.manual.grid_remove()
        self.vars['mode'].trace_add('write', show_manual)
        show_manual()
        self.reject = tk.BooleanVar(value=cam.get('reject-green', True))
        ttk.Checkbutton(camera_page, text='Detect corrupted green frames', variable=self.reject, bootstyle='round-toggle').grid(row=5, column=0, columnspan=3, sticky='w', pady=(4, 8))
        ttk.Label(camera_page, text='Turn this off only if your specimen really fills the image with green.', wraplength=650, bootstyle='secondary').grid(row=6, column=0, columnspan=3, sticky='w', pady=(0, 20))
        ttk.Separator(camera_page).grid(row=7, column=0, columnspan=3, sticky='ew', pady=(0, 16))
        self.field(8, 'Camera driver', 'type', cam.get('type', 'webcam'), ['webcam', 'basler', 'flir', 'none'], camera_page)
        ttk.Label(camera_page, text='Use webcam for a standard USB camera. Basler and FLIR require their vendor software.', wraplength=650, bootstyle='secondary').grid(row=9, column=1, columnspan=2, sticky='w')
        ttk.Button(camera_page, text='Connect camera', command=self.apply).grid(row=10, column=0, columnspan=3, sticky='w', pady=(24, 12))
        self.status = tk.StringVar(value='Ready to connect a camera.')
        ttk.Label(camera_page, textvariable=self.status, wraplength=780, justify='left').grid(row=11, column=0, columnspan=3, sticky='w', pady=(8, 0))
        ttk.Button(camera_page, text='Copy camera diagnostics', command=self.copy_diagnostics, bootstyle='secondary-outline').grid(row=12, column=0, columnspan=3, sticky='w', pady=(16, 0))
        stage = config.get('stage', {})
        ttk.Label(stage_page, text='Stage connection', font='StepperSection').grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 16))
        self.stage_enabled = tk.BooleanVar(value=stage.get('enabled', True))
        ttk.Checkbutton(stage_page, text='Enable the motion stage', variable=self.stage_enabled, bootstyle='round-toggle').grid(row=1, column=0, columnspan=2, sticky='w', pady=12)
        self.field(2, 'Serial port', 'stage-port', stage.get('port', 'auto'), parent=stage_page)
        self.invert_z = tk.BooleanVar(value=stage.get('invert-z', True))
        ttk.Checkbutton(stage_page, text='Reverse Z direction', variable=self.invert_z, bootstyle='round-toggle').grid(row=4, column=0, columnspan=2, sticky='w', pady=12)
        ttk.Label(stage_page, text='Reverses Z movement and position readback in this app. Save and restart to apply.', wraplength=650, bootstyle='secondary').grid(row=5, column=0, columnspan=2, sticky='w')
        ttk.Label(stage_page, text='Use auto to find the controller, or enter its serial port. Save and restart to apply stage changes.', wraplength=650, bootstyle='secondary').grid(row=3, column=0, columnspan=2, sticky='w', pady=16)
        ttk.Label(appearance_page, text='Comfortable for your display', font='StepperSection').grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 16))
        self.field(1, 'Color theme', 'theme', config.get('ui', {}).get('theme', 'studio-dark'), ['studio', 'studio-dark'], appearance_page)
        self.field(2, 'Text size', 'text-scale', str(config.get('ui', {}).get('text-scale', 1.0)), ['1.0', '1.25', '1.5'], appearance_page)
        ttk.Label(appearance_page, text='Choose 1.25 or 1.5 for larger text and controls. You can preview this without reconnecting any devices.', wraplength=650, bootstyle='secondary').grid(row=3, column=0, columnspan=2, sticky='w', pady=16)
        ttk.Button(appearance_page, text='Apply appearance', command=self.apply_appearance).grid(row=4, column=0, columnspan=2, sticky='w', pady=12)
        actions = ttk.Frame(self.frame)
        actions.grid(row=3, column=0, sticky='ew', pady=(24, 12))
        ttk.Button(actions, text='Save settings', command=self.save).pack(side='left')
        ttk.Label(actions, text='Keep these choices for your next session.', bootstyle='secondary').pack(side='left', padx=16)
        self.notice = tk.StringVar(value=f'Settings file: {self.path.name}')
        ttk.Label(self.frame, textvariable=self.notice, wraplength=800, bootstyle='secondary').grid(row=4, column=0, sticky='w')
        self.scan()
        self.tick()

    def copy_diagnostics(self):
        camera = self.app.camera.camera
        lines = [f"Camera backend: {self.vars['type'].get()}",
                 f"Selected device: {self.selected_device()}",
                 f"Capture mode: {self.vars['mode'].get()}",
                 f"Status: {getattr(camera, 'status', 'Camera disabled')}",
                 "Discovered devices:"]
        lines.extend(d.label() for d in self.devices)
        lines.append("Capture attempts:")
        lines.extend(getattr(camera, 'diagnostics', []))
        self.frame.clipboard_clear()
        self.frame.clipboard_append('\n'.join(lines))
        self.notice.set('Camera diagnostics copied. Paste them when reporting a capture problem.')

    def field(self, row, label, key, value, choices=None, parent=None):
        parent = parent or self.frame
        if not hasattr(self, 'widgets'):
            self.widgets = {}
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', padx=(0, 24), pady=12)
        variable = tk.StringVar(value=str(value))
        self.vars[key] = variable
        labels = {
            'type': {'webcam': 'Standard USB camera', 'basler': 'Basler', 'flir': 'FLIR', 'none': 'Camera off'},
            'mode': {'auto': 'Automatic · recommended', 'manual': 'Choose a capture mode'},
            'theme': {'studio': 'Light', 'studio-dark': 'Dark'},
            'text-scale': {'1.0': 'Comfortable · 100%', '1.25': 'Large · 125%', '1.5': 'Extra large · 150%'},
        }.get(key)
        if labels:
            display = tk.StringVar(value=labels.get(str(value), str(value)))
            widget = ttk.Combobox(parent, textvariable=display, values=list(labels.values()), state='readonly')
            widget.bind('<<ComboboxSelected>>', lambda _: variable.set(next(k for k, v in labels.items() if v == display.get())))
            variable.trace_add('write', lambda *_: display.set(labels.get(variable.get(), variable.get())))
        else:
            # Device entry remains editable for exact paths and camera indices.
            widget = ttk.Combobox(parent, textvariable=variable, values=choices, state='normal' if key == 'device' else 'readonly') if choices else ttk.Entry(parent, textvariable=variable)
        widget.grid(row=row, column=1, sticky='ew', pady=8)
        self.widgets[key] = widget
        return widget

    def apply_appearance(self):
        try:
            theme = self.vars['theme'].get()
            scale = float(self.vars['text-scale'].get())
            configure_theme(self.app.root, theme, scale)
            for page in self.app.pages.values():
                page.canvas.configure(background=ttk.Style().colors.bg)
            prepare_dropdowns(self.app.root)
            self.notice.set('Appearance updated. Save settings to keep it.')
        except ValueError as exc:
            messagebox.showerror('Appearance', str(exc))

    def selected_device(self):
        text = self.vars['device'].get().strip()
        return next((d.device for d in self.devices if d.label() == text), 'auto' if text == 'Automatic selection' else text)

    def scan(self):
        self.notice.set('Scanning camera devices…')
        threading.Thread(target=lambda: self.results.put(('devices', discover_devices())), daemon=True).start()

    def scan_modes(self):
        device = self.selected_device()
        self.notice.set('Reading supported modes…')
        threading.Thread(target=lambda: self.results.put(('modes', (device, parse_modes(v4l_info(device, '--list-formats-ext'))))), daemon=True).start()

    def choose_mode(self, _=None):
        if self.mode_choice.current() < 0:
            return
        mode = self.modes[self.mode_choice.current()]
        for key in ('fourcc', 'width', 'height', 'fps'):
            self.vars[key].set(str(getattr(mode, key)))
        self.vars['mode'].set('manual')

    def validated(self):
        result = deepcopy(self.config)
        cam = result.setdefault('camera', {})
        for key in ('type', 'mode', 'fourcc'):
            cam[key] = self.vars[key].get().strip()
        if cam['type'] not in ('webcam', 'basler', 'flir', 'none') or cam['mode'] not in ('auto', 'manual'):
            raise ValueError('Choose a listed backend and auto or manual capture.')
        cam['device'] = self.selected_device()
        if not cam['device']:
            raise ValueError('Enter auto, a camera index, or a device path.')
        for key in ('width', 'height'):
            cam[key] = int(self.vars[key].get())
            if not 1 <= cam[key] <= 16384:
                raise ValueError('Resolution must be between 1 and 16384 pixels.')
        cam['fps'] = float(self.vars['fps'].get())
        if not math.isfinite(cam['fps']) or not 0 < cam['fps'] <= 240:
            raise ValueError('Frame rate must be between 0 and 240 fps.')
        if len(cam['fourcc']) != 4 or not cam['fourcc'].isascii():
            raise ValueError('Pixel format must be a four-character ASCII code.')
        if cam['type'] == 'basler':
            cam['index'] = int(cam['device']) if cam['device'] != 'auto' else 0
        cam['reject-green'] = self.reject.get()
        result.setdefault('stage', {})['port'] = self.vars['stage-port'].get().strip()
        result['stage']['enabled'] = self.stage_enabled.get()
        result['stage']['invert-z'] = self.invert_z.get()
        theme = self.vars['theme'].get()
        if theme not in ('studio', 'studio-dark', 'darkly', 'flatly', 'superhero'):
            raise ValueError('Choose a listed appearance.')
        result.setdefault('ui', {})['theme'] = theme
        scale = float(self.vars['text-scale'].get())
        if scale not in (1.0, 1.25, 1.5):
            raise ValueError('Choose a listed text size.')
        result['ui']['text-scale'] = scale
        return result

    def apply(self):
        dispatcher = self.app.event_dispatcher
        if dispatcher.patterning_busy or dispatcher.autofocus_busy:
            messagebox.showinfo('Device busy', 'Finish exposure or autofocus before changing cameras.')
            return
        try:
            config = self.validated()
            cam = config['camera']
            if cam['type'] == 'webcam':
                camera = Webcam(settings=cam)
            elif cam['type'] == 'basler':
                from camera.pylon import BaslerPylon
                camera = BaslerPylon(cam.get('index', 0))
            elif cam['type'] == 'flir':
                from camera.flir.flir_camera import FlirCamera
                camera = FlirCamera()
            else:
                camera = None
            self.app.camera.replace(camera)
            configure_theme(self.app.root, config['ui']['theme'], config['ui']['text-scale'])
            for page in self.app.pages.values():
                page.canvas.configure(background=ttk.Style().colors.bg)
            self.notice.set('Camera changes applied. Stage changes require saving and restarting. Save to keep settings.')
        except Exception as exc:
            messagebox.showerror('Settings could not be applied', str(exc))

    def save(self):
        try:
            config = self.validated()
            save_config(self.path, config)
            self.config = config
            self.notice.set(f'Saved to {self.path}. Existing file backed up. Apply camera changes or restart to use saved settings.')
        except Exception as exc:
            messagebox.showerror('Settings could not be saved', str(exc))

    def tick(self):
        try:
            while True:
                kind, value = self.results.get_nowait()
                if kind == 'devices':
                    self.devices = value
                    self.device.configure(values=['Automatic selection'] + [d.label() for d in value])
                    self.notice.set(f'{len(value)} camera candidates found. Select a device or use auto.')
                else:
                    device, modes = value
                    if device != self.selected_device():
                        continue
                    self.modes = modes
                    self.mode_choice.configure(values=[m.label() for m in modes])
                    self.notice.set(f'{len(modes)} advertised modes. If none are listed, use Auto or enter a manual mode.')
        except queue.Empty:
            pass
        device = self.selected_device()
        hints = [d.connection_hint() for d in self.devices if device == 'auto' or d.device == device]
        self.connection_hint.set(next((hint for hint in hints if hint), 'Choose the microscope camera. Close OBS and other camera apps before connecting.'))
        camera = self.app.camera.camera
        self.status.set(getattr(camera, 'status', 'Camera disabled' if camera is None else 'Vendor camera') +
                        (f'\nMeasured capture rate: {camera.measured_fps:.1f} fps' if isinstance(camera, Webcam) else ''))
        self.frame.after(500, self.tick)
