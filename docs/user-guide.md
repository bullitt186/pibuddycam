# User guide

Day-to-day use of a PiBuddyCam after [installation](install.md): the web console, updates, your
data, recovery and security. Home Assistant, RTSP, ONVIF and MQTT are covered in
[integrations](integrations.md).

## Web console

Open `https://pibuddycam-<device-id>.local/admin` and sign in with the administrator password from
the setup wizard. Typing `http://<address>` (or the `.local` name) without `https` also works: the
camera answers on port 80 with a redirect to the console.
- **Certificate.** It's self-signed by the device, so a browser warning is expected. The console
  refuses to serve plain HTTP if TLS is unavailable.
- **Trusted LAN only.** Never port-forward it.
- **No external assets.** It loads only local files under a strict Content Security Policy: no
  CDN, fonts, analytics or framework.

Sessions expire after idle time and after an absolute maximum. Changing credentials and other
destructive actions ask for your password again, and sign-in is rate-limited.

### Overview

- **Status and telemetry:** resolution, quality, Wi-Fi signal, CPU temperature, uptime, free
  storage and the active release, plus a status chip for each subsystem (camera, Prusa,
  snapshots, RTSP, WebRTC, MQTT, storage, updates).
- **Live view:**
  - Local WebRTC video from the shared camera stream.
  - A low-rate snapshot monitor (about 1 frame/s while the tab is visible), with *Pause/Resume*
    and *Download current snapshot*.
  - Neither opens a second camera pipeline, so they never disturb Prusa Connect or RTSP.

### Camera

Changes apply immediately and are saved. They go through the same settings path as changes made
from Prusa Connect or MQTT.

| Setting | Values | Notes |
|---|---|---|
| Camera name | text | Shown in Prusa Connect and Home Assistant |
| Video quality | SD 640×480, HD 1280×720, FHD 1920×1080 | While a WebRTC viewer uses a relay (TURN), raising quality is refused, as on the genuine camera |
| Image rotation | 0°, 90°, 180°, 270° clockwise | Applies to every stream, snapshot and timelapse frame, and survives reboots. Video restarts briefly. 90°/270° produce portrait video, use more CPU and may lower the frame rate |
| Snapshot upload / interval | on/off, 10–600 s | Periodic uploads to Prusa Connect |
| Timelapse capture / interval / playback FPS | on/off, 1–3600 s, 1–30 | |
| Timelapse trigger | every N seconds, or GPIO pulse | See [Timelapse GPIO trigger](#timelapse-gpio-trigger-prusa-gpio-hackerboard) |
| Prusa RTSP | enabled/disabled | Controls `rtsp://<device>:8554/live` |
| WebRTC | enabled/disabled | Whether Prusa Connect may start a live view |

The Pi has no IR light, speaker, fan or motors, so those controls are shown as unsupported rather
than faked.

### Timelapse GPIO trigger (Prusa GPIO Hackerboard)

The Camera view's **Timelapse trigger** switches the timelapse from a timer to a pulse from the
printer, so each frame is taken at the same head position after a layer change. This is a
PiBuddyCam extension: Prusa Connect still sees only the enable flag and the interval.

- **Wiring.** The Hackerboard outputs are open-drain (active = connected to ground, otherwise
  floating), so the Pi reads them directly with its internal pull-up. Connect
  Hackerboard **OUT0** to the header pin shown for the layer pin (default GPIO17, header pin 11),
  and Hackerboard **GND** to the ground pin shown (pin 9). Any of OUT0–OUT7 works; OUT0–OUT3 need
  no jumper. Never connect a printer voltage (24 V or 5 V) to the Pi and add no external
  pull-up other than to 3.3 V. The pin table and the reasoning are in
  [hardware](hardware.md#gpio-header).
- **Printer settings** (PrusaSlicer, Printer Settings, Custom G-code). The console shows this
  panel, adjusted to your pins and to the measured delay:
  - Start G-code: `M262 P0 B0` (OUT0 as an output).
  - After layer change:
    ```
    G1 X160 Y160 F{travel_speed*60}
    G4 S0
    M264 P0 B1
    G4 P100
    M264 P0 B0
    G4 S<dwell>
    ```
    The pulse is an explicit 100 ms on/off pair; back-to-back toggle commands can be too short to
    detect. `<dwell>` must exceed the time from the pulse to the stored frame; the console fills
    it in from the measured value plus a margin.
- **Freshness.** A pulse captures a frame that was produced *after* it, never the stream's cached
  keyframe. The console shows the measured pulse-to-frame time.
- **Per-print sessions (optional recording pin).** Pick a second pin (default suggestion GPIO27,
  header pin 13) and connect it to Hackerboard **OUT1**. While it is active (low) a session is
  open: each print gets its own folder `session_<date>-<time>`, and when the pin is released the
  session is built into `session_<date>-<time>.avi` automatically. Add to the Start G-code
  `M262 P1 B0`, `M264 P1 B0`, `G4 P300`, `M264 P1 B1` (this forces a clean start edge, so a
  cancelled print never leaves a stale session), and to the End G-code `M264 P1 B0`. The
  Timelapses view has a session picker and builds one session at a time; if a build is running,
  the automatic build waits for it. Frames are never deleted after a build.
- **Needs a new image.** Access to the GPIO chip needs the `gpio` group and udev rule that ship
  with the image from the release after 1.4.0. On an older image the console shows *permission
  denied* instead of arming.

### Integrations

- **Prusa Connect:** server, token and fingerprint.
  - Stored values are never displayed. A blank field keeps the current value; a checkbox clears it.
  - Changing the fingerprint can break the token's binding (see
    [troubleshooting](troubleshooting.md#prusa-connect-rejects-the-camera)).
- **MQTT / Home Assistant:**
  - Settings: broker URI, client ID, credentials, CA file, discovery and topic prefixes.
  - *Test connection* checks the broker without saving. Saving after a failed test requires
    *Save anyway*.
  - Changes take effect after the camera runtime restarts; the console says so.

### Timelapses

- Library statistics and **Build video**, which assembles stored frames into one MJPEG AVI. Only
  one build runs at a time. With a print session selected under *Frames*, that session is built.
- A **Print session** picker lists the per-print folders created by the GPIO recording pin.
- A warning appears while the camera clock is not synchronized (see
  [Network](#network)): new frames and sessions could carry a wrong date until then.
- A filterable, paginated gallery with downloads and a frame browser. Inline playback depends on
  your browser's MJPEG AVI support; downloading always works.
- Loose frames and videos have deliberately no delete button. A finished **print session** can be
  deleted (its frames, and optionally its built video) after an acknowledgement and your
  password. The recording session and any moment a build is running are refused. Use the Samba
  share `smb://<device>/sdcard` for bulk export. The oldest frames, including those inside
  sessions, are pruned automatically when space runs low.

### System

- **Health and version:** application version, active release, source commit, provisioning
  state, storage, temperature.
- **Network:** see [Network](#network).
- **Updates:** see [Updates](#updates).
- **Diagnostics:** view or download a redacted log of the current boot.
- **Access and recovery:** enable/disable SSH, or *Enter setup / recovery mode*.
- **Danger zone:**
  - *Reboot* is rate-limited.
  - *Factory reset* requires typing `RESET` and keeps a dated backup. You choose whether stored
    timelapse media is also deleted.

### Network

The Network card in the System view shows the Wi-Fi network, IP address, gateway, DNS, hostname
and whether the clock is synchronized. You can change:

- **Wi-Fi and IP.** Scan for networks, enter the password (blank keeps the current one), and pick
  DHCP or a static IPv4 address (address, prefix length 1–30, a gateway inside the subnet, up to
  three DNS servers). Applying needs your password and an acknowledgement, because the camera may
  change address.
- **Automatic revert.** The new profile must come up with an address and a default route (a
  static gateway must also answer) within about a minute. Otherwise the camera restores the old
  profile; if even that fails it starts the setup hotspot. The console reports which happened.
  After a change to a new address, reconnect at the address the console shows.
- **Watchdog.** If a claimed camera has had no usable Wi-Fi for 10 minutes (router replaced,
  password changed), it starts the setup hotspot `PiBuddyCam-Setup-<id>` so you can reach it from
  a phone, and retries your network every 10 minutes; when the network is back the hotspot stops
  by itself. The hotspot is open, so anyone in range can reach the console's login page while it
  runs. The System view reports `hotspot` as the last network result.
- **Hostname.** Applied immediately and remembered. Reboot once so the console's certificate
  matches the new name.
- **Time servers.** The camera has no clock chip, so time comes from the network. Blank means
  automatic: the server your router advertises (DHCP option 42), then the Debian pool. Enter up
  to three hostnames or IPv4 addresses for networks that block outbound NTP. The last known time
  is kept across reboots, so names and certificates rarely carry a stale date.

Applying network changes, the hostname, scanning and the persistent clock need the image released
after 1.4.0; on an older image the console says so and changes nothing.

## Updates

The device checks this project's GitHub releases about once a day and on *Check for updates*.
**It never installs on its own.** *Install update* asks for your password, then:

1. Verifies the minisign signatures, SHA-256, size, version and compatibility, and the archive
   paths.
2. Stages the new release and pre-checks it (compile, import, migration dry run).
3. Switches to it atomically. The previous release is kept.
4. Runs local health checks for up to 90 seconds. **If they fail, the previous release is
   restored** and the failed version is marked bad.

Services restart during an install, so the console disconnects briefly. Reconnect and re-check,
and don't start a second install. Home Assistant shows the same state as an MQTT `update` entity.

Updates replace the application: Python code, web assets and pure-Python wheels. The kernel,
system packages, services and boot configuration only change with a **new image**, and the release
notes say when one is needed.

## Your data

Durable data lives on the `PERSIST` partition, mounted at `/data`. The system partition is
read-only, and logs and runtime files are kept in RAM, so pulling the power is safe.

```text
/data/pibuddycam/config/device.toml     appliance settings (not secret)
/data/pibuddycam/config/secrets.toml    Prusa token, MQTT and admin credentials (0600)
/data/pibuddycam/state.json             camera settings (quality, rotation, intervals, …)
/data/pibuddycam/releases/              installed application releases (current, previous)
/data/pibuddycam/backups/               configuration backups
/data/network/system-connections/       Wi-Fi profiles
/data/sdcard/timelapse/                 timelapse frames and videos (Samba: smb://<device>/sdcard)
```

**Before reflashing, export your timelapses and settings.** Flashing recreates `/data`.

## Recovery and factory reset

**Recovery** re-enters the setup wizard without deleting data. There are two ways in:
- *Enter setup / recovery mode* in the web console.
- With the device powered off, create an empty file named `pibuddycam-recovery` on the card's
  `BOOT` (FAT) partition. Remove the file once you're done.

A missing or corrupt `/data` also boots into setup, never into a half-working camera.

**Factory reset** (web console → *Danger zone*) deletes configuration, Wi-Fi profiles, MQTT state
and releases, and optionally the timelapse media. It writes a dated backup first and removes it
after the next successful boot.

## Security

- **Setup network.** The setup wizard is reachable only on the setup network, which is switched
  off after setup.
- **Admin password.** It's stored as a salted scrypt hash. Sessions are server-side, cookies are
  `Secure`/`HttpOnly`/`SameSite`, and every change carries a CSRF token.
- **Redaction.** Tokens, Wi-Fi keys, MQTT credentials and cookies are redacted from the UI,
  logs, diagnostics and MQTT state.
- **SSH** is off by default.
- **Unauthenticated local streams.** ONVIF, `/snapshot.jpg` and RTSP (`8554`/`8555`) have no
  authentication, by design, for Home Assistant compatibility. **Keep the device on a trusted LAN
  and never expose ports 80, 443, 8554 or 8555 to the Internet.**

Report vulnerabilities privately: see [SECURITY.md](../SECURITY.md).
