# Pi Camera Impersonator

Runs on a Raspberry Pi and registers to Prusa Connect **as a genuine Buddy3D camera**.
It authenticates with a camera registration token, uploads camera-info and snapshots,
speaks the Socket.IO signaling protocol, streams H.264 via local RTSP, negotiates
WebRTC, and exposes an ONVIF-compatible LAN facade for Home Assistant. In the appliance image,
ROOT is mounted directly read-only, volatile operating state is on tmpfs, and durable state and
signed application releases live on `/data`.

Protocol spec: [`../docs/protocol.md`](../docs/protocol.md) —
what works vs. what's still gated: [`../docs/status.md`](../docs/status.md) —
implementation backlog: [`../docs/firmware-implementation-gap-tracker.md`](../docs/firmware-implementation-gap-tracker.md).

## Hardware

- **Raspberry Pi** — tested on **Pi Zero 2 W** (512 MB RAM). Any Pi with a CSI port works.
- **Camera module** — tested with **OV5647** (Pi Camera v1, 5 MP). Any `libcamera`-supported
  module works; adjust `--rotation` in `systemd/rpicam-source.service` for your physical mount.
- **MicroSD** — 8 GB minimum, 16 GB recommended.

## Supported appliance path

For a new appliance, build/flash the image described in [`../image/README.md`](../image/README.md)
and follow the onboarding flow in
[`../docs/appliance-user-guide.md`](../docs/appliance-user-guide.md). A released appliance runs as
the dedicated `pibuddycam` account, reads durable TOML configuration from `/data/pibuddycam/config`,
and launches `/data/pibuddycam/releases/current` with `/opt/pibuddycam` as the factory fallback.

Application updates use signed bundles built by `image/scripts/make-app-release.sh`; image-owned
units, root helpers, packages, and boot configuration require an image change. See `AGENTS.md` and
the private `.agent/pi-ops.md` before live work.

## Legacy developer install — one command

Flash **Raspberry Pi OS Lite 64-bit** via `rpi-imager` (enable SSH + Wi-Fi in settings,
username `pi`). Then from this repo on your machine:

```bash
PI=pi@<PI_IP> pi-impersonator/bootstrap.sh
```

`bootstrap.sh` installs dependencies, creates the dedicated account and `/opt/pibuddycam`, builds
the venv, installs the runtime units, and deploys the code. Follow the final commands printed by
the script to install `config.ini` as `pibuddycam`; do not copy it into the SSH user's home.

```bash
cp pi-impersonator/config.ini.example /tmp/config.ini
# edit /tmp/config.ini: set token
scp /tmp/config.ini pi@<PI_IP>:/tmp/config.ini
ssh pi@<PI_IP> 'sudo install -o pibuddycam -g pibuddycam -m 0600 \
  /tmp/config.ini /opt/pibuddycam/config.ini && unlink /tmp/config.ini'
ssh pi@<PI_IP> 'sudo systemctl restart pibuddycam'
```

The following overlay step applies only to this legacy Raspberry Pi OS developer installation. It
is not the deployment mechanism for the appliance image:

```bash
PI=pi@<PI_IP> pi-impersonator/deploy.sh --enable-overlay
```

## Configuration

`config.ini` (copy from `config.ini.example`, **never commit** — it holds a live secret):

| Key | Value |
|---|---|
| `token` | Camera registration token from Prusa Connect (Web UI → Camera → *Token*) |
| `fingerprint` | Optional. The fingerprint the token is bound to; required to impersonate an existing camera (Connect returns `Invalid fingerprint` otherwise). If omitted, derived from `wlan0` |
| `interval` | Snapshot upload interval in seconds, `10`–`600` (default `10`) |

Resolution is not configured here: it follows the persisted video-quality tier
(`/etc/pibuddycam/quality.env`) and the ephemeral live override
(`/etc/pibuddycam/quality.live.env`), so snapshots, RTSP, WebRTC and status always agree.

Fingerprint precedence: an explicit `[identity] fingerprint` wins, so an already-registered token
keeps working. With no configured value, the fingerprint is generated automatically from `wlan0`
exactly like firmware 3.1.6 (normalize the MAC as uppercase colon-separated ASCII, send its
lowercase MD5 digest). Connect binds the fingerprint on a token's first use, so changing the
fingerprint requires a fresh token.

## Architecture

Four systemd services split cloud control from LAN availability:

```
rpicam-source.service   rpicam-vid -o - | stream_mux.py → H.264 TCP :8888 (multi-client)
        │                 resolution driven by quality.env (persisted) + quality.live.env (live override)
        │                 --rotation 180  --intra 30  --flush
        ↓
pibuddycam-rtsp.service      rtsp_server.py (GStreamer) → rtsp://<pi>:8554/live
        │
pibuddycam-ha-rtsp.service   rtsp_server.py (GStreamer) → rtsp://<pi>:8555/live (always on)
        │
pibuddycam.service       main.py
                          /c/info upload · snapshot loop · Socket.IO signaling · WebRTC
                          HTTP snapshot · ONVIF SOAP · WS-Discovery
```

`main.py` starts/stops `pibuddycam-rtsp` on command from Prusa. The independent
`pibuddycam-ha-rtsp` endpoint remains available to Home Assistant, so Prusa's RTSP mode
cannot remove Home Assistant's stream. Both servers and JPEG capture consume the
existing H.264 fan-out and do not open a second camera pipeline. `main.py` reconfigures the encoder
resolution live on video-quality commands (SD 640×480 / HD 1280×720 / FHD 1920×1080) by
writing the ephemeral `/etc/pibuddycam/quality.live.env` and restarting `rpicam-source`;
a persistence flag (whose event wiring is still being recovered, see `GAP-QUALITY-02`) also
writes `/etc/pibuddycam/quality.env` for the next boot.

## Files

| File | Role |
|---|---|
| `main.py` | Entry point: config, `/c/info`, snapshot loop, signaling, WebRTC, quality handler |
| `signaling.py` | Socket.IO/Engine.IO client → `camera-signaling.prusa3d.com` |
| `webrtc.py` | WebRTC offer/answer via GStreamer `webrtcbin` |
| `upload.py` | HTTP uploads → `webcam.connect.prusa3d.com` |
| `identity.py` | Firmware-faithful MAC normalization and fingerprint derivation |
| `proto.py` | Minimal protobuf encode/decode (nanopb wire format) |
| `camera.py` | JPEG snapshot via `gst-launch-1.0` reading from `stream_mux.py` on port 8888 (avoids fighting `rpicam-vid` for the sensor — libcamera is single-consumer) |
| `rtsp_server.py` | Configurable GStreamer `GstRtspServer`; Prusa uses `:8554/live`, Home Assistant uses `:8555/live` |
| `rtsp_config.py` | Validated environment configuration for independent RTSP instances |
| `local_http.py` | Local HTTP snapshot and ONVIF SOAP endpoints on port 80 |
| `onvif_facade.py` | Host-testable ONVIF Device/Media and WS-Discovery XML implementation |
| `onvif_discovery.py` | UDP multicast adapter for WS-Discovery on `239.255.255.250:3702` |
| `features.py` | Camera feature/capability advertisement |
| `quality.py` | Video-quality tier state (SD/HD/FHD) — atomic writes, crash-safe |
| `stream_mux.py` | TCP broadcast mux: fans the H264 stream from `rpicam-vid` out to multiple clients (RTSP server, snapshot code) on port 8888. libcamera is single-consumer; this replaces the old `--listen` single-client model. |
| `config.ini.example` | Config template |
| `deploy.sh` | Legacy developer-install overlay deploy helper; not the appliance OTA path |
| `bootstrap.sh` | One-command fresh-Pi provisioning |
| `systemd/` | Ready-to-install unit files (run with `User=pibuddycam` from `/opt/pibuddycam`; installed verbatim) |

## Local development

The source-level tests use only the Python standard library and run without Pi/GStreamer runtime
packages:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q pi-impersonator tests
```

Firmware-parity work must name and update a `GAP-*` item in the implementation tracker. Follow the
evidence precedence and completion workflow in [`../CLAUDE.md`](../CLAUDE.md); in particular, do
not invent fields whose descriptor is still marked as required.

For coding agents, repository edits and these tests are local-only. They do not authorize running
the deployment/bootstrap commands below, changing a Connect token, or operating a physical device.
Those actions require an explicit user request and the private `.agent/pi-ops.md` runbook.

## Appliance deployment

Do not `rsync` application code into the appliance. Commit and test the source, create a signed
application bundle with `../image/scripts/make-app-release.sh`, then install it through the updater's
fixed root action. The launcher atomically selects `/data/pibuddycam/releases/current`, retains
`previous` for rollback, and falls back to `/opt/pibuddycam` only when no valid release exists.

ROOT is the real ext4 filesystem mounted read-only; `overlayroot` did not activate during hardware
acceptance. `/var` and `/etc/pibuddycam` are tmpfs, while `/data` is durable. A remount-rw ROOT edit
persists and therefore creates drift: use it only for an explicitly authorized test of an
image-owned asset, restore read-only state, and commit the identical `image/` change immediately.

The commands below are retained only for the legacy developer installation.

## Legacy developer-install deployment

The root filesystem runs as **read-only overlayfs** — runtime writes go to RAM and are
discarded on reboot. A bare `rsync` or `nano` on a running Pi is silently lost. Always deploy
via `deploy.sh`:

```bash
# From repo root:
PI=pi@<PI_IP> pi-impersonator/deploy.sh
# dev (overlay OFF):  rsync + restart, no reboot, ~5 s
# prod (overlay ON):  disable → reboot → rsync → re-enable → reboot, ~3 min (automated)
```

Check mode: `ssh pi@<PI_IP> 'findmnt -no FSTYPE /'` → `overlay` = prod, `ext4` = dev.

Maintenance window (apt installs, `/etc` edits):

```bash
PI=pi@<PI_IP> pi-impersonator/deploy.sh --disable-overlay
# … make changes …
PI=pi@<PI_IP> pi-impersonator/deploy.sh --enable-overlay   # verifies initramfs before rebooting
```

**By contrast, intentionally ephemeral on the appliance** (re-materialized from `/data` on boot):
- `/etc/pibuddycam/quality.env` and `/etc/pibuddycam/quality.live.env`
- journald logs — in RAM (`Storage=volatile`); read with `journalctl -u pibuddycam`

## Manual/developer install

`bootstrap.sh` is the authoritative legacy developer/migration installer. It mirrors the service
identity and `/opt/pibuddycam` layout closely enough for protocol work, but it does not reproduce the
appliance partitioning, onboarding, recovery, or signed-release lifecycle. Do not maintain a second
set of ad-hoc install commands here: update `bootstrap.sh`, its tests, and this description together
when that developer path changes.

## Verify

```bash
ssh pi@<PI_IP> 'journalctl -u pibuddycam -n 30 --no-pager'
# expect lines like:
#   /c/info upload: 200
#   /c/info response: … registered=True …
#   Snapshot: 200 (… bytes, …ms)
```

Prusa-controlled RTSP: `vlc rtsp://<PI_IP>:8554/live`

Always-on Home Assistant RTSP: `vlc rtsp://<PI_IP>:8555/live`

## Home Assistant

The camera is advertised on the local network using ONVIF WS-Discovery. In Home Assistant,
open **Settings → Devices & services**. The discovered ONVIF device should appear under
the camera's configured name; select it and leave username/password empty. Home Assistant receives:

- H.264 stream: `rtsp://<PI_IP>:8555/live`
- JPEG still image: `http://<PI_IP>/snapshot.jpg`
- one ONVIF media profile whose name and resolution follow the shared camera state

If multicast discovery is filtered between VLANs, add the built-in **ONVIF** integration
manually and enter the Pi's IP address, port `80`, with no credentials. UDP multicast
`239.255.255.250:3702`, TCP `80`, and TCP `8555` must be reachable from Home Assistant.

This facade is deliberately unauthenticated and intended only for a trusted LAN. It does
not expose the Prusa token or fingerprint. Do not forward ports 80 or 8555 to the Internet.
It implements the ONVIF calls Home Assistant needs; it is not claimed as ONVIF Profile S
certified.

Discovery/HTTP failures are isolated from Prusa Connect and the Prusa app. The HA RTSP
consumer is independent of the Prusa-controlled `:8554` service, while all outputs share
the same encoder through `stream_mux.py`. Resolution changes restart the shared source and
the always-on HA endpoint, and only restart Prusa RTSP when it is already active.

## Latency notes

Measured end-to-end (from this setup):

- **Server-side first-frame: ~70 ms** (warm, after the first client connect)
- **Cold join: ~2.5 s** (one-time on first boot — `stream_mux.py` needs to accumulate the first IDR bootstrap block before it serves new clients; subsequent connections are instant)
- **What you see in VLC: ~1 s** — this is VLC's `network-caching` default (1000 ms), not the Pi.
  Use `vlc --network-caching=100 rtsp://…` to see the true ~70 ms server latency.

## Recovery

If the Pi will not boot after a power cut:

1. Reflash the SD card (Raspberry Pi OS Lite 64-bit, same settings as before).
2. Run `PI=pi@<PI_IP> pi-impersonator/bootstrap.sh` to rebuild everything.
3. Restore `config.ini` (token) and restart `pibuddycam`.
4. For the supported product path, rebuild/reflash the appliance image. The overlay command is only
   for the legacy developer installation.

## WebRTC prerequisites

Live view uses GStreamer `webrtcbin`, which needs the ICE plugin
`gstreamer1.0-nice` (`libgstnice.so`). Without it, `webrtcbin` fails to link the
H264 RTP stream ("Your GStreamer installation is missing a plug-in").
`bootstrap.sh` installs it; on an already-provisioned Pi where it is missing,
install it in the legacy developer environment. On the appliance this package is image-owned and
must be added through the image build, not installed ad hoc on the running card.
