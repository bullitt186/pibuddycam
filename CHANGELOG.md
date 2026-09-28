# Changelog

Notable changes to PiBuddyCam. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/). "Needs a new image" means the change
only reaches a device by reflashing; everything else arrives as an OTA update.

## [Unreleased]

### Changed
- Repository reorganised for open-source contributors: new user and contributor documentation,
  `app/` (formerly `pi-impersonator/`), tests grouped by area, historical material moved to
  `_archive/`.

### Removed
- The legacy `config.ini` developer install and its importer. The flashable image is the only
  supported install path.
- The Rust cloud proxy, now archived and unmaintained.

## [1.4.0] (unreleased): project renamed to PiBuddyCam (needs a new image)

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
- The Rust proxy crate, container and RTSP path are renamed `pibuddycam-proxy` and `/pibuddycam`.

### Added
- New images check this project's GitHub releases for updates by default.
- The updater enforces a release's minimum image version, using the image's own build info.
- A release refuses to start on a pre-rename image, so the old updater rolls back instead of
  leaving the device broken.

### Upgrade notes
- Reflash with the 1.4.0 image and run the setup wizard again. An OTA update from 1.3.x rolls
  back.

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

[Unreleased]: https://github.com/bullitt186/pibuddycam/compare/v1.3.1...HEAD
[1.4.0]: https://github.com/bullitt186/pibuddycam/compare/v1.3.1...HEAD
[1.3.1]: https://github.com/bullitt186/pibuddycam/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/bullitt186/pibuddycam/releases/tag/v1.3.0
