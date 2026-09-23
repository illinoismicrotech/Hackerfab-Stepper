"""Integration smoke check: requires a display; never opens physical hardware."""
import unittest
import traceback
from unittest.mock import patch
from types import SimpleNamespace
import tempfile
from pathlib import Path

from tk_runtime import enable_font_support
enable_font_support()

import tkinter
import tkinter.font as tkfont
from PIL import Image
import ttkbootstrap as ttk
import gui
from projector import TkProjector
from stage_control.stage_controller import StageController


class GuiTests(unittest.TestCase):
    def test_workspaces_settings_and_camera_disabled(self):
        try:
            root = ttk.Window(themename='darkly')
        except tkinter.TclError as exc:
            self.skipTest(f'Display unavailable: {exc}')
        errors = []
        root.report_callback_exception = lambda *args: errors.append("".join(traceback.format_exception(*args)))
        config = gui.LithographerConfig(StageController(), None, .25, 4167, 25000,
                  gui.AlignmentConfig(False, '', 1820, 280, 269, 1075, -1100, 800))
        with tempfile.TemporaryDirectory() as folder, patch('gui.TkProjector', return_value=TkProjector(root)), patch('gui.YOLO', side_effect=RuntimeError('No model in smoke test')):
            app = gui.LithographerGui(config, root, {'camera': {'type': 'none'}}, Path(folder) / 'config.toml')
            try:
                root.update()
                self.assertFalse(any(isinstance(widget, tkinter.Toplevel) for widget in root.winfo_children()))
                app.camera.label.event_generate('<Button-1>')
                root.update()
                self.assertEqual(app.fullscreen.kind, 'camera')
                self.assertTrue(app.fullscreen.frame.winfo_ismapped())
                app.fullscreen.close_button.invoke()
                self.assertIsNone(app.fullscreen.kind)
                app.projector_display.label.event_generate('<Button-1>')
                root.update()
                self.assertEqual(app.fullscreen.kind, 'projector')
                app.fullscreen._escape()
                self.assertIsNone(app.fullscreen.kind)
                # Exposure output uses the same root; exiting aborts instead of
                # silently counting an exposure with no pattern displayed.
                app.event_dispatcher.set_patterning_busy(True)
                app.event_dispatcher.hardware.projector.show(Image.new('RGB', (1280, 720), 'black'))
                self.assertEqual(app.fullscreen.kind, 'projector')
                app.fullscreen.close_button.invoke()
                self.assertTrue(app.event_dispatcher.should_abort)
                self.assertIsNone(app.fullscreen.kind)
                app.event_dispatcher.set_patterning_busy(False)
                app.event_dispatcher.should_abort = False
                for name in app.pages:
                    app.show_page(name)
                    root.update()
                    self.assertTrue(app.pages[name].frame.winfo_ismapped())
                for index in range(3):
                    app.mode_select_frame.notebook.select(index)
                    root.update()
                # Dropdown labels map to configuration values and popup content
                # has a safe inset even under compositor-rounded corners.
                mode = app.settings_page.widgets['mode']
                mode.current(1)
                mode.event_generate('<<ComboboxSelected>>')
                root.update()
                self.assertEqual(app.settings_page.vars['mode'].get(), 'manual')
                root.tk.call('ttk::combobox::Post', str(mode))
                popup = root.tk.call('ttk::combobox::PopdownWindow', str(mode))
                self.assertEqual(int(root.tk.call(f'{popup}.f.l', 'cget', '-borderwidth')), 12)
                root.tk.call('ttk::combobox::Unpost', str(mode))
                app.settings_page.vars['text-scale'].set('1.25')
                app.settings_page.apply_appearance()
                self.assertEqual(tkfont.nametofont('TkDefaultFont').cget('size'), 15)
                # Explicit stage controls retain movement locking.
                controls = app.mode_select_frame.red_mode_frame.stage_position_frame
                right = next(w for w in controls.xy_widgets if isinstance(w, ttk.Button) and w.cget('text') == 'X →')
                controls.step_size.set('25')
                with patch.object(app.event_dispatcher, 'move_relative') as move:
                    right.invoke()
                    move.assert_called_once_with({'x': 25.0})
                    app.event_dispatcher.set_patterning_busy(True)
                    root.update()
                    right.invoke()
                    self.assertEqual(move.call_count, 1)
                    app.event_dispatcher.set_patterning_busy(False)
                    root.update()
                # No accidental blank exposure, and the image chooser updates
                # the full-resolution pattern plus its preview.
                with patch('gui.messagebox.showinfo') as notice:
                    app.event_dispatcher.begin_patterning()
                    notice.assert_called_once()
                pattern_path = Path(folder) / 'pattern.png'
                Image.new('RGB', (48, 32), 'white').save(pattern_path)
                with patch('gui.filedialog.askopenfilename', return_value=str(pattern_path)):
                    app.mode_select_frame.pattern_upload_frame.choose_image()
                self.assertEqual(app.event_dispatcher.pattern_image.size, (48, 32))
                self.assertEqual(app.event_dispatcher.pattern_image_path, str(pattern_path))
                self.assertTrue(app.settings_page.invert_z.get())
                self.assertEqual(app.settings_page.vars['theme'].get(), 'studio-dark')
                app.camera.camera = SimpleNamespace(state='error', status='No decoded frames', close=lambda: None)
                root.after(100, root.quit)
                root.mainloop()
                self.assertIn('Camera connection failed', app.camera.label.cget('text'))
                self.assertEqual(str(app.camera.snapshot.button.cget('state')), 'disabled')
                app.settings_page.apply()
                app.settings_page.save()
                self.assertTrue((Path(folder) / 'config.toml').exists())
                self.assertIsNone(app.event_dispatcher.camera)
                self.assertEqual(gui.compute_focus_score(__import__('numpy').zeros((48, 64, 3), dtype='uint8'), False), 0)
                self.assertEqual(errors, [])
            finally:
                app.cleanup()


if __name__ == '__main__':
    unittest.main()
