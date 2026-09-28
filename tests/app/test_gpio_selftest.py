"""The hardware self-test logic, driven against a fake chip (no hardware)."""
import struct
import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import gpio_selftest as st  # noqa: E402
import gpio_trigger as gt  # noqa: E402


class JumperChip:
    """A fake chip whose output line is wired to its input line."""

    def __init__(self, wired=True, filter_glitches=True, error=None):
        self.wired = wired
        self.filter_glitches = filter_glitches
        self.error = error
        self.out_level = 1
        self.pending = []
        self.closed = []
        self.requests = []

    def find_chip(self):
        if self.error:
            raise self.error
        return '/dev/gpiochip0'

    def request_output(self, chip, offset, value):
        self.requests.append(('out', offset, value))
        self.out_level = value
        return 10

    def request(self, chip, lines):
        self.requests.append(('in', lines))
        return 11

    def get_values(self, fd, count):
        return self.out_level if self.wired else 1

    def read_events(self, fd):
        events, self.pending = self.pending, []
        return events

    def set_value(self, fd, value):
        if value != self.out_level and self.wired:
            if (value == 1 and self.filter_glitches and len(self.pending) == 1
                    and self.pending[0][1] == gt.EDGE_FALLING):
                self.pending.clear()      # released before the debounce time: no event
            else:
                self.pending.append((17, gt.EDGE_RISING if value else gt.EDGE_FALLING))
        self.out_level = value

    def close(self, fd):
        self.closed.append(fd)


def fast_wait(chip, fd, timeout):
    return chip.read_events(fd)


class SelftestTests(unittest.TestCase):
    def run_it(self, chip):
        lines = []
        results = st.run(27, 17, backend=chip, wait=fast_wait, sleep=lambda s: None,
                         emit=lines.append)
        return results, lines

    def test_a_wired_jumper_passes_every_check(self):
        chip = JumperChip()
        results, lines = self.run_it(chip)
        names = {name: ok for name, ok, _d in results}
        self.assertTrue(names['GPIO chip found and readable'])
        self.assertTrue(names['falling edge is delivered'])
        self.assertTrue(names['rising edge is delivered'])
        self.assertTrue(names['input reads high while the output is high'])
        self.assertTrue(names['a glitch shorter than the debounce time is filtered'])
        self.assertTrue(all(names.values()))
        self.assertEqual(sorted(chip.closed), [10, 11])          # both lines released

    def test_a_chip_without_debounce_fails_the_glitch_check(self):
        results, _lines = self.run_it(JumperChip(filter_glitches=False))
        names = {name: ok for name, ok, _d in results}
        self.assertFalse(names['a glitch shorter than the debounce time is filtered'])
        self.assertTrue(names['falling edge is delivered'])

    def test_a_missing_jumper_fails_the_edge_checks(self):
        results, lines = self.run_it(JumperChip(wired=False))
        names = {name: ok for name, ok, _d in results}
        self.assertFalse(names['falling edge is delivered'])
        self.assertFalse(names['rising edge is delivered'])
        self.assertTrue(any(line.startswith('[FAIL] falling edge') for line in lines))

    def test_no_chip_stops_early_with_the_reason(self):
        results, _lines = self.run_it(
            JumperChip(error=gt.GpioError('permission denied (needs the gpio group)')))
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][1])
        self.assertIn('permission denied', results[0][2])

    def test_the_input_uses_the_runtime_request(self):
        chip = JumperChip()
        self.run_it(chip)
        kind, lines = chip.requests[1]
        self.assertEqual(kind, 'in')
        (offset, flags, debounce), = lines
        self.assertEqual((offset, debounce), (17, gt.SHOT_DEBOUNCE_US))
        self.assertTrue(flags & gt.FLAG_EDGE_FALLING)


class CliTests(unittest.TestCase):
    def test_unsafe_or_equal_pins_are_refused_before_touching_hardware(self):
        for args in (['--out-pin', '3', '--in-pin', '17'],
                     ['--out-pin', '17', '--in-pin', '17'],
                     ['--out-pin', '14', '--in-pin', '17']):
            self.assertEqual(st.main(args), 2, args)


class OutputRequestTests(unittest.TestCase):
    def test_output_request_layout(self):
        blob = gt.pack_output_request(27, 1)
        self.assertEqual(len(blob), 592)
        self.assertEqual(struct.unpack_from('<I', blob, 0)[0], 27)
        flags, num_attrs = struct.unpack_from('<QI', blob, 288)
        self.assertEqual((flags, num_attrs), (gt.FLAG_OUTPUT, 1))
        attr = struct.unpack_from('<IIQQ', blob, 288 + 32)
        self.assertEqual(attr, (gt.ATTR_OUTPUT_VALUES, 0, 1, 1))
        self.assertEqual(struct.unpack_from('<I', gt.pack_output_request(4, 0), 288 + 32 + 8)[0], 0)
        self.assertEqual(gt.GPIO_V2_LINE_SET_VALUES_IOCTL, 0xC010B40F)

    def test_set_value_packs_bits_and_mask(self):
        seen = {}

        def ioctl(fd, request, buf):
            seen['request'], seen['buf'] = request, bytes(buf)
            return buf

        gt.LinuxGpio(ioctl=ioctl).set_value(9, 1)
        self.assertEqual(seen['request'], gt.GPIO_V2_LINE_SET_VALUES_IOCTL)
        self.assertEqual(struct.unpack('<QQ', seen['buf']), (1, 1))
        gt.LinuxGpio(ioctl=ioctl).set_value(9, 0)
        self.assertEqual(struct.unpack('<QQ', seen['buf']), (0, 1))


if __name__ == '__main__':
    unittest.main()
