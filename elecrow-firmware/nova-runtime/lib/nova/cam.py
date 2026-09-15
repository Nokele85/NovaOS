# GC2145 camera, through the `camera` module of cnadler86/micropython-camera-API.
#
# Present only on a MicroPython image built with that module; on stock
# MicroPython `available()` is False and nothing else here is called.
# Rook's lessons (README): the sensor has no hardware JPEG - RGB565 + CIF -
# and OPI PSRAM is mandatory. A picture is an observation with a timestamp,
# never a fact about who is in the room.
# Status: pins from Rook's board.h; this wrapper UNVERIFIED on the board.
from . import board as B

_cam = None


def available():
    try:
        import camera  # noqa: F401
        return True
    except ImportError:
        return False


def open_camera():
    global _cam
    if _cam is not None:
        return _cam
    from camera import Camera, FrameSize, GrabMode, PixelFormat
    _cam = Camera(data_pins=list(B.CAM_DATA), vsync_pin=B.CAM_VSYNC,
                  href_pin=B.CAM_HREF, sda_pin=B.CAM_SDA, scl_pin=B.CAM_SCL,
                  pclk_pin=B.CAM_PCLK, xclk_pin=B.CAM_XCLK,
                  xclk_freq=B.CAM_XCLK_HZ, powerdown_pin=-1, reset_pin=-1,
                  pixel_format=PixelFormat.RGB565, frame_size=FrameSize.CIF,
                  fb_count=2, grab_mode=GrabMode.LATEST)
    return _cam


def capture():
    """One RGB565 CIF frame (352x288) as bytes."""
    return bytes(open_camera().capture())


def close():
    global _cam
    if _cam is not None:
        try:
            _cam.deinit()
        finally:
            _cam = None
