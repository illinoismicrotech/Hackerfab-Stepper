from tk_runtime import enable_font_support
enable_font_support()

import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import toml

from camera.discovery import CameraDevice, CaptureMode, candidate_modes, parse_modes
from camera.webcam import Webcam, _capture, frame_problem
from settings import save_config


class CameraTests(unittest.TestCase):
    def test_advertised_formats_keep_resolution_and_fps_together(self):
        modes = parse_modes("""
[0]: 'MJPG' (Motion-JPEG)
    Size: Discrete 1280x720
        Interval: Discrete 0.033s (30.000 fps)
        Interval: Discrete 0.067s (15.000 fps)
[1]: 'YUYV' (YUYV)
    Size: Discrete 640x480
        Interval: Discrete 0.100s (10.000 fps)
""")
        self.assertEqual(modes, [CaptureMode('MJPG', 1280, 720, 30), CaptureMode('MJPG', 1280, 720, 15), CaptureMode('YUYV', 640, 480, 10)])

    def test_green_corruption_is_optional_and_black_is_valid(self):
        green = np.zeros((64, 64, 3), np.uint8)
        green[:, :, 1] = 128
        self.assertIn('green', frame_problem(green))
        self.assertEqual(frame_problem(green, False), '')
        self.assertEqual(frame_problem(np.zeros_like(green)), '')
        self.assertTrue(frame_problem(None))

    def test_b0477_usb2_fallback_is_explained(self):
        camera = CameraDevice('/dev/video4', 'Arducam B0477 (USB3 20MP)', '480')
        self.assertIn('YUYV, 10 fps', camera.connection_hint())
        self.assertEqual(CameraDevice('/dev/video4', camera.name, '5000').connection_hint(), '')

    def test_manual_mode_does_not_fallback(self):
        self.assertEqual(candidate_modes('0', {'mode': 'manual', 'width': 640, 'height': 480, 'fps': 15, 'fourcc': 'YUYV'}), [CaptureMode('YUYV', 640, 480, 15)])

    def test_worker_rejects_green_then_streams_rgb(self):
        stop = threading.Event()
        events = []
        class Channel:
            def put(self, item, timeout=0):
                events.append(item)
                if item[0] == 'frame':
                    stop.set()
        class Capture:
            def __init__(self, green):
                self.green = green
                self.released = False
            def isOpened(self): return True
            def set(self, *args): return True
            def get(self, prop): return 30 if prop == 5 else 0
            def read(self):
                frame = np.zeros((48, 64, 3), np.uint8)
                frame[:] = (0, 128, 0) if self.green else (10, 20, 180)
                return True, frame
            def release(self): self.released = True
        rejected, accepted = Capture(True), Capture(False)
        with patch('camera.webcam.candidate_modes', return_value=[None, None]), patch('camera.webcam.cv2.VideoCapture', side_effect=[rejected, accepted]):
            _capture({'device': '0'}, Channel(), stop)
        frame = next(value for kind, value in events if kind == 'frame')
        self.assertEqual(frame[0, 0].tolist(), [180, 20, 10])
        self.assertTrue(rejected.released and accepted.released)
        self.assertTrue(any(kind == 'ready' for kind, _ in events))

    def test_missing_device_reports_actionable_error(self):
        channel = queue.Queue()
        with patch('camera.webcam.discover_devices', return_value=[]):
            _capture({'device': 'auto'}, channel, threading.Event())
        kind, message = channel.get_nowait()
        self.assertEqual(kind, 'error')
        self.assertIn('No camera found', message)

    def test_busy_device_reports_failure_and_releases(self):
        channel = queue.Queue()
        cap = Mock()
        cap.isOpened.return_value = False
        with patch('camera.webcam.candidate_modes', return_value=[None]), patch('camera.webcam.cv2.VideoCapture', return_value=cap):
            _capture({'device': '0'}, channel, threading.Event())
        cap.release.assert_called_once()
        self.assertEqual(channel.get_nowait()[0], 'status')
        attempts = []
        while not channel.empty():
            attempts.append(channel.get_nowait())
        kind, message = attempts[-1]
        self.assertEqual(kind, 'error')
        self.assertIn('could not open device', message)
        self.assertIn('Close OBS', message)

    def test_auto_does_not_choose_auxiliary_ir_stream(self):
        channel = queue.Queue()
        with patch('camera.webcam.discover_devices', return_value=[CameraDevice('2', 'IR')]), patch('camera.webcam.candidate_modes', return_value=[CaptureMode('GREY', 640, 360, 30)]), patch('camera.webcam.cv2.VideoCapture') as capture:
            _capture({'device': 'auto'}, channel, threading.Event())
        capture.assert_not_called()
        self.assertIn('Skipping monochrome', channel.get_nowait()[1])
        self.assertEqual(channel.get_nowait()[0], 'error')

    def test_watchdog_terminates_a_blocked_driver(self):
        camera = Webcam()
        camera.channel = Mock()
        camera.channel.get.side_effect = queue.Empty
        camera.process = Mock()
        camera.process.is_alive.return_value = True
        with patch('camera.webcam.time.monotonic', side_effect=[0, 13]):
            camera._monitor()
        camera.process.terminate.assert_called_once()
        self.assertIn('timed out', camera.status)
        self.assertFalse(camera.isOpen())

    def test_save_preserves_other_sections_and_backs_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('# Original\n[alignment]\nenabled = false\n')
            original = path.read_text()
            config = {'alignment': {'enabled': False}, 'camera': {'device': 'auto'}}
            save_config(path, config)
            self.assertEqual(toml.load(path), config)
            self.assertEqual(next(Path(directory).glob('*.bak')).read_text(), original)


if __name__ == '__main__':
    unittest.main()
