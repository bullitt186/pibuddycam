"""Entry point of ``rpicam-source.service``: build and exec the camera pipeline.

The unit starts this script through ``launcher.sh`` so the pipeline definition
ships with the signed application release instead of the image-owned unit. It
reads ``CAM_WIDTH``/``CAM_HEIGHT`` (``quality.env``) and ``CAM_ROTATION``
(``rotation.env``) from the environment, builds one shell pipeline whose stdout
is H.264 Annex-B, pipes it into ``stream_mux.py`` and replaces itself with
``/bin/bash`` (no extra resident Python process on the Zero 2 W).

Rotation paths (absolute, clockwise):

* ``0`` — plain ``rpicam-vid``.
* ``90``/``180``/``270`` — a GStreamer source: ``libcamerasrc`` → rotation →
  ``v4l2h264enc``. With the ``isp`` backend the bcm2835 ISP m2m device
  (``v4l2convert``) rotates: 180 via its ``horizontal_flip``/``vertical_flip``
  controls (**confirmed** on a Pi Zero 2 W: 30 fps up to 720p, 25 fps at
  1080p), 90/270 via its ``rotate`` control (**assumption**: not
  exposed by the tested kernel). With the ``software`` backend ``videoflip``
  rotates on the CPU at a capped frame rate.

180 is not done with ``rpicam-vid --rotation 180`` (sensor h/v flips): on the
OV5647 the flip sometimes does not reach the sensor while libcamera still
assumes the flipped Bayer order, so the picture comes out unrotated with red
and blue swapped, differently on each start (**confirmed** on hardware).

Every variant re-emits SPS/PPS before each IDR (``--inline`` /
``repeat_sequence_header``), which ``stream_mux`` relies on.
"""
import fcntl
import glob
import os
import shlex
import struct
import sys

import rotation as rotation_mod

DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
FRAMERATE = 30
#: Frame rate of the ISP backend above 1280x720: the bcm2835 m2m ISP manages about
#: 25 fps at 1080p, so asking the sensor for 30 only drops frames (**confirmed**,
#: Pi Zero 2 W; 720p keeps 30).
ISP_FHD_FRAMERATE = 25
#: Frame-rate cap of the CPU rotation fallback (Pi Zero 2 W, 4x Cortex-A53).
SOFTWARE_FRAMERATE = 10
INTRA_PERIOD = 30

#: Encoder bitrate (bit/s) by picture size. Without ``--bitrate`` rpicam-vid falls back
#: to the encoder default (about 10 Mbit/s at 1080p), far more than a static printer
#: scene needs. The HD/FHD values follow ONVIF's ``BitrateLimit`` (4000 kbit/s).
#: **assumption**: the values are picked for a still scene; measure on hardware.
BITRATE_SD = 1_500_000    # up to 640x480
BITRATE_HD = 3_000_000    # up to 1280x720
BITRATE_FHD = 4_000_000   # above that

BACKEND_ISP = 'isp'
BACKEND_SOFTWARE = 'software'

#: ``V4L2_CID_ROTATE`` and ``VIDIOC_QUERYCTRL`` (struct v4l2_queryctrl, 68 bytes).
V4L2_CID_ROTATE = 0x00980922
V4L2_CID_HFLIP = 0x00980914
V4L2_CID_VFLIP = 0x00980915
VIDIOC_QUERYCTRL = 0xC0445624
_QUERYCTRL_FORMAT = '=II32siiiiI2I'
#: ``/sys/class/video4linux/*/name`` of the bcm2835 ISP m2m device.
ISP_DEVICE_NAME = 'bcm2835-codec-isp'

_VIDEOFLIP = {90: '90r', 180: '180', 270: '90l'}
#: ``v4l2convert`` controls per rotation of the ISP backend.
_ISP_ROTATE = {90: 'rotate=90', 180: 'horizontal_flip=1,vertical_flip=1', 270: 'rotate=270'}


def _int_env(env, key, default):
    try:
        value = int(env.get(key, ''))
    except ValueError:
        return default
    return value if value > 0 else default


def settings_from_env(env):
    """Return validated ``(width, height, rotation)`` from an environment map."""
    width = _int_env(env, 'CAM_WIDTH', DEFAULT_WIDTH)
    height = _int_env(env, 'CAM_HEIGHT', DEFAULT_HEIGHT)
    try:
        rotation = rotation_mod.valid_rotation(int(env.get('CAM_ROTATION', '')))
    except ValueError:
        rotation = None
    if rotation is None:
        rotation = rotation_mod.DEFAULT_ROTATION
    return width, height, rotation


def bitrate_for(width, height):
    """Return the encoder bitrate in bit/s for a ``width`` x ``height`` picture."""
    pixels = width * height
    if pixels <= 640 * 480:
        return BITRATE_SD
    if pixels <= 1280 * 720:
        return BITRATE_HD
    return BITRATE_FHD


def rpicam_command(width, height, rotation):
    """The zero-cost ``rpicam-vid`` capture for 0 degrees."""
    return (
        f'/usr/bin/rpicam-vid -v 0 --codec h264 -t 0 --width {width} --height {height} '
        f'--framerate {FRAMERATE} --rotation {rotation} --profile baseline '
        f'--intra {INTRA_PERIOD} --bitrate {bitrate_for(width, height)} --flush --inline -o -'
    )


def gstreamer_command(width, height, rotation, backend):
    """The GStreamer capture for 90/180/270 degrees (90/270 output ``height``x``width``)."""
    out_w, out_h = rotation_mod.oriented(width, height, rotation)
    if backend == BACKEND_ISP:
        fps = FRAMERATE if width * height <= 1280 * 720 else ISP_FHD_FRAMERATE
        # disable-passthrough: same caps in and out (180) would skip the ISP.
        # dmabuf to the encoder: with copies 1080p reaches only about 17 fps.
        rotate = ('v4l2convert disable-passthrough=true capture-io-mode=dmabuf '
                  f'extra-controls="c,{_ISP_ROTATE[rotation]}"')
        encoder = 'v4l2h264enc output-io-mode=dmabuf-import'
    else:
        fps = SOFTWARE_FRAMERATE
        rotate = f'videoconvert ! videoflip video-direction={_VIDEOFLIP[rotation]} ! videoconvert'
        encoder = 'v4l2h264enc'
    return (
        '/usr/bin/gst-launch-1.0 -q '
        f'libcamerasrc ! video/x-raw,format=NV12,width={width},height={height},'
        f'framerate={fps}/1 '
        f'! {rotate} '
        f'! video/x-raw,format=NV12,width={out_w},height={out_h} '
        f'! {encoder} extra-controls="controls,repeat_sequence_header=1,'
        f'h264_profile=0,h264_i_frame_period={INTRA_PERIOD},'
        f'video_bitrate={bitrate_for(width, height)}" '
        # Quoted: the pipeline runs under bash, where a bare "(" is a syntax error.
        '! "video/x-h264,level=(string)4" '
        '! h264parse config-interval=-1 '
        '! video/x-h264,stream-format=byte-stream,alignment=au '
        '! fdsink fd=1'
    )


def build_source_command(width, height, rotation, backend, *, python, mux):
    """Return the full ``capture | stream_mux`` shell pipeline."""
    if rotation:
        capture = gstreamer_command(width, height, rotation, backend)
    else:
        capture = rpicam_command(width, height, rotation)
    return f'{capture} | {shlex.quote(python)} {shlex.quote(mux)}'


def find_isp_device(sysfs='/sys/class/video4linux'):
    """Return the ``/dev/videoN`` path of the bcm2835 ISP m2m device, or None."""
    for name_path in sorted(glob.glob(os.path.join(sysfs, 'video*', 'name'))):
        try:
            with open(name_path) as f:
                name = f.read().strip()
        except OSError:
            continue
        if name == ISP_DEVICE_NAME:
            return '/dev/' + os.path.basename(os.path.dirname(name_path))
    return None


def _control_range(fd, cid):
    """Return ``(minimum, maximum)`` of control ``cid`` on ``fd``, or None."""
    buf = bytearray(struct.pack(_QUERYCTRL_FORMAT, cid, 0, b'', 0, 0, 0, 0, 0, 0, 0))
    try:
        fcntl.ioctl(fd, VIDIOC_QUERYCTRL, buf)
    except OSError:
        return None
    _id, _type, _name, minimum, maximum, _step, _default, _flags, _r0, _r1 = (
        struct.unpack(_QUERYCTRL_FORMAT, bytes(buf)))
    return minimum, maximum


def isp_supports_rotation(device, rotation=270):
    """True when ``device`` can apply ``rotation``.

    180 needs both flip controls; 90/270 need ``V4L2_CID_ROTATE`` covering 270.
    """
    try:
        fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        if rotation == 180:
            flips = [_control_range(fd, cid) for cid in (V4L2_CID_HFLIP, V4L2_CID_VFLIP)]
            return all(r is not None and r[1] >= 1 for r in flips)
        rotate = _control_range(fd, V4L2_CID_ROTATE)
        return rotate is not None and rotate[0] <= 90 and rotate[1] >= 270
    finally:
        os.close(fd)


def detect_rotation_backend(rotation=270):
    """Pick the backend for ``rotation``: the ISP when it can, else software."""
    device = find_isp_device()
    if device and isp_supports_rotation(device, rotation):
        return BACKEND_ISP
    return BACKEND_SOFTWARE


def main(env=None):
    env = os.environ if env is None else env
    width, height, rotation = settings_from_env(env)
    backend = detect_rotation_backend(rotation) if rotation else None
    here = os.path.dirname(os.path.abspath(__file__))
    command = build_source_command(
        width, height, rotation, backend,
        python=sys.executable, mux=os.path.join(here, 'stream_mux.py'),
    )
    print(f'camera source: {width}x{height} rotation={rotation}'
          f'{" backend=" + backend if backend else ""}', file=sys.stderr, flush=True)
    # pipefail: a dead capture process must fail the unit so systemd restarts it.
    os.execv('/bin/bash', ['/bin/bash', '-o', 'pipefail', '-c', command])


if __name__ == '__main__':
    main()
