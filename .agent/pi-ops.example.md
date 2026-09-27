# Pi & Camera Operations Runbook — TEMPLATE

> Copy to `pi-ops.md` (git-ignored) and fill in your real host/network details.
> The filled-in `pi-ops.md` is never committed.

## The devices

| Device | What | Access |
|---|---|---|
| **Pi** | Raspberry Pi Zero 2 W, Debian 13 (trixie). Runs the impersonator. | `ssh <PI_USER>@<PI_IP>` (SSH **key** auth, no password) |
| **Camera** | Optional genuine Prusa Buddy3D Camera, firmware 3.1.6 (Rockchip, ARM). The RE target. | via Prusa Connect / read-only SD copy |

On the appliance, the factory app lives at `/opt/pibuddycam` and the launcher prefers the signed
release at `/data/pibuddycam/releases/current`. Repo source of truth is `app/`; image
assets and fixed root helpers live under `image/`.

## SSH quick checks

```bash
PI=<PI_USER>@<PI_IP>   # fill in your Pi
ssh $PI 'systemctl is-active rpicam-source pibuddycam-ha-rtsp pibuddycam-rtsp pibuddycam'
ssh $PI 'journalctl -u pibuddycam -n 50 --no-pager'                 # impersonator log
ssh $PI 'journalctl -fu pibuddycam'                                 # follow live
ssh $PI 'readlink -f /data/pibuddycam/releases/current; findmnt -no OPTIONS /'
```

## Deploy code changes (repo → Pi, only when explicitly authorized)

Repository edits/tests do not authorize a live deployment. For the appliance, application code is
deployed as a signed bundle made by `image/scripts/make-app-release.sh`; record the clean source
commit in the manifest and install via `pibuddycam-priv install-update`. Document in the private copy:

- signing-key location and wheel cache (never commit either);
- temporary HTTPS/CA procedure;
- exact backup/restore procedure for `/etc/pibuddycam-updater.conf`;
- post-install checks and cleanup.

```bash
image/scripts/make-app-release.sh --version X.Y.Z --out-dir <dir> --wheels <dir> \
  --url-base https://<temporary-server> --key <private-key> --source-commit <git-sha>
# On the device, after configuring the explicitly temporary manifest/CA:
/usr/libexec/pibuddycam/pibuddycam-priv install-update
```

The legacy `app/deploy.sh` flow is only for the old developer install, not the appliance.
An application OTA cannot update image-owned files. For an explicitly authorized helper/unit test,
copy the exact repo asset to `/tmp`, verify its hash, remount `/` rw, install it root-owned with the
repo mode, and remount `/` ro. Commit the identical image change in the same session.

## Service management

```bash
# four runtime units:
#   rpicam-source.service  -> rpicam-vid H264 to tcp://0.0.0.0:8888
#   pibuddycam-rtsp.service     -> rtsp_server.py, rtsp://<pi>:8554/live  (toggled by main.py)
#   pibuddycam-ha-rtsp.service  -> rtsp_server.py, rtsp://<pi>:8555/live  (always on)
#   pibuddycam.service      -> main.py (registers to Prusa, uploads, signaling/WebRTC)
ssh $PI 'systemctl restart pibuddycam'
ssh $PI 'systemctl try-restart pibuddycam-rtsp'
vlc rtsp://<PI_IP>:8554/live      # verify local stream
```

## Rotate / change the registration token

Token and fingerprint live in `/data/pibuddycam/config/secrets.toml` and `device.toml`. Prefer the
admin/onboarding path. Direct root edits are hazardous: the files must remain owned by
`pibuddycam:pibuddycam` (`0600` secrets, `0640` device). Token rotation and backend registration are
separate, explicitly authorized operations.

```bash
ssh $PI 'stat -c "%U:%G %a %n" /data/pibuddycam/config/*.toml'
```

## Flash / reimage the Pi (SD card)

Build and validate the project image on the native arm64 build host, then let the user perform the
flash/card move. A normal image has SSH disabled; diagnostic SSH must be deliberately re-injected.
Never write a reference/known-working card. Copy it block-for-block while unmounted and kernel-ro,
hash the image, then mount only the copy read-only through a loop device.

## Update a genuine CAMERA firmware / OTA  ⚠️ destructive

The camera's own `RK_OTA_update.sh` (see `docs/sources.md`) is the Rockchip on-device updater: for each `/dev/block/by-name/*`
it finds a matching `<name>.img` and does `flash_eraseall` + `nandwrite`, then erases `misc`.
This **overwrites the camera's NAND partitions** — a bad image bricks the camera. Only run on
the camera itself (not the Pi), with known-good images, and a recovery plan. Prefer letting the
camera OTA itself via `connect-ota.prusa3d.com` unless you specifically need a custom image.

## Release runner (private details)

Record the self-hosted arm64 runner here, not in tracked docs (public contract:
`docs/releasing.md`):

- Host: `<RUNNER_HOST>`, SSH user `<USER>`; runner directory `<RUNNER_DIR>`, name `<RUNNER_NAME>`.
- Service: `actions.runner.<owner>-<repo>.<RUNNER_NAME>.service`.
- Signing key: `<RUNNER_KEY_PATH>` (`0600`); must match the committed public key.
- Maintenance: `sudo ./svc.sh status|stop|start` in the runner directory; re-register with a fresh
  registration token only when the user asks.

## End-of-session checks

- active release points to the expected version and all four services are active;
- `/` is read-only and temporary updater URLs/CAs are gone;
- config ownership/modes are unchanged;
- repo is clean and every device-side edit has an identical committed image source;
- snapshot and both RTSP endpoints work; record whether Connect validation was UI-visible or used
  an authenticated signaling replay because the registry gate hid the controls.
