import unittest
from unittest.mock import Mock, patch

from stage_control.grbl_stage import GrblStage


class StageDirectionTests(unittest.TestCase):
    def stage(self, invert_z):
        serial = Mock()
        serial.read_until.return_value = b'<Idle|MPos:1.000,2.000,-3.000>\n'
        with patch('stage_control.grbl_stage.time.sleep'):
            stage = GrblStage(serial, False, invert_z=invert_z)
        serial.write.reset_mock()
        serial.read_until.return_value = b'ok\n'
        return stage, serial

    def test_reverses_absolute_and_relative_z_only(self):
        stage, serial = self.stage(True)
        stage.move_to({'x': 1000, 'y': -2000, 'z': 3000})
        self.assertEqual([call.args[0] for call in serial.write.call_args_list], [b'G90\n', b'G0 X1.0000 Y-2.0000 Z-3.0000\n'])
        serial.write.reset_mock()
        stage.move_by({'z': -250})
        self.assertEqual([call.args[0] for call in serial.write.call_args_list], [b'G91\n', b'G0 Z0.2500\n'])

    def test_readback_matches_app_coordinates(self):
        stage, serial = self.stage(True)
        serial.read_until.return_value = b'<Idle|MPos:1.000,2.000,-3.000>\n'
        self.assertEqual(stage._query_state(), (True, (1., 2., 3.)))

    def test_can_disable_reversal(self):
        stage, serial = self.stage(False)
        stage.move_to({'z': 3000})
        self.assertEqual(serial.write.call_args.args[0], b'G0 Z3.0000\n')
        serial.read_until.return_value = b'<Idle|MPos:1.000,2.000,-3.000>\n'
        self.assertEqual(stage._query_state(), (True, (1., 2., -3.)))

if __name__ == '__main__':
    unittest.main()
