"""Camera source pipeline builder (rpicam-source.service entry point)."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
REPO = PI_DIR.parent
sys.path.insert(0, str(PI_DIR))

import camera_source  # noqa: E402

PY = '/data/pibuddycam/releases/current/venv/bin/python'
MUX = '/data/pibuddycam/releases/current/stream_mux.py'


def build(width, height, rotation, backend=None):
    return camera_source.build_source_command(
        width, height, rotation, backend, python=PY, mux=MUX)


class SettingsFromEnvTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(camera_source.settings_from_env({}), (1920, 1080, 0))

    def test_reads_values(self):
        env = {'CAM_WIDTH': '1280', 'CAM_HEIGHT': '720', 'CAM_ROTATION': '270'}
        self.assertEqual(camera_source.settings_from_env(env), (1280, 720, 270))

    def test_invalid_values_fall_back(self):
        for value in ('45', 'abc', '-90', ''):
            with self.subTest(value=value):
                env = {'CAM_WIDTH': 'x', 'CAM_HEIGHT': '0', 'CAM_ROTATION': value}
                self.assertEqual(camera_source.settings_from_env(env), (1920, 1080, 0))


class CommandTests(unittest.TestCase):
    def test_zero_and_180_use_rpicam_rotation_flag(self):
        for degrees in (0, 180):
            with self.subTest(degrees=degrees):
                cmd = build(1920, 1080, degrees)
                self.assertEqual(cmd, (
                    '/usr/bin/rpicam-vid -v 0 --codec h264 -t 0 --width 1920 --height 1080 '
                    f'--framerate 30 --rotation {degrees} --profile baseline --intra 30 '
                    f'--flush --inline -o - | {PY} {MUX}'))

    def test_isp_backend_rotates_in_v4l2convert_with_swapped_caps(self):
        cmd = build(1920, 1080, 90, camera_source.BACKEND_ISP)
        self.assertTrue(cmd.startswith('/usr/bin/gst-launch-1.0 -q libcamerasrc'))
        self.assertIn('width=1920,height=1080,framerate=30/1', cmd)
        self.assertIn('v4l2convert extra-controls="c,rotate=90"', cmd)
        self.assertIn('video/x-raw,format=NV12,width=1080,height=1920', cmd)
        self.assertIn('repeat_sequence_header=1', cmd)
        self.assertIn('stream-format=byte-stream', cmd)
        self.assertNotIn('videoflip', cmd)
        self.assertTrue(cmd.endswith(f'fdsink fd=1 | {PY} {MUX}'))

    def test_software_backend_uses_videoflip_at_capped_rate(self):
        cmd = build(1280, 720, 270, camera_source.BACKEND_SOFTWARE)
        self.assertIn('videoflip video-direction=90l', cmd)
        self.assertIn(f'framerate={camera_source.SOFTWARE_FRAMERATE}/1', cmd)
        self.assertIn('width=720,height=1280', cmd)
        self.assertNotIn('v4l2convert', cmd)
        self.assertIn('videoflip video-direction=90r',
                      build(1280, 720, 90, camera_source.BACKEND_SOFTWARE))

    def test_paths_are_shell_quoted(self):
        cmd = camera_source.build_source_command(
            640, 480, 0, None, python='/opt/a b/python', mux='/opt/a b/stream_mux.py')
        self.assertTrue(cmd.endswith("| '/opt/a b/python' '/opt/a b/stream_mux.py'"))


class BackendDetectionTests(unittest.TestCase):
    def test_finds_isp_device_by_sysfs_name(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for node, name in (('video10', 'bcm2835-codec-decode'),
                               ('video12', 'bcm2835-codec-isp')):
                Path(d, node).mkdir()
                Path(d, node, 'name').write_text(name + '\n')
            self.assertEqual(camera_source.find_isp_device(d), '/dev/video12')

    def test_no_isp_device_selects_software(self):
        with patch.object(camera_source, 'find_isp_device', return_value=None):
            self.assertEqual(camera_source.detect_rotation_backend(),
                             camera_source.BACKEND_SOFTWARE)

    def test_isp_with_rotate_control_selects_isp(self):
        with patch.object(camera_source, 'find_isp_device', return_value='/dev/video12'), \
             patch.object(camera_source, 'isp_supports_rotation', return_value=True):
            self.assertEqual(camera_source.detect_rotation_backend(),
                             camera_source.BACKEND_ISP)

    def test_unopenable_device_has_no_rotation(self):
        self.assertFalse(camera_source.isp_supports_rotation('/nonexistent/video99'))


class MainTests(unittest.TestCase):
    def test_execs_bash_with_pipefail_and_release_local_mux(self):
        with patch.object(camera_source.os, 'execv') as execv:
            camera_source.main({'CAM_WIDTH': '640', 'CAM_HEIGHT': '480', 'CAM_ROTATION': '180'})
        path, argv = execv.call_args.args
        self.assertEqual(path, '/bin/bash')
        self.assertEqual(argv[:4], ['/bin/bash', '-o', 'pipefail', '-c'])
        self.assertIn('--rotation 180', argv[4])
        self.assertIn(str(PI_DIR / 'stream_mux.py'), argv[4])

    def test_zero_rotation_never_probes_the_isp(self):
        with patch.object(camera_source.os, 'execv'), \
             patch.object(camera_source, 'detect_rotation_backend') as detect:
            camera_source.main({})
        detect.assert_not_called()


class UnitTests(unittest.TestCase):
    def test_unit_reads_rotation_env_and_execs_camera_source(self):
        unit = (PI_DIR / 'systemd' / 'rpicam-source.service').read_text()
        self.assertIn('Environment=CAM_WIDTH=1920 CAM_HEIGHT=1080 CAM_ROTATION=0', unit)
        self.assertIn('EnvironmentFile=-/etc/pibuddycam/rotation.env', unit)
        self.assertIn('ExecStart=/opt/pibuddycam/launcher.sh camera_source.py', unit)
        self.assertNotIn('--rotation 180', unit)
        # The rotation file is read after the quality files, independent of them.
        self.assertLess(unit.index('quality.live.env'), unit.index('rotation.env\n'))


if __name__ == '__main__':
    unittest.main()
