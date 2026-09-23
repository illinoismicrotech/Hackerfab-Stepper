"""Fullscreen live preview inside the existing application window."""
import tkinter as tk
from PIL import Image, ImageTk


class FullscreenPreview:
    def __init__(self, root, on_close=None):
        self.root = root
        self.on_close = on_close
        self.kind = None
        self.provider = None
        self.timer = None
        self.photo = None
        self.previous_fullscreen = False
        self.previous_focus = None
        self.frame = tk.Frame(root, background='black')
        self.label = tk.Label(self.frame, background='black', foreground='white', font='TkDefaultFont')
        self.label.pack(fill='both', expand=True)
        self.close_button = tk.Button(self.frame, text='×', font=('sans', 24), command=self.close,
                                      background='#252525', foreground='white', borderwidth=0,
                                      activebackground='#444444', activeforeground='white', cursor='hand2')
        self.close_button.place(relx=1, x=-16, y=16, anchor='ne', width=52, height=52)
        self.escape_binding = root.bind('<Escape>', self._escape, add='+')

    def open(self, kind, provider):
        if self.kind is None:
            self.previous_fullscreen = bool(self.root.attributes('-fullscreen'))
            self.previous_focus = self.root.focus_get()
        self.kind, self.provider = kind, provider
        self.frame.place(x=0, y=0, relwidth=1, relheight=1)
        self.frame.lift()
        self.root.attributes('-fullscreen', True)
        self.close_button.focus_set()
        self.refresh()
        if self.timer is None:
            self.timer = self.root.after(66, self._tick)

    def refresh(self):
        if self.kind is None:
            return
        picture = self.provider()
        if picture is None:
            self.photo = None
            self.label.configure(image='', text='No live camera image' if self.kind == 'camera' else 'No projector output')
            return
        image = picture.copy()
        width = max(1, self.frame.winfo_width())
        height = max(1, self.frame.winfo_height())
        if width <= 1 or height <= 1:
            width, height = self.root.winfo_width(), self.root.winfo_height()
        # Fit the whole image; never crop the pattern or change its aspect ratio.
        ratio = min(width / image.width, height / image.height)
        size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
        image = image.resize(size, Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(image, master=self.root)
        self.label.configure(image=self.photo, text='')

    def _tick(self):
        self.timer = None
        if self.kind is not None:
            self.refresh()
            self.timer = self.root.after(66, self._tick)

    def _escape(self, _event=None):
        if self.kind is not None:
            self.close()
            return 'break'

    def close(self):
        kind = self.kind
        if kind is None:
            return
        self.kind = None
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        self.frame.place_forget()
        self.root.attributes('-fullscreen', self.previous_fullscreen)
        self.label.configure(image='', text='')
        self.photo = None
        if self.previous_focus is not None and self.previous_focus.winfo_exists():
            self.previous_focus.focus_set()
        if self.on_close:
            self.on_close(kind)

    def cleanup(self):
        self.close()
        self.root.unbind('<Escape>', self.escape_binding)
        self.frame.destroy()
