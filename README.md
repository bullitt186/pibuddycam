# PiBuddyCam

[![CI](https://github.com/bullitt186/pibuddycam/actions/workflows/ci.yml/badge.svg)](https://github.com/bullitt186/pibuddycam/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/bullitt186/pibuddycam?include_prereleases)](https://github.com/bullitt186/pibuddycam/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**A Raspberry Pi Zero 2 W camera for Prusa Connect.** PiBuddyCam speaks the same cloud protocol
as Prusa's Buddy3D camera, reverse-engineered from its firmware. It also gives you what the
original doesn't: local RTSP, ONVIF for Home Assistant, MQTT, a local web console and timelapses.
Flash an SD card, answer a few questions in a setup wizard, and your printer has a camera.

> PiBuddyCam is an independent community project. It is not affiliated with or endorsed by
> Prusa Research. Formerly `prusa-buddy3d-camera-re`.

## Features

- **Prusa Connect camera:**
  - registers with a normal camera token and uploads snapshots;
  - follows settings from Connect (name, quality, intervals);
  - answers WebRTC live-view requests.
- **Local streaming:** two RTSP endpoints (one follows Prusa's setting, one always on), a JPEG
  snapshot URL, and ONVIF discovery for Home Assistant.
- **MQTT and Home Assistant discovery** with controls, state and an update entity.
- **Web console** over HTTPS: live view, camera settings (quality, 0°/90°/180°/270° rotation,
  intervals), timelapse library, diagnostics, recovery.
- **Timelapses** stored on the card, assembled into AVI, and exported over a Samba share.
- **A dependable appliance:**
  - a read-only system partition, so pulling the power is safe;
  - signed OTA updates that you approve and that roll back automatically;
  - recovery without reflashing.

## Quick start

1. **Get the hardware:** a Raspberry Pi Zero 2 W, a CSI camera module (OV5647 tested) and a
   microSD card (16 GB recommended).
2. **Flash** the image from the [latest release](https://github.com/bullitt186/pibuddycam/releases/latest)
   with Raspberry Pi Imager.
3. **Power on and join `PiBuddyCam-Setup-…`**. The wizard asks for your Wi-Fi, your Prusa Connect
   camera token and an admin password.

The full walkthrough is in [docs/install.md](docs/install.md).

## Status

Everything above works on real hardware. One external limitation applies: Prusa Connect currently
lists PiBuddyCam under *Other cameras* and may hide its live-view controls, while snapshots and
settings keep working. Rotation to 90°/270° is still to be verified on hardware. See
[docs/status.md](docs/status.md) for the details and [docs/roadmap.md](docs/roadmap.md) for
what's next.

## Documentation

| For | Start with |
|---|---|
| Using it | [Install](docs/install.md) · [User guide](docs/user-guide.md) · [Integrations](docs/integrations.md) · [Troubleshooting](docs/troubleshooting.md) · [Hardware](docs/hardware.md) |
| Developing it | [Architecture](docs/architecture.md) · [Development](docs/development.md) · [Releasing](docs/releasing.md) · [Roadmap](docs/roadmap.md) |
| The protocol | [Reverse engineering](docs/reverse-engineering/README.md): protocol spec, firmware evidence, parity tracker |

All documentation is listed in [docs/README.md](docs/README.md).

## Contributing

Bug reports, hardware test results and pull requests are welcome. Read
[CONTRIBUTING.md](CONTRIBUTING.md) first; it explains how to set up, run the tests and what must
never be committed. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md). Report security
issues privately as described in [SECURITY.md](SECURITY.md). Coding agents start at
[AGENTS.md](AGENTS.md).

## License and legal

PiBuddyCam is released under the [MIT License](LICENSE).
- **Independent implementation.** It interoperates with Prusa Connect based on independent
  analysis. The repository contains no Prusa firmware, binaries or decompiled code.
- **Trademarks.** Prusa®, Prusa Connect and Buddy3D are trademarks of Prusa Research a.s.
  Raspberry Pi is a trademark of Raspberry Pi Ltd.

See [NOTICE.md](NOTICE.md) for details, including third-party components in the image.

## Acknowledgements

The analysis built on public Prusa documentation and on community work, notably
[tlchandler/Improved-Buddy3D-Camera-for-Prusa-CORE-One](https://github.com/tlchandler/Improved-Buddy3D-Camera-for-Prusa-CORE-One).
Full provenance is in [docs/reverse-engineering/sources.md](docs/reverse-engineering/sources.md).
