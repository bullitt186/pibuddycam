"""Hardware self-test for the GPIO timelapse trigger (run by hand, never by a unit).

Wire a jumper between two offered header pins (for example GPIO17 and GPIO27) and run,
as the service account so the udev/group rule is exercised too::

    sudo -u pibuddycam /opt/pibuddycam/venv/bin/python /opt/pibuddycam/gpio_selftest.py \
        --out-pin 27 --in-pin 17

Stop the camera runtime first (``systemctl stop pibuddycam.service``) if it has armed
either pin. The test drives ``--out-pin`` as a push-pull output and watches
``--in-pin`` with the same request the runtime uses (pull-up, falling edge, 10 ms
debounce), so it verifies on the real kernel what the unit tests only model:

1. the Pi SoC GPIO chip is found and openable (label, permissions);
2. the input reads high through its pull-up while nothing drives it;
3. a falling edge is delivered when the output goes low;
4. a rising edge is delivered when it returns high;
5. a pulse shorter than the debounce time is filtered.

It changes no configuration and touches nothing but the two pins. Exit status 0 means
every check passed. Stdlib only; importing runs nothing.
"""
import argparse
import select
import sys
import time

import gpio_pins
import gpio_trigger as gt

EVENT_TIMEOUT_SECONDS = 1.0


def _wait_events(backend, fd, timeout):
    """Return the edges that arrive within ``timeout`` seconds."""
    events = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return events
        ready, _w, _x = select.select([fd], [], [], remaining)
        if not ready:
            return events
        events.extend(backend.read_events(fd))
        if events:
            return events


def run(out_pin, in_pin, backend=None, wait=_wait_events, sleep=time.sleep,
        emit=print):
    """Run the checks; returns ``[(name, ok, detail), ...]``."""
    backend = backend or gt.LinuxGpio()
    results = []

    def record(name, ok, detail=''):
        results.append((name, bool(ok), detail))
        emit(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f': {detail}' if detail else ''))
        return ok

    try:
        chip = backend.find_chip()
    except gt.GpioError as e:
        record('GPIO chip found and readable', False, str(e))
        return results
    record('GPIO chip found and readable', True, chip)

    out_fd = in_fd = None
    try:
        try:
            out_fd = backend.request_output(chip, out_pin, 1)
            in_fd = backend.request(chip, [(
                in_pin, gt.FLAG_EDGE_FALLING | gt.FLAG_EDGE_RISING,
                gt.SHOT_DEBOUNCE_US)])
        except gt.GpioError as e:
            record('both lines can be requested', False, str(e))
            return results
        record('both lines can be requested', True,
               f'output GPIO{out_pin}, input GPIO{in_pin} with pull-up')
        sleep(0.05)
        backend.read_events(in_fd)      # drop anything from the request itself

        level = backend.get_values(in_fd, 1) & 1
        record('input reads high while the output is high', level == 1,
               f'level={level}; is the jumper fitted between the two pins?')

        backend.set_value(out_fd, 0)
        events = wait(backend, in_fd, EVENT_TIMEOUT_SECONDS)
        record('falling edge is delivered', any(edge == gt.EDGE_FALLING
                                                for _o, edge in events),
               f'events={[(o, e) for o, e in events]}')

        backend.set_value(out_fd, 1)
        events = wait(backend, in_fd, EVENT_TIMEOUT_SECONDS)
        record('rising edge is delivered', any(edge == gt.EDGE_RISING
                                               for _o, edge in events),
               f'events={[(o, e) for o, e in events]}')

        backend.read_events(in_fd)
        backend.set_value(out_fd, 0)
        sleep(gt.SHOT_DEBOUNCE_US / 4_000_000)     # a quarter of the debounce time
        backend.set_value(out_fd, 1)
        events = wait(backend, in_fd, 0.3)
        record('a glitch shorter than the debounce time is filtered', not events,
               f'events={[(o, e) for o, e in events]}')
    finally:
        for fd in (in_fd, out_fd):
            if fd is not None:
                backend.close(fd)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description='PiBuddyCam GPIO hardware self-test')
    parser.add_argument('--out-pin', type=int, required=True,
                        help='BCM pin driven by the test (jumper to --in-pin)')
    parser.add_argument('--in-pin', type=int, required=True,
                        help='BCM pin watched with the runtime settings')
    args = parser.parse_args(argv)
    for pin in (args.out_pin, args.in_pin):
        if not gpio_pins.valid_pin(pin):
            print(f'GPIO{pin} is not an offered pin (see docs/hardware.md)', file=sys.stderr)
            return 2
    if args.out_pin == args.in_pin:
        print('--out-pin and --in-pin must differ', file=sys.stderr)
        return 2
    results = run(args.out_pin, args.in_pin)
    failed = [name for name, ok, _detail in results if not ok]
    print(f'{len(results) - len(failed)}/{len(results)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
