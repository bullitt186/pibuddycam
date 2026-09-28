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
| Network | `hotspot-start`, `hotspot-stop`, `wifi-station-apply` |
| Updates and power | `check-update`, `install-update`, `reboot` |

No argument or command can be passed through from the browser, MQTT or Connect.

## Robustness

- **Power cuts.** `ROOT` is read-only and runtime files are on tmpfs. Rare durable writes are
  atomic and fsync'd, and journald keeps logs in RAM. The device is expected to lose power with
  the printer.
- **Isolation.** Failures in MQTT, ONVIF or Home Assistant never affect Prusa Connect or the
  streams. The Home Assistant RTSP endpoint is independent of the Prusa RTSP mode.
- **Crash diagnostics.** `bootlog.sh` writes a snapshot of unit and network state to
  `BOOT/bootlog.txt` on each boot, readable on any computer.
