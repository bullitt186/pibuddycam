# Agent instructions — PiBuddyCam

Reverse engineering of the Prusa Buddy3D Camera cloud protocol (firmware through `3.1.6`)
plus two working reimplementations. Full orientation is in
[`README.md`](README.md); this file is the fast path for coding agents.

## Where things are

| Need | Go to |
|---|---|
| Implement firmware-parity gaps | `docs/firmware-implementation-gap-tracker.md` ⭐ read before coding |
| What's confirmed vs. still failing | `docs/status.md` |
| The wire protocol (source of truth) | `docs/protocol.md` |
| Errors / red herrings / corrected assumptions | `docs/dead-ends.md` — check before re-deriving anything |
| Reproduce the RE (Ghidra, VMAs, techniques) | `docs/reverse-engineering.md` |
| Pi camera impersonator (Python, primary impl) | `app/` |
| Appliance image/build/update operations | `image/README.md`, `docs/hardware-bring-up-lessons.md` |
| Rust cloud-stream proxy + control tool | `proxy/` |
| Tools / sources | `docs/tools.md`, `docs/sources.md` |
| CI, releases, arm64 runner, OTA publishing | `docs/releasing.md` |

## Evidence precedence

When documents disagree, use this order:

1. Direct 3.1.6 decompiler control flow, descriptors, or genuine-camera captures cited in
   `docs/firmware-implementation-gap-tracker.md`.
2. Confirmed statements in `docs/protocol.md` and `docs/status.md`.
3. Version-delta and reproduction notes in `docs/firmware-3.1.6.md` and
   `docs/reverse-engineering.md`.
4. Historical journal material, which may contain superseded conclusions.

Treat a conflict as documentation debt: implement from the higher-ranked evidence and update the
lower-ranked document in the same change. Never silently choose the older prose. Fields marked
`descriptor required`, `BLOCKED`, or **assumption** are not implementation specifications.

## Closing an implementation gap

Use the tracker as the work queue. Work on one named `GAP-*` item, or an explicitly stated cohesive
group, and follow this sequence:

1. Read the gap, its evidence row, every cited decompiled line range, and the current target code.
2. Confirm that no prerequisite in the tracker's ambiguity table is unresolved. If one is, recover
   the descriptor/capture first or leave the gap open; do not infer numeric tags or enum values.
3. Implement the smallest shared-state/API change that reproduces the documented behavior. Keep
   hardware-specific Pi policy separate from wire compatibility.
4. Add automated tests matching the gap's acceptance criteria. Prefer decoded/golden wire fixtures
   over assertions against implementation internals.
5. Run the local validation commands below. If the gap requires hardware or Connect verification,
   additionally follow `.agent/pi-ops.md` only after live work has been explicitly requested.
6. Update the tracker checkbox and record the implementing commit, tests, verification date, and
   whether the result is confirmed or still an assumption. Update `protocol.md`/`status.md` when
   externally visible behavior changed.

The owner must choose policy before closing gaps that offer alternatives such as implement versus
stop advertising. This currently includes OTA, timelapse, reboot, WebRTC audio, and unsupported IR,
speaker, fan, or MicroSD behavior. Do not make that product decision implicitly.

## Local development and validation

The Python tests use the standard library and do not require the Pi runtime dependencies:

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app tests
```

When changing the Rust proxy, also run:

```bash
cd proxy
cargo test
```

The Pi runtime dependencies (`aiohttp`, `python-socketio`, GStreamer, and PyGObject) are provisioned
by `app/bootstrap.sh`; do not add host-specific virtual environments to the repository.
There is currently no repository-wide formatter or type checker. Do not claim those checks ran.

CI (`.github/workflows/ci.yml`) runs the same suite on a fresh checkout with PyYAML, minisign,
mtools and dosfstools installed, so it also catches files that exist only on your disk. Releases
come from pushing a `vX.Y.Z` tag, which builds on the self-hosted arm64 runner. Read
`docs/releasing.md` before touching workflows, release scripts or the secret scanner. Never push a
release tag or change repository/runner settings unless the user asks for it.

## Working rules

- **Never commit personal data or firmware.** IPs, MACs, SSIDs, tokens, real usernames/home
  paths, `*.img`, `lp_app*`, `.har`, screenshots, `config.ini`, `.env`, `tokens.json` are all
  git-ignored — keep it that way. Redact captured values to `<PLACEHOLDER>` form.
- The firmware binary is **not** in the repo (IP). See `docs/sources.md` for how to obtain it.
- Protocol facts must stay honest: mark **confirmed** (verified live/against firmware) vs.
  **assumption**. Don't promote a guess to a fact without evidence.
- Ghidra work: prefer **GhidrAssistMCP** (`mcp__ghidrassist__*`) over headless — it attaches to
  the open GUI session on `lp_app`. MCP config is in `.codex/config.toml`.
- For the checked full decompiler export and exact `file:line` anchors, follow
  `docs/reverse-engineering.md`. The export and firmware remain outside Git.

## Working with the physical Pi / camera

Live device access (SSH host, deploy, flash, service management, OTA) is **personal and
git-ignored**. If [`.agent/pi-ops.md`](.agent/pi-ops.md) exists in your checkout, read it for
the real connection details and runbook. It is not on GitHub by design — see
`.agent/pi-ops.example.md` for the (redacted) template if the local file is missing.

**Authorization boundary:** editing or testing repository files does not authorize touching a live
Pi, Connect account, token, camera, or firmware. Do not deploy, mint/rotate tokens, call mutating
backend endpoints, reboot devices, or flash firmware unless the user explicitly requests that live
action. Read-only inspection is still subject to the redaction rules above.

## Developing against the appliance

The current image does **not** run an overlay root. Hardware acceptance found that `overlayroot`
never activated; `/` is the real ext4 ROOT mounted read-only. `/var` and `/etc/pibuddycam` are tmpfs,
while `/data` is the durable PERSIST partition. Do not reuse the older overlay-development model:

- The active application is `/data/pibuddycam/releases/current`, selected by `launcher.sh`.
  `/opt/pibuddycam` is only the factory fallback. Application changes should be committed, packaged
  by `image/scripts/make-app-release.sh`, and installed through the signed updater.
- A signed application bundle cannot update image-owned helpers, units, packages, udev/NM rules,
  or boot configuration. Those changes belong in `image/`; an explicitly authorized live test may
  remount ROOT rw and install the identical repo file, but ROOT must be remounted ro afterward.
- Writes made while ROOT is remounted rw persist. This is not a disposable overlay, so a live-only
  patch creates drift and is forbidden unless the identical change lands in the repo/image in the
  same session.
- Durable configuration is `/data/pibuddycam/config/{device,secrets}.toml`. Keep both owned by
  `pibuddycam`; a root-owned secrets file makes the service read an empty token.
- `app/deploy.sh` remains a legacy developer-install tool. Do not use it on the
  appliance release layout.
- After live work verify the active release, service health, ROOT ro state, updater configuration,
  snapshots and RTSP, and remove temporary CAs/manifests. Exact private commands are in
  `.agent/pi-ops.md`; public workflow and field evidence are in `AGENTS.md` and
  `docs/hardware-bring-up-lessons.md`.

Current cloud limitation: camera-side auth, snapshots, nested configuration, RTSP, and the WebRTC
implementation work, but the currently registered camera is absent from Prusa's camera-service
registry (`GET /v1/cameras/<token>` → 404). Connect lists it under “Other cameras” and hides the
normal live/settings controls. Historical WebRTC viewer probes returned ACK 5, but a 2026-09-25
control viewer received ACK 0 and delivered configuration, so do not describe all viewer signaling
as rejected. Treat the successful 2026-09-19/20 WebRTC sessions as historical implementation
evidence, not the current deployment state.
