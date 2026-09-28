# Changelog

Notable changes to PiBuddyCam. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/). "Needs a new image" means the change
only reaches a device by reflashing; everything else arrives as an OTA update.

## [Unreleased]

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
