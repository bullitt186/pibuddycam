See [`CLAUDE.md`](CLAUDE.md) for the complete agent instructions, repository map, evidence
precedence, gap-closing workflow, and validation commands. Its instructions are mandatory.

Before changing the impersonator, read these in order:

1. [`CLAUDE.md`](CLAUDE.md)
2. [`docs/firmware-implementation-gap-tracker.md`](docs/firmware-implementation-gap-tracker.md)
3. The relevant sections of [`docs/protocol.md`](docs/protocol.md) and
   [`docs/dead-ends.md`](docs/dead-ends.md)

Work against a named `GAP-*` item. Direct 3.1.6 decompiler evidence cited by the tracker takes
precedence over conflicting older prose. Do not guess fields marked `descriptor required`, and do
not close a gap without its tests and evidence record.

Local source edits and tests do not authorize deployment, live backend mutation, token rotation,
reboots, or firmware flashing. Perform those only when the user explicitly requests them. Live
Pi/camera access is in the git-ignored `.agent/pi-ops.md` (template:
`.agent/pi-ops.example.md`).

---

## Developing in the loop with real hardware

The appliance is exercised on a real **Pi Zero 2 W + OV5647 CSI camera**. Offline
tests and `validate-image.sh` pass on things that fail live, so iterate on the
device — but keep hardware and repo in sync (below). The full field record is
[`docs/hardware-bring-up-lessons.md`](docs/hardware-bring-up-lessons.md); read it
before touching the image layer, units, camera path, or NetworkManager config.

### Current appliance baseline (2026-09-25)

- The accepted hardware is a Pi Zero 2 W + OV5647 running application release `1.0.4`.
- The launcher prefers `/data/pibuddycam/releases/current`; `/opt/pibuddycam` is the immutable
  factory fallback. Editing `/opt/pibuddycam/*.py` does **not** change a running release.
- ROOT is a directly mounted read-only ext4 filesystem. `overlayroot` is configured but did not
  activate on the appliance. `/var` and `/etc/pibuddycam` provide the required volatile state on
  tmpfs; durable configuration, state, media, and application releases live under `/data`.
- Signed application install and rollback are live-accepted. The latest field record is
  [`docs/hardware-bring-up-lessons.md`](docs/hardware-bring-up-lessons.md).

### Choose the deployment path by ownership

| Change | Correct live path | Persistent source of truth |
|---|---|---|
| Python application/static application asset | Build and install a signed application release; do not patch `/opt` | `app/` + signed bundle |
| Image-owned helper, unit, udev/NM rule, package, boot config | Patch ROOT only for an explicitly authorized hardware test, then add the identical change to `image/`; validate with a fresh image when required | `image/` and reused `app/systemd/` assets |
| Device configuration/state | Write through the application/admin path where possible | `/data/pibuddycam/` |
| Kernel, boot firmware, partitioning, base packages | New image + user-performed flash | `image/` |

The legacy `app/deploy.sh` overlay maintenance flow is for the older developer install,
not the current appliance image.

### The hardware loop

1. Make the change in the repo (source/units/image assets) with its tests.
2. Commit the exact source that will be deployed, so an application manifest can record its
   source commit.
3. For an application-only change, build a signed application release with
   `image/scripts/make-app-release.sh` (a published release instead comes from a `vX.Y.Z` tag on
   the arm64 release runner; see `docs/releasing.md`) and install it through `pibuddycam-priv install-update` using the
   private runbook. For an image-owned file needed in the same authorized session, remount ROOT,
   install the exact repo file with root ownership/mode, then remount ROOT read-only:
   ```sh
   mount -o remount,rw /
   install -o root -g root -m <mode> /tmp/<file> <exact-image-owned-destination>
   mount -o remount,ro /
   systemctl restart <unit>         # e.g. pibuddycam pibuddycam-admin pibuddycam-rtsp rpicam-source
   ```
   Config files under `/data/pibuddycam/config/` **must stay `pibuddycam`-owned**
   (`0640` `device.toml`, `0600` `secrets.toml`). Prefer the app/admin writer. If an authorized
   diagnostic root write is unavoidable, restore each file's exact owner and mode explicitly; a
   root-owned secrets document makes the app read an empty token and Connect rejects everything.
4. Verify the active release path, snapshot, both RTSP endpoints, admin UI, affected journal, and
   relevant Connect behavior. Restore temporary updater URLs/CAs and ROOT read-only state.
5. Record the hardware evidence and application/image version in the named `GAP-*` item.

### Keeping hardware and repo in sync (mandatory)

- **Every on-device edit must land in the repo in the same session.** If you patch
  a file on the device, commit the identical change to the repo before finishing.
- Anything the device needs that is not produced by the image — a udev rule, an
  NM conf drop-in, a `config.txt` line, a unit change, a package — is a **repo
  bug**: add it to the image so the next flash persists it.
- A remounted-rw ROOT write is persistent, which makes it a drift risk. Never leave a device-only
  fix behind; correctness must also come from the image. ROOT's mount mode returns to read-only on
  reboot, but the bytes written while it was writable remain.
- An OTA bundle cannot replace image-owned files such as `/usr/libexec/pibuddycam/pibuddycam-priv`, base
  units, packages, or boot configuration.

### Build + flash (only when the change must persist / be validated on a fresh card)

- Published images come from the arm64 release runner via a `vX.Y.Z` tag or a
  `workflow_dispatch` dry run (`docs/releasing.md`). For an ad-hoc build, use the
  native arm64 build host named in `.agent/pi-ops.md`: sync the repo there, run
  `image/scripts/build-image.sh`, then `image/scripts/validate-image.sh` (run as
  root with a full `PATH` so `dumpe2fs`/`mtools` resolve; `--mount-root` is a
  mounted `root.ext4`).
- Flashing and card moves are a **user action** (SD card in the USB reader); the
  device then re-onboards from scratch.

### Diagnosing without a console

- `bootlog.sh` persists unit states, `nmcli`, camera detection (`vcgencmd
  get_camera`, `rpicam-hello --list-cameras`), `/data` state and the relevant
  journals to `/boot/firmware/bootlog.txt` (FAT — readable on any PC). It is also
  `WantedBy=pibuddycam.target`, so the claim→runtime boot is captured.
- The journal is volatile; capture it live or via `bootlog.txt`.

### Hardware lessons (highlights — full list in the lessons doc)

- Config-file ownership is load-bearing (empty-token → Connect rejects).
- NetworkManager randomizes the wlan0 MAC during scans; any MAC-derived identity
  must disable it (`wifi.scan-rand-mac-address=no`) or persist the value.
- The image installs without recommends: NM's `dnsmasq-base` (setup hotspot) and
  `nftables`/`iptables` (shared NAT) must be added explicitly.
- `overlayroot` did not activate; ROOT stays read-only and volatile state lives on
  tmpfs (`/var`, `/etc/pibuddycam`).
- libcamera is single-consumer: probe only pre-runtime; snapshots come from the
  `stream_mux` TCP fan-out, never a second `rpicam` capture while the source runs.
- MBR PARTUUIDs are zero-padded (`b33dcafe-03`), so compare numerically.
- The normal **Prusa Connect live-view UI is currently gated/hidden** while
  `GET camera-service-api.prusa3d.com/v1/cameras/<token>` returns 404 and the camera is listed under
  “Other cameras.” Earlier WebRTC viewer probes received `client_authentication` ACK 5. Do not
  generalize that to all viewer sessions: on 2026-09-25 a non-WebRTC authenticated control client
  received ACK 0 and successfully relayed nested configuration. The current end-to-end WebRTC
  result is therefore “UI/backend enrollment blocked,” not “all signaling rejected.”
- Nested Connect configuration delivery is working. Release `1.0.4` live-verified quality changes
  FHD→HD→FHD after routing the restart through `pibuddycam-priv quality-restart`. The Connect UI may
  still hide those controls while the camera is classified under “Other cameras.”

### Authorization during a hardware session

The rule above still holds: no flashing, reboots, token rotation, or live backend
mutation without the user's explicit request. When authorized, say what you are
about to do before a disruptive action (reboot, stopping the setup AP), and
remember that stopping the AP before the station link is proven drops the device
off the network.
