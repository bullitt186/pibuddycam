# Changelog

Notable changes to PiBuddyCam. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/). "Needs a new image" means the change
only reaches a device by reflashing; everything else arrives as an OTA update.

## [Unreleased]

### Added
- Network settings in the console (System, Network). Changes need a new image, see below.
  - Change Wi-Fi network and password, DHCP or a static IPv4 address, DNS servers, and the
    hostname, and scan for networks.
  - A change that does not come up within about a minute is reverted automatically; as a last
    resort the setup hotspot starts.
  - Time servers (up to three; blank means DHCP, then the Debian pool) and a clock status.
- Timelapse GPIO trigger for the Prusa GPIO Hackerboard (Camera, Timelapse trigger).
  - A layer pulse captures a frame taken after the pulse.
  - An optional recording pin creates one session folder per print and builds its video
    automatically.
  - The Timelapses view has a print-session picker and a warning while the clock is unsynchronized.
  - Wiring and printer G-code are shown in the console and in the [user guide](docs/user-guide.md).
- `http://<address>` now redirects to the HTTPS console instead of showing plain text.

- Network watchdog: a claimed camera with no usable Wi-Fi for 10 minutes starts the setup hotspot
  and retries its network every 10 minutes (disable with `PIBUDDYCAM_NETWORK_WATCHDOG_MINUTES=0`).
  Needs a new image. The hotspot is open, so its login page is reachable in range while it runs.

### Changed
- The setup wizard's Wi-Fi scan uses the root `wifi-scan` verb.
- `pibuddycam-priv` gains the verbs `network-apply`, `hostname-apply`, `wifi-scan` and `ntp-apply`.

### Upgrade notes
- **OTA-deliverable:** the HTTP redirect, the Network and timelapse UI, the API, the settings and
  the session storage and builds.
- **Needs a new image:** applying network changes, the hostname, Wi-Fi scanning (root helper verbs,
  the `pibuddycam-network-apply` unit), access to the GPIO chip (the `gpio` group and udev rule),
  and the persistent clock and DHCP time servers (the NetworkManager dispatcher script and the
  state-directory mount). On an older image the UI degrades: the Network card reports *needs a
  newer image*, and the GPIO status shows *permission denied*.
- The image installer now creates missing device groups (`gpio` among them) and no longer ignores
  a failing group assignment.

## [1.4.0]: 2026-09-28 (needs a new image)

The project is now **PiBuddyCam** and is prepared for open source. This is the first image with
the new names, the built-in update source and the new signing key.

### Changed
- The project, repository (`bullitt186/pibuddycam`) and runtime are renamed:
  - service account and directories `pibuddycam` (`/data`, `/etc`, `/opt`, `/usr/share`);
  - units `pibuddycam*.service` and `pibuddycam.target`;
  - helper `pibuddycam-priv`;
  - updater config `/etc/pibuddycam-updater.conf` and `PIBUDDYCAM_*` variables;
  - hostname `pibuddycam`, setup network `PiBuddyCam-Setup-<id>`;
  - image and bundle files `pibuddycam-*`.
- The MQTT prefix and Home Assistant discovery IDs are now `pibuddycam`. Home Assistant creates a
  new device; delete the old `buddy3d_*` one.
- The repository is reorganised for contributors:
  - new user and contributor documentation;
  - `app/` (formerly `pi-impersonator/`), with tests grouped by area;
  - historical material moved to `_archive/`.

### Added
- New images check this project's GitHub releases for updates by default. You still approve every
  install.
- The updater enforces a release's minimum image version, using the image's own build info.
- A release refuses to start on a pre-rename image, so the old updater rolls back instead of
  leaving the device broken.

### Security
- The release-signing key is rotated (new key ID `704A1F5710E6EA94`). Images from 1.4.0 on trust
  only the new key.

### Removed
- The legacy `config.ini` developer install and its importer. The flashable image is the only
  supported install path.
- The Rust cloud proxy, now archived and unmaintained.

### Upgrade notes
- Reflash with the 1.4.0 image and run the setup wizard again. An OTA update from 1.3.x rolls
  back, and 1.3.x images can't verify releases signed with the new key.

## [1.3.1]: 2026-09-27

### Added
- The first release built by the arm64 release pipeline, including a flashable image with the
  rotation-capable camera service.

### Fixed
- CI works on fresh checkouts (a missing icon, a test without its PyYAML guard), and the
  signature and image tests really run.

## [1.3.0]: 2026-09-27

### Added
- A camera image rotation setting (0°/90°/180°/270°) in the web console, applied to every stream
  and still and kept across reboots. **Needs a new image**, which 1.3.1 provides.
- Configuration load errors are logged loudly instead of silently running with an empty token.

### Changed
- MQTT commands run on the runtime's event loop.
- The default rotation is 0°. Before this release 180° was hard-coded, so select 180° once if
  your camera is mounted upside down.

[Unreleased]: https://github.com/bullitt186/pibuddycam/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/bullitt186/pibuddycam/compare/v1.3.1...v1.4.0
[1.3.1]: https://github.com/bullitt186/pibuddycam/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/bullitt186/pibuddycam/releases/tag/v1.3.0
