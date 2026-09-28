"""Raspberry Pi 40-pin header table for the timelapse GPIO trigger.

Only pins that are safe to use as plain inputs on a Pi Zero 2 W are offered:

* **Excluded** GPIO0/1 (HAT ID EEPROM), GPIO2/3 (I2C1 with fixed 1.8 kOhm
  pull-ups), GPIO7-11 (SPI0) and GPIO14/15 (UART console).
* Everything else on the header is offered, with its physical pin number and a
  nearby ground pin so the wiring panel can name both.

A Prusa GPIO Hackerboard output is open-drain (active = connected to GND, else
floating), so the Pi reads it directly with its internal pull-up to 3.3 V: no
level shifting and no extra parts, only OUTn -> GPIO and Hackerboard GND -> Pi GND.

Stdlib only; pure data.
"""

#: ``bcm -> (physical header pin, nearest ground pin)``.
SAFE_PINS = {
    4: (7, 9),
    5: (29, 30),
    6: (31, 30),
    12: (32, 34),
    13: (33, 34),
    16: (36, 34),
    17: (11, 9),
    18: (12, 14),
    19: (35, 34),
    20: (38, 39),
    21: (40, 39),
    22: (15, 14),
    23: (16, 14),
    24: (18, 20),
    25: (22, 20),
    26: (37, 39),
    27: (13, 14),
}

#: Suggested layer (shot) and recording pins; GPIO17 has ground right next to it.
DEFAULT_SHOT_PIN = 17
DEFAULT_RECORD_PIN = 27

#: Header pins that are never offered, with the reason shown in the docs/UI.
EXCLUDED = {
    0: 'HAT ID EEPROM', 1: 'HAT ID EEPROM',
    2: 'I2C1 (fixed pull-ups)', 3: 'I2C1 (fixed pull-ups)',
    7: 'SPI0', 8: 'SPI0', 9: 'SPI0', 10: 'SPI0', 11: 'SPI0',
    14: 'UART console', 15: 'UART console',
}


def valid_pin(value):
    """True for a BCM number from the safe list (``bool`` is rejected)."""
    return type(value) is int and value in SAFE_PINS


def pin_table():
    """Return the dropdown rows, sorted by BCM number."""
    return [
        {
            'bcm': bcm,
            'header_pin': header,
            'ground_pin': ground,
            'label': f'GPIO{bcm} — header pin {header} (GND: pin {ground})',
        }
        for bcm, (header, ground) in sorted(SAFE_PINS.items())
    ]
