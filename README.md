# PiBuddyCam — a Raspberry Pi camera for Prusa Connect

> Formerly `prusa-buddy3d-camera-re`. PiBuddyCam is an independent, community project and is not
> affiliated with or endorsed by Prusa Research.

Reverse engineering of the **Prusa Buddy3D Camera** cloud protocol (firmware through `3.1.6`),
plus two working implementations that let a Linux box (Raspberry Pi)
take the camera's place or pull its stream locally.

The camera normally only works through Prusa Connect's cloud. This project documents its
protocol from the ARM firmware and reimplements it so you can:

- **Impersonate** the camera from a Raspberry Pi + any camera module — it registers to
  Prusa Connect as a genuine Buddy3D camera. → [`pi-impersonator/`](pi-impersonator/)
- **Proxy** an already-registered camera's cloud WebRTC stream to a local RTSP URL and
  send control commands (reboot, resolution, IR mode). → [`proxy/`](proxy/)

> **Current status (2026-09-25):** the appliance registers, uploads snapshots, accepts nested
> settings, streams both local RTSP endpoints, and has a live-verified WebRTC implementation.
> Cloud WebRTC did play in the app/browser on 2026-09-19/20, but the **currently registered token**
> is absent from Prusa's camera-service registry (`GET /v1/cameras/<token>` → 404). Connect now
> lists it under “Other cameras,” and the UI hides live/settings controls. Earlier WebRTC viewer
> probes returned ACK 5, while a 2026-09-25 authenticated control viewer returned ACK 0 and relayed
> settings; the current result is an enrollment/UI block, not a blanket signaling failure. Signed application
> release `1.0.4` live-verified configuration-driven FHD→HD→FHD switching on 2026-09-25.
> Genuinely open:
> wiring the RTSP `configuration` `tag3.11`/`tag3.12`, the remaining `configuration` `tag3`
> subfields, the `file_list` envelope (implemented + unit-tested but not app-exercisable), and
> thermal throttling/cooling on the passively cooled Pi. See
> [`docs/status.md`](docs/status.md) for the exact confirmed-vs-pending line.

## Start here

| If you want to… | Read |
|---|---|
| Implement the remaining firmware-parity gaps | [`docs/firmware-implementation-gap-tracker.md`](docs/firmware-implementation-gap-tracker.md) ⭐ |
| Know what actually works vs. what's still theory | [`docs/status.md`](docs/status.md) ⭐ |
| Understand the wire protocol (the spec) | [`docs/protocol.md`](docs/protocol.md) |
| **Set up the Pi impersonator (one command)** | [`pi-impersonator/README.md`](pi-impersonator/README.md) ⭐ |
| **Use the released appliance image (flash → onboarding → HA/MQTT)** | [`docs/appliance-user-guide.md`](docs/appliance-user-guide.md) |
| **Operate the local web console (Camera/MQTT/Timelapses/System)** | [`docs/appliance-user-guide.md#local-web-console`](docs/appliance-user-guide.md#local-web-console) |
| Run the local RTSP proxy / control tool | [`proxy/README.md`](proxy/README.md) |
| Reproduce or extend the RE work | [`docs/reverse-engineering.md`](docs/reverse-engineering.md) |
| See exactly what changed in firmware 3.1.6 | [`docs/firmware-3.1.6.md`](docs/firmware-3.1.6.md) |
| See what was tried and failed | [`docs/dead-ends.md`](docs/dead-ends.md) |
| Know the tools used | [`docs/tools.md`](docs/tools.md) |
| Understand CI, releases and OTA publishing | [`docs/releasing.md`](docs/releasing.md) |
| Know the firmware / project sources | [`docs/sources.md`](docs/sources.md) |
| Check the official REST spec (no WebRTC) | [`docs/openapi.yaml`](docs/openapi.yaml) — pointer to Prusa's source |
| Understand licensing & IP boundaries | [`NOTICE.md`](NOTICE.md) |

## Local web console

Every **claimed** appliance serves a self-hosted control console — no CDN, web font, analytics, or
JavaScript framework, and no Internet exposure required:

```
https://pibuddycam-<device-id>.local/admin
```

The certificate is device-generated and self-signed, so a browser warning is expected and
acceptable. The console is **trusted-LAN only** and every device-data request is authenticated.

- **Overview** — camera status, telemetry (resolution, quality, Wi-Fi RSSI, CPU temperature,
  uptime, free storage, active release), per-subsystem status chips, and a **Local monitor**. The
  monitor is an authenticated low-rate snapshot refresh (about 1 fps while visible, slower when
  hidden) taken from the shared camera stream; it is **not full-rate video** and never opens a
  second camera consumer. Pause/Resume and snapshot Download are available.
- **Camera** — camera name, SD/HD/FHD quality, periodic snapshot upload + interval (10–600 s),
  timelapse capture/interval (1–3600 s)/playback FPS (1–30), Prusa RTSP mode, and WebRTC mode.
  A quality raise refused by the WebRTC TURN lock is explained and the authoritative value is
  restored; unsupported IR/light/speaker/fan/motor hardware is shown as non-interactive.
- **Integrations** — Prusa Connect server/token/fingerprint replacement (with the fingerprint
  binding warning) and MQTT/Home Assistant (enablement, URI, client ID, optional credentials, TLS
  CA, discovery/topic prefixes, a live connection test, effective-topic preview, and runtime
  state). Credential changes require fresh password re-authentication; stored secrets are never
  rendered — fields show `configured`/`not configured` and blank means “keep”.
- **Timelapses** — library stats, one serialized **Build video** action (busy/progress/error, no
  duplicate builds), a filterable/paginated gallery with direct download and HTTP range support, a
  paginated frame browser with previews, and honest browser-playback detection with a download
  fallback for MJPEG AVI. **Deletion is not offered**; use the Samba share for bulk export.
- **System** — health/version/release/commit/provisioning/SSH, a **report-only** signed-update
  check and an explicitly approved install (fresh re-auth; warns the console may disconnect),
  bounded redacted current-boot diagnostics, SSH enable/disable, enter setup/recovery, reboot, and
  a two-step factory reset with a typed `RESET` phrase and an include-media choice.

Security model: `hashlib.scrypt` admin password hash (never the password), server-side sessions
with idle/absolute expiry, `Secure`/`HttpOnly`/`SameSite` cookies, per-session CSRF tokens, login
and re-auth rate limiting, and re-authentication for every credential/destructive action. Tokens,
MQTT credentials, PSKs, cookies, and personal paths are redacted from responses and logs.

The full navigation semantics, limitations, and recovery instructions are in
[`docs/appliance-user-guide.md`](docs/appliance-user-guide.md#local-web-console).

## Repository layout

```
├── README.md                 ← you are here (index)
├── NOTICE.md                 licensing decision + Prusa-IP / firmware boundary
├── docs/
│   ├── status.md             confirmed vs pending, evidence vs assumption ⭐
│   ├── protocol.md           definitive wire-protocol spec (single source of truth)
│   ├── implementation.md     build guide with constants & payload shapes
│   ├── reverse-engineering.md how to reproduce the RE (Ghidra, VMAs, techniques)
│   ├── firmware-3.1.6.md     direct 3.1.5 → 3.1.6 binary/package delta
│   ├── firmware-implementation-gap-tracker.md  actionable parity backlog + decompiler evidence
│   ├── tools.md              every tool used
│   ├── sources.md            firmware image + third-party projects referenced
│   ├── openapi.yaml           Prusa's official Camera API spec (v0.22.0, REST only — no WebRTC)
│   ├── dead-ends.md          errors, red herrings, corrected assumptions
│   ├── next-steps.md         prioritised open work
│   ├── camerainfo-verification.md  checklist that pinned the CameraInfo struct
│   └── journal/              raw research journal (archive; contains superseded claims)
├── pi-impersonator/          Python impersonator that runs on the Pi (primary impl)
│   ├── bootstrap.sh          one-command fresh-Pi provisioning (run from your machine)
│   ├── deploy.sh             legacy developer-install deploy (not the appliance OTA path)
│   ├── web/                  self-hosted local web console (semantic HTML/CSS/vanilla JS)
│   ├── config.ini.example    config template (copy → config.ini, fill in token — never commit)
│   └── systemd/              ready-to-install unit files
├── proxy/                    Rust cloud-stream proxy + camera control tool
├── config/                   config templates (real secrets stay out of git)
└── research/                 RE helper scripts (struct generator, OTA script)
```

## Evidence conventions

Throughout the docs:

- **Confirmed** / verified live / observed = proven against the real firmware or backend.
- **Assumption** / likely / probably / *guess* = inferred, not yet proven.
- `<PLACEHOLDER>` = a value redacted from real captures (IP, MAC, SSID, token). Supply your own.

## License and non-affiliation

This repository is released under the **MIT License** — see [`LICENSE`](LICENSE) and the full
[`NOTICE.md`](NOTICE.md).

It is an **independent, community-developed project** and is **not affiliated with or endorsed by
Prusa Research**. Prusa®, Prusa Connect, and Buddy3D are trademarks of Prusa Research a.s. No
Prusa source code or firmware is distributed here; the protocol documentation is the result of
independent reverse engineering for interoperability. The appliance is not ONVIF certified.

## Not included (on purpose)

The Prusa firmware binary, its decompilation, and Ghidra databases are **not** in this repo
for IP reasons — only *how to obtain and analyse them* is documented. See
[`docs/sources.md`](docs/sources.md) and [`NOTICE.md`](NOTICE.md).
