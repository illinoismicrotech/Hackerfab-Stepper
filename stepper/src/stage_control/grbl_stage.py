# Hacker Fab
# GRBL Stage Controller

import time

from stage_control.stage_controller import StageController, UnsupportedCommand

_SERIAL_TIMEOUT = 5.0  # seconds to wait for a GRBL response before raising


class GrblStage(StageController):
    """Controls a GRBL-based stepper motor stage over a serial port."""

    def __init__(self, controller_target, enable_homing: bool, invert_z: bool = False):
        self.controller_target = controller_target
        self.controller_target.timeout = _SERIAL_TIMEOUT
        self.enable_homing = enable_homing
        self.z_direction = -1.0 if invert_z else 1.0

        # Give GRBL time to boot, then discard the startup banner
        time.sleep(2.0)
        self.controller_target.reset_input_buffer()

        idle, pos = self._query_state()
        print(f"GRBL connected – idle={idle}, position={pos}")

    # ------------------------------------------------------------------ #
    # Internal communication
    # ------------------------------------------------------------------ #

    def _send_msg(self, msg: bytes) -> None:
        """Send a G-code line and wait for the 'ok' acknowledgement."""
        self.controller_target.write(msg)
        for _ in range(20):
            line = self.controller_target.read_until(b'\n').rstrip(b'\r\n')
            if not line:
                raise TimeoutError(
                    f"GRBL timeout waiting for response to {msg!r}"
                )
            if line == b'ok':
                return
            if line.startswith(b'error:'):
                raise RuntimeError(
                    f"GRBL error: {line.decode('ascii', errors='replace')}"
                )
            # Skip bracketed info/status messages (e.g. [MSG:...], [GC:...])
            if line.startswith(b'[') or line.startswith(b'<'):
                continue
            print(f"GRBL: {line.decode('ascii', errors='replace')}")
        raise RuntimeError("GRBL: too many unexpected responses")

    def _query_state(self) -> tuple[bool, tuple[float, float, float]]:
        """Send '?' and parse the real-time status response."""
        self.controller_target.write(b'?')
        for _ in range(10):
            line = (
                self.controller_target.read_until(b'\n')
                .rstrip(b'\r\n')
                .decode('ascii', errors='replace')
            )
            if line.startswith('<') and line.endswith('>'):
                idle, position = self._parse_state(line)
                # Keep readback in the same coordinate system as application moves.
                return idle, (position[0], position[1], position[2] * self.z_direction)
            if not line:
                break
            # Discard any queued 'ok' or info lines
        return False, (0.0, 0.0, 0.0)

    @staticmethod
    def _parse_state(resp: str) -> tuple[bool, tuple[float, float, float]]:
        """Parse a GRBL status string like '<Idle|MPos:0.000,0.000,0.000|...>'."""
        idle = False
        position = (0.0, 0.0, 0.0)
        for part in resp[1:-1].split('|'):  # strip the surrounding < >
            if 'Idle' in part:
                idle = True
            elif part.startswith('MPos:'):
                try:
                    x, y, z = part[5:].split(',')
                    position = (float(x), float(y), float(z))
                except (ValueError, IndexError):
                    pass
        return idle, position

    def wait_for_idle(self) -> None:
        """Poll GRBL status until it reports Idle."""
        while True:
            idle, _ = self._query_state()
            if idle:
                return
            time.sleep(0.05)

    # ------------------------------------------------------------------ #
    # StageController interface
    # ------------------------------------------------------------------ #

    def has_homing(self) -> bool:
        return self.enable_homing

    def home(self) -> None:
        if not self.enable_homing:
            raise UnsupportedCommand()
        self._send_msg(b'$H\n')

    def _move(self, microns: dict[str, float], relative: bool) -> None:
        self._send_msg(b'G91\n' if relative else b'G90\n')
        parts = ['G0']
        if 'x' in microns:
            parts.append(f'X{microns["x"] / 1000.0:.4f}')
        if 'y' in microns:
            parts.append(f'Y{microns["y"] / 1000.0:.4f}')
        if 'z' in microns:
            parts.append(f'Z{microns["z"] * self.z_direction / 1000.0:.4f}')
        self._send_msg((' '.join(parts) + '\n').encode('ascii'))

    def move_by(self, amounts: dict[str, float]) -> None:
        print('moving relative', amounts)
        self._move(amounts, relative=True)

    def move_to(self, amounts: dict[str, float]) -> None:
        print('moving absolute', amounts)
        self._move(amounts, relative=False)

    def close(self) -> None:
        if self.controller_target.is_open:
            self.controller_target.close()
