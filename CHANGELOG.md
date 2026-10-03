# Changelog

Notable changes to PiBuddyCam. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/). "Needs a new image" means the change
only reaches a device by reflashing; everything else arrives as an OTA update.

## [Unreleased]

### Added
- Setup wizard: **Later** for the Prusa Connect token, the fingerprint and MQTT. Only Wi-Fi and the
  administrator password are required, so you can set up Wi-Fi first, switch your phone back to
  your normal network and add the token afterwards in the console. A banner reminds you until a
  token is saved, and saving it restarts the camera application. Without a token the camera makes
  no Prusa Connect requests. The restart verb is image-owned: needs a new image (an older image
  keeps saying a restart is required).
- Developer images: `image/scripts/build-image.sh --dev` builds an image with key-only SSH
  (`pibuddydev`, passwordless `sudo`) for debugging. Release images are unchanged and still ship
  with SSH off; the channel is recorded in `build-info.json`. Dev images also serve the setup
  wizard on every interface, Ethernet included. Needs a new image.
- A real setup wizard in the browser, in the look of the web console: start check, Wi-Fi scan
  list or manual network, Prusa Connect token, admin password, optional MQTT (with connection
  test) and fingerprint, a review with *Change* links, and a final page that says where to find
  the camera once the setup network is gone. It resumes after a reload. It replaces the
  placeholder page that only said "Submit this step through the setup API".
- The setup page opens by itself after joining `PiBuddyCam-Setup-<id>` (captive portal), on
  iPhone, Android, macOS and Windows. Needs a new image for the DNS part, see below.
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

- Home Assistant gets two optional entities for the GPIO trigger (recording, pulse-to-frame
  time); the MQTT state document gains `timelapse_recording` and `timelapse_trigger_latency_s`.
- Finished print sessions can be deleted from the Timelapses view (frames, optionally the built
  video), after an acknowledgement and your password. Loose frames and videos still cannot.
- Time zone setting (System, Network): the camera's own zone, applied now and after every reboot.
  Needs a new image (new helper verb).
- Network watchdog: a claimed camera with no usable Wi-Fi for 10 minutes starts the setup hotspot
  and retries its network every 10 minutes (disable with `PIBUDDYCAM_NETWORK_WATCHDOG_MINUTES=0`).
  Needs a new image. The hotspot is open, so its login page is reachable in range while it runs.

### Fixed
- The setup wizard no longer tears down the Wi-Fi it just joined. Starting `pibuddycam.target`
  makes systemd stop the wizard's own service and cut off the running `sudo` helper, so the start
  was reported as failed ("camera target failed to start; device stayed in setup") although the
  camera was starting. The wizard then restarted the setup hotspot, which dropped the new Wi-Fi
  connection. A failed helper call now only counts as a failure when the target is not coming
  up either.

### Changed
- When a claimed camera falls back to the setup hotspot, the phone's sign-in window shows how to
  reach the console (`https://192.168.4.1/admin`) instead of "404: Not Found".
- The setup wizard's Wi-Fi scan uses the root `wifi-scan` verb.
- Setup requests (`/setup/step/<n>`, `/setup/finish`, `/setup/wifi/scan`) must be JSON and are
  refused for a foreign `Host`; new read route `GET /setup/state`.
- `pibuddycam-priv` gains the verbs `network-apply`, `hostname-apply`, `wifi-scan` and `ntp-apply`.

### Upgrade notes
- **OTA-deliverable:** the HTTP redirect, the Network and timelapse UI, the API, the settings and
  the session storage and builds.
- **OTA-deliverable:** the setup wizard page and the captive-portal redirects in the setup server.
- **OTA-deliverable:** the hotspot info page of the camera's port-80 server.
- **Needs a new image:** the captive-portal DNS of the setup network
  (`/etc/NetworkManager/dnsmasq-shared.d/50-pibuddycam-captive.conf`). Without it the wizard
  still works at `http://192.168.4.1`, it just doesn't open by itself.
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
