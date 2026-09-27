# PiBuddyCam — User Guide

Operator-facing guide for the Raspberry Pi camera appliance image published by this project.
It describes the intended released image and the flows implemented in this repository. Deep
protocol, partition, and design detail lives in the referenced documents; this guide links to
them rather than duplicating them.

> **Acceptance status (2026-09-25).** The core appliance has been exercised on a Pi Zero 2 W +
> OV5647: boot/runtime, capture, snapshots, RTSP, Connect auth/configuration, and signed application
> install/rollback are live-accepted. The complete release matrix (all card sizes, every onboarding
> route, HA/Prusa soak, recovery and destructive power-loss cases) is still incomplete. See
> [Acceptance status](#acceptance-status) at the end.

## What it is / scope

PiBuddyCam turns a **Raspberry Pi Zero 2 W** with one CSI camera module into a
Prusa Connect–compatible network camera, and exposes it locally over ONVIF/RTSP and (optionally)
MQTT for Home Assistant.

- **Supported v1 hardware:** Raspberry Pi Zero 2 W (arm64) with a single CSI/libcamera sensor.
  OV5647 is the first certified sensor; other libcamera sensors are experimental until they pass
  the capture matrix.
- **What is shipped:** a signed, compressed SD-card image built from the pinned configuration in
  [`image/`](../image/README.md), plus signed application update bundles.
- **Local interfaces:** ONVIF + RTSP + JPEG snapshot for Home Assistant, and an optional generic
  MQTT interface for settings and diagnostics.
- **Not a firmware replacement:** the appliance is a clean-room interoperability reimplementation.
  It does not distribute or run Prusa firmware. See [Non-affiliation](#license-and-non-affiliation).

The design of record is
[`docs/public-appliance-distribution-plan.md`](public-appliance-distribution-plan.md) (source of
truth for the distribution, partition, onboarding, MQTT, update, and acceptance model). The
impersonator internals are described in [`app/README.md`](../app/README.md),
and the cloud wire protocol in [`docs/protocol.md`](protocol.md).

## Hardware

| Item | Requirement |
|---|---|
| Board | Raspberry Pi Zero 2 W (arm64). The image is validated for the Zero 2 W only. |
| Camera | One CSI camera module; OV5647 (Pi Camera v1) is the certified development sensor. |
| microSD | **8 GB minimum; 16 GB or larger recommended** for timelapse use. |
| Power supply | Adequate 5 V supply; the board powers on/off with the printer in typical use. |
| Network | 2.4 GHz Wi-Fi (the Zero 2 W has no 5 GHz radio); wired Ethernet via adapter is not part of v1. |

The SD-size guidance comes from the distribution plan (§1.1). The image uses three partitions —
a 512 MiB FAT `BOOT`, a 4 GiB ext4 `ROOT` mounted directly read-only, and a 512 MiB
ext4 `PERSIST` that grows to fill the card on first boot. Because `PERSIST` (your durable data)
lives after `ROOT`, a larger card gives you more room for timelapse media and releases; the
extracted image must still fit an 8 GB card. See [`image/README.md`](../image/README.md) for the
exact disk layout.

## Install / flash

> A development image has booted on the target hardware. A release candidate still needs the full
> clean-card/card-size matrix in [Acceptance status](#acceptance-status).

Preferred method — Raspberry Pi Imager:

1. Install **Raspberry Pi Imager 2.x**.
2. Add the project's Imager OS-list/repository JSON and select the PiBuddyCam image. The
   rendered manifest and hashes are produced by the release pipeline; see
   [`image/README.md`](../image/README.md) for the manifest contract.
3. Optionally supply Wi-Fi/hostname/SSH customization in Imager. Wi-Fi values provided here are
   prefilled into the first-boot wizard.
4. Flash the SD card, insert it into the Pi, connect the CSI camera, and power on.

Fallback — `Use Custom` with a raw local image. Raspberry Pi Imager cannot infer the correct
Trixie initialization format from a raw local image, so the project manifest is the preferred
path; use `Use Custom` only when you understand that caveat.

Local image builds (developers):

```sh
git clone --branch v2.8.0 --depth 1 \
  https://github.com/raspberrypi/rpi-image-gen /path/to/rpi-image-gen
RPI_IMAGE_GEN_DIR=/path/to/rpi-image-gen image/scripts/build-image.sh
```

Builds require a native arm64 Debian/Raspberry Pi OS Trixie host; `build-image.sh` refuses
foreign-architecture builds. See [`image/README.md`](../image/README.md) for the pinned revision,
release-artifact assembly, signing, and validation steps.

## First-boot onboarding

> The setup/runtime path has been exercised during hardware bring-up, but every supported onboarding
> entry (Imager prefill, hotspot, manual token, recovery) still needs the release acceptance matrix.

While the device is **unclaimed**, it exposes a temporary setup access point and captive portal:

- **Setup hotspot SSID:** `PiBuddyCam-Setup-<last6-device-id>` (for example `PiBuddyCam-Setup-a1b2c3`).
- **Captive portal:** `http://192.168.4.1`.

The setup AP is open by design, because a headless DIY device cannot deliver a unique credential
before setup. **Perform initial setup in a trusted physical location.** The unclaimed wizard is
served only on the setup AP, never on the normal LAN.

The wizard steps, in order:

1. **Status** — storage (`PERSIST`/`/data`) and camera probe results, with a retry on failure.
2. **Imager customization** — import and prefill Wi-Fi/hostname/SSH supplied by Raspberry Pi Imager.
3. **Wi-Fi** — scan for networks or enter a hidden SSID manually.
4. **Prusa token** — scan an original Prusa pairing QR or enter the registration token manually.
5. **Fingerprint** — optional explicit fingerprint; otherwise the MAC-derived default is retained.
6. **Administrator password** — required, with confirmation.
7. **MQTT** — optional broker configuration with a live connection test.
8. **Summary** — a redacted review before committing.
9. **Save** — atomically persist configuration and secrets.
10. **Finish** — stop provisioning/QR capture, disable the hotspot, activate station Wi-Fi, and
    start the normal camera target.

After a successful claim, the setup hotspot stops and the administration UI is served on the
station network at `https://pibuddycam-<device-id>.local`. That certificate is device-generated and
self-signed, so a browser warning is expected and acceptable.

The provisioning state machine is
`factory → storage_ready → camera_validated → unclaimed → claimed → configured → running`, with an
explicit `recovery` state. “Claimed” means an administrator password is set and durable
configuration has passed validation — not merely that a token exists. See the distribution plan
§4 for the full design.

## Home Assistant, ONVIF, and RTSP

ONVIF is the media interface for Home Assistant and works independently of MQTT.

| Service | Address / port |
|---|---|
| ONVIF WS-Discovery | UDP `3702` (multicast `239.255.255.250`) |
| ONVIF device/media SOAP + JPEG snapshot | TCP `80` (`/`, `/snapshot.jpg`) |
| Prusa RTSP | `rtsp://<device>:8554/live` (controlled only by Prusa RTSP mode) |
| Home Assistant RTSP | `rtsp://<device>:8555/live` (always available) |

Home Assistant discovers exactly one ONVIF camera on the same multicast domain. If multicast
cannot cross a VLAN, add the camera manually by IP and port 80. Port `8555` is intentionally
separate from `8554` so Home Assistant availability does not depend on the cloud-controlled Prusa
RTSP mode. See [`docs/home-assistant-onvif-implementation-plan.md`](home-assistant-onvif-implementation-plan.md)
for the facade, discovery, and snapshot details.

### MQTT (optional)

MQTT is **disabled until explicitly configured** and supports generic brokers (`mqtt://` and
`mqtts://`, IPv4/IPv6/DNS, optional username/password, system CA or a custom CA PEM). There is no
broker auto-discovery and no credential extraction from Home Assistant.

Stable topics are derived from the persisted appliance UUID (not the name or IP):

```text
pibuddycam/<device-id>/availability              retained "online" + retained LWT "offline"
pibuddycam/<device-id>/state                     retained authoritative state JSON
pibuddycam/<device-id>/command/<name>            QoS 1, never retained
pibuddycam/<device-id>/update/state              retained update state (HA update schema)
pibuddycam/<device-id>/update/install            non-retained literal install command
```

The default discovery prefix is `homeassistant`; the default base prefix is `pibuddycam`. Both are
configurable. Discovery is published as one retained device document at
`<discovery-prefix>/device/pibuddycam_<device-id>/config`, creating a separate
`<camera name> Controls` device (no duplicate MQTT camera entity). Home Assistant’s `online`
birth message triggers a republish of discovery and state after a randomized delay, so entities
survive broker and HA restarts.

Commands validate a bounded payload, apply and persist through the shared settings coordinator,
then publish the authoritative state even on rejection (`last_command_error` carries a bounded,
non-secret reason). State JSON never contains secrets. MQTT failures are isolated and cannot
disturb Prusa signaling or local media services.

## Local web console

After a successful claim, the appliance serves a self-hosted administration console at:

```text
https://pibuddycam-<device-id>.local/admin
```

The TLS certificate is device-generated and self-signed, so the browser warning is expected and
acceptable. The keypair is provisioned once on the durable `/data` partition and reused on every
boot; the console **fails closed** rather than serving plaintext if TLS is unavailable. The console
is **trusted-LAN only**; do not port-forward it. It uses only local assets
(semantic HTML, one stylesheet, one vanilla ES module, one SVG icon) with a strict Content Security
Policy — no CDN, web font, analytics, or framework. Unauthenticated clients see only the sign-in
form; all device data lives behind the authenticated API.

Sign in with the **administrator password** set during onboarding. Sessions are server-side with
idle and absolute expiry and an `HttpOnly`/`Secure`/`SameSite` cookie; a session that expires (or is
revoked) returns the console to the sign-in view on the next request. Every state-changing request
carries a per-session CSRF token, and credential or destructive actions additionally require a
fresh password re-authentication in a modal. Login and re-authentication are rate-limited.

### Overview

- Camera status and telemetry: resolution, quality, Wi-Fi RSSI, CPU temperature, uptime, free
  storage, active application release, and per-subsystem status chips (camera, Prusa, snapshots,
  RTSP, WebRTC, MQTT, storage, updates) with configured-vs-runtime-vs-freshness distinctions.
- **Local monitor** — an authenticated **low-rate snapshot view**, roughly 1 frame per second while
  the tab is visible (slower when hidden), taken from the shared camera stream. It is explicitly
  **not full-rate video** and never opens a second libcamera consumer, so it cannot disturb the
  camera source, snapshots, or Prusa signaling. Use **Pause/Resume** to stop or restart refreshing
  and **Download current snapshot** for a one-off JPEG. If no frame is available the console says so
  rather than showing a stale image.
- A persistent trusted-LAN warning reminds you that ONVIF, `/snapshot.jpg`, and RTSP are
  unauthenticated by v1 policy.

### Camera

Settings are applied through the same shared settings coordinator as Prusa/MQTT/startup restore;
each form waits for the authoritative response before reporting success.

| Control | Range / values | Notes |
|---|---|---|
| Camera name | text | Shown in Prusa Connect and Home Assistant. |
| Video quality | SD 640×480 / HD 1280×720 / FHD 1920×1080 | A raise refused by the WebRTC **TURN quality lock** is explained and the current value is restored automatically. |
| Image rotation | 0° / 90° / 180° / 270° (clockwise) | Applied once at the camera source, so every stream (RTSP, HA RTSP, WebRTC, local live view), snapshot and timelapse frame is rotated. Default 0°. It persists across reboots and quality changes. Applying it restarts the video streams briefly. 90°/270° show a warning: they use a slower GStreamer rotation path (higher CPU, possibly a lower frame rate) and produce portrait output. |
| Snapshot upload | on/off | Periodic uploads to Prusa Connect. |
| Snapshot interval | 10–600 s | |
| Timelapse capture | on/off | |
| Timelapse interval | 1–3600 s | |
| Timelapse playback FPS | 1–30 | |
| Prusa RTSP | enabled/disabled | Controls the `:8554/live` endpoint. |
| WebRTC | enabled/disabled | Configured mode; active streaming is reported separately. |

Unsupported IR/light, speaker, fan, and motor hardware is shown as a non-interactive note; those
controls are never fabricated.

### Integrations

- **Prusa Connect** — server, token replacement, and fingerprint. Stored values are **never
  displayed**: the form shows `configured`/`not configured` and a blank field means “keep the
  current value”; explicit clear checkboxes remove one. Replacing the fingerprint warns that it can
  invalidate the existing token binding. Saving requires fresh password re-authentication.
- **MQTT / Home Assistant** — enablement, broker URI (no embedded credentials), client ID, optional
  username/password, TLS CA file, discovery prefix, and topic prefix. **Test connection** performs a
  non-persistent broker test (it may use a stored credential when the replacement field is left
  blank). A save is refused after a failed or stale test unless you tick **Save anyway**; it always
  requires fresh re-authentication and reports truthfully that the camera runtime must restart
  before the change is active. The effective-topic preview and runtime connection state are shown.

### Timelapses

- Library statistics (video count, frame count, library size).
- **Build video** assembles stored JPEG frames into one MJPEG AVI. Only one build runs at a time; a
  duplicate request is refused and the console follows the running job with progress, success, or a
  bounded error reason.
- A filterable (completed/error/pending/unknown), paginated video gallery with **direct download**
  (whole-file and HTTP range) and a frame browser with bounded previews.
- **Inline playback** depends on the browser's MJPEG AVI support; when the browser cannot decode it,
  the console says so and direct download always works. No CPU-heavy transcoding runs on the Pi.
- **Deletion is deliberately not offered.** Use the Samba share (`smb://<device>/sdcard`) for bulk
  export and the existing low-space pruning for retention.

### System

- **Health & version** — application version, active release, source commit, provisioning state,
  runtime freshness, storage, temperature, and uptime.
- **Signed update** — read-only state on load; **Check for updates** is report-only and never
  installs; **Install update** requires fresh re-authentication and warns that the camera and admin
  services restart, so the console may disconnect. Reconnect and re-check the state; never start a
  second install.
- **Diagnostics** — load or download a bounded, redacted current-boot journal for the fixed
  appliance units. No command, unit, or path can be supplied from the browser.
- **Access & recovery** — enable/disable SSH (re-authentication required) and **Enter setup /
  recovery mode**, which writes the BOOT recovery sentinel and restarts into the setup hotspot after
  an explicit browser confirmation. The console disconnects and the device must be re-onboarded.
- **Danger zone** — **Reboot** needs a checked acknowledgement plus re-authentication and is rate
  limited (a second reboot within the window is refused with a retry hint). **Factory reset**
  requires the typed phrase `RESET`, an explicit confirmation, and re-authentication; it keeps a
  dated backup and lets you choose whether stored timelapse media is also deleted, then reports the
  exact file count removed.

The raw TOML editor remains available to experts through the existing configuration API and the
documented apply/validate commands; it is not the primary settings UX.

### Console limitations (current)

- The local monitor is a low-rate snapshot refresh, not full-rate video; a full local video path is
  a separately documented follow-up.
- MJPEG AVI inline playback is browser-dependent; download is the guaranteed path.
- Local browsing does **not** close the still-open Connect `file_list`/`client_trigger` portions of
  `GAP-TIMELAPSE-01` or `GAP-SIO-01`, and it does not change the external camera-service registry
  classification that currently hides the normal Connect live/settings UI.
- Credential changes and MQTT/Prusa edits require a camera-runtime restart before they take effect.

## Backup, reflash, recovery, and factory reset

### Durable data (`/data`)

`PERSIST` mounts at `/data`; volatile operating state is explicitly placed on tmpfs. The durable
layout is:

```text
/data/pibuddycam/config/device.toml       non-secret appliance configuration
/data/pibuddycam/config/secrets.toml      Prusa/MQTT/admin secrets (mode 0600)
/data/pibuddycam/state.json               versioned runtime state
/data/pibuddycam/identity.json            stable appliance UUID
/data/pibuddycam/releases/<version>/      installed application releases
/data/pibuddycam/releases/current         atomic symlink to the active release
/data/pibuddycam/releases/previous        atomic symlink to the rollback release
/data/network/system-connections/        durable NetworkManager profiles
/data/sdcard/timelapse/                  emulated SD/timelapse store
/data/pibuddycam/backups/                 bounded configuration backups
```

`/mnt/sdcard` is the bind-mounted firmware-facing path and is also exported read/write as the
Samba share `[sdcard]` — browse it at `smb://<device>/sdcard` (guest access, mapped to the
`pibuddycam` service account). See the distribution plan §2.5 for the layout contract.

### Backup

Export configuration and timelapse media from `/data` (the Samba share or a data backup) **before
reflashing**. A full image flash rewrites the partition table and can destroy existing `PERSIST`
data; in-place preservation across a full-image flash is not claimed.

### Recovery

Two documented recovery entries start setup/recovery mode without reflashing:

- **Authenticated “Enter setup mode”** action in the web UI (or CLI).
- **BOOT sentinel:** with the device powered off, create an empty file named `pibuddycam-recovery`
  on the `BOOT` (FAT) partition; the next boot starts the setup hotspot instead of the camera
  target. Remove the sentinel once recovery is complete.

A missing, corrupt, or read-only `/data` starts only recovery/setup services rather than a
look-alike volatile runtime.

### Factory reset

Factory reset is separate from recovery and destructive. It requires a **two-step confirmation**,
first writes a dated backup on `/data`, and then deletes configuration, network profiles, MQTT
state, application releases, and optionally timelapse media. The backup is removed only after the
reset system completes a successful claimed boot. The UI reports exactly what will be deleted.

## Security posture

- The **unclaimed wizard is reachable only through the setup AP** at `192.168.4.1`; it is never
  bound to the normal LAN interface, and the hotspot stops after claim.
- The admin password is stored as a salted **`hashlib.scrypt`** hash — the password itself is
  never stored. Sessions use random server-side identifiers with idle and absolute expiry,
  `Secure`/`HttpOnly`/`SameSite` cookies, CSRF tokens, and login rate limiting.
- Prusa tokens, fingerprints where appropriate, MQTT credentials, Wi-Fi PSKs, cookies, and
  `Authorization` headers are **redacted** from UI responses and logs. State JSON and diagnostics
  are secret-free.
- Sensitive actions — credential changes, update installation, factory reset, secret-bearing
  backup export, and SSH enablement — require **re-authentication with the current admin password**.
- **SSH is disabled by default.** Raspberry Pi Imager may enable it and create the operator
  account; an expert can then edit the durable TOML configuration and run the supplied validator
  and single apply command. Invalid files are rejected without replacing live configuration.
- **Trusted-LAN only:** ONVIF SOAP, `/snapshot.jpg`, and RTSP ports 8554/8555 are unauthenticated
  by v1 product policy. Do **not** expose TCP 80, 8554, or 8555 to the Internet or port-forward
  them.

The full security model, including the TOML schema, migrations, and the expert/recovery paths, is
in the distribution plan §4 and §5.

## Updates

Application updates replace project Python code, static web assets, and pinned pure-Python wheels.
They **cannot** replace the kernel, boot firmware, partition table, systemd base units, Debian or
GStreamer/libcamera packages, or the signing public key — those require a new image.

- **Artifacts:** each release is a signed `pibuddycam-app-<version>.tar.zst` bundle with
  `.sha256` and `.minisig`, plus a signed `update-manifest.json`. The image trusts only the
  committed public key (`image/keys/pibuddycam-release.pub`); the private signing key never leaves
  the release environment.
- **Report-only checks:** the updater checks for a new stable version about once per 24 hours
  (with jitter) and on an authenticated manual request. It never auto-installs.
- **Approval and install:** installation starts only after explicit approval. The bundle is
  verified (manifest/bundle signatures, SHA-256, size, version, compatibility, archive paths, free
  space) before extraction; unsafe paths, owners, and setuid/setgid content are rejected.
- **Atomic switch and rollback:** a verified release is staged, preflighted (compile, import, and
  migration dry-run), then activated by swapping the `current` symlink with the prior release kept
  as `previous`. Local health checks run for up to 90 seconds; on failure the previous release is
  restored and the attempted version is marked bad. Prusa cloud reachability is deliberately not a
  health requirement, so an Internet outage cannot trigger a rollback.
- **Factory fallback:** the launcher falls back to the immutable factory application when `/data`
  holds no valid active release. Older releases are pruned only after a successful boot.
- **Home Assistant:** availability is surfaced as an HA MQTT `update` entity (`installed_version`,
  `latest_version`, `release_summary`, `release_url`, `in_progress`, `update_percentage`). Only the
  literal, non-retained install payload declared by discovery is accepted.

> **Live-accepted 2026-09-24/25.** Signed installs activated releases `1.0.1`, `1.0.3`, and `1.0.4`;
> a deliberately unhealthy `1.0.2` rolled back to the previous release. The temporary test
> manifest/CA were removed and ROOT was restored read-only after each acceptance run.

The full algorithm is in the distribution plan §7.

## Backups and data

- Durable state lives on `PERSIST` (`/data`). ROOT is the real ext4 filesystem mounted read-only;
  `/var` and `/etc/pibuddycam` hold intended volatile state on tmpfs. `overlayroot` was configured
  in the early design but did not activate on the accepted hardware.
- Journald logging is volatile and size-limited. The web UI can expose a redacted, bounded
  diagnostic log assembled from the current boot.
- `/etc/pibuddycam/quality.env` is intentionally ephemeral: the live video tier resets to the
  persisted value on reboot.
- Before reflashing, export configuration and timelapse media (see [Backup](#backup)).

## Troubleshooting

Collect logs with `journalctl`:

```sh
journalctl -u pibuddycam-provisioning -n 100 --no-pager   # setup hotspot / captive portal
journalctl -u pibuddycam       -n 100 --no-pager      # main application
journalctl -u rpicam-source   -n 100 --no-pager      # camera source (libcamera owner)
journalctl -u pibuddycam-rtsp      -n 100 --no-pager      # Prusa RTSP :8554
journalctl -u pibuddycam-ha-rtsp   -n 100 --no-pager      # Home Assistant RTSP :8555
journalctl -u pibuddycam-updater   -n 100 --no-pager      # update availability checks
```

Common first-boot issues:

- **No setup hotspot:** confirm the device is still unclaimed and that `PERSIST` mounted at
  `/data` (a missing/corrupt `/data` starts recovery, not the camera). Check
  `journalctl -u pibuddycam-provisioning`.
- **Camera probe fails:** reseat the CSI ribbon, confirm exactly one supported sensor, and use the
  wizard’s retry action. A failed camera keeps the appliance in setup/recovery mode and does not
  cause a restart loop.
- **Cannot join station Wi-Fi:** verify the SSID/password and that the network offers DHCP and
  working DNS. Hidden networks require manual SSID entry.
- **Prusa token rejected:** the wizard returns you to setup without overwriting last-known-good
  credentials; obtain a fresh pairing QR/token from Prusa Connect.
- **MQTT will not connect:** the wizard tests DNS, TCP/TLS, authentication, publish, and subscribe
  before saving. Check the broker URI, TLS/CA settings, and credentials. ONVIF/RTSP are unaffected
  by an MQTT outage.
- **Home Assistant cannot find the camera:** ensure both devices are on the same multicast domain,
  or add the camera manually by IP and port 80. The JPEG snapshot is at `/snapshot.jpg`.
- **Update appears but does not install:** installs are manual and require approval; a failed
  signature/hash/compatibility/free-space check rejects the update before activation.

Re-flashing:

1. Export any configuration and timelapse data you want to keep.
2. Re-flash the SD card using the project Imager manifest (preferred) or the raw image fallback.
3. Re-run first-boot onboarding, or restore a previously exported configuration.

## License and non-affiliation

This project is released under the **MIT License** — see [`LICENSE`](../LICENSE) and
[`NOTICE.md`](../NOTICE.md).

It is an **independent, community-developed project**. It is **not affiliated with or endorsed by
Prusa Research**. Prusa®, Prusa Connect, and Buddy3D are trademarks of Prusa Research a.s. The
project does not distribute Prusa firmware or decompilation; it is a clean-room interoperability
effort. The appliance is not ONVIF certified.

## Acceptance status

The core runtime is hardware-accepted on one Pi Zero 2 W + OV5647 setup, including signed update
and rollback. The appliance is **not yet release-matrix complete**: the physical SD-card matrix,
all onboarding variants, broader camera capture matrix, Home Assistant/Prusa coexistence soak,
power-loss durability, and recovery matrices have not all been completed and recorded.

This guide describes the documented design and must not be read as a record of completed hardware
testing. Acceptance is tracked by the acceptance work packages (WP-R6 acceptance matrices and the
WP-R8 acceptance stage) and the criteria in
[`docs/public-appliance-distribution-plan.md`](public-appliance-distribution-plan.md) §8–§9. The
image must not be published as stable before every acceptance section passes.
