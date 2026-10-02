# Architecture

How PiBuddyCam is built. This page is the map; the code in [`app/`](../app/README.md) and the
image in [`image/`](../image/README.md) are the details. The wire protocol it speaks is in
[reverse engineering](reverse-engineering/protocol.md).

## The big picture

```text
                    ┌──────────────── Raspberry Pi Zero 2 W ────────────────┐
 CSI camera ──────▶ │ rpicam-source  (camera_source.py → rpicam-vid or      │
                    │                 GStreamer → stream_mux.py)            │
                    │      │ H.264 fan-out on 127.0.0.1:8888 (+8889 for     │
                    │      │ WebRTC with a patched SPS)                     │
                    │      ├──▶ pibuddycam-rtsp     :8554/live (Prusa mode)  │
                    │      ├──▶ pibuddycam-ha-rtsp  :8555/live (always on)   │
                    │      ├──▶ pibuddycam (main.py)                         │──▶ Prusa Connect
                    │      │     snapshots, /c/info, Socket.IO signaling,   │    (HTTPS, Socket.IO,
                    │      │     WebRTC, ONVIF + /snapshot.jpg :80, MQTT    │     WebRTC)
                    │      └──▶ pibuddycam-admin (admin_app.py) :443 console│◀── browser (LAN)
                    └───────────────────────────────────────────────────────┘
```

**One encoder, many readers.** libcamera allows a single consumer, so `rpicam-source` owns the
sensor and `stream_mux.py` fans the encoded stream out. Nothing else opens the camera, and a slow
reader never stalls the others.

## Processes

| Unit | Runs | Role |
|---|---|---|
| `rpicam-source` | `camera_source.py` | Builds the capture pipeline from `quality.env` (resolution) and `rotation.env`, then pipes it into `stream_mux.py` |
| `pibuddycam` | `main.py` | Prusa Connect client (`/c/info`, snapshots, Socket.IO, WebRTC answer), ONVIF/HTTP on :80, WS-Discovery, MQTT, timelapse, runtime IPC server |
| `pibuddycam-rtsp` / `pibuddycam-ha-rtsp` | `rtsp_server.py` | Two independent GStreamer RTSP servers on :8554 and :8555. Only :8554 follows the Prusa RTSP mode |
| `pibuddycam-admin` | `admin_app.py` | HTTPS web console. Talks to the runtime over a local IPC socket (`runtime_ipc.py`) |
| `pibuddycam-provisioning` | `admin_app.py` (setup mode) | Setup hotspot and wizard; runs only while unclaimed |
| `pi-persist` | `persist_restore.py` | Boot-time restore of `/data` state into tmpfs (`quality.env`, `rotation.env`, RTSP mode, admin TLS) and the Samba bind mount |
| `pibuddycam-updater` / `-install` | `updater_install.py` | Daily report-only check, and the approved install with rollback |
| `pibuddycam-network-watchdog` (+ `.timer`) | `network_watchdog.py` | Root oneshot run every minute: starts the setup hotspot when a claimed device has had no usable Wi-Fi for 10 minutes |
| `pibuddycam-network-apply` | `network_apply.py` | Root oneshot started only by the helper: applies a console network change with automatic revert |
| `pibuddycam-boot-mode`, `-data-grow`, `-data-ready` | image helpers | Choose setup vs. camera runtime; grow and verify `/data` |

The units in `app/systemd/` are installed verbatim by the image. The image-only units, the
ordering and the drop-ins are in `image/assets/systemd/`. The boot order is in
[`image/README.md`](../image/README.md).

## State and settings

- **One settings path.** Every change goes through `settings_coordinator.py`, whether it comes
  from Prusa Connect, MQTT, the console or the boot restore. The coordinator validates the value,
  applies it live, persists it to `/data/pibuddycam/state.json`, and republishes the state.
  Invalid input never persists or restarts anything.
- **Shared runtime state.** `state.py` holds the runtime state that every outbound surface reads
  (status, `/c/info`, ONVIF, MQTT), so they can never disagree.
- **Configuration.** `/data/pibuddycam/config/device.toml` (not secret) and `secrets.toml` (mode
  `0600`) are validated by `config_schema.py`. Both files belong to the `pibuddycam` user.

## Provisioning

```text
factory → storage_ready → camera_validated → unclaimed → claimed → configured → running
                                     ↘ recovery ↙
```

*Claimed* means an administrator password is set and the durable configuration validated, not
just that a token exists. `boot_mode.py` starts the setup hotspot while unclaimed, or when the
`pibuddycam-recovery` sentinel is on `BOOT` or `/data` is unusable. Otherwise it starts
`pibuddycam.target`.

The setup hotspot is a captive portal. NetworkManager's dnsmasq for the shared AP answers every
DNS name with `192.168.4.1` and announces the portal URI (DHCP option 114) through
`/etc/NetworkManager/dnsmasq-shared.d/50-pibuddycam-captive.conf`. In setup mode `admin_http`
redirects any request for a foreign `Host` (the operating systems' connectivity probes) and any
unknown path to the portal, which is what makes phones and laptops open the wizard by themselves.
The wizard page (`web/setup.html`, `setup.js`) keeps no state in the browser: the setup session
holds it server-side and `GET /setup/state` returns it redacted, so a reload resumes. Setup POSTs
must be JSON and are refused for a foreign `Host`, so a page on the open network cannot drive them
cross-origin.

## Application vs. image

| Owned by | Contents | Changes reach devices by |
|---|---|---|
| **Application release** (`/data/pibuddycam/releases/<version>`) | Everything in `app/` (Python, web assets, pure-Python wheels) | Signed OTA bundle ([releasing](releasing.md)) |
| **Image** (read-only `ROOT`) | Kernel, packages, systemd units, `launcher.sh`, the privileged helper, sudoers/udev/NetworkManager rules, the updater's factory copy, the signing public key | Reflashing a new image |

`launcher.sh` runs `releases/current` when it is complete and falls back to the factory copy in
`/opt/pibuddycam` otherwise. The **updater always runs from the factory copy**, so a broken release
can never break updating. `image_guard.py` stops a release from starting on an image whose layout
it doesn't match; the updater's health check then rolls back.

## Privilege boundary

The runtime runs as the unprivileged `pibuddycam` user. Everything that needs root goes through
the fixed-verb helper `/usr/libexec/pibuddycam/pibuddycam-priv` (`privileged.py`), allowed by a
narrow sudoers rule. Its verbs:

| Group | Verbs |
|---|---|
| Services | `start-camera`, `stop-provisioning`, `rtsp-start`, `rtsp-stop`, `quality-restart` |
| Network | `hotspot-start`, `hotspot-stop`, `wifi-station-apply`, `network-apply`, `hostname-apply`, `wifi-scan` |
| Time | `ntp-apply`, `timezone-apply <IANA name>` |
| Updates and power | `check-update`, `install-update`, `reboot` |

No argument or command can be passed through from the browser, MQTT or Connect. Two verbs take
input, and both re-validate it as root: `hostname-apply <label>` accepts only an RFC 1123 label,
and `network-apply` reads a size-limited JSON request from **stdin** (it can carry the new Wi-Fi
PSK, so it never appears in argv or a log) into a root-only file that `network_apply.py` deletes
as soon as it has read it.

## Network changes

`PUT /api/network` (fresh re-authentication, CSRF and an explicit confirmation) validates the
request in `network_settings.py` and calls `network-apply`, which starts the root oneshot
`pibuddycam-network-apply.service` with `--no-block`. The unit has no `[Install]` section, so an
admin restart or a dropped HTTP connection cannot interrupt the transaction. `network_apply.py`:

1. snapshots the stored `pibuddycam-station` keyfile;
2. applies the new profile (`wifi_station.apply`, DHCP or static IPv4; the PSK goes only through
   a `0600` passwd-file, an empty PSK on an unchanged SSID keeps the stored key);
3. waits up to about 60 s for an activated link with an address and a default route (a static
   profile must also reach its gateway);
4. on failure restores the snapshot and re-activates it, and as a last resort starts the setup
   hotspot;
5. writes a secret-free `network-result.json` (`applying`, `applied`, `reverted` or `hotspot`).

The console answers 202 and polls `GET /api/network`; the PSK is saved to `secrets.toml` only
after the result is `applied`. Reads (`nmcli`, `timedatectl`) are unprivileged **[assumption]:
the service account can read the non-secret connection settings; verify on a device**.

**Watchdog.** The revert above only covers a change made through the console. If the router is
replaced or the Wi-Fi password changes later, `network_watchdog.py` (started every minute by
`pibuddycam-network-watchdog.timer`) counts how long the station link has had no address and
default route. Past 10 minutes (`PIBUDDYCAM_NETWORK_WATCHDOG_MINUTES`, `0` disables; set it with a
systemd drop-in) it starts the setup hotspot, and writes `hotspot` to the result file the console
reads. While the hotspot is up it retries the station profile every 10 minutes and stops the
hotspot when the link is healthy again. It only runs on a claimed device (the station profile
exists), never while a console change is `applying`, and its counters live in root-only `/run`, so
a reboot always gets a fresh chance to join. The setup hotspot is open by design, so a device in
this state exposes the console's login page on that network until the Wi-Fi returns.

The hostname is stored in `device.toml [admin].hostname`; `persist_restore.py` re-applies it at
boot before the admin certificate is provisioned, and `admin_tls.ensure` regenerates the
certificate when the names change.

## Time sync

There is no RTC. `systemd-timesyncd` (default Debian pool) sets the clock once the network is
up. Servers are resolved as: `device.toml [network] ntp_servers`, then DHCP option 42, then the
default pool. `ntp_apply.py` (root) writes `/run/systemd/timesyncd.conf.d/50-pibuddycam.conf` and
restarts timesyncd only when the content changed. Triggers: the `ntp-apply` verb after the
console saves the setting, a NetworkManager dispatcher script
(`/etc/NetworkManager/dispatcher.d/50-pibuddycam-ntp`, which passes only validated hostname or
IPv4 tokens from `DHCP4_NTP_SERVERS`; timesyncd learns per-link servers only from
systemd-networkd, so option 42 would otherwise be ignored under NetworkManager), and
`persist_restore.py` at boot. **[assumption]** NetworkManager's internal DHCP client requests
option 42 and exports it to dispatcher scripts; verify on a device.

The **time zone** is a coordinator setting (`timezone`, saved in `state.json`). Its live apply is
the `timezone-apply` verb (`timedatectl set-timezone`; the helper accepts only an IANA-shaped name
that exists under `/usr/share/zoneinfo`). Because the root filesystem is read-only,
`persist_restore.py` re-applies it at every boot before the clock is restored. It is unrelated to
the `/etc/TZ` value the firmware-compatible status reports to Prusa Connect.

`persist_restore.py` bind-mounts `/data/pibuddycam/timesync` onto timesyncd's state directory
(resolved through the `/var/lib/private` symlink if present) and restarts timesyncd, so the last
known time survives a reboot. There is deliberately no `After=pi-persist.service` drop-in on
timesyncd: timesyncd runs before `sysinit.target` and `pi-persist.service` after it, which would
be a dependency cycle. The console shows the sync state; session names never collide on a stale
clock.

## GPIO timelapse trigger

`gpio_trigger.py` (stdlib only) requests the layer pin and the optional recording pin as inputs
with pull-up in one GPIO uAPI v2 line request and reads edge events from the event loop. It ignores
pulses less than 1 s apart and everything while the timelapse is disabled. The recording pin opens
and closes per-print session folders (`session_<stamp>` with an `.active_session` marker); the
closed session is queued in the serialized `media_build.BuildManager`, which retries a build that
finds it busy. `camera.capture_jpeg_after` skips the multiplexer's cached bootstrap keyframe and
returns the first live frame newer than the pulse. The settings (`timelapse_trigger`,
`timelapse_gpio_pin`, `timelapse_gpio_record_pin`) go through the settings coordinator like every
other setting; its apply hook re-arms or releases the lines. Errors (no chip, permission, busy
line) are reported as status, never raised. The safe pin list is `gpio_pins.py`.

## Robustness

- **Power cuts.** `ROOT` is read-only and runtime files are on tmpfs. Rare durable writes are
  atomic and fsync'd, and journald keeps logs in RAM. The device is expected to lose power with
  the printer.
- **Isolation.** Failures in MQTT, ONVIF or Home Assistant never affect Prusa Connect or the
  streams. The Home Assistant RTSP endpoint is independent of the Prusa RTSP mode.
- **Crash diagnostics.** `bootlog.sh` writes a snapshot of unit and network state to
  `BOOT/bootlog.txt` on each boot, readable on any computer.
