# Licensing, trademarks and legal notice

## License

PiBuddyCam's original work is released under the **MIT License** (see [LICENSE](LICENSE)). That
covers the application (`app/`), the image build (`image/`), the tests, the research helpers
(`research/`), the documentation (`docs/`), and the historical material in `_archive/`.

## Relationship to Prusa Research

- **Not affiliated.** PiBuddyCam is an independent community project. It is **not affiliated
  with, endorsed by or supported by Prusa Research**.
- **Trademarks.** Prusa®, Prusa Connect and Buddy3D are trademarks of Prusa Research a.s. They
  are used only to describe what PiBuddyCam interoperates with.
- **Interoperability.** The protocol was reconstructed from observed network behaviour, public
  Prusa documentation, and analysis of the shipped camera firmware. No Prusa source code is
  included (EU Software Directive 2009/24/EC Art. 6; US fair use).
- **Prusa's Camera API specification** is not included. The documentation links to Prusa's
  published copy.

### Firmware is never redistributed

The camera firmware, its binaries and any decompiled output are Prusa's property. They are kept
out of the repository and blocked by `.gitignore`:

| Excluded | What it is |
|---|---|
| `oem.img`, `boot.img`, `*.img` | Raw firmware images |
| `lp_app*` | The camera's main binary and exports of it |
| `oem_extracted/` | Unpacked firmware filesystem |
| `ghidrassist_*.db`, `*.gzf` | Analysis databases |

How to obtain and analyse the firmware yourself is described in
[docs/reverse-engineering/sources.md](docs/reverse-engineering/sources.md) and
[methods.md](docs/reverse-engineering/methods.md).

## Third-party components in the image

The SD-card image bundles Debian and Raspberry Pi OS packages (for example the Linux kernel,
GStreamer, libcamera and NetworkManager) and pure-Python wheels pinned in
`image/requirements.lock`: aiohttp, python-socketio, paho-mqtt and their dependencies. Each keeps
its own license. Every release publishes an SPDX SBOM
(`pibuddycam-pi-zero2w-<version>.spdx.json`) and the full installed-package list
(`.packages.txt`). The Debian copyright files are in the image under `/usr/share/doc/*/copyright`.

Raspberry Pi is a trademark of Raspberry Pi Ltd. ONVIF® is a trademark of ONVIF, Inc.; PiBuddyCam
implements the calls Home Assistant uses but is **not ONVIF certified**.

## No warranty

Provided as-is, without warranty of any kind (see [LICENSE](LICENSE)). Using an unofficial camera
with Prusa Connect may be affected by changes on Prusa's side or by their terms of service. You
are responsible for your own use.
