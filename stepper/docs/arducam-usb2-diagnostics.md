# Arducam B0477 USB 2 capture failure

Observed 2026-09-22 with OBS closed, including after physically reconnecting the camera.

- Device: Arducam B0477 (USB3 20MP), UVC / `uvcvideo`.
- Negotiated link: 480 Mb/s (USB 2).
- Only advertised capture mode: YUYV, 1280 × 720, 10 fps.
- Expected uncompressed frame size: 1,843,200 bytes.
- Direct `v4l2-ctl` mmap capture repeatedly returned 180,092-byte buffers flagged `error`.
- A 12-second initial capture produced no valid frames; the same incomplete frames recurred after reconnecting.
- Requesting 5 fps returned 10 fps. Lowering the frame rate is not supported by this connection's advertised mode.
- Settings were restored to 10 fps after the test.

The failure occurs before OpenCV conversion or Tk rendering. These observations
identify incomplete camera/driver delivery, but do not distinguish firmware,
USB transport, or driver faults. The application cannot reconstruct the missing
image data. Disabling its green-frame check merely permits a corrupt preview.

Arducam documents this USB 2 fallback as supported:
https://www.arducam.com/downloads/datasheet/B0477_20MP_IMX283_USB3.0_Camera_Datasheet.pdf

Next useful comparisons: a different known-good USB data cable/port, the same
camera on another host, or USB 3 operation. If USB 2 remains broken, provide
these results to Arducam for a B0477-specific firmware/compatibility diagnosis.
No firmware was flashed and no system USB-driver settings were changed.
