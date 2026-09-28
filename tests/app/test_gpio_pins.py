import sys
import unittest
from pathlib import Path

PI_DIR = Path(__file__).resolve().parents[2] / 'app'
sys.path.insert(0, str(PI_DIR))

import gpio_pins  # noqa: E402


class AllowlistTests(unittest.TestCase):
    def test_excluded_pins_are_never_offered(self):
        excluded = {0, 1, 2, 3, 7, 8, 9, 10, 11, 14, 15}
        self.assertFalse(excluded & set(gpio_pins.SAFE_PINS))
        self.assertEqual(set(gpio_pins.EXCLUDED), excluded)
        for pin in excluded:
            self.assertFalse(gpio_pins.valid_pin(pin), pin)

    def test_offered_pins(self):
        self.assertEqual(
            sorted(gpio_pins.SAFE_PINS),
            [4, 5, 6, 12, 13, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27])

    def test_valid_pin_is_strict_about_types(self):
        for value in (True, False, '17', 17.0, None, -1, 28, 40):
            self.assertFalse(gpio_pins.valid_pin(value), repr(value))
        self.assertTrue(gpio_pins.valid_pin(17))

    def test_defaults_and_ground_next_to_gpio17(self):
        self.assertEqual(gpio_pins.DEFAULT_SHOT_PIN, 17)
        self.assertEqual(gpio_pins.SAFE_PINS[17], (11, 9))
        self.assertEqual(gpio_pins.DEFAULT_RECORD_PIN, 27)
        self.assertEqual(gpio_pins.SAFE_PINS[27][0], 13)
        self.assertTrue(gpio_pins.valid_pin(gpio_pins.DEFAULT_RECORD_PIN))

    def test_header_pins_are_real_and_grounds_are_ground_pins(self):
        power_and_ground = {9, 14, 20, 25, 30, 34, 39, 6}
        used = set()
        for bcm, (header, ground) in gpio_pins.SAFE_PINS.items():
            self.assertTrue(1 <= header <= 40, bcm)
            self.assertIn(ground, power_and_ground - {6}, bcm)
            self.assertNotIn(header, used, 'header pin listed twice')
            used.add(header)

    def test_table_labels(self):
        table = gpio_pins.pin_table()
        self.assertEqual([row['bcm'] for row in table], sorted(gpio_pins.SAFE_PINS))
        row = next(r for r in table if r['bcm'] == 17)
        self.assertEqual(row['label'], 'GPIO17 — header pin 11 (GND: pin 9)')


if __name__ == '__main__':
    unittest.main()
